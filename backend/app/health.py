from __future__ import annotations

import logging
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import select
from sqlalchemy.orm import Session

from .storage import object_storage_ready
from .web_security import is_production


logger = logging.getLogger(__name__)
BACKEND_DIR = Path(__file__).resolve().parents[1]


def _schema_is_current(db: Session) -> bool:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    expected_heads = set(ScriptDirectory.from_config(config).get_heads())
    current_heads = set(MigrationContext.configure(db.connection()).get_current_heads())
    return bool(expected_heads) and current_heads == expected_heads


def application_ready(db: Session) -> bool:
    """Check critical dependencies without exposing credentials or topology."""
    try:
        db.execute(select(1))
        if is_production() and not _schema_is_current(db):
            logger.error("Database schema is not at the application migration head")
            return False
        if is_production() and not object_storage_ready():
            logger.error("Object storage readiness probe failed")
            return False
    except Exception:  # noqa: BLE001
        logger.exception("Application readiness check failed")
        return False
    return True
