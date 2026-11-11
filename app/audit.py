import json
from typing import Any, Optional

from fastapi import Request
from sqlalchemy.orm import Session

from .models import AuditLog


def client_ip(request: Optional[Request]) -> Optional[str]:
    if request is None:
        return None
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    return request.client.host if request.client else None


def record(
    db: Session,
    action: str,
    *,
    workspace_id: Optional[int] = None,
    actor_user_id: Optional[int] = None,
    actor_api_key_id: Optional[int] = None,
    target_type: Optional[str] = None,
    target_id: Any = None,
    details: Optional[dict] = None,
    request: Optional[Request] = None,
) -> AuditLog:
    """Add an audit row to the session. The caller's commit persists it, so the
    audit entry and the change it describes succeed or fail together."""
    entry = AuditLog(
        workspace_id=workspace_id,
        actor_user_id=actor_user_id,
        actor_api_key_id=actor_api_key_id,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        details=json.dumps(details, default=str) if details else None,
        ip=client_ip(request),
    )
    db.add(entry)
    return entry
