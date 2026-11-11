import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session

from .. import audit
from ..api_keys import generate_key
from ..database import get_db
from ..deps import WorkspaceContext, get_current_user, require_workspace
from ..models import ApiKey, AuditLog, Membership, Role, User, Workspace, utcnow
from ..permissions import SCOPES
from ..schemas import (
    ApiKeyCreate,
    ApiKeyOut,
    ApiKeyWithSecret,
    AuditLogOut,
    MemberAdd,
    MemberOut,
    MemberRoleUpdate,
    OwnershipTransfer,
    WorkspaceCreate,
    WorkspaceOut,
    WorkspaceUpdate,
)

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


def _ws_out(ws: Workspace, role: str) -> WorkspaceOut:
    return WorkspaceOut(
        id=ws.id, name=ws.name, personal=ws.personal, tier=ws.tier.value,
        role=role, created_at=ws.created_at,
    )


def _member_out(m: Membership) -> MemberOut:
    return MemberOut(user_id=m.user_id, email=m.user.email, role=m.role, created_at=m.created_at)


def _key_out(key: ApiKey, secret: Optional[str] = None):
    data = dict(
        id=key.id, workspace_id=key.workspace_id, name=key.name, prefix=key.prefix,
        scopes=key.scope_list, created_by_id=key.created_by_id, created_at=key.created_at,
        last_used_at=key.last_used_at, rotated_at=key.rotated_at, revoked_at=key.revoked_at,
    )
    return ApiKeyWithSecret(secret=secret, **data) if secret else ApiKeyOut(**data)


def _get_membership(db: Session, workspace_id: int, user_id: int) -> Membership:
    m = (
        db.query(Membership)
        .filter(Membership.workspace_id == workspace_id, Membership.user_id == user_id)
        .first()
    )
    if m is None:
        raise HTTPException(status_code=404, detail="Member not found")
    return m


# --- workspaces ---------------------------------------------------------------

@router.get("", response_model=list[WorkspaceOut])
def list_my_workspaces(user: User = Depends(get_current_user)):
    return [
        _ws_out(m.workspace, m.role)
        for m in sorted(user.memberships, key=lambda m: (not m.workspace.personal, m.workspace_id))
    ]


