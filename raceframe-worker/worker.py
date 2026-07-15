from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Any

import numpy as np
import requests


BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "").rstrip("/")
WORKER_API_TOKEN = os.getenv("WORKER_API_TOKEN", "")
WORKER_POLL_SECONDS = float(os.getenv("WORKER_POLL_SECONDS", "3"))
WORKER_JOB_TYPES = [item.strip() for item in os.getenv("WORKER_JOB_TYPES", "ocr,face_photo_scan").split(",") if item.strip()]


@dataclass
class FaceResult:
    face_index: int
    embedding: list[float]
    bounding_box_json: dict[str, Any]
    detection_score: float | None
    quality_score: float | None


class WorkerConfigError(RuntimeError):
    pass


def require_config() -> None:
    missing = []
    for name, value in [
        ("BACKEND_BASE_URL", BACKEND_BASE_URL),
        ("WORKER_API_TOKEN", WORKER_API_TOKEN),
    ]:
        if not value:
            missing.append(name)
    if missing:
        raise WorkerConfigError(f"Missing required env vars: {', '.join(missing)}")


def api_post(path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    response = requests.post(
        f"{BACKEND_BASE_URL}{path}",
        json=payload or {},
        headers={"Authorization": f"Bearer {WORKER_API_TOKEN}"},
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def download_url(download_url: str | None) -> bytes:
    if not download_url:
        raise RuntimeError("Backend did not provide a download URL for this job.")
    response = requests.get(download_url, timeout=120)
    response.raise_for_status()
    return response.content


def normalize_detected_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().upper()


def run_google_ocr(image_bytes: bytes) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from google.cloud import vision
    from google.protobuf.json_format import MessageToDict

    client = vision.ImageAnnotatorClient()
    image = vision.Image(content=image_bytes)
    response = client.document_text_detection(image=image)
    if response.error.message:
        raise RuntimeError(f"Google OCR failed: {response.error.message}")
    return extract_google_ocr_detections(response), MessageToDict(response._pb, preserving_proto_field_name=True)


def extract_google_ocr_detections(response) -> list[dict[str, Any]]:
    detections: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, tuple[tuple[int, int], ...]]] = set()

    full_text_annotation = getattr(response, "full_text_annotation", None)
    if full_text_annotation and full_text_annotation.pages:
        for page_index, page in enumerate(full_text_annotation.pages):
            for block_index, block in enumerate(page.blocks):
                for paragraph_index, paragraph in enumerate(block.paragraphs):
                    for word_index, word in enumerate(paragraph.words):
                        detected_text = "".join(symbol.text for symbol in word.symbols).strip()
                        normalized_text = normalize_detected_text(detected_text)
                        if not normalized_text:
                            continue
                        box_json = bounding_poly_to_json(
                            word.bounding_box,
                            source="word",
                            page_index=page_index,
                            block_index=block_index,
                            paragraph_index=paragraph_index,
                            word_index=word_index,
                        )
                        dedupe_key = (normalized_text, vertices_key_from_box(box_json))
                        if dedupe_key in seen_keys:
                            continue
                        seen_keys.add(dedupe_key)
                        detections.append(
                            {
                                "detected_text": detected_text,
                                "normalized_text": normalized_text,
                                "confidence": float(word.confidence) if getattr(word, "confidence", None) is not None else None,
                                "bounding_box_json": box_json,
                            }
                        )

    if detections:
        return sort_ocr_detections(detections)

    annotations = list(getattr(response, "text_annotations", []) or [])
    for annotation_index, annotation in enumerate(annotations[1:], start=1):
        detected_text = annotation.description.strip()
        normalized_text = normalize_detected_text(detected_text)
        if not normalized_text:
            continue
        box_json = bounding_poly_to_json(
            annotation.bounding_poly,
            source="text_annotation",
            annotation_index=annotation_index,
        )
        dedupe_key = (normalized_text, vertices_key_from_box(box_json))
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        detections.append(
            {
                "detected_text": detected_text,
                "normalized_text": normalized_text,
                "confidence": None,
                "bounding_box_json": box_json,
            }
        )

    return sort_ocr_detections(detections)


