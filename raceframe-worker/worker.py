from __future__ import annotations

import io
import json
import os
import random
import re
import signal
import threading
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter
from typing import Any
from urllib.parse import urlparse

import numpy as np
import requests
from requests.adapters import HTTPAdapter


BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "").rstrip("/")
WORKER_API_TOKEN = os.getenv("WORKER_API_TOKEN", "")
WORKER_STARTED_AT = datetime.now(timezone.utc)
WORKER_POLL_SECONDS = float(os.getenv("WORKER_POLL_SECONDS", "3"))
WORKER_JOB_TYPES = [item.strip() for item in os.getenv("WORKER_JOB_TYPES", "ocr,face_photo_scan").split(",") if item.strip()]
WORKER_ID = os.getenv("WORKER_ID", "raceframe-worker-1").strip()
WORKER_VERSION = os.getenv("WORKER_VERSION", "development").strip()
WORKER_HEARTBEAT_SECONDS = float(os.getenv("WORKER_HEARTBEAT_SECONDS", "30"))
WORKER_HTTP_RETRIES = int(os.getenv("WORKER_HTTP_RETRIES", "4"))
WORKER_HTTP_BACKOFF_SECONDS = float(os.getenv("WORKER_HTTP_BACKOFF_SECONDS", "0.75"))
WORKER_MAX_DOWNLOAD_BYTES = int(os.getenv("WORKER_MAX_DOWNLOAD_BYTES", str(25 * 1024 * 1024)))
WORKER_MAX_IMAGE_PIXELS = int(os.getenv("WORKER_MAX_IMAGE_PIXELS", "40000000"))
WORKER_MAX_IMAGE_DIMENSION = int(os.getenv("WORKER_MAX_IMAGE_DIMENSION", "12000"))
SEARCH_FACE_MIN_EDGE = int(os.getenv("SEARCH_FACE_MIN_EDGE", "80"))
SEARCH_FACE_MIN_DETECTION_SCORE = float(os.getenv("SEARCH_FACE_MIN_DETECTION_SCORE", "0.65"))
MAX_FACE_DETECTIONS = 256


@dataclass
class FaceResult:
    face_index: int
    embedding: list[float]
    bounding_box_json: dict[str, Any]
    detection_score: float | None
    quality_score: float | None


class WorkerConfigError(RuntimeError):
    pass


class PermanentJobError(RuntimeError):
    """Input or result is invalid and retrying the same job cannot fix it."""


class TransientJobError(RuntimeError):
    """A provider or object-store error may succeed on a later job attempt."""


class ControlPlaneError(RuntimeError):
    """The backend could not durably acknowledge a worker operation."""


class LeaseLostError(ControlPlaneError):
    """Another attempt owns the job or its lease expired."""


def log_event(level: str, event: str, **fields: Any) -> None:
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "event": event,
        **fields,
    }
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str), flush=True)


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
    parsed_backend = urlparse(BACKEND_BASE_URL)
    if parsed_backend.scheme not in {"http", "https"} or not parsed_backend.netloc:
        raise WorkerConfigError("BACKEND_BASE_URL must be an absolute HTTP(S) URL.")
    if not (0.1 <= WORKER_POLL_SECONDS <= 60):
        raise WorkerConfigError("WORKER_POLL_SECONDS must be between 0.1 and 60.")
    if not (1 <= WORKER_HTTP_RETRIES <= 10):
        raise WorkerConfigError("WORKER_HTTP_RETRIES must be between 1 and 10.")
    if not (10 <= WORKER_HEARTBEAT_SECONDS <= 300):
        raise WorkerConfigError("WORKER_HEARTBEAT_SECONDS must be between 10 and 300.")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", WORKER_ID):
        raise WorkerConfigError("WORKER_ID must be an opaque 1-128 character identifier.")
    if not re.fullmatch(r"[A-Za-z0-9_.:+-]{1,64}", WORKER_VERSION):
        raise WorkerConfigError("WORKER_VERSION must be a short release identifier.")
    if WORKER_MAX_DOWNLOAD_BYTES < 1:
        raise WorkerConfigError("WORKER_MAX_DOWNLOAD_BYTES must be positive.")
    if not (1_000_000 <= WORKER_MAX_IMAGE_PIXELS <= 100_000_000):
        raise WorkerConfigError("WORKER_MAX_IMAGE_PIXELS must be between 1,000,000 and 100,000,000.")
    if not (1_024 <= WORKER_MAX_IMAGE_DIMENSION <= 32_768):
        raise WorkerConfigError("WORKER_MAX_IMAGE_DIMENSION must be between 1,024 and 32,768.")
    if set(WORKER_JOB_TYPES) - {"ocr", "face_photo_scan"} or not WORKER_JOB_TYPES:
        raise WorkerConfigError("WORKER_JOB_TYPES must contain ocr and/or face_photo_scan.")
    if SEARCH_FACE_MIN_EDGE < 32:
        raise WorkerConfigError("SEARCH_FACE_MIN_EDGE must be at least 32 pixels.")
    if not (0.0 <= SEARCH_FACE_MIN_DETECTION_SCORE <= 1.0):
        raise WorkerConfigError("SEARCH_FACE_MIN_DETECTION_SCORE must be between 0 and 1.")


