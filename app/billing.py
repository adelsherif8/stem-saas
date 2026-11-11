"""Quota enforcement. Quotas are per *workspace* (not per user): a team on the
Pro plan shares one unlimited allowance, and a free workspace's 3 songs/month
are shared by its members. A user's personal workspace keeps the old per-user
behaviour for solo accounts."""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .config import settings
from .models import Project, Tier, Workspace


def used_this_month(db: Session, workspace: Workspace) -> int:
    now = datetime.now(timezone.utc)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    return (
        db.query(Project)
        .filter(Project.workspace_id == workspace.id, Project.created_at >= month_start)
        .count()
    )


def quota(db: Session, workspace: Workspace) -> dict:
    used = used_this_month(db, workspace)
    if workspace.tier == Tier.pro:
        return {
            "tier": workspace.tier.value,
            "used_this_month": used,
            "limit": None,
            "remaining": None,
        }
    limit = settings.free_tier_monthly_limit
    return {
        "tier": workspace.tier.value,
        "used_this_month": used,
        "limit": limit,
        "remaining": max(0, limit - used),
    }


def can_create(db: Session, workspace: Workspace) -> bool:
    if workspace.tier == Tier.pro:
        return True
    return used_this_month(db, workspace) < settings.free_tier_monthly_limit
