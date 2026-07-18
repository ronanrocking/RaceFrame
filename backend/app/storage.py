from __future__ import annotations

import time
import threading
from contextlib import contextmanager
from functools import lru_cache
from typing import BinaryIO

from prometheus_client import Counter, Histogram

from .config import settings


STORAGE_OPERATIONS = Counter(
    "raceframe_storage_operations_total",
    "Object-storage operations by operation and outcome.",
    ("operation", "outcome"),
)
STORAGE_OPERATION_DURATION = Histogram(
    "raceframe_storage_operation_duration_seconds",
    "Object-storage operation duration.",
    ("operation",),
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)
_health_lock = threading.Lock()
_health_checked_at = 0.0
_health_ok = False


@contextmanager
def _observe_storage(operation: str):
    started = time.perf_counter()
    try:
        yield
    except Exception:
        STORAGE_OPERATIONS.labels(operation=operation, outcome="error").inc()
        raise
    else:
        STORAGE_OPERATIONS.labels(operation=operation, outcome="success").inc()
    finally:
        STORAGE_OPERATION_DURATION.labels(operation=operation).observe(time.perf_counter() - started)


def is_object_storage_configured() -> bool:
    return all(
        (
            settings.r2_bucket_name,
            settings.r2_endpoint,
            settings.r2_access_key_id,
            settings.r2_secret_access_key,
        )
    )


@lru_cache(maxsize=1)
def get_object_storage_client():
    if not is_object_storage_configured():
        raise RuntimeError("Object storage is not configured.")
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise RuntimeError("Object storage dependency is unavailable.") from exc

    return boto3.client(
        "s3",
        endpoint_url=settings.r2_endpoint,
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        region_name="auto",
        config=Config(
            connect_timeout=5,
            read_timeout=30,
            max_pool_connections=32,
            retries={"max_attempts": 4, "mode": "adaptive"},
            signature_version="s3v4",
        ),
    )


def object_storage_ready(*, cache_seconds: int = 30) -> bool:
    """Perform a cached authenticated bucket probe for readiness checks."""
    global _health_checked_at, _health_ok
    now = time.monotonic()
    with _health_lock:
        if now - _health_checked_at < max(1, cache_seconds):
            return _health_ok
        try:
            with _observe_storage("health"):
                get_object_storage_client().head_bucket(Bucket=settings.r2_bucket_name)
        except Exception:
            _health_ok = False
        else:
            _health_ok = True
        _health_checked_at = now
        return _health_ok


def put_object(*, object_key: str, content: bytes, content_type: str, cache_control: str = "private, no-store") -> None:
    with _observe_storage("put"):
        get_object_storage_client().put_object(
            Bucket=settings.r2_bucket_name,
            Key=object_key,
            Body=content,
            ContentLength=len(content),
            ContentType=content_type,
            CacheControl=cache_control,
            ServerSideEncryption="AES256",
        )


def delete_object(*, object_key: str) -> None:
    if not object_key:
        return
    with _observe_storage("delete"):
        get_object_storage_client().delete_object(Bucket=settings.r2_bucket_name, Key=object_key)


def object_exists(*, object_key: str) -> bool:
    try:
        with _observe_storage("head"):
            get_object_storage_client().head_object(Bucket=settings.r2_bucket_name, Key=object_key)
        return True
    except Exception as exc:  # botocore is optional at import time
        response = getattr(exc, "response", {}) or {}
        status_code = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status_code == 404:
            return False
        raise


def generate_download_url(*, object_key: str, download_name: str | None = None, ttl_seconds: int | None = None) -> str:
    params: dict[str, str] = {"Bucket": settings.r2_bucket_name, "Key": object_key}
    if download_name:
        safe_name = download_name.replace('"', "").replace("\r", "").replace("\n", "")[:200]
        params["ResponseContentDisposition"] = f'attachment; filename="{safe_name}"'
    return get_object_storage_client().generate_presigned_url(
        "get_object",
        Params=params,
        ExpiresIn=max(60, min(ttl_seconds or settings.r2_presigned_url_ttl_seconds, 3600)),
    )


def get_object_body(*, object_key: str) -> tuple[BinaryIO, int | None, str | None]:
    with _observe_storage("get"):
        response = get_object_storage_client().get_object(Bucket=settings.r2_bucket_name, Key=object_key)
    return response["Body"], response.get("ContentLength"), response.get("ContentType")
