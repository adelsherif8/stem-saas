"""Authentication (who is calling) and authorization (may they do this here).

Two kinds of principal:
  * a signed-in user, via a JWT (`Authorization: Bearer <jwt>`), acting in a
    workspace chosen with the `X-Workspace-ID` header (default: their personal
    workspace) and limited by their role there;
  * an API key, via `Authorization: Bearer sk_...` or `X-API-Key: sk_...`,
    bound to exactly one workspace and limited by its scopes.
"""
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, Header, HTTPException
from fastapi.security import APIKeyHeader, OAuth2PasswordBearer
from sqlalchemy.orm import Session

from .api_keys import hash_key, looks_like_api_key
from .database import get_db, scope_session_to_workspace
from .models import ApiKey, Membership, Role, User, Workspace, utcnow
from .permissions import ACTIONS, role_allows, scopes_allow
from .security import decode_token

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login", auto_error=False)
api_key_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)


@dataclass
class Principal:
    user: Optional[User] = None
    api_key: Optional[ApiKey] = None

    @property
    def actor_user_id(self) -> Optional[int]:
        return self.user.id if self.user else None

    @property
    def actor_api_key_id(self) -> Optional[int]:
        return self.api_key.id if self.api_key else None


@dataclass
class WorkspaceContext:
    workspace: Workspace
    principal: Principal
    role: Optional[Role]  # None when the caller is an API key

    @property
    def acting_user_id(self) -> int:
        """User to attribute created rows to (API keys act as their creator)."""
        if self.principal.user:
            return self.principal.user.id
        return self.principal.api_key.created_by_id

    def audit_actor(self) -> dict:
        return {
            "actor_user_id": self.principal.actor_user_id,
            "actor_api_key_id": self.principal.actor_api_key_id,
        }


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=401, detail=detail, headers={"WWW-Authenticate": "Bearer"}
    )


def get_principal(
    token: Optional[str] = Depends(oauth2_scheme),
    x_api_key: Optional[str] = Depends(api_key_scheme),
    db: Session = Depends(get_db),
) -> Principal:
    raw_key = x_api_key or (token if token and looks_like_api_key(token) else None)
    if raw_key:
        key = db.query(ApiKey).filter(ApiKey.key_hash == hash_key(raw_key)).first()
        if key is None or key.revoked_at is not None:
            raise _unauthorized("Invalid or revoked API key")
        key.last_used_at = utcnow()
        db.commit()
        return Principal(api_key=key)

    if not token:
        raise _unauthorized("Not authenticated")
    sub = decode_token(token)
    if not sub:
        raise _unauthorized("Invalid or expired token")
    user = db.query(User).filter(User.email == sub).first()
    if not user:
        raise _unauthorized("User not found")
    return Principal(user=user)


def get_current_user(principal: Principal = Depends(get_principal)) -> User:
    """For account-level endpoints that only a human session may call."""
    if principal.user is None:
        raise HTTPException(
            status_code=403,
            detail="This endpoint requires a signed-in user; API keys are not accepted",
        )
    return principal.user


def authorize(
    db: Session, principal: Principal, workspace_id: Optional[int], action: str
) -> WorkspaceContext:
    spec = ACTIONS[action]
    not_found = HTTPException(status_code=404, detail="Workspace not found")

    if principal.api_key is not None:
        key = principal.api_key
        if workspace_id is not None and workspace_id != key.workspace_id:
            raise not_found
        if spec.scope is None:
            raise HTTPException(
                status_code=403, detail=f"API keys cannot perform '{action}'"
            )
        if not scopes_allow(key.scope_list, action):
            raise HTTPException(
                status_code=403, detail=f"API key is missing the '{spec.scope}' scope"
            )
        ctx = WorkspaceContext(workspace=key.workspace, principal=principal, role=None)
    else:
        user = principal.user
        if workspace_id is None:
            ws = user.personal_workspace
            if ws is None:
                raise not_found
            workspace_id = ws.id
        membership = (
            db.query(Membership)
            .filter(Membership.workspace_id == workspace_id, Membership.user_id == user.id)
            .first()
        )
        # Non-members get 404, not 403: don't confirm the workspace exists.
        if membership is None:
            raise not_found
        role = membership.role_enum
        if not role_allows(role, action):
            raise HTTPException(
                status_code=403,
                detail=f"'{action}' requires role '{spec.min_role.value}' or higher "
                f"(you are '{role.value}')",
            )
        ctx = WorkspaceContext(workspace=membership.workspace, principal=principal, role=role)

    scope_session_to_workspace(db, ctx.workspace.id)
    return ctx


def require(action: str):
    """Authorize `action` in the caller's active workspace (X-Workspace-ID)."""

    def dependency(
        x_workspace_id: Optional[int] = Header(
            None, description="Workspace to act in (default: your personal workspace)"
        ),
        principal: Principal = Depends(get_principal),
        db: Session = Depends(get_db),
    ) -> WorkspaceContext:
        return authorize(db, principal, x_workspace_id, action)

    return dependency


def require_workspace(action: str):
    """Authorize `action` in the workspace named by the `{workspace_id}` path param."""

    def dependency(
        workspace_id: int,
        principal: Principal = Depends(get_principal),
        db: Session = Depends(get_db),
    ) -> WorkspaceContext:
        return authorize(db, principal, workspace_id, action)

    return dependency
