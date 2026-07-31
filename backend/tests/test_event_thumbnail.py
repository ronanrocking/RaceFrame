from __future__ import annotations

import io

from PIL import Image

from app.event_thumbnail import THUMBNAIL_SIZE, prepare_event_thumbnail


def test_event_thumbnail_is_cropped_and_saved_at_fixed_aspect_ratio() -> None:
    source = io.BytesIO()
    Image.new("RGB", (1600, 900), (241, 90, 41)).save(source, format="PNG")

    prepared = prepare_event_thumbnail(
        content=source.getvalue(),
        file_name="race.png",
        content_type="image/png",
    )

    with Image.open(io.BytesIO(prepared.content)) as image:
        assert image.format == "JPEG"
        assert image.size == THUMBNAIL_SIZE
