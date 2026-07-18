from __future__ import annotations

import hashlib
import io
import warnings
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, UploadFile, status
from PIL import Image, UnidentifiedImageError

from .config import settings


ALLOWED_IMAGE_FORMATS = {
    "JPEG": ("image/jpeg", {".jpg", ".jpeg"}),
    "PNG": ("image/png", {".png"}),
    "WEBP": ("image/webp", {".webp"}),
}


@dataclass(frozen=True)
class ValidatedImage:
    content_type: str
    width: int
    height: int
    sha256: str


async def read_upload_limited(upload: UploadFile, limit: int) -> bytes:
    """Read at most ``limit + 1`` bytes from Starlette's spooled upload.

    A single bounded read avoids the second, full-size allocation caused by
    accumulating a list of chunks and joining it after the limit check.
    """
    if limit <= 0:
        raise RuntimeError("Upload limit must be positive.")

    declared_length = upload.headers.get("content-length")
    if declared_length:
        try:
            if int(declared_length) > limit:
                raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Upload is too large.")
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid Content-Length header.") from None

    content = await upload.read(limit + 1)
    if len(content) > limit:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Upload is too large.")
    if not content:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Uploaded file is empty.")
    return content


def validate_image_bytes(
    content: bytes,
    *,
    file_name: str,
    declared_content_type: str | None,
    max_bytes: int,
) -> ValidatedImage:
    if not content:
        raise ValueError("Uploaded image is empty.")
    if len(content) > max_bytes:
        raise ValueError(f"Image exceeds the {max_bytes // (1024 * 1024)} MB upload limit.")
    if len(file_name) > 255:
        raise ValueError("Image file name is too long.")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                image_format = str(image.format or "").upper()
                width, height = image.size
                frame_count = int(getattr(image, "n_frames", 1) or 1)
                image.verify()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError("Image dimensions are too large.") from None
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        raise ValueError("The uploaded file is not a valid supported image.") from None

    allowed = ALLOWED_IMAGE_FORMATS.get(image_format)
    if allowed is None:
        raise ValueError("Upload JPG, PNG, or WEBP images only.")
    actual_content_type, extensions = allowed
    extension = Path(file_name).suffix.lower()
    if extension not in extensions:
        raise ValueError("The file extension does not match the image contents.")

    normalized_declared_type = (declared_content_type or "").split(";", 1)[0].strip().lower()
    if normalized_declared_type and normalized_declared_type != actual_content_type:
        raise ValueError("The declared image type does not match the image contents.")
    if width <= 0 or height <= 0:
        raise ValueError("Image dimensions are invalid.")
    if width > settings.max_image_dimension or height > settings.max_image_dimension:
        raise ValueError("Image width or height exceeds the configured limit.")
    if width * height > settings.max_image_pixels:
        raise ValueError("Image pixel count exceeds the configured limit.")
    if frame_count != 1:
        raise ValueError("Animated or multi-frame images are not supported.")

    return ValidatedImage(
        content_type=actual_content_type,
        width=width,
        height=height,
        sha256=hashlib.sha256(content).hexdigest(),
    )
