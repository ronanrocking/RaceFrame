from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import photographer
from app.photographer import UserSearchPhotoItem


def test_result_page_signs_only_twenty_preview_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    signed: list[int] = []
    monkeypatch.setattr(photographer, "safe_photo_preview_url", lambda photo: signed.append(photo.id) or f"/{photo.id}")
    items = [
        UserSearchPhotoItem(
            photo=SimpleNamespace(id=index),
            photo_url=None,
            matched_participants=[],
            direct_match_values=[],
            evidence_labels=[],
        )
        for index in range(45)
    ]

    page = photographer.paginate_user_search_photo_items(items, offset=20, limit=20)

    assert [item.photo.id for item in page] == list(range(20, 40))
    assert signed == list(range(20, 40))
    assert all(item.photo_url is None for item in items[:20] + items[40:])