def bounding_poly_to_json(bounding_poly, **metadata) -> dict[str, Any] | None:
    if not bounding_poly or not getattr(bounding_poly, "vertices", None):
        return metadata or None

    vertices = [
        {"x": int(getattr(vertex, "x", 0) or 0), "y": int(getattr(vertex, "y", 0) or 0)}
        for vertex in bounding_poly.vertices
    ]
    xs = [vertex["x"] for vertex in vertices]
    ys = [vertex["y"] for vertex in vertices]
    payload: dict[str, Any] = {
        "vertices": vertices,
        "left": min(xs) if xs else 0,
        "top": min(ys) if ys else 0,
        "right": max(xs) if xs else 0,
        "bottom": max(ys) if ys else 0,
        "width": (max(xs) - min(xs)) if xs else 0,
        "height": (max(ys) - min(ys)) if ys else 0,
    }
    payload.update(metadata)
    return payload


def vertices_key_from_box(box_json: dict[str, Any] | None) -> tuple[tuple[int, int], ...]:
    if not box_json:
        return ()
    return tuple(
        (int(vertex.get("x", 0)), int(vertex.get("y", 0)))
        for vertex in box_json.get("vertices", [])
    )


def sort_ocr_detections(detections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        detections,
        key=lambda detection: (
            int((detection.get("bounding_box_json") or {}).get("top", 0)),
            int((detection.get("bounding_box_json") or {}).get("left", 0)),
            detection.get("detected_text") or "",
        ),
    )


