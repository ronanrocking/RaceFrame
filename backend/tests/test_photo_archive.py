from __future__ import annotations

import io
import zipfile

from app import photo_archive


def test_stream_photo_zip_creates_one_valid_archive(monkeypatch):
    payloads = {
        "one": b"first original photo",
        "two": b"second original photo",
    }

    def fake_get_object_body(*, object_key: str):
        payload = payloads[object_key]
        return io.BytesIO(payload), len(payload), "image/jpeg"

    monkeypatch.setattr(photo_archive, "get_object_body", fake_get_object_body)
    archive = b"".join(
        photo_archive.stream_photo_zip(
            [("one", "race/photo.jpg"), ("two", "photo.jpg")]
        )
    )

    with zipfile.ZipFile(io.BytesIO(archive)) as result:
        assert result.namelist() == ["photo.jpg", "photo-2.jpg"]
        assert result.read("photo.jpg") == payloads["one"]
        assert result.read("photo-2.jpg") == payloads["two"]
