from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


INPUT_DIR = Path(r"D:\PWD\Porjects\raceframe\test-data\testsetA")
OUTPUT_DIR = Path(r"D:\PWD\Porjects\raceframe\testing\output")
CREDENTIALS_FILE = Path(r"D:\PWD\Porjects\raceframe\raceframeocr-f8e5bc26748a.json")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


@dataclass
class OCRTextDetectionResult:
    detected_text: str
    normalized_text: str
    confidence: float | None
    bounding_box_json: dict | None


@dataclass
class OCRResult:
    detections: list[OCRTextDetectionResult]
    raw_response_json: dict


def normalize_detected_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().upper()


def run_google_ocr(*, image_bytes: bytes) -> OCRResult:
    if not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
        if not CREDENTIALS_FILE.exists():
            raise RuntimeError(
                "Google OCR is not configured. Set GOOGLE_APPLICATION_CREDENTIALS or update CREDENTIALS_FILE."
            )
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(CREDENTIALS_FILE)

    try:
        from google.cloud import vision
        from google.protobuf.json_format import MessageToDict
    except ImportError as exc:
        raise RuntimeError("Google OCR dependency is missing. Install google-cloud-vision.") from exc

    client = vision.ImageAnnotatorClient()
    image = vision.Image(content=image_bytes)
    response = client.document_text_detection(image=image)
    if response.error.message:
        raise RuntimeError(f"Google OCR failed: {response.error.message}")

    return OCRResult(
        detections=extract_google_ocr_detections(response),
        raw_response_json=MessageToDict(response._pb, preserving_proto_field_name=True),
    )


def extract_google_ocr_detections(response) -> list[OCRTextDetectionResult]:
    detections: list[OCRTextDetectionResult] = []
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
                            OCRTextDetectionResult(
                                detected_text=detected_text,
                                normalized_text=normalized_text,
                                confidence=float(word.confidence)
                                if getattr(word, "confidence", None) is not None
                                else None,
                                bounding_box_json=box_json,
                            )
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
            OCRTextDetectionResult(
                detected_text=detected_text,
                normalized_text=normalized_text,
                confidence=None,
                bounding_box_json=box_json,
            )
        )

    return sort_ocr_detections(detections)


def bounding_poly_to_json(bounding_poly, **metadata) -> dict | None:
    if not bounding_poly or not getattr(bounding_poly, "vertices", None):
        return metadata or None

    vertices = [
        {"x": int(getattr(vertex, "x", 0) or 0), "y": int(getattr(vertex, "y", 0) or 0)}
        for vertex in bounding_poly.vertices
    ]
    xs = [vertex["x"] for vertex in vertices]
    ys = [vertex["y"] for vertex in vertices]
    payload = {
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


def vertices_key_from_box(box_json: dict | None) -> tuple[tuple[int, int], ...]:
    if not box_json:
        return ()
    return tuple(
        (int(vertex.get("x", 0)), int(vertex.get("y", 0)))
        for vertex in box_json.get("vertices", [])
    )


def sort_ocr_detections(detections: list[OCRTextDetectionResult]) -> list[OCRTextDetectionResult]:
    return sorted(
        detections,
        key=lambda detection: (
            int((detection.bounding_box_json or {}).get("top", 0)),
            int((detection.bounding_box_json or {}).get("left", 0)),
            detection.detected_text,
        ),
    )


def list_input_images() -> list[Path]:
    if not INPUT_DIR.exists():
        raise RuntimeError(f"Input folder does not exist: {INPUT_DIR}")

    return sorted(
        path for path in INPUT_DIR.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    )


def build_output_text(image_path: Path, result: OCRResult) -> str:
    lines = [f"image: {image_path.name}", f"labels_found: {len(result.detections)}", ""]
    for index, detection in enumerate(result.detections, start=1):
        confidence = "" if detection.confidence is None else f" | confidence={detection.confidence:.4f}"
        lines.append(
            f"{index}. detected_text={detection.detected_text} | normalized_text={detection.normalized_text}{confidence}"
        )
    if not result.detections:
        lines.append("No labels found.")
    return "\n".join(lines) + "\n"


def process_image(image_path: Path) -> Path:
    result = run_google_ocr(image_bytes=image_path.read_bytes())
    output_path = OUTPUT_DIR / f"{image_path.stem}.txt"
    output_path.write_text(build_output_text(image_path, result), encoding="utf-8")
    return output_path


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    images = list_input_images()
    if not images:
        raise RuntimeError(f"No supported images found in {INPUT_DIR}")

    print(f"Processing {len(images)} image(s) from: {INPUT_DIR}")
    print(f"Writing OCR label files to: {OUTPUT_DIR}")

    success_count = 0
    failure_count = 0
    for image_path in images:
        try:
            output_path = process_image(image_path)
        except Exception as exc:
            failure_count += 1
            print(f"[error] {image_path.name} -> {exc}")
            continue

        success_count += 1
        print(f"[ok] {image_path.name} -> {output_path.name}")

    print("")
    print(f"Finished. Success: {success_count} | Failed: {failure_count}")


if __name__ == "__main__":
    main()