class FaceProcessor:
    def __init__(self) -> None:
        self._app = None

    @property
    def app(self):
        if self._app is None:
            from insightface.app import FaceAnalysis

            model_name = os.getenv("FACE_MODEL_NAME", "buffalo_l")
            det_size = int(os.getenv("FACE_DET_SIZE", "640"))
            self._app = FaceAnalysis(name=model_name, providers=["CPUExecutionProvider"])
            self._app.prepare(ctx_id=-1, det_size=(det_size, det_size))
        return self._app

    def scan(self, image_bytes: bytes) -> list[FaceResult]:
        import cv2

        array = np.frombuffer(image_bytes, dtype=np.uint8)
        image = cv2.imdecode(array, cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError("Could not decode image for face recognition.")

        image = resize_image(image)
        faces = self.app.get(image)
        results: list[FaceResult] = []
        for index, face in enumerate(faces):
            embedding = getattr(face, "normed_embedding", None)
            if embedding is None:
                embedding = normalize_embedding(getattr(face, "embedding", None))
            if embedding is None:
                continue

            bbox = [float(value) for value in getattr(face, "bbox", [0, 0, 0, 0])]
            left, top, right, bottom = bbox
            detection_score = getattr(face, "det_score", None)
            score = float(detection_score) if detection_score is not None else None
            results.append(
                FaceResult(
                    face_index=index,
                    embedding=[float(value) for value in embedding.tolist()],
                    bounding_box_json={
                        "left": int(left),
                        "top": int(top),
                        "right": int(right),
                        "bottom": int(bottom),
                        "width": int(max(0, right - left)),
                        "height": int(max(0, bottom - top)),
                    },
                    detection_score=score,
                    quality_score=score,
                )
            )
        return results

    def enroll_best_face(self, image_bytes: bytes) -> FaceResult:
        faces = self.scan(image_bytes)
        if not faces:
            raise RuntimeError("No face was detected in the selfie.")
        return max(
            faces,
            key=lambda face: (
                face.quality_score or 0.0,
                face.bounding_box_json.get("width", 0) * face.bounding_box_json.get("height", 0),
            ),
        )


def resize_image(image):
    max_edge = int(os.getenv("FACE_MAX_IMAGE_EDGE", "1600"))
    height, width = image.shape[:2]
    longest = max(height, width)
    if longest <= max_edge:
        return image
    scale = max_edge / float(longest)
    import cv2

    return cv2.resize(image, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA)


def normalize_embedding(value):
    if value is None:
        return None
    array = np.asarray(value, dtype=np.float32)
    norm = np.linalg.norm(array)
    if norm <= 0:
        return None
    return array / norm


face_processor = FaceProcessor()


def process_face_job(job: dict[str, Any]) -> None:
    image_bytes = download_url(job["face_image"].get("download_url"))
    face = face_processor.enroll_best_face(image_bytes)
    api_post(
        f"/internal/worker/face-jobs/{job['id']}/complete",
        {
            "embedding": face.embedding,
            "bounding_box_json": face.bounding_box_json,
            "detection_score": face.detection_score,
            "quality_score": face.quality_score,
            "raw_response_json": {
                "provider": "insightface",
                "model": os.getenv("FACE_MODEL_NAME", "buffalo_l"),
            },
        },
    )


def process_face_search_job(job: dict[str, Any]) -> None:
    image_bytes = download_url(job["face_image"].get("download_url"))
    face = face_processor.enroll_best_face(image_bytes)
    api_post(
        f"/internal/worker/face-search-jobs/{job['id']}/complete",
        {
            "embedding": face.embedding,
            "bounding_box_json": face.bounding_box_json,
            "detection_score": face.detection_score,
            "quality_score": face.quality_score,
            "raw_response_json": {
                "provider": "insightface",
                "model": os.getenv("FACE_MODEL_NAME", "buffalo_l"),
            },
        },
    )


def process_photo_job(job: dict[str, Any]) -> None:
    image_bytes = download_url(job["photo"].get("download_url"))
    if job["job_type"] == "ocr":
        detections, raw_response = run_google_ocr(image_bytes)
        api_post(
            f"/internal/worker/photo-jobs/{job['id']}/complete",
            {"detections": detections, "raw_response_json": raw_response},
        )
        return

    if job["job_type"] == "face_photo_scan":
        faces = face_processor.scan(image_bytes)
        api_post(
            f"/internal/worker/photo-jobs/{job['id']}/complete",
            {
                "face_detections": [
                    {
                        "face_index": face.face_index,
                        "embedding": face.embedding,
                        "bounding_box_json": face.bounding_box_json,
                        "detection_score": face.detection_score,
                        "quality_score": face.quality_score,
                    }
                    for face in faces
                ],
                "raw_response_json": {
                    "provider": "insightface",
                    "model": os.getenv("FACE_MODEL_NAME", "buffalo_l"),
                    "face_count": len(faces),
                },
            },
        )
        return

    raise RuntimeError(f"Unsupported photo job type: {job['job_type']}")


def work_once() -> bool:
    face_claim = api_post("/internal/worker/face-jobs/claim")
    if face_claim.get("job"):
        job = face_claim["job"]
        try:
            process_face_job(job)
            print(f"completed face job {job['id']}", flush=True)
        except Exception as exc:  # noqa: BLE001
            api_post(f"/internal/worker/face-jobs/{job['id']}/fail", {"error_message": str(exc)})
            print(f"failed face job {job['id']}: {exc}", flush=True)
        return True

    face_search_claim = api_post("/internal/worker/face-search-jobs/claim")
    if face_search_claim.get("job"):
        job = face_search_claim["job"]
        try:
            process_face_search_job(job)
            print(f"completed face search job {job['id']}", flush=True)
        except Exception as exc:  # noqa: BLE001
            api_post(f"/internal/worker/face-search-jobs/{job['id']}/fail", {"error_message": str(exc)})
            print(f"failed face search job {job['id']}: {exc}", flush=True)
        return True

    photo_claim = api_post("/internal/worker/photo-jobs/claim", {"job_types": WORKER_JOB_TYPES})
    if photo_claim.get("job"):
        job = photo_claim["job"]
        try:
            process_photo_job(job)
            print(f"completed photo job {job['id']} ({job['job_type']})", flush=True)
        except Exception as exc:  # noqa: BLE001
            api_post(f"/internal/worker/photo-jobs/{job['id']}/fail", {"error_message": str(exc)})
            print(f"failed photo job {job['id']} ({job['job_type']}): {exc}", flush=True)
        return True

    return False


def main() -> None:
    require_config()
    print("raceframe worker started", flush=True)
    while True:
        try:
            did_work = work_once()
        except Exception as exc:  # noqa: BLE001
            print(f"worker loop error: {exc}", flush=True)
            did_work = False
        if not did_work:
            time.sleep(WORKER_POLL_SECONDS)


if __name__ == "__main__":
    main()