@router.post("", response_model=WorkspaceOut, status_code=201)
def create_workspace(
    payload: WorkspaceCreate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ws = Workspace(name=payload.name, personal=False)
    db.add(ws)
    db.flush()
    db.add(Membership(workspace_id=ws.id, user_id=user.id, role=Role.owner.value))
    audit.record(db, "workspace.create", workspace_id=ws.id, actor_user_id=user.id,
                 target_type="workspace", target_id=ws.id, details={"name": ws.name},
                 request=request)
    db.commit()
    db.refresh(ws)
    return _ws_out(ws, Role.owner.value)


@router.get("/{workspace_id}", response_model=WorkspaceOut)
def get_workspace(ctx: WorkspaceContext = Depends(require_workspace("workspace:read"))):
    return _ws_out(ctx.workspace, ctx.role.value)


@router.patch("/{workspace_id}", response_model=WorkspaceOut)
def rename_workspace(
    payload: WorkspaceUpdate,
    request: Request,
    db: Session = Depends(get_db),
    ctx: WorkspaceContext = Depends(require_workspace("workspace:update")),
):
    old = ctx.workspace.name
    ctx.workspace.name = payload.name
    audit.record(db, "workspace.rename", workspace_id=ctx.workspace.id,
                 target_type="workspace", target_id=ctx.workspace.id,
                 details={"from": old, "to": payload.name}, request=request, **ctx.audit_actor())
    db.commit()
    return _ws_out(ctx.workspace, ctx.role.value)


@router.delete("/{workspace_id}", status_code=204)
def delete_workspace(
    request: Request,
    db: Session = Depends(get_db),
    ctx: WorkspaceContext = Depends(require_workspace("workspace:delete")),
):
    ws = ctx.workspace
    if ws.personal:
        raise HTTPException(status_code=400, detail="A personal workspace cannot be deleted")
    # Recorded in the same transaction; audit_logs.workspace_id has no FK, so
    # this row outlives the workspace.
    audit.record(db, "workspace.delete", workspace_id=ws.id, target_type="workspace",
                 target_id=ws.id, details={"name": ws.name}, request=request,
                 **ctx.audit_actor())
    db.delete(ws)
    db.commit()
    return Response(status_code=204)


@router.post("/{workspace_id}/transfer", response_model=WorkspaceOut)
def transfer_ownership(
    payload: OwnershipTransfer,
    request: Request,
    db: Session = Depends(get_db),
    ctx: WorkspaceContext = Depends(require_workspace("workspace:transfer")),
):
    ws = ctx.workspace
    if ws.personal:
        raise HTTPException(status_code=400, detail="A personal workspace cannot be transferred")
    me = ctx.principal.user
    if payload.user_id == me.id:
        raise HTTPException(status_code=400, detail="You already own this workspace")
    target = _get_membership(db, ws.id, payload.user_id)
    current = _get_membership(db, ws.id, me.id)
    # Demote first and flush, so the one-owner-per-workspace index never sees two.
    current.role = Role.admin.value
    db.flush()
    target.role = Role.owner.value
    audit.record(db, "workspace.transfer", workspace_id=ws.id, target_type="user",
                 target_id=target.user_id, details={"from_user_id": me.id},
                 request=request, **ctx.audit_actor())
    db.commit()
    return _ws_out(ws, Role.admin.value)


# --- members ------------------------------------------------------------------

@router.get("/{workspace_id}/members", response_model=list[MemberOut])
def list_members(ctx: WorkspaceContext = Depends(require_workspace("workspace:read"))):
    return [_member_out(m) for m in sorted(ctx.workspace.memberships, key=lambda m: m.id)]


@router.post("/{workspace_id}/members", response_model=MemberOut, status_code=201)
def add_member(
    payload: MemberAdd,
    request: Request,
    db: Session = Depends(get_db),
    ctx: WorkspaceContext = Depends(require_workspace("members:manage")),
):
    ws = ctx.workspace
    if ws.personal:
        raise HTTPException(status_code=400, detail="Personal workspaces cannot have members; create a team workspace")
    user = db.query(User).filter(User.email == payload.email).first()
    if user is None:
        # No email invites in this demo: the person must have an account.
        raise HTTPException(status_code=404, detail="No user with that email; ask them to sign up first")
    exists = (
        db.query(Membership)
        .filter(Membership.workspace_id == ws.id, Membership.user_id == user.id)
        .first()
    )
    if exists:
        raise HTTPException(status_code=409, detail="User is already a member")
    m = Membership(workspace_id=ws.id, user_id=user.id, role=payload.role)
    db.add(m)
    audit.record(db, "member.add", workspace_id=ws.id, target_type="user", target_id=user.id,
                 details={"email": user.email, "role": payload.role}, request=request,
                 **ctx.audit_actor())
    db.commit()
    db.refresh(m)
    return _member_out(m)


@router.patch("/{workspace_id}/members/{user_id}", response_model=MemberOut)
def change_member_role(
    user_id: int,
    payload: MemberRoleUpdate,
    request: Request,
    db: Session = Depends(get_db),
    ctx: WorkspaceContext = Depends(require_workspace("members:manage")),
):
    m = _get_membership(db, ctx.workspace.id, user_id)
    if m.role_enum == Role.owner:
        raise HTTPException(status_code=403, detail="The owner's role can only change via ownership transfer")
    old = m.role
    m.role = payload.role
    audit.record(db, "member.role_change", workspace_id=ctx.workspace.id, target_type="user",
                 target_id=user_id, details={"from": old, "to": payload.role},
                 request=request, **ctx.audit_actor())
    db.commit()
    db.refresh(m)
    return _member_out(m)


@router.delete("/{workspace_id}/members/{user_id}", status_code=204)
def remove_member(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    ctx: WorkspaceContext = Depends(require_workspace("members:manage")),
):
    m = _get_membership(db, ctx.workspace.id, user_id)
    if m.role_enum == Role.owner:
        raise HTTPException(status_code=403, detail="The owner cannot be removed; transfer ownership first")
    audit.record(db, "member.remove", workspace_id=ctx.workspace.id, target_type="user",
                 target_id=user_id, details={"email": m.user.email, "role": m.role},
                 request=request, **ctx.audit_actor())
    db.delete(m)
    db.commit()
    return Response(status_code=204)


# --- API keys -----------------------------------------------------------------

@router.get("/{workspace_id}/api-keys", response_model=list[ApiKeyOut])
def list_api_keys(
    db: Session = Depends(get_db),
    ctx: WorkspaceContext = Depends(require_workspace("keys:manage")),
):
    keys = (
        db.query(ApiKey)
        .filter(ApiKey.workspace_id == ctx.workspace.id)
        .order_by(ApiKey.id)
        .all()
    )
    return [_key_out(k) for k in keys]


@router.post("/{workspace_id}/api-keys", response_model=ApiKeyWithSecret, status_code=201)
def create_api_key(
    payload: ApiKeyCreate,
    request: Request,
    db: Session = Depends(get_db),
    ctx: WorkspaceContext = Depends(require_workspace("keys:manage")),
):
    unknown = sorted(set(payload.scopes) - set(SCOPES))
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown scope(s): {', '.join(unknown)}. Valid: {', '.join(SCOPES)}",
        )
    secret, prefix, digest = generate_key()
    key = ApiKey(
        workspace_id=ctx.workspace.id,
        name=payload.name,
        prefix=prefix,
        key_hash=digest,
        scopes=" ".join(sorted(set(payload.scopes))),
        created_by_id=ctx.principal.user.id,
    )
    db.add(key)
    db.flush()
    audit.record(db, "key.create", workspace_id=ctx.workspace.id, target_type="api_key",
                 target_id=key.id, details={"name": key.name, "scopes": key.scope_list},
                 request=request, **ctx.audit_actor())
    db.commit()
    db.refresh(key)
    return _key_out(key, secret)


