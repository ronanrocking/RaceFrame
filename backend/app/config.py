from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_csv(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(item.strip() for item in os.getenv(name, default).split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    environment: str = os.getenv("APP_ENV", "development").strip().lower()
    app_secret_key: str = os.getenv("RACEFRAME_SECRET_KEY", "")
    allowed_hosts: tuple[str, ...] = _env_csv("ALLOWED_HOSTS", "localhost,127.0.0.1,testserver")
    public_origins: tuple[str, ...] = _env_csv("PUBLIC_ORIGINS", "http://localhost,http://127.0.0.1")
    trust_proxy_headers: bool = _env_bool("TRUST_PROXY_HEADERS", False)
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./raceframe.db")
    app_name: str = os.getenv("APP_NAME", "RaceFrame Backend")
    r2_account_id: str = os.getenv("CLOUDFLARE_R2_ACCOUNT_ID", "")
    r2_bucket_name: str = os.getenv("CLOUDFLARE_R2_BUCKET_NAME", "")
    r2_endpoint: str = os.getenv("CLOUDFLARE_R2_ENDPOINT", "")
    r2_access_key_id: str = os.getenv("CLOUDFLARE_R2_ACCESS_KEY_ID", "")
    r2_secret_access_key: str = os.getenv("CLOUDFLARE_R2_SECRET_ACCESS_KEY", "")
    r2_presigned_url_ttl_seconds: int = int(os.getenv("CLOUDFLARE_R2_PRESIGNED_URL_TTL_SECONDS", "3600"))
    max_photo_upload_bytes: int = int(os.getenv("MAX_PHOTO_UPLOAD_BYTES", str(25 * 1024 * 1024)))
    max_selfie_upload_bytes: int = int(os.getenv("MAX_SELFIE_UPLOAD_BYTES", str(10 * 1024 * 1024)))
    max_participant_upload_bytes: int = int(os.getenv("MAX_PARTICIPANT_UPLOAD_BYTES", str(5 * 1024 * 1024)))
    max_photo_batch_files: int = int(os.getenv("MAX_PHOTO_BATCH_FILES", "50"))
    max_selfie_batch_files: int = int(os.getenv("MAX_SELFIE_BATCH_FILES", "5"))
    max_photo_request_bytes: int = int(os.getenv("MAX_PHOTO_REQUEST_BYTES", str(256 * 1024 * 1024)))
    max_form_request_bytes: int = int(os.getenv("MAX_FORM_REQUEST_BYTES", str(2 * 1024 * 1024)))
    max_image_pixels: int = int(os.getenv("MAX_IMAGE_PIXELS", "40000000"))
    max_image_dimension: int = int(os.getenv("MAX_IMAGE_DIMENSION", "12000"))
    max_participant_rows: int = int(os.getenv("MAX_PARTICIPANT_ROWS", "10000"))
    max_participant_columns: int = int(os.getenv("MAX_PARTICIPANT_COLUMNS", "32"))
    max_participant_cell_chars: int = int(os.getenv("MAX_PARTICIPANT_CELL_CHARS", "512"))
    max_spreadsheet_uncompressed_bytes: int = int(
        os.getenv("MAX_SPREADSHEET_UNCOMPRESSED_BYTES", str(50 * 1024 * 1024))
    )
    photo_thumbnail_max_edge: int = int(os.getenv("PHOTO_THUMBNAIL_MAX_EDGE", "640"))
    photo_thumbnail_quality: int = int(os.getenv("PHOTO_THUMBNAIL_QUALITY", "72"))
    max_face_search_backlog: int = int(os.getenv("MAX_FACE_SEARCH_BACKLOG", "100"))
    max_photo_job_backlog: int = int(os.getenv("MAX_PHOTO_JOB_BACKLOG", "10000"))
    max_search_results: int = int(os.getenv("MAX_SEARCH_RESULTS", "250"))
    max_search_faces_per_event: int = int(os.getenv("MAX_SEARCH_FACES_PER_EVENT", "25000"))
    search_capability_ttl_seconds: int = int(os.getenv("SEARCH_CAPABILITY_TTL_SECONDS", "3600"))
    biometric_retention_hours: int = int(os.getenv("BIOMETRIC_RETENTION_HOURS", "24"))
    raw_response_retention_hours: int = int(os.getenv("RAW_RESPONSE_RETENTION_HOURS", "24"))
    deletion_retry_batch_size: int = int(os.getenv("DELETION_RETRY_BATCH_SIZE", "100"))
    deletion_task_retention_days: int = int(os.getenv("DELETION_TASK_RETENTION_DAYS", "7"))
    worker_lease_seconds: int = int(os.getenv("WORKER_LEASE_SECONDS", "300"))
    worker_max_attempts: int = int(os.getenv("WORKER_MAX_ATTEMPTS", "5"))
    worker_retry_base_seconds: int = int(os.getenv("WORKER_RETRY_BASE_SECONDS", "10"))
    worker_retry_max_seconds: int = int(os.getenv("WORKER_RETRY_MAX_SECONDS", "900"))
    worker_api_token: str = os.getenv("WORKER_API_TOKEN", "")
    metrics_api_token: str = os.getenv("METRICS_API_TOKEN", "")
    worker_heartbeat_stale_seconds: int = int(os.getenv("WORKER_HEARTBEAT_STALE_SECONDS", "120"))
    worker_heartbeat_retention_days: int = int(os.getenv("WORKER_HEARTBEAT_RETENTION_DAYS", "30"))
    face_match_similarity_threshold: float = float(os.getenv("FACE_MATCH_SIMILARITY_THRESHOLD", "0.45"))
    face_candidate_similarity_threshold: float = float(os.getenv("FACE_CANDIDATE_SIMILARITY_THRESHOLD", "0.36"))
    face_medium_similarity_threshold: float = float(os.getenv("FACE_MEDIUM_SIMILARITY_THRESHOLD", "0.42"))
    face_strong_similarity_threshold: float = float(os.getenv("FACE_STRONG_SIMILARITY_THRESHOLD", "0.50"))
    face_reinforcement_similarity_threshold: float = float(os.getenv("FACE_REINFORCEMENT_SIMILARITY_THRESHOLD", "0.48"))
    face_reinforcement_max_embeddings: int = int(os.getenv("FACE_REINFORCEMENT_MAX_EMBEDDINGS", "12"))
    bib_only_seed_photo_limit: int = int(os.getenv("BIB_ONLY_SEED_PHOTO_LIMIT", "10"))
    bib_only_seed_cluster_min_photos: int = int(os.getenv("BIB_ONLY_SEED_CLUSTER_MIN_PHOTOS", "2"))
    bib_only_seed_cluster_majority_ratio: float = float(os.getenv("BIB_ONLY_SEED_CLUSTER_MAJORITY_RATIO", "0.60"))
    bib_only_seed_cluster_lead_ratio: float = float(os.getenv("BIB_ONLY_SEED_CLUSTER_LEAD_RATIO", "1.50"))


settings = Settings()
