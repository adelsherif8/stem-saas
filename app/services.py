from typing import Optional

from sqlalchemy.orm import Session

from .models import Membership, Role, User, Workspace


def create_user_with_workspace(
    db: Session, email: str, hashed_password: Optional[str]
) -> User:
    """Create a user plus their personal workspace (user = owner). No commit."""
    user = User(email=email, hashed_password=hashed_password)
    workspace = Workspace(name=f"{email.split('@')[0]}'s workspace", personal=True)
    db.add_all([user, workspace])
    db.flush()
    db.add(Membership(workspace_id=workspace.id, user_id=user.id, role=Role.owner.value))
    db.flush()
    db.refresh(user)
    return user
