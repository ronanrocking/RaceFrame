from __future__ import annotations

import argparse

from sqlalchemy import select

from .db import SessionLocal
from .maintenance import enqueue_object_deletion, process_object_deletions
from .models import FaceSearchImage, ParticipantFaceImage, Photo
from .storage import get_object_storage_client
from .config import settings


def list_r2_keys(prefix: str = "events/") -> set[str]:
    client = get_object_storage_client()
    paginator = client.get_paginator("list_objects_v2")
    keys: set[str] = set()
    for page in paginator.paginate(Bucket=settings.r2_bucket_name, Prefix=prefix, PaginationConfig={"PageSize": 500}):
        keys.update(item["Key"] for item in page.get("Contents", []) if item.get("Key"))
    return keys


def is_sensitive_temporary_key(key: str) -> bool:
    return "/face-search/" in key or ("/participants/" in key and "/faces/" in key)


def reconcile(*, apply: bool) -> tuple[int, int]:
    r2_keys = list_r2_keys()
    with SessionLocal() as session:
        referenced = set(session.execute(select(Photo.original_object_key)).scalars())
        referenced.update(key for key in session.execute(select(Photo.thumbnail_object_key)).scalars() if key)
        referenced.update(session.execute(select(FaceSearchImage.object_key)).scalars())
        referenced.update(session.execute(select(ParticipantFaceImage.object_key)).scalars())

        missing = referenced - r2_keys
        sensitive_orphans = {key for key in r2_keys - referenced if is_sensitive_temporary_key(key)}
        if apply:
            for object_key in sensitive_orphans:
                enqueue_object_deletion(session, object_key)
            session.commit()
            process_object_deletions(session, limit=len(sensitive_orphans) or 1)
    return len(missing), len(sensitive_orphans)


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile RaceFrame database references with R2 objects.")
    parser.add_argument("--apply", action="store_true", help="Queue and process deletion of sensitive orphan objects.")
    args = parser.parse_args()
    missing, sensitive_orphans = reconcile(apply=args.apply)
    print(f"missing_referenced_objects={missing} sensitive_orphans={sensitive_orphans} apply={args.apply}")
    if missing:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
