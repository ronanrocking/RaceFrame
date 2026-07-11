from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass
from pathlib import Path

from PIL import Image


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_DIR = REPO_ROOT / "test-data" / "testsetA"
DEFAULT_GROUND_TRUTH_PATH = REPO_ROOT / "testing" / "bib_ground_truth.txt"
DEFAULT_LABEL_DIR = REPO_ROOT / "testing" / "bib_detection" / "labels"
DEFAULT_CREDENTIALS_FILE = REPO_ROOT / "raceframeocr-f8e5bc26748a.json"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


@dataclass
class ExpectedToken:
    raw: str
    partial: bool

    @property
    def base(self) -> str:
        return self.raw[:-1] if self.partial else self.raw


@dataclass
class WordBox:
    text: str
    left: int
    top: int
    right: int
    bottom: int


def normalize_detected_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().upper()


def parse_ground_truth(path: Path) -> dict[str, list[ExpectedToken]]:
    result: dict[str, list[ExpectedToken]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split()
        result[parts[0]] = [ExpectedToken(raw=token, partial=token.endswith("?")) for token in parts[1:]]
    return result


def token_matches(expected: ExpectedToken, detected: str) -> bool:
    if expected.partial:
        return expected.base in detected or detected in expected.base
    return expected.base == detected


def run_google_word_boxes(*, image_path: Path, credentials_file: Path) -> list[WordBox]:
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
    response = client.document_text_detection(image=vision.Image(content=image_path.read_bytes()))
    if response.error.message:
        raise RuntimeError(f"Google OCR failed: {response.error.message}")

    boxes: list[WordBox] = []
    full_text_annotation = getattr(response, "full_text_annotation", None)
    if not full_text_annotation:
        return boxes

    for page in full_text_annotation.pages:
        for block in page.blocks:
            for paragraph in block.paragraphs:
                for word in paragraph.words:
                    text = normalize_detected_text("".join(symbol.text for symbol in word.symbols).strip())
                    if not text:
                        continue
                    vertices = word.bounding_box.vertices
                    xs = [int(getattr(vertex, "x", 0) or 0) for vertex in vertices]
                    ys = [int(getattr(vertex, "y", 0) or 0) for vertex in vertices]
                    if xs and ys:
                        boxes.append(WordBox(text=text, left=min(xs), top=min(ys), right=max(xs), bottom=max(ys)))
    return boxes


def yolo_line_for_box(box: WordBox, image_width: int, image_height: int, pad_ratio: float) -> str:
    width = box.right - box.left
    height = box.bottom - box.top
    pad_x = int(width * pad_ratio)
    pad_y = int(height * pad_ratio)
    left = max(0, box.left - pad_x)
    top = max(0, box.top - pad_y)
    right = min(image_width, box.right + pad_x)
    bottom = min(image_height, box.bottom + pad_y)

    x_center = ((left + right) / 2) / image_width
    y_center = ((top + bottom) / 2) / image_height
    norm_width = (right - left) / image_width
    norm_height = (bottom - top) / image_height
    return f"0 {x_center:.6f} {y_center:.6f} {norm_width:.6f} {norm_height:.6f}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Use Google Vision word boxes plus bib_ground_truth.txt to create starter YOLO bib labels."
    )
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--ground-truth", type=Path, default=DEFAULT_GROUND_TRUTH_PATH)
    parser.add_argument("--label-dir", type=Path, default=DEFAULT_LABEL_DIR)
    parser.add_argument("--credentials-file", type=Path, default=DEFAULT_CREDENTIALS_FILE)
    parser.add_argument("--pad-ratio", type=float, default=1.4)
    args = parser.parse_args()

    ground_truth = parse_ground_truth(args.ground_truth)
    args.label_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    missing = 0

    for image_name, expected_tokens in sorted(ground_truth.items(), key=lambda item: int(Path(item[0]).stem)):
        image_path = args.input_dir / image_name
        if not image_path.exists():
            print(f"[missing image] {image_name}")
            missing += 1
            continue
        with Image.open(image_path) as image:
            image_width, image_height = image.size

        word_boxes = run_google_word_boxes(image_path=image_path, credentials_file=args.credentials_file)
        label_lines: list[str] = []
        used_indices: set[int] = set()
        for expected in expected_tokens:
            for index, word_box in enumerate(word_boxes):
                if index in used_indices:
                    continue
                if token_matches(expected, word_box.text):
                    label_lines.append(yolo_line_for_box(word_box, image_width, image_height, args.pad_ratio))
                    used_indices.add(index)
                    break

        label_path = args.label_dir / f"{Path(image_name).stem}.txt"
        label_path.write_text("\n".join(label_lines) + ("\n" if label_lines else ""), encoding="utf-8")
        written += 1
        print(f"[ok] {image_name} -> {len(label_lines)} label(s)")

    print("")
    print(f"Finished. Label files written: {written} | Missing images: {missing}")


if __name__ == "__main__":
    main()
