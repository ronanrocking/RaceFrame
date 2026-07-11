from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GROUND_TRUTH_PATH = REPO_ROOT / "testing" / "bib_ground_truth.txt"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "testing" / "bib_crop_output"


@dataclass
class ExpectedToken:
    raw: str
    partial: bool

    @property
    def base(self) -> str:
        return self.raw[:-1] if self.partial else self.raw


def parse_ground_truth(path: Path) -> dict[str, list[ExpectedToken]]:
    result: dict[str, list[ExpectedToken]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split()
        result[parts[0]] = [ExpectedToken(raw=token, partial=token.endswith("?")) for token in parts[1:]]
    return result


def parse_detected_tokens(output_path: Path) -> list[str]:
    tokens: list[str] = []
    pattern = re.compile(r"normalized_text=(.+?)(?: \| confidence=|$)")
    if not output_path.exists():
        return tokens
    for line in output_path.read_text(encoding="utf-8").splitlines():
        match = pattern.search(line)
        if not match:
            continue
        token = match.group(1).strip()
        if token:
            tokens.append(token)
    return tokens


def token_matches(expected: ExpectedToken, detected: str, *, strict: bool) -> bool:
    if strict:
        return expected.base == detected
    if expected.partial:
        return expected.base in detected or detected in expected.base
    return expected.base == detected


def output_file_for(output_dir: Path, image_name: str) -> Path:
    stem = Path(image_name).stem
    return output_dir / stem / f"{stem}.txt"


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare bib-crop OCR output against testing/bib_ground_truth.txt.")
    parser.add_argument("--ground-truth", type=Path, default=DEFAULT_GROUND_TRUTH_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--strict", action="store_true", help="Require exact token matches.")
    args = parser.parse_args()

    ground_truth = parse_ground_truth(args.ground_truth)
    image_rows: list[str] = []
    total_expected = 0
    total_matched = 0

    for image_name, expected_tokens in sorted(ground_truth.items(), key=lambda item: int(Path(item[0]).stem)):
        detected_tokens = parse_detected_tokens(output_file_for(args.output_dir, image_name))
        matched: list[str] = []
        missed: list[str] = []
        false_positives = detected_tokens.copy()

        for expected in expected_tokens:
            total_expected += 1
            match_value = next(
                (detected for detected in detected_tokens if token_matches(expected, detected, strict=args.strict)),
                None,
            )
            if match_value is None:
                missed.append(expected.raw)
                continue
            matched.append(f"{expected.raw}->{match_value}")
            total_matched += 1
            if match_value in false_positives:
                false_positives.remove(match_value)

        recall = (len(matched) / len(expected_tokens) * 100) if expected_tokens else 0.0
        image_rows.append(
            f"{image_name}: matched {len(matched)}/{len(expected_tokens)} ({recall:.1f}%)"
            f" | missed=[{', '.join(missed) if missed else '-'}]"
            f" | matched_values=[{', '.join(matched) if matched else '-'}]"
            f" | extra_detected={len(false_positives)}"
        )

    overall_recall = (total_matched / total_expected * 100) if total_expected else 0.0
    print(f"Overall matched {total_matched}/{total_expected} ({overall_recall:.1f}%)")
    print("")
    for row in image_rows:
        print(row)


if __name__ == "__main__":
    main()
