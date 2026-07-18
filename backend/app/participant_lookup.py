from __future__ import annotations

import re


def normalize_bib_lookup(value: str) -> str:
    """Canonical, bounded event-local bib key used for indexed equality lookup."""
    return re.sub(r"[^A-Z0-9]+", "", value.upper())[:64]


def normalize_name_lookup(value: str) -> str:
    """Canonical exact-name key; intentionally does not implement fuzzy matching."""
    return " ".join(value.lower().split())[:255]
