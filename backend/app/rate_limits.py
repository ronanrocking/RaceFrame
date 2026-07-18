from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta, timezone

from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .config import settings
from .db import SessionLocal
from .models import RateLimitBucket


def _privacy_hash(value: str) -> str:
    key = settings.app_secret_key.encode("utf-8") or b"raceframe-development-only"
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def hit_persistent_limit(*, bucket: str, key: str, limit: int, window_seconds: int, units: int = 1) -> int | None:
    """Atomically increment a cross-process fixed-window limit.

    Returns Retry-After seconds when the limit has been exceeded.
    """
    if limit <= 0 or window_seconds <= 0 or units <= 0:
        raise RuntimeError("Rate limit configuration must be positive.")
    now = datetime.now(timezone.utc)
    epoch = int(now.timestamp())
    window_epoch = epoch - (epoch % window_seconds)
    window_start = datetime.fromtimestamp(window_epoch, tz=timezone.utc)
    expires_at = window_start + timedelta(seconds=window_seconds * 2)
    normalized_bucket = bucket[:64]
    key_hash = _privacy_hash(key)

    with SessionLocal() as session:
        table = RateLimitBucket.__table__
        insert_factory = sqlite_insert if session.get_bind().dialect.name == "sqlite" else postgresql_insert
        statement = insert_factory(table).values(
            bucket=normalized_bucket,
            key_hash=key_hash,
            window_start=window_start,
            count=units,
            expires_at=expires_at,
            updated_at=now,
        )
        result = session.execute(
            statement.on_conflict_do_update(
                index_elements=[table.c.bucket, table.c.key_hash, table.c.window_start],
                set_={
                    "count": table.c.count + units,
                    "expires_at": excluded_expires_at(statement),
                    "updated_at": now,
                },
            ).returning(table.c.count)
        )
        count = int(result.scalar_one())
        session.commit()

    if count <= limit:
        return None
    return max(1, int((window_start + timedelta(seconds=window_seconds) - now).total_seconds()) + 1)


def excluded_expires_at(statement):
    # Kept as a helper because both PostgreSQL and SQLite dialect inserts expose
    # the same `excluded` namespace but static type checkers do not model it.
    return statement.excluded.expires_at
