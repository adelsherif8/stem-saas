from datetime import timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import audit
from ..config import settings
from ..database import get_db
from ..logging_conf import get_logger
from ..models import OAuthAccount, OAuthState, User, utcnow
from ..oauth import (
    PROVIDERS,
    OAuthError,
    ProviderConfig,
    authorization_url,
    exchange_code,
    fetch_identity,
    new_code_verifier,
    new_state,
)
from ..security import create_access_token
from ..services import create_user_with_workspace

router = APIRouter(prefix="/auth/oauth", tags=["auth"])
logger = get_logger(__name__)

STATE_TTL = timedelta(minutes=10)
STATE_COOKIE = "oauth_state"


def _provider(name: str) -> ProviderConfig:
    provider = PROVIDERS.get(name)
    if provider is None:
        raise HTTPException(status_code=404, detail=f"Unknown OAuth provider '{name}'")
    if not provider.enabled:
        raise HTTPException(
            status_code=503,
            detail=f"{provider.label} login is not configured on this server "
            f"(set {provider.env_hint}).",
        )
    return provider


@router.get("/providers")
def list_providers():
    """Which social logins are available (used by the web UI to show buttons)."""
    return [
        {"name": p.name, "label": p.label, "enabled": p.enabled,
         "authorize_url": f"/auth/oauth/{p.name}/authorize"}
        for p in PROVIDERS.values()
    ]


@router.get("/{provider}/authorize")
def authorize(
    provider: str,
    redirect: bool = Query(True, description="false = return the URL as JSON instead of a 307"),
    db: Session = Depends(get_db),
):
    p = _provider(provider)
    # Opportunistically clear expired states.
    db.query(OAuthState).filter(OAuthState.created_at < utcnow() - STATE_TTL).delete()
    state, verifier = new_state(), new_code_verifier()
    db.add(OAuthState(state=state, provider=p.name, code_verifier=verifier))
    db.commit()

    url = authorization_url(p, state, verifier)
    response = (
        RedirectResponse(url, status_code=307)
        if redirect
        else JSONResponse({"authorization_url": url, "state": state})
    )
    response.set_cookie(
        STATE_COOKIE, state,
        max_age=int(STATE_TTL.total_seconds()),
        httponly=True,
        samesite="lax",  # sent on the provider's top-level redirect back to us
        secure=settings.oauth_redirect_base_url.startswith("https"),
        path="/auth/oauth",
    )
    return response


def _find_or_create_user(db: Session, provider: str, subject: str, email: Optional[str]) -> tuple[User, bool]:
    account = (
        db.query(OAuthAccount)
        .filter(OAuthAccount.provider == provider, OAuthAccount.subject == subject)
        .first()
    )
    if account is not None:
        return account.user, False
    if not email:
        raise HTTPException(
            status_code=400,
            detail="Your account did not return a verified email address; verify one with the provider and retry.",
        )
    created = False
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        # Social-only account: no password until they set one.
        user = create_user_with_workspace(db, email, hashed_password=None)
        created = True
    db.add(OAuthAccount(user_id=user.id, provider=provider, subject=subject, email=email))
    db.flush()
    return user, created


@router.get("/{provider}/callback")
def callback(
    provider: str,
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    oauth_state: Optional[str] = Cookie(None),
    db: Session = Depends(get_db),
):
    p = _provider(provider)
    if error:
        raise HTTPException(status_code=400, detail=f"{p.label} returned an error: {error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state")

    row = db.get(OAuthState, state)
    if row is None:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")
    row_provider, verifier, created_at = row.provider, row.code_verifier, row.created_at
    # Single use: consume the state whatever happens next.
    db.delete(row)
    db.commit()
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)  # SQLite drops tz
    if row_provider != p.name or created_at < utcnow() - STATE_TTL or oauth_state != state:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")

    try:
        access_token = exchange_code(p, code, verifier)
        identity = fetch_identity(p, access_token)
    except OAuthError as exc:
        logger.warning("oauth_failed", extra={"provider": p.name, "error": str(exc)})
        raise HTTPException(status_code=400, detail=str(exc))

    user, created = _find_or_create_user(db, p.name, identity.subject, identity.email)
    ws = user.personal_workspace
    audit.record(
        db, "auth.login", workspace_id=ws.id if ws else None, actor_user_id=user.id,
        target_type="user", target_id=user.id,
        details={"method": p.name, "new_user": created}, request=request,
    )
    db.commit()

    token = create_access_token(user.email)
    response = RedirectResponse(
        f"{settings.oauth_success_redirect}#access_token={token}&token_type=bearer",
        status_code=303,
    )
    response.delete_cookie(STATE_COOKIE, path="/auth/oauth")
    return response
