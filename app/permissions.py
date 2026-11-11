"""Single source of truth for who may do what.

Every protected endpoint names one action from ACTIONS. An action has:
  * the minimum workspace role a *user* needs, and
  * the API-key scope that grants it (None = API keys can never do this;
    workspace administration is reserved for signed-in humans).

The README role matrix and the permission-matrix tests are both derived
from this table.
"""
from dataclasses import dataclass
from typing import Optional

from .models import Role

# Scopes an API key can be granted.
SCOPES = {
    "jobs:read": "List projects, read job status and quota",
    "jobs:write": "Upload songs / start separation jobs",
    "stems:read": "List and download produced stems",
}


@dataclass(frozen=True)
class Action:
    min_role: Role
    scope: Optional[str]
    description: str


ACTIONS: dict[str, Action] = {
    # Data plane: usable by every member, and by API keys with the scope.
    "projects:create": Action(Role.member, "jobs:write", "Upload a song (creates project + job)"),
    "projects:read": Action(Role.member, "jobs:read", "List projects, read quota"),
    "jobs:read": Action(Role.member, "jobs:read", "Read job status/progress"),
    "stems:read": Action(Role.member, "stems:read", "List/download stems"),
    # Workspace administration: humans only.
    "workspace:read": Action(Role.member, None, "View workspace and member list"),
    "workspace:update": Action(Role.admin, None, "Rename workspace"),
    "members:manage": Action(Role.admin, None, "Add/remove members, change roles"),
    "keys:manage": Action(Role.admin, None, "Create/list/revoke/rotate API keys"),
    "audit:read": Action(Role.admin, None, "Read the audit log"),
    "billing:manage": Action(Role.owner, None, "Upgrade/cancel the workspace plan"),
    "workspace:transfer": Action(Role.owner, None, "Transfer ownership"),
    "workspace:delete": Action(Role.owner, None, "Delete the workspace"),
}


def role_allows(role: Role, action: str) -> bool:
    return role.rank >= ACTIONS[action].min_role.rank


def scopes_allow(scopes: list[str], action: str) -> bool:
    needed = ACTIONS[action].scope
    return needed is not None and needed in scopes
