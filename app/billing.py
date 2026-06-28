from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .config import settings
from .models import Project, Tier, User


def used_this_month(db: Session, user: User) -> int:
    now = datetime.now(timezone.utc)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    return (
        db.query(Project)
        .filter(Project.owner_id == user.id, Project.created_at >= month_start)
        .count()
    )


def quota(db: Session, user: User) -> dict:
    used = used_this_month(db, user)
    if user.tier == Tier.pro:
        return {
            "tier": user.tier.value,
            "used_this_month": used,
            "limit": None,
            "remaining": None,
        }
    limit = settings.free_tier_monthly_limit
    return {
        "tier": user.tier.value,
        "used_this_month": used,
        "limit": limit,
        "remaining": max(0, limit - used),
    }


def can_create(db: Session, user: User) -> bool:
    if user.tier == Tier.pro:
        return True
    return used_this_month(db, user) < settings.free_tier_monthly_limit
