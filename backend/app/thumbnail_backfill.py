from __future__ import annotations

import argparse
import logging

from sqlalchemy import select

from .config import settings
from .db import SessionLocal
from .models import Photo
from .photographer import build_thumbnail_object_key, create_photo_thumbnail, upload_thumbnail_photo
from .storage import get_object_body
from .uploads import validate_image_bytes


logger = logging.getLogger(__name__)


def run_batch(*, limit: int, apply: bool) -> tuple[int, int]:
    processed = 0
    failed = 0
    with SessionLocal() as session:
        photos = list(
            session.execute(
                select(Photo)
                .where(Photo.thumbnail_object_key.is_(None))
                .order_by(Photo.created_at)
                .limit(max(1, min(limit, 1_000)))
            ).scalars()
        )
        if not apply:
            return len(photos), 0

        for photo in photos:
            try:
                body, content_length, _stored_content_type = get_object_body(object_key=photo.original_object_key)
                if content_length is not None and content_length > settings.max_photo_upload_bytes:
                    raise ValueError("Stored original exceeds the configured limit.")
                content = body.read(settings.max_photo_upload_bytes + 1)
                image = validate_image_bytes(
                    content,
                    file_name=photo.file_name,
                    declared_content_type=photo.content_type,
                    max_bytes=settings.max_photo_upload_bytes,
                )
                thumbnail_key = build_thumbnail_object_key(
                    event_id=photo.event_id,
                    photo_id=photo.id,
                    file_name=photo.file_name,
                )
                upload_thumbnail_photo(content=create_photo_thumbnail(content=content), object_key=thumbnail_key)
                photo.thumbnail_object_key = thumbnail_key
                if not photo.checksum_sha256:
                    duplicate_id = session.scalar(
                        select(Photo.id).where(
                            Photo.event_id == photo.event_id,
                            Photo.checksum_sha256 == image.sha256,
                            Photo.id != photo.id,
                        )
                    )
                    if duplicate_id is None:
                        photo.checksum_sha256 = image.sha256
                    else:
                        logger.warning(
                            "Legacy duplicate photo left without a checksum",
                            extra={"photo_id": str(photo.id)},
                        )
                session.commit()
                processed += 1
            except Exception:  # noqa: BLE001
                session.rollback()
                failed += 1
                logger.exception("Thumbnail backfill failed", extra={"photo_id": str(photo.id)})
    return processed, failed


def main() -> None:
    parser = argparse.ArgumentParser(description="Idempotently backfill missing RaceFrame thumbnails.")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--apply", action="store_true", help="Write thumbnails. Without this flag, only count rows.")
    args = parser.parse_args()
    processed, failed = run_batch(limit=args.limit, apply=args.apply)
    print(f"processed={processed} failed={failed} apply={args.apply}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
