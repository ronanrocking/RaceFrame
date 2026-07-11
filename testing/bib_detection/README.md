# RaceFrame Bib Detection Test Harness

This folder is for testing the crop-first pipeline:

1. find likely bib regions in a race photo
2. crop those regions
3. run Google Vision on the crops
4. compare crop OCR against `testing/bib_ground_truth.txt`

## Install local test dependencies

Use the Codex Python runtime on this machine:

```powershell
$py = "C:\Users\Hp-\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
& $py -m pip install -r testing\requirements-bib-detection.txt
```

## Run crop generation without Google Vision

This creates crop files and debug images only. It is the fastest smoke test.

```powershell
$py = "C:\Users\Hp-\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
& $py testing\bib_crop_google_vision.py --crop-only
```

Outputs go to:

```text
testing/bib_crop_output/
  summary.json
  0/
    debug_boxes.jpg
    crops/
    0.txt
    0.json
```

## Run bib crop + Google Vision

```powershell
$py = "C:\Users\Hp-\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
& $py testing\bib_crop_google_vision.py
```

Then compare:

```powershell
& $py testing\compare_bib_crop_ground_truth.py
& $py testing\compare_bib_crop_ground_truth.py --strict
```

## Detector modes

Current modes:

- `classical`: no trained model; finds likely paper-like bib regions. Useful today on the 30 local images.
- `manual`: reads YOLO-format labels from `testing/bib_detection/labels`.

The classical detector is not the final production answer. It is a cheap way to start the crop-first experiment while we collect labels.

## Bootstrap YOLO labels from Google Vision

This optional script uses full-image Google Vision word boxes plus `bib_ground_truth.txt` to create starter YOLO labels.

```powershell
& $py testing\bootstrap_bib_labels_from_google_vision.py
```

That writes:

```text
testing/bib_detection/labels/*.txt
```

Those labels should be reviewed before training a real model. They are useful as a starting point, not final truth.

## Recommended production detector path

After the local experiment proves crop-first OCR helps, train a tiny one-class detector:

- class name: `bib`
- preferred model family: YOLOX-Nano or YOLOX-Tiny
- export format for the worker VPS: ONNX
- worker behavior: process one image at a time on CPU

Keep the public backend on `ssh oci`. Put model inference and queue-style processing on `ssh oci-worker`.
