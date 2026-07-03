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


settings = Settings()
