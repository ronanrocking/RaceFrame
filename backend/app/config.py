from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./raceframe.db")
    app_name: str = os.getenv("APP_NAME", "RaceFrame Backend")
    r2_account_id: str = os.getenv("CLOUDFLARE_R2_ACCOUNT_ID", "")
    r2_bucket_name: str = os.getenv("CLOUDFLARE_R2_BUCKET_NAME", "")
    r2_endpoint: str = os.getenv("CLOUDFLARE_R2_ENDPOINT", "")
    r2_access_key_id: str = os.getenv("CLOUDFLARE_R2_ACCESS_KEY_ID", "")
    r2_secret_access_key: str = os.getenv("CLOUDFLARE_R2_SECRET_ACCESS_KEY", "")
    r2_presigned_url_ttl_seconds: int = int(os.getenv("CLOUDFLARE_R2_PRESIGNED_URL_TTL_SECONDS", "3600"))
    google_cloud_project: str = os.getenv("GOOGLE_CLOUD_PROJECT", "")
    google_application_credentials: str = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
    max_photo_upload_bytes: int = int(os.getenv("MAX_PHOTO_UPLOAD_BYTES", str(25 * 1024 * 1024)))
    max_selfie_upload_bytes: int = int(os.getenv("MAX_SELFIE_UPLOAD_BYTES", str(10 * 1024 * 1024)))
    worker_api_token: str = os.getenv("WORKER_API_TOKEN", "")
    face_match_similarity_threshold: float = float(os.getenv("FACE_MATCH_SIMILARITY_THRESHOLD", "0.45"))
    face_candidate_similarity_threshold: float = float(os.getenv("FACE_CANDIDATE_SIMILARITY_THRESHOLD", "0.36"))
    face_medium_similarity_threshold: float = float(os.getenv("FACE_MEDIUM_SIMILARITY_THRESHOLD", "0.42"))
    face_strong_similarity_threshold: float = float(os.getenv("FACE_STRONG_SIMILARITY_THRESHOLD", "0.50"))
    face_reinforcement_similarity_threshold: float = float(os.getenv("FACE_REINFORCEMENT_SIMILARITY_THRESHOLD", "0.48"))
    face_reinforcement_max_embeddings: int = int(os.getenv("FACE_REINFORCEMENT_MAX_EMBEDDINGS", "12"))


settings = Settings()
