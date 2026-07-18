from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from .models import AdminAuditLog


logger = logging.getLogger(__name__)


def append_admin_audit(
    session: Session,
    request: Request,
    *,
    action: str,
    target_type: str | None = None,
    target_id: str | uuid.UUID | None = None,
    event_id: str | uuid.UUID | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    safe_metadata = metadata or None
    if safe_metadata is not None:
        encoded = json.dumps(safe_metadata, default=str, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 16_000:
            safe_metadata = {"truncated": True}

    actor_email = request.headers.get("cf-access-authenticated-user-email") or None
    actor_id = actor_email or request.cookies.get("raceframe_admin_session") or None
    parsed_event_id: uuid.UUID | None = None
    if event_id:
        try:
            parsed_event_id = uuid.UUID(str(event_id))
        except ValueError:
            logger.warning("Audit event ID was invalid", extra={"event": {"action": action}})

    session.add(
        AdminAuditLog(
            actor_id=(actor_id or "unknown")[:255],
            actor_email=actor_email[:320] if actor_email else None,
            action=action[:128],
            target_type=target_type[:64] if target_type else None,
            target_id=str(target_id)[:128] if target_id is not None else None,
            event_id=parsed_event_id,
            request_id=str(getattr(request.state, "request_id", ""))[:64] or None,
            metadata_json=safe_metadata,
        )
    )
    session.commit()