def _retry_delay(attempt: int, response: requests.Response | None = None) -> float:
    if response is not None:
        retry_after = response.headers.get("Retry-After", "").strip()
        if retry_after.isdigit():
            return min(60.0, max(0.1, float(retry_after)))
    exponential = min(30.0, WORKER_HTTP_BACKOFF_SECONDS * (2**attempt))
    return max(0.05, exponential * random.uniform(0.75, 1.25))  # nosec B311


class BackendClient:
    """Persistent, bounded HTTP clients for the backend and presigned object URLs."""

    def __init__(self) -> None:
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0)
        self._api = requests.Session()
        self._api.headers.update(
            {
                "Authorization": f"Bearer {WORKER_API_TOKEN}",
                "Accept": "application/json",
                "User-Agent": "raceframe-worker/1",
            }
        )
        self._api.mount("http://", adapter)
        self._api.mount("https://", adapter)

        # Never send the worker bearer token to an R2 presigned URL.
        download_adapter = HTTPAdapter(pool_connections=2, pool_maxsize=4, max_retries=0)
        self._downloads = requests.Session()
        self._downloads.headers.update({"User-Agent": "raceframe-worker/1"})
        self._downloads.mount("http://", download_adapter)
        self._downloads.mount("https://", download_adapter)

    def close(self) -> None:
        self._api.close()
        self._downloads.close()

    def post(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        retry_safe: bool = False,
        timeout: tuple[float, float] = (5.0, 60.0),
    ) -> dict[str, Any]:
        attempts = WORKER_HTTP_RETRIES if retry_safe else 1
        for attempt in range(attempts):
            response: requests.Response | None = None
            try:
                response = self._api.post(
                    f"{BACKEND_BASE_URL}{path}",
                    json=payload or {},
                    timeout=timeout,
                )
            except requests.RequestException as exc:
                if attempt + 1 >= attempts:
                    raise ControlPlaneError("Backend request failed after bounded retries.") from exc
                threading.Event().wait(_retry_delay(attempt))
                continue

            if 200 <= response.status_code < 300:
                try:
                    body = response.json()
                except ValueError as exc:
                    raise ControlPlaneError("Backend returned a non-JSON response.") from exc
                if not isinstance(body, dict):
                    raise ControlPlaneError("Backend returned an invalid response shape.")
                return body

            if response.status_code == 409:
                raise LeaseLostError("Backend rejected a stale or expired worker attempt.")
            if response.status_code in {401, 403}:
                raise WorkerConfigError("Backend rejected WORKER_API_TOKEN.")
            if response.status_code == 404:
                raise LeaseLostError("The claimed job no longer exists.")
            if response.status_code == 429 or response.status_code >= 500:
                if attempt + 1 < attempts:
                    threading.Event().wait(_retry_delay(attempt, response))
                    continue
                raise ControlPlaneError(f"Backend returned transient HTTP {response.status_code}.")
            raise PermanentJobError(f"Backend rejected the worker result with HTTP {response.status_code}.")

        raise ControlPlaneError("Backend request exhausted its retry budget.")

    def download(self, download_url: str | None) -> bytes:
        if not download_url:
            raise PermanentJobError("Backend did not provide a download URL for this job.")
        parsed = urlparse(download_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise PermanentJobError("Backend provided an invalid download URL.")

        for attempt in range(WORKER_HTTP_RETRIES):
            response: requests.Response | None = None
            try:
                response = self._downloads.get(download_url, timeout=(5.0, 120.0), stream=True)
                if response.status_code == 404:
                    raise PermanentJobError("The source image no longer exists in object storage.")
                if response.status_code == 429 or response.status_code >= 500 or response.status_code in {408, 425}:
                    if attempt + 1 < WORKER_HTTP_RETRIES:
                        response.close()
                        threading.Event().wait(_retry_delay(attempt, response))
                        continue
                    raise TransientJobError(f"Object storage returned HTTP {response.status_code}.")
                if response.status_code >= 400:
                    raise TransientJobError(f"Object download was rejected with HTTP {response.status_code}.")

                content_length = response.headers.get("Content-Length", "")
                if content_length.isdigit() and int(content_length) > WORKER_MAX_DOWNLOAD_BYTES:
                    raise PermanentJobError("Source image exceeds the worker download limit.")
                chunks: list[bytes] = []
                received = 0
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    received += len(chunk)
                    if received > WORKER_MAX_DOWNLOAD_BYTES:
                        raise PermanentJobError("Source image exceeds the worker download limit.")
                    chunks.append(chunk)
                if received == 0:
                    raise PermanentJobError("Source image is empty.")
                return b"".join(chunks)
            except (PermanentJobError, TransientJobError):
                raise
            except requests.RequestException as exc:
                if attempt + 1 >= WORKER_HTTP_RETRIES:
                    raise TransientJobError("Object download failed after bounded retries.") from exc
                threading.Event().wait(_retry_delay(attempt))
            finally:
                if response is not None:
                    response.close()
        raise TransientJobError("Object download exhausted its retry budget.")


def normalize_detected_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().upper()


class OCRProcessor:
    def __init__(self) -> None:
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from google.cloud import vision

            self._client = vision.ImageAnnotatorClient()
        return self._client

    def scan(self, image_bytes: bytes) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        from google.cloud import vision

        width, height = validate_image_header(image_bytes)
        started_at = perf_counter()
        image = vision.Image(content=image_bytes)
        try:
            response = self.client.document_text_detection(image=image, timeout=90)
        except Exception as exc:  # Provider transports expose several retryable exception types.
            error_name = type(exc).__name__
            if error_name in {"BadRequest", "InvalidArgument", "FailedPrecondition", "OutOfRange"}:
                raise PermanentJobError("Google Vision rejected the validated source image.") from exc
            if error_name in {"Unauthorized", "Unauthenticated", "Forbidden", "PermissionDenied"}:
                raise WorkerConfigError("Google Vision credentials are invalid or lack permission.") from exc
            raise TransientJobError("Google Vision OCR request failed.") from exc
        if response.error.message:
            error_code = int(getattr(response.error, "code", 0) or 0)
            if error_code in {3, 9, 11}:  # invalid argument, failed precondition, out of range
                raise PermanentJobError(f"Google Vision rejected the image: {response.error.message[:500]}")
            raise TransientJobError(f"Google Vision OCR failed: {response.error.message[:500]}")

        detections = extract_google_ocr_detections(response)
        raw_summary = {
            "provider": "google_vision",
            "request": "document_text_detection",
            "model": "google_vision_builtin",
            "duration_ms": round((perf_counter() - started_at) * 1_000, 1),
            "image_width": width,
            "image_height": height,
            "detection_count": len(detections),
        }
        return detections, raw_summary


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


def validate_image_header(image_bytes: bytes) -> tuple[int, int]:
    """Reject decompression bombs and animated/multi-frame input before OpenCV allocates pixels."""

    from PIL import Image, UnidentifiedImageError

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(image_bytes)) as image:
                width, height = image.size
                frame_count = int(getattr(image, "n_frames", 1) or 1)
                if frame_count != 1:
                    raise PermanentJobError("Animated or multi-frame images are not accepted.")
                if width <= 0 or height <= 0:
                    raise PermanentJobError("Image dimensions are invalid.")
                if max(width, height) > WORKER_MAX_IMAGE_DIMENSION:
                    raise PermanentJobError(
                        f"Image dimensions exceed the {WORKER_MAX_IMAGE_DIMENSION}px worker limit."
                    )
                if width * height > WORKER_MAX_IMAGE_PIXELS:
                    raise PermanentJobError(
                        f"Image exceeds the {WORKER_MAX_IMAGE_PIXELS:,}-pixel worker limit."
                    )
                image.verify()
    except PermanentJobError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise PermanentJobError("Image exceeds the safe decompression limit.") from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise PermanentJobError("Image header is invalid or the image is corrupt.") from exc
    return width, height


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
        results, _detected_face_count = self._scan_with_detection_count(image_bytes)
        return results

    def _scan_with_detection_count(self, image_bytes: bytes) -> tuple[list[FaceResult], int]:
        import cv2

        validate_image_header(image_bytes)
        array = np.frombuffer(image_bytes, dtype=np.uint8)
        image = cv2.imdecode(array, cv2.IMREAD_COLOR)
        if image is None:
            raise PermanentJobError("Could not decode the source image for face recognition.")

        image = resize_image(image)
        faces = list(self.app.get(image) or [])
        results: list[FaceResult] = []
        for index, face in enumerate(faces[:MAX_FACE_DETECTIONS]):
            embedding = getattr(face, "normed_embedding", None)
            if embedding is None:
                embedding = normalize_embedding(getattr(face, "embedding", None))
            if embedding is None:
                continue

            embedding_values = [float(value) for value in embedding.tolist()]
            if len(embedding_values) != 512 or not all(np.isfinite(value) for value in embedding_values):
                continue

            bbox = [float(value) for value in getattr(face, "bbox", [0, 0, 0, 0])]
            left, top, right, bottom = bbox
            detection_score = getattr(face, "det_score", None)
            score = float(detection_score) if detection_score is not None else None
            if score is not None and not np.isfinite(score):
                score = None
            results.append(
                FaceResult(
                    face_index=index,
                    embedding=embedding_values,
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
        return results, len(faces)

    def enroll_single_search_face(self, image_bytes: bytes) -> FaceResult:
        faces, detected_face_count = self._scan_with_detection_count(image_bytes)
        if detected_face_count == 0:
            raise PermanentJobError("Exactly one clear face is required; no face was detected.")
        if detected_face_count != 1:
            raise PermanentJobError("Exactly one face is required; group selfies are not accepted.")
        if len(faces) != 1:
            raise PermanentJobError("The detected face did not produce a valid recognition embedding.")
        face = faces[0]
        width = int(face.bounding_box_json.get("width", 0) or 0)
        height = int(face.bounding_box_json.get("height", 0) or 0)
        if min(width, height) < SEARCH_FACE_MIN_EDGE:
            raise PermanentJobError(
                f"The face is too small; use a closer photo with a face at least {SEARCH_FACE_MIN_EDGE}px wide and tall."
            )
        if (face.detection_score or 0.0) < SEARCH_FACE_MIN_DETECTION_SCORE:
            raise PermanentJobError("The face is not clear enough; use a well-lit, front-facing selfie.")
        return face

    def enroll_best_face(self, image_bytes: bytes) -> FaceResult:
        faces = self.scan(image_bytes)
        if not faces:
            raise PermanentJobError("No face was detected in the selfie.")
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
ocr_processor = OCRProcessor()


def _operation_path(job: dict[str, Any], operation: str) -> str:
    heartbeat_path = str(job.get("heartbeat_path") or "")
    if not heartbeat_path.endswith("/heartbeat"):
        raise PermanentJobError("Claim response did not include a valid heartbeat path.")
    return f"{heartbeat_path[:-len('/heartbeat')]}/{operation}"


class LeaseHeartbeat:
    def __init__(self, job: dict[str, Any]) -> None:
        self._job = job
        lease_seconds = max(30.0, float(job.get("lease_seconds") or 300))
        self._lease_seconds = lease_seconds
        self._interval = max(5.0, min(60.0, lease_seconds / 3.0))
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._failure: Exception | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "LeaseHeartbeat":
        self._thread = threading.Thread(
            target=self._run,
            name=f"lease-heartbeat-{self._job.get('id', 'unknown')}",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        client = BackendClient()
        last_success = datetime.now(timezone.utc)
        try:
            while not self._stop.wait(self._interval):
                try:
                    client.post(
                        str(self._job["heartbeat_path"]),
                        {"attempt_id": self._job["attempt_id"]},
                        retry_safe=True,
                        timeout=(3.0, 15.0),
                    )
                    last_success = datetime.now(timezone.utc)
                except LeaseLostError as exc:
                    self._failure = exc
                    self._lost.set()
                    log_event("warning", "job_lease_lost", job_id=self._job.get("id"))
                    return
                except WorkerConfigError as exc:
                    self._failure = exc
                    self._lost.set()
                    return
                except ControlPlaneError:
                    elapsed = (datetime.now(timezone.utc) - last_success).total_seconds()
                    log_event(
                        "warning",
                        "job_heartbeat_failed",
                        job_id=self._job.get("id"),
                        seconds_since_renewal=round(elapsed, 1),
                    )
                    if elapsed >= self._lease_seconds * 0.8:
                        self._failure = LeaseLostError("Unable to renew the job lease before its deadline.")
                        self._lost.set()
                        return
        finally:
            client.close()

    def ensure_owned(self) -> None:
        if self._lost.is_set():
            if self._failure is not None:
                raise self._failure
            raise LeaseLostError("The worker no longer owns this job.")


class WorkerPresenceHeartbeat:
    """Publish process liveness without exposing hostnames, addresses, or credentials."""

    def __init__(self) -> None:
        self._status = "idle"
        self._current_job_id: str | None = None
        self._current_job_type: str | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._fatal_error: WorkerConfigError | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name="worker-presence-heartbeat",
            daemon=True,
        )
        self._thread.start()

    def set_active(self, job: dict[str, Any]) -> None:
        with self._lock:
            self._status = "active"
            self._current_job_id = str(job["id"])
            self._current_job_type = str(job["job_type"])
        self._wake.set()

    def set_idle(self) -> None:
        with self._lock:
            self._status = "idle"
            self._current_job_id = None
            self._current_job_type = None
        self._wake.set()

    def set_draining(self) -> None:
        with self._lock:
            self._status = "draining"
            self._current_job_id = None
            self._current_job_type = None
        self._wake.set()

    def payload(self) -> dict[str, Any]:
        with self._lock:
            return {
                "worker_id": WORKER_ID,
                "worker_version": WORKER_VERSION,
                "started_at": WORKER_STARTED_AT.isoformat(),
                "status": self._status,
                "current_job_id": self._current_job_id,
                "current_job_type": self._current_job_type,
            }

    def ensure_healthy(self) -> None:
        if self._fatal_error is not None:
            raise self._fatal_error

    def _send(
        self,
        client: BackendClient,
        *,
        retry_safe: bool = True,
        timeout: tuple[float, float] = (3.0, 15.0),
    ) -> None:
        client.post(
            "/internal/worker/heartbeat",
            self.payload(),
            retry_safe=retry_safe,
            timeout=timeout,
        )

    def _run(self) -> None:
        client = BackendClient()
        try:
            while not self._stop.is_set():
                try:
                    self._send(client)
                except WorkerConfigError as exc:
                    self._fatal_error = exc
                    return
                except PermanentJobError as exc:
                    self._fatal_error = WorkerConfigError("Backend rejected worker presence heartbeat payload.")
                    log_event(
                        "error",
                        "worker_presence_heartbeat_rejected",
                        error_type=exc.__class__.__name__,
                    )
                    return
                except ControlPlaneError as exc:
                    log_event(
                        "warning",
                        "worker_presence_heartbeat_failed",
                        error_type=exc.__class__.__name__,
                    )
                self._wake.wait(WORKER_HEARTBEAT_SECONDS)
                self._wake.clear()
        finally:
            client.close()

    def stop(self) -> None:
        self.set_draining()
        # Publish the terminal process state once, without delaying shutdown forever.
        final_client = BackendClient()
        try:
            try:
                self._send(final_client, retry_safe=False, timeout=(3.0, 5.0))
            except (ControlPlaneError, PermanentJobError, WorkerConfigError):
                pass
        finally:
            final_client.close()
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)


def _embedding_completion_payload(job: dict[str, Any], face: FaceResult) -> dict[str, Any]:
    return {
        "attempt_id": job["attempt_id"],
        "embedding": face.embedding,
        "bounding_box_json": face.bounding_box_json,
        "detection_score": face.detection_score,
        "quality_score": face.quality_score,
        "raw_response_json": {
            "provider": "insightface",
            "model": os.getenv("FACE_MODEL_NAME", "buffalo_l"),
        },
    }


def process_face_job(client: BackendClient, job: dict[str, Any], heartbeat: LeaseHeartbeat) -> None:
    image_bytes = client.download(job["face_image"].get("download_url"))
    heartbeat.ensure_owned()
    face = face_processor.enroll_best_face(image_bytes)
    heartbeat.ensure_owned()
    client.post(
        _operation_path(job, "complete"),
        _embedding_completion_payload(job, face),
        retry_safe=True,
    )


def process_face_search_job(client: BackendClient, job: dict[str, Any], heartbeat: LeaseHeartbeat) -> None:
    image_bytes = client.download(job["face_image"].get("download_url"))
    heartbeat.ensure_owned()
    face = face_processor.enroll_single_search_face(image_bytes)
    heartbeat.ensure_owned()
    client.post(
        _operation_path(job, "complete"),
        _embedding_completion_payload(job, face),
        retry_safe=True,
    )


def process_photo_job(client: BackendClient, job: dict[str, Any], heartbeat: LeaseHeartbeat) -> None:
    image_bytes = client.download(job["photo"].get("download_url"))
    heartbeat.ensure_owned()
    if job["job_type"] == "ocr":
        detections, raw_response = ocr_processor.scan(image_bytes)
        heartbeat.ensure_owned()
        client.post(
            _operation_path(job, "complete"),
            {
                "attempt_id": job["attempt_id"],
                "detections": detections,
                "raw_response_json": raw_response,
            },
            retry_safe=True,
        )
        return

    if job["job_type"] == "face_photo_scan":
        faces = face_processor.scan(image_bytes)
        heartbeat.ensure_owned()
        client.post(
            _operation_path(job, "complete"),
            {
                "attempt_id": job["attempt_id"],
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
            retry_safe=True,
        )
        return

    raise PermanentJobError(f"Unsupported photo job type: {job['job_type']}")


def _report_failure(
    client: BackendClient,
    job: dict[str, Any],
    error: Exception,
    *,
    retryable: bool,
    error_code: str,
) -> None:
    message = str(error).strip() or error.__class__.__name__
    try:
        result = client.post(
            _operation_path(job, "fail"),
            {
                "attempt_id": job["attempt_id"],
                "error_message": message[:2_000],
                "retryable": retryable,
                "error_code": error_code,
            },
            retry_safe=True,
        )
        log_event(
            "warning",
            "job_failed",
            job_id=job.get("id"),
            job_type=job.get("job_type"),
            retryable=retryable,
            resulting_status=result.get("status"),
            error_code=error_code,
        )
    except (ControlPlaneError, PermanentJobError) as report_error:
        log_event(
            "error",
            "job_failure_report_failed",
            job_id=job.get("id"),
            error_type=report_error.__class__.__name__,
        )


def handle_claimed_job(
    client: BackendClient,
    job: dict[str, Any],
    processor,
    presence: WorkerPresenceHeartbeat,
) -> None:
    presence.set_active(job)
    try:
        with LeaseHeartbeat(job) as heartbeat:
            processor(client, job, heartbeat)
        log_event(
            "info",
            "job_completed",
            job_id=job.get("id"),
            job_type=job.get("job_type"),
            attempt_count=job.get("attempt_count"),
        )
    except WorkerConfigError:
        raise
    except LeaseLostError:
        log_event("warning", "job_abandoned_after_lease_loss", job_id=job.get("id"))
    except ControlPlaneError as exc:
        # Do not report a processing failure when the backend itself is unavailable;
        # the lease will expire and make the job reclaimable.
        log_event(
            "error",
            "job_control_plane_unavailable",
            job_id=job.get("id"),
            error_type=exc.__class__.__name__,
        )
    except PermanentJobError as exc:
        _report_failure(client, job, exc, retryable=False, error_code="invalid_job_input")
    except TransientJobError as exc:
        _report_failure(client, job, exc, retryable=True, error_code="transient_processing_error")
    except Exception as exc:  # noqa: BLE001
        _report_failure(client, job, exc, retryable=True, error_code="unexpected_processing_error")
    finally:
        presence.set_idle()


class FairClaimPoller:
    def __init__(self) -> None:
        self._cursor = 0
        self._claims = (
            ("/internal/worker/face-jobs/claim", {}, process_face_job),
            ("/internal/worker/face-search-jobs/claim", {}, process_face_search_job),
            ("/internal/worker/photo-jobs/claim", {"job_types": WORKER_JOB_TYPES}, process_photo_job),
        )

    def work_once(self, client: BackendClient, presence: WorkerPresenceHeartbeat) -> bool:
        for offset in range(len(self._claims)):
            index = (self._cursor + offset) % len(self._claims)
            path, payload, processor = self._claims[index]
            claimed = client.post(path, payload, retry_safe=False)
            job = claimed.get("job")
            if not job:
                continue
            if not isinstance(job, dict):
                raise ControlPlaneError("Backend returned an invalid job payload.")
            self._cursor = (index + 1) % len(self._claims)
            handle_claimed_job(client, job, processor, presence)
            return True
        self._cursor = (self._cursor + 1) % len(self._claims)
        return False


def main() -> None:
    require_config()
    stop = threading.Event()

    def request_shutdown(_signum, _frame) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)

    client = BackendClient()
    poller = FairClaimPoller()
    presence = WorkerPresenceHeartbeat()
    presence.start()
    consecutive_errors = 0
    log_event("info", "worker_started", job_types=WORKER_JOB_TYPES)
    try:
        while not stop.is_set():
            try:
                presence.ensure_healthy()
                did_work = poller.work_once(client, presence)
                consecutive_errors = 0
            except WorkerConfigError:
                log_event("critical", "worker_authentication_failed")
                raise
            except ControlPlaneError as exc:
                consecutive_errors += 1
                delay = min(60.0, WORKER_HTTP_BACKOFF_SECONDS * (2 ** min(consecutive_errors, 7)))
                delay *= random.uniform(0.75, 1.25)  # nosec B311
                log_event(
                    "error",
                    "worker_loop_control_plane_error",
                    error_type=exc.__class__.__name__,
                    retry_in_seconds=round(delay, 2),
                )
                stop.wait(delay)
                continue
            except Exception as exc:  # noqa: BLE001
                consecutive_errors += 1
                delay = min(60.0, 2 ** min(consecutive_errors, 6)) * random.uniform(0.75, 1.25)  # nosec B311
                log_event(
                    "error",
                    "worker_loop_error",
                    error_type=exc.__class__.__name__,
                    retry_in_seconds=round(delay, 2),
                )
                stop.wait(delay)
                continue

            if not did_work:
                stop.wait(WORKER_POLL_SECONDS * random.uniform(0.85, 1.15))  # nosec B311
    finally:
        presence.stop()
        client.close()
        log_event("info", "worker_stopped")


if __name__ == "__main__":
    main()
