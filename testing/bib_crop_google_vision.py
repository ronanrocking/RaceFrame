from __future__ import annotations

import argparse
import io
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_DIR = REPO_ROOT / "test-data" / "testsetA"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "testing" / "bib_crop_output"
DEFAULT_LABEL_DIR = REPO_ROOT / "testing" / "bib_detection" / "labels"
DEFAULT_CREDENTIALS_FILE = REPO_ROOT / "raceframeocr-f8e5bc26748a.json"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


@dataclass
class BibBox:
    left: int
    top: int
    right: int
    bottom: int
    confidence: float
    source: str

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


@dataclass
class OCRTextDetectionResult:
    detected_text: str
    normalized_text: str
    confidence: float | None
    bounding_box_json: dict | None
    crop_name: str
    crop_box: BibBox


def normalize_detected_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().upper()


def list_input_images(input_dir: Path) -> list[Path]:
    if not input_dir.exists():
        raise RuntimeError(f"Input folder does not exist: {input_dir}")
    return sorted(
        path for path in input_dir.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    )


def clamp_box(box: BibBox, image_width: int, image_height: int) -> BibBox | None:
    left = max(0, min(image_width - 1, int(box.left)))
    top = max(0, min(image_height - 1, int(box.top)))
    right = max(left + 1, min(image_width, int(box.right)))
    bottom = max(top + 1, min(image_height, int(box.bottom)))
    if right <= left or bottom <= top:
        return None
    return BibBox(left=left, top=top, right=right, bottom=bottom, confidence=box.confidence, source=box.source)


def expand_box(box: BibBox, image_width: int, image_height: int, pad_ratio: float) -> BibBox:
    pad_x = int(box.width * pad_ratio)
    pad_y = int(box.height * pad_ratio)
    expanded = BibBox(
        left=box.left - pad_x,
        top=box.top - pad_y,
        right=box.right + pad_x,
        bottom=box.bottom + pad_y,
        confidence=box.confidence,
        source=box.source,
    )
    clamped = clamp_box(expanded, image_width, image_height)
    if clamped is None:
        return box
    return clamped


def boxes_overlap(a: BibBox, b: BibBox) -> float:
    left = max(a.left, b.left)
    top = max(a.top, b.top)
    right = min(a.right, b.right)
    bottom = min(a.bottom, b.bottom)
    if right <= left or bottom <= top:
        return 0.0
    intersection = (right - left) * (bottom - top)
    a_area = a.width * a.height
    b_area = b.width * b.height
    union = a_area + b_area - intersection
    return intersection / union if union else 0.0


def dedupe_boxes(boxes: list[BibBox], iou_threshold: float) -> list[BibBox]:
    selected: list[BibBox] = []
    for box in sorted(boxes, key=lambda item: item.confidence, reverse=True):
        if all(boxes_overlap(box, existing) < iou_threshold for existing in selected):
            selected.append(box)
    return sorted(selected, key=lambda item: (item.top, item.left))


