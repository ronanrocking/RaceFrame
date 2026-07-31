from __future__ import annotations

import io
import uuid
from dataclasses import dataclass

from PIL import Image, ImageOps
from sqlalchemy.orm import Session

from .config import settings
from .maintenance import enqueue_object_deletion, process_object_deletions
from .models import Event
from .storage import delete_object, put_object
from .uploads import validate_image_bytes


THUMBNAIL_SIZE = (1200, 800)


@dataclass(frozen=True)
class PreparedEventThumbnail:
    content: bytes


def prepare_event_thumbnail(*, content: bytes, file_name: str, content_type: str | None) -> PreparedEventThumbnail:
    validate_image_bytes(
        content,
        file_name=file_name,
        declared_content_type=content_type,
        max_bytes=settings.max_event_thumbnail_upload_bytes,
    )

    with Image.open(io.BytesIO(content)) as source:
        source = ImageOps.exif_transpose(source)
        image = ImageOps.fit(source, THUMBNAIL_SIZE, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
        if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
            alpha_source = image.convert("RGBA")
            background = Image.new("RGB", alpha_source.size, (248, 247, 242))
            background.paste(alpha_source, mask=alpha_source.getchannel("A"))
            image = background
        else:
            image = image.convert("RGB")

        output = io.BytesIO()
        image.save(output, format="JPEG", quality=84, optimize=True, progressive=True)
        return PreparedEventThumbnail(content=output.getvalue())


def save_event_thumbnail(session: Session, *, event: Event, prepared: PreparedEventThumbnail) -> None:
    new_key = f"events/{event.id}/event-thumbnails/{uuid.uuid4()}.jpg"
    previous_key = event.thumbnail_object_key
    put_object(
        object_key=new_key,
        content=prepared.content,
        content_type="image/jpeg",
        cache_control="private, max-age=86400",
    )
    try:
        event.thumbnail_object_key = new_key
        if previous_key:
            enqueue_object_deletion(session, previous_key)
        session.commit()
        session.refresh(event)
    except Exception:
        session.rollback()
        try:
            delete_object(object_key=new_key)
        except Exception:
            pass
        raise
    if previous_key:
        process_object_deletions(session, limit=1)


def remove_event_thumbnail(session: Session, *, event: Event) -> bool:
    previous_key = event.thumbnail_object_key
    if not previous_key:
        return False
    event.thumbnail_object_key = None
    enqueue_object_deletion(session, previous_key)
    session.commit()
    session.refresh(event)
    process_object_deletions(session, limit=1)
    return True