def _get_key(db: Session, ctx: WorkspaceContext, key_id: int) -> ApiKey:
    key = db.get(ApiKey, key_id)
    if key is None or key.workspace_id != ctx.workspace.id:
        raise HTTPException(status_code=404, detail="API key not found")
    return key


@router.delete("/{workspace_id}/api-keys/{key_id}", response_model=ApiKeyOut)
def revoke_api_key(
    key_id: int,
    request: Request,
    db: Session = Depends(get_db),
    ctx: WorkspaceContext = Depends(require_workspace("keys:manage")),
):
    key = _get_key(db, ctx, key_id)
    if key.revoked_at is None:
        key.revoked_at = utcnow()
        audit.record(db, "key.revoke", workspace_id=ctx.workspace.id, target_type="api_key",
                     target_id=key.id, details={"name": key.name}, request=request,
                     **ctx.audit_actor())
        db.commit()
        db.refresh(key)
    return _key_out(key)


@router.post("/{workspace_id}/api-keys/{key_id}/rotate", response_model=ApiKeyWithSecret)
def rotate_api_key(
    key_id: int,
    request: Request,
    db: Session = Depends(get_db),
    ctx: WorkspaceContext = Depends(require_workspace("keys:manage")),
):
    """Issue a new secret for the same key (same id, name, scopes). The old
    secret stops working immediately."""
    key = _get_key(db, ctx, key_id)
    if key.revoked_at is not None:
        raise HTTPException(status_code=409, detail="Revoked keys cannot be rotated")
    secret, prefix, digest = generate_key()
    old_prefix = key.prefix
    key.prefix, key.key_hash, key.rotated_at = prefix, digest, utcnow()
    audit.record(db, "key.rotate", workspace_id=ctx.workspace.id, target_type="api_key",
                 target_id=key.id, details={"old_prefix": old_prefix, "new_prefix": prefix},
                 request=request, **ctx.audit_actor())
    db.commit()
    db.refresh(key)
    return _key_out(key, secret)


# --- audit log ----------------------------------------------------------------

@router.get("/{workspace_id}/audit", response_model=list[AuditLogOut])
def list_audit_log(
    action: Optional[str] = Query(None, description="Filter by action, e.g. key.create"),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    ctx: WorkspaceContext = Depends(require_workspace("audit:read")),
):
    q = db.query(AuditLog).filter(AuditLog.workspace_id == ctx.workspace.id)
    if action:
        q = q.filter(AuditLog.action == action)
    rows = q.order_by(AuditLog.id.desc()).limit(limit).all()
    return [
        AuditLogOut(
            id=r.id, workspace_id=r.workspace_id, actor_user_id=r.actor_user_id,
            actor_api_key_id=r.actor_api_key_id, action=r.action, target_type=r.target_type,
            target_id=r.target_id, details=json.loads(r.details) if r.details else None,
            ip=r.ip, created_at=r.created_at,
        )
        for r in rows
    ]
