from __future__ import annotations

import struct
import zlib
from datetime import datetime, timezone
from pathlib import PurePath
from typing import Iterable, Iterator

from .storage import get_object_body


ZIP32_MAX = 0xFFFFFFFF
ZIP_CHUNK_SIZE = 256 * 1024


def safe_download_name(file_name: str, *, fallback: str = "photo.jpg") -> str:
    normalized = file_name.replace("\\", "/")
    name = PurePath(normalized).name
    name = "".join(character for character in name if ord(character) >= 32 and character not in {'"', ':'})
    return name[:200].strip(" .") or fallback


def unique_download_names(file_names: Iterable[str]) -> list[str]:
    names: list[str] = []
    used: set[str] = set()
    for index, raw_name in enumerate(file_names, start=1):
        name = safe_download_name(raw_name, fallback=f"photo-{index}.jpg")
        candidate = name
        suffix = 2
        stem, dot, extension = name.rpartition(".")
        while candidate.casefold() in used:
            candidate = f"{stem or name}-{suffix}{dot}{extension}" if dot else f"{name}-{suffix}"
            suffix += 1
        used.add(candidate.casefold())
        names.append(candidate)
    return names


def _dos_timestamp() -> tuple[int, int]:
    now = datetime.now(timezone.utc)
    year = max(1980, min(now.year, 2107))
    dos_time = (now.hour << 11) | (now.minute << 5) | (now.second // 2)
    dos_date = ((year - 1980) << 9) | (now.month << 5) | now.day
    return dos_time, dos_date


def stream_photo_zip(entries: list[tuple[str, str]]) -> Iterator[bytes]:
    """Stream a ZIP archive without buffering full-resolution photos in app memory."""
    names = unique_download_names(file_name for _object_key, file_name in entries)
    central_records: list[tuple[bytes, int, int, int, int, int]] = []
    offset = 0
    flags = 0x0008 | 0x0800  # Data descriptor + UTF-8 names.

    for (object_key, _file_name), safe_name in zip(entries, names, strict=True):
        encoded_name = safe_name.encode("utf-8")
        dos_time, dos_date = _dos_timestamp()
        local_offset = offset
        local_header = struct.pack(
            "<IHHHHHIIIHH",
            0x04034B50,
            20,
            flags,
            0,
            dos_time,
            dos_date,
            0,
            0,
            0,
            len(encoded_name),
            0,
        ) + encoded_name
        yield local_header
        offset += len(local_header)

        body, _content_length, _content_type = get_object_body(object_key=object_key)
        crc = 0
        file_size = 0
        try:
            while chunk := body.read(ZIP_CHUNK_SIZE):
                crc = zlib.crc32(chunk, crc)
                file_size += len(chunk)
                if file_size > ZIP32_MAX:
                    raise RuntimeError("A photo is too large for this archive format.")
                yield chunk
                offset += len(chunk)
        finally:
            body.close()

        descriptor = struct.pack("<IIII", 0x08074B50, crc & ZIP32_MAX, file_size, file_size)
        yield descriptor
        offset += len(descriptor)
        if offset > ZIP32_MAX:
            raise RuntimeError("The requested archive is too large.")
        central_records.append((encoded_name, crc & ZIP32_MAX, file_size, local_offset, dos_time, dos_date))

    central_offset = offset
    for encoded_name, crc, file_size, local_offset, dos_time, dos_date in central_records:
        central_header = struct.pack(
            "<IHHHHHHIIIHHHHHII",
            0x02014B50,
            20,
            20,
            flags,
            0,
            dos_time,
            dos_date,
            crc,
            file_size,
            file_size,
            len(encoded_name),
            0,
            0,
            0,
            0,
            0,
            local_offset,
        ) + encoded_name
        yield central_header
        offset += len(central_header)

    central_size = offset - central_offset
    yield struct.pack(
        "<IHHHHIIH",
        0x06054B50,
        0,
        0,
        len(central_records),
        len(central_records),
        central_size,
        central_offset,
        0,
    )
