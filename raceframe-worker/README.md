# RaceFrame Worker

Dockerized background worker for RaceFrame OCR and face recognition.

It does not connect to Postgres or R2 directly. It communicates with the backend through token-protected `/internal/worker/*` endpoints and downloads image bytes through backend-issued presigned URLs.

Runtime responsibilities:

- claim queued `ocr` photo jobs
- claim queued `face_photo_scan` photo jobs
- claim queued `face_selfie_enroll` participant jobs
- run Google Vision OCR for bib detection
- run InsightFace for face detection and embeddings
- post results back to the backend

Server layout:

```text
~/raceframe/raceframe-worker/
  compose.yaml
  Dockerfile
  requirements.txt
  worker.py
  .env
  secrets/
  model-cache/
```

Start:

```bash
docker compose up -d --build
docker compose logs -f --tail 80
```