def load_manual_yolo_boxes(image_path: Path, label_dir: Path) -> list[BibBox]:
    label_path = label_dir / f"{image_path.stem}.txt"
    if not label_path.exists():
        return []

    with Image.open(image_path) as image:
        image_width, image_height = image.size

    boxes: list[BibBox] = []
    for line_number, line in enumerate(label_path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split()
        if len(parts) < 5:
            raise RuntimeError(f"Bad YOLO label in {label_path} line {line_number}: {line}")
        _, x_center, y_center, width, height = parts[:5]
        x_center_f = float(x_center) * image_width
        y_center_f = float(y_center) * image_height
        width_f = float(width) * image_width
        height_f = float(height) * image_height
        box = BibBox(
            left=int(x_center_f - width_f / 2),
            top=int(y_center_f - height_f / 2),
            right=int(x_center_f + width_f / 2),
            bottom=int(y_center_f + height_f / 2),
            confidence=1.0,
            source="manual_yolo",
        )
        clamped = clamp_box(box, image_width, image_height)
        if clamped:
            boxes.append(clamped)
    return boxes


def detect_bib_candidates_classical(image: Image.Image, *, max_boxes: int) -> list[BibBox]:
    """Find paper-like rectangular bib candidates without a model.

    This is a starter detector for today's local test set. It favors light, low-saturation
    rectangular regions near runner torsos, then OCR decides whether the crop contains a bib.
    """
    rgb = image.convert("RGB")
    array = np.asarray(rgb).astype(np.int16)
    height, width, _ = array.shape

    max_channel = array.max(axis=2)
    min_channel = array.min(axis=2)
    saturation = max_channel - min_channel
    brightness = max_channel

    light_paper = (brightness >= 155) & (saturation <= 105)
    yellow_or_blue_bib = ((array[:, :, 2] > 115) & (array[:, :, 0] < 120)) | (
        (array[:, :, 0] > 145) & (array[:, :, 1] > 120) & (array[:, :, 2] < 110)
    )
    mask = light_paper | yellow_or_blue_bib

    # Race bibs usually sit below faces and above legs. Keep the whole width,
    # but downweight sky/banner regions by ignoring the top slice.
    mask[: int(height * 0.18), :] = False

    scale = max(width, height) / 900
    block = max(8, int(14 * scale))
    grid_h = int(np.ceil(height / block))
    grid_w = int(np.ceil(width / block))
    grid = np.zeros((grid_h, grid_w), dtype=bool)
    for row in range(grid_h):
        y0 = row * block
        y1 = min(height, y0 + block)
        for col in range(grid_w):
            x0 = col * block
            x1 = min(width, x0 + block)
            if mask[y0:y1, x0:x1].mean() >= 0.24:
                grid[row, col] = True

    seen = np.zeros_like(grid, dtype=bool)
    boxes: list[BibBox] = []
    for start_y in range(grid_h):
        for start_x in range(grid_w):
            if seen[start_y, start_x] or not grid[start_y, start_x]:
                continue
            stack = [(start_x, start_y)]
            seen[start_y, start_x] = True
            xs: list[int] = []
            ys: list[int] = []
            while stack:
                x, y = stack.pop()
                xs.append(x)
                ys.append(y)
                for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                    if 0 <= nx < grid_w and 0 <= ny < grid_h and not seen[ny, nx] and grid[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((nx, ny))

            left = min(xs) * block
            top = min(ys) * block
            right = min(width, (max(xs) + 1) * block)
            bottom = min(height, (max(ys) + 1) * block)
            candidate_width = right - left
            candidate_height = bottom - top
            area = candidate_width * candidate_height
            image_area = width * height
            aspect = candidate_width / max(candidate_height, 1)

            if candidate_width < width * 0.025 or candidate_height < height * 0.018:
                continue
            if area < image_area * 0.0006 or area > image_area * 0.035:
                continue
            if aspect < 0.9 or aspect > 4.2:
                continue
            vertical_center = ((top + bottom) / 2) / height
            if vertical_center < 0.25 or vertical_center > 0.88:
                continue

            crop_mask = mask[top:bottom, left:right]
            fill_ratio = float(crop_mask.mean()) if crop_mask.size else 0.0
            crop_brightness = brightness[top:bottom, left:right]
            dark_ratio = float((crop_brightness < 95).mean()) if crop_brightness.size else 0.0
            if dark_ratio < 0.015:
                continue
            torso_bias = 1.0 - abs(((top + bottom) / 2 / height) - 0.58)
            confidence = max(0.01, min(0.99, fill_ratio * 0.45 + dark_ratio * 2.8 + torso_bias * 0.22))
            box = BibBox(left=left, top=top, right=right, bottom=bottom, confidence=confidence, source="classical")
            boxes.append(expand_box(box, width, height, pad_ratio=0.18))

    return dedupe_boxes(boxes, iou_threshold=0.35)[:max_boxes]


def run_google_ocr(*, image_bytes: bytes, credentials_file: Path) -> list[dict]:
    if not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
        if not credentials_file.exists():
            raise RuntimeError(
                "Google OCR is not configured. Set GOOGLE_APPLICATION_CREDENTIALS or update --credentials-file."
            )
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(credentials_file)

    try:
        from google.cloud import vision
    except ImportError as exc:
        raise RuntimeError(
            "Google OCR dependency is missing. Install testing/requirements-bib-detection.txt."
        ) from exc

    client = vision.ImageAnnotatorClient()
    image = vision.Image(content=image_bytes)
    response = client.document_text_detection(image=image)
    if response.error.message:
        raise RuntimeError(f"Google OCR failed: {response.error.message}")

    detections: list[dict] = []
    full_text_annotation = getattr(response, "full_text_annotation", None)
    if full_text_annotation and full_text_annotation.pages:
        for page in full_text_annotation.pages:
            for block in page.blocks:
                for paragraph in block.paragraphs:
                    for word in paragraph.words:
                        detected_text = "".join(symbol.text for symbol in word.symbols).strip()
                        normalized_text = normalize_detected_text(detected_text)
                        if not normalized_text:
                            continue
                        detections.append(
                            {
                                "detected_text": detected_text,
                                "normalized_text": normalized_text,
                                "confidence": float(word.confidence)
                                if getattr(word, "confidence", None) is not None
                                else None,
                                "bounding_box_json": bounding_poly_to_json(word.bounding_box),
                            }
                        )
    return detections


def bounding_poly_to_json(bounding_poly) -> dict | None:
    if not bounding_poly or not getattr(bounding_poly, "vertices", None):
        return None
    vertices = [
        {"x": int(getattr(vertex, "x", 0) or 0), "y": int(getattr(vertex, "y", 0) or 0)}
        for vertex in bounding_poly.vertices
    ]
    xs = [vertex["x"] for vertex in vertices]
    ys = [vertex["y"] for vertex in vertices]
    return {
        "vertices": vertices,
        "left": min(xs) if xs else 0,
        "top": min(ys) if ys else 0,
        "right": max(xs) if xs else 0,
        "bottom": max(ys) if ys else 0,
        "width": (max(xs) - min(xs)) if xs else 0,
        "height": (max(ys) - min(ys)) if ys else 0,
    }


def image_to_png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def draw_debug_image(image: Image.Image, boxes: list[BibBox], output_path: Path) -> None:
    debug = image.convert("RGB").copy()
    draw = ImageDraw.Draw(debug)
    for index, box in enumerate(boxes, start=1):
        color = (20, 180, 90) if box.source == "manual_yolo" else (255, 120, 0)
        draw.rectangle((box.left, box.top, box.right, box.bottom), outline=color, width=4)
        draw.text((box.left + 4, max(0, box.top - 18)), f"{index}:{box.confidence:.2f}", fill=color)
    debug.save(output_path)


def build_output_text(image_path: Path, detections: list[OCRTextDetectionResult], boxes: list[BibBox]) -> str:
    lines = [
        f"image: {image_path.name}",
        f"bib_boxes_found: {len(boxes)}",
        f"labels_found: {len(detections)}",
        "",
    ]
    for index, detection in enumerate(detections, start=1):
        confidence = "" if detection.confidence is None else f" | confidence={detection.confidence:.4f}"
        box = detection.crop_box
        lines.append(
            f"{index}. crop={detection.crop_name}"
            f" | crop_box={box.left},{box.top},{box.right},{box.bottom}"
            f" | detected_text={detection.detected_text}"
            f" | normalized_text={detection.normalized_text}{confidence}"
        )
    if not detections:
        lines.append("No labels found.")
    return "\n".join(lines) + "\n"


def process_image(
    image_path: Path,
    *,
    output_dir: Path,
    detector: str,
    label_dir: Path,
    max_boxes: int,
    pad_ratio: float,
    crop_only: bool,
    credentials_file: Path,
) -> dict:
    image = Image.open(image_path).convert("RGB")
    image_width, image_height = image.size

    if detector == "manual":
        boxes = load_manual_yolo_boxes(image_path, label_dir)
    elif detector == "classical":
        boxes = detect_bib_candidates_classical(image, max_boxes=max_boxes)
    else:
        raise RuntimeError(f"Unsupported detector: {detector}")

    boxes = [
        expanded
        for box in boxes
        if (expanded := expand_box(box, image_width, image_height, pad_ratio=pad_ratio)) is not None
    ][:max_boxes]

    image_output_dir = output_dir / image_path.stem
    crop_dir = image_output_dir / "crops"
    crop_dir.mkdir(parents=True, exist_ok=True)

    draw_debug_image(image, boxes, image_output_dir / "debug_boxes.jpg")

    all_detections: list[OCRTextDetectionResult] = []
    crop_rows: list[dict] = []
    for index, box in enumerate(boxes, start=1):
        crop = image.crop((box.left, box.top, box.right, box.bottom))
        crop_name = f"{image_path.stem}_bib_{index:02d}.png"
        crop_path = crop_dir / crop_name
        crop.save(crop_path)
        crop_rows.append({"crop_name": crop_name, "crop_path": str(crop_path), "box": asdict(box)})

        if crop_only:
            continue

        ocr_rows = run_google_ocr(image_bytes=image_to_png_bytes(crop), credentials_file=credentials_file)
        for row in ocr_rows:
            all_detections.append(
                OCRTextDetectionResult(
                    detected_text=row["detected_text"],
                    normalized_text=row["normalized_text"],
                    confidence=row["confidence"],
                    bounding_box_json=row["bounding_box_json"],
                    crop_name=crop_name,
                    crop_box=box,
                )
            )

    text_path = image_output_dir / f"{image_path.stem}.txt"
    text_path.write_text(build_output_text(image_path, all_detections, boxes), encoding="utf-8")

    report = {
        "image": image_path.name,
        "detector": detector,
        "bib_boxes_found": len(boxes),
        "labels_found": len(all_detections),
        "boxes": [asdict(box) for box in boxes],
        "crops": crop_rows,
        "detections": [
            {
                "detected_text": detection.detected_text,
                "normalized_text": detection.normalized_text,
                "confidence": detection.confidence,
                "bounding_box_json": detection.bounding_box_json,
                "crop_name": detection.crop_name,
                "crop_box": asdict(detection.crop_box),
            }
            for detection in all_detections
        ],
    }
    (image_output_dir / f"{image_path.stem}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Detect likely bib regions, crop them, and OCR the crops with Google Vision.")
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--detector", choices=["classical", "manual"], default="classical")
    parser.add_argument("--label-dir", type=Path, default=DEFAULT_LABEL_DIR)
    parser.add_argument("--credentials-file", type=Path, default=DEFAULT_CREDENTIALS_FILE)
    parser.add_argument("--max-boxes", type=int, default=18)
    parser.add_argument("--pad-ratio", type=float, default=0.16)
    parser.add_argument("--crop-only", action="store_true", help="Only write crops/debug images; skip Google Vision calls.")
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    images = list_input_images(args.input_dir)
    if not images:
        raise RuntimeError(f"No supported images found in {args.input_dir}")

    print(f"Processing {len(images)} image(s)")
    print(f"Detector: {args.detector}")
    print(f"Output: {args.output_dir}")

    summary: list[dict] = []
    success_count = 0
    failure_count = 0
    for image_path in images:
        try:
            report = process_image(
                image_path,
                output_dir=args.output_dir,
                detector=args.detector,
                label_dir=args.label_dir,
                max_boxes=args.max_boxes,
                pad_ratio=args.pad_ratio,
                crop_only=args.crop_only,
                credentials_file=args.credentials_file,
            )
        except Exception as exc:
            failure_count += 1
            print(f"[error] {image_path.name} -> {exc}")
            continue
        success_count += 1
        summary.append(report)
        print(
            f"[ok] {image_path.name} -> boxes={report['bib_boxes_found']} labels={report['labels_found']}"
        )

    (args.output_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print("")
    print(f"Finished. Success: {success_count} | Failed: {failure_count}")


if __name__ == "__main__":
    main()
