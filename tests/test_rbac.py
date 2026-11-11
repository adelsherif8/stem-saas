"""Permission matrix: every role x every protected action, over real HTTP.

EXPECTED_MIN_ROLE is written out by hand (it is the README role matrix) and is
also checked against app/permissions.py, so the docs, the code and the
behaviour cannot drift apart.
"""
import pytest

from app.models import Role
from app.permissions import ACTIONS

from .conftest import upload

ROLES = ["owner", "admin", "member", "outsider"]
RANK = {"outsider": 0, "member": 1, "admin": 2, "owner": 3}

EXPECTED_MIN_ROLE = {
    "projects:create": "member",
    "projects:read": "member",
    "jobs:read": "member",
    "stems:read": "member",
    "workspace:read": "member",
    "workspace:update": "admin",
    "members:manage": "admin",
    "keys:manage": "admin",
    "audit:read": "admin",
    "billing:manage": "owner",
    "workspace:transfer": "owner",
    "workspace:delete": "owner",
}


def test_matrix_matches_permission_table():
    assert {a: s.min_role.value for a, s in ACTIONS.items()} == EXPECTED_MIN_ROLE


# Each operation: (permission it exercises, callable(client, team, user) -> response)
def _ws(t):
    return f"/workspaces/{t.ws_id}"


OPERATIONS = {
    "upload song": ("projects:create", lambda c, t, u: upload(c, u.in_ws(t.ws_id))),
    "list projects": ("projects:read", lambda c, t, u: c.get("/projects", headers=u.in_ws(t.ws_id))),
    "read quota": ("projects:read", lambda c, t, u: c.get("/projects/quota", headers=u.in_ws(t.ws_id))),
    "read job": ("jobs:read", lambda c, t, u: c.get(f"/jobs/{t.job_id}", headers=u.in_ws(t.ws_id))),
    "list stems": ("stems:read", lambda c, t, u: c.get(f"/jobs/{t.job_id}/stems", headers=u.in_ws(t.ws_id))),
    "download stem": (
        "stems:read",
        lambda c, t, u: c.get(f"/jobs/{t.job_id}/stems/vocals", headers=u.in_ws(t.ws_id)),
    ),
    "view workspace": ("workspace:read", lambda c, t, u: c.get(_ws(t), headers=u.headers)),
    "list members": ("workspace:read", lambda c, t, u: c.get(f"{_ws(t)}/members", headers=u.headers)),
    "rename workspace": (
        "workspace:update",
        lambda c, t, u: c.patch(_ws(t), json={"name": "Renamed"}, headers=u.headers),
    ),
    "add member": (
        "members:manage",
        lambda c, t, u: c.post(
            f"{_ws(t)}/members", json={"email": t.outsider.email}, headers=u.headers
        ),
    ),
    "change member role": (
        "members:manage",
        lambda c, t, u: c.patch(
            f"{_ws(t)}/members/{t.target.id}", json={"role": "admin"}, headers=u.headers
        ),
    ),
    "remove member": (
        "members:manage",
        lambda c, t, u: c.delete(f"{_ws(t)}/members/{t.target.id}", headers=u.headers),
    ),
    "list api keys": ("keys:manage", lambda c, t, u: c.get(f"{_ws(t)}/api-keys", headers=u.headers)),
    "create api key": (
        "keys:manage",
        lambda c, t, u: c.post(
            f"{_ws(t)}/api-keys", json={"name": "k", "scopes": ["jobs:read"]}, headers=u.headers
        ),
    ),
    "revoke api key": (
        "keys:manage",
        lambda c, t, u: c.delete(f"{_ws(t)}/api-keys/{t.key_id}", headers=u.headers),
    ),
    "rotate api key": (
        "keys:manage",
        lambda c, t, u: c.post(f"{_ws(t)}/api-keys/{t.key_id}/rotate", headers=u.headers),
    ),
    "read audit log": ("audit:read", lambda c, t, u: c.get(f"{_ws(t)}/audit", headers=u.headers)),
    "upgrade plan": (
        "billing:manage",
        lambda c, t, u: c.post("/billing/mock-pay?session_id=cs_t", headers=u.in_ws(t.ws_id)),
    ),
    "transfer ownership": (
        "workspace:transfer",
        lambda c, t, u: c.post(f"{_ws(t)}/transfer", json={"user_id": t.target.id}, headers=u.headers),
    ),
    "delete workspace": ("workspace:delete", lambda c, t, u: c.delete(_ws(t), headers=u.headers)),
}


def test_every_action_is_exercised():
    assert {perm for perm, _ in OPERATIONS.values()} == set(ACTIONS)


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("operation", list(OPERATIONS))
def test_permission_matrix(client, team, role, operation):
    perm, call = OPERATIONS[operation]
    resp = call(client, team, team.user(role))
    if role == "outsider":
        assert resp.status_code == 404, resp.text  # existence not revealed
    elif RANK[role] >= RANK[EXPECTED_MIN_ROLE[perm]]:
        assert 200 <= resp.status_code < 300, f"{role} should be allowed: {resp.text}"
    else:
        assert resp.status_code == 403, f"{role} should be denied: {resp.text}"
        assert perm in resp.json()["detail"]


# --- role-change rules beyond the plain matrix --------------------------------

def test_owner_cannot_be_removed_or_demoted(client, team):
    for actor in (team.owner, team.admin):
        r = client.delete(f"/workspaces/{team.ws_id}/members/{team.owner.id}", headers=actor.headers)
        assert r.status_code == 403
        r = client.patch(
            f"/workspaces/{team.ws_id}/members/{team.owner.id}",
            json={"role": "member"},
            headers=actor.headers,
        )
        assert r.status_code == 403


def test_role_cannot_be_set_to_owner_directly(client, team):
    r = client.patch(
        f"/workspaces/{team.ws_id}/members/{team.member.id}",
        json={"role": "owner"},
        headers=team.owner.headers,
    )
    assert r.status_code == 422


def test_transfer_ownership_swaps_roles(client, team):
    r = client.post(
        f"/workspaces/{team.ws_id}/transfer", json={"user_id": team.member.id}, headers=team.owner.headers
    )
    assert r.status_code == 200
    roles = {m["user_id"]: m["role"] for m in client.get(
        f"/workspaces/{team.ws_id}/members", headers=team.owner.headers).json()}
    assert roles[team.member.id] == "owner"
    assert roles[team.owner.id] == "admin"
    # The old owner lost owner-only powers; the new owner has them.
    assert client.delete(f"/workspaces/{team.ws_id}", headers=team.owner.headers).status_code == 403
    assert client.delete(f"/workspaces/{team.ws_id}", headers=team.member.headers).status_code == 204


def test_transfer_requires_existing_member(client, team):
    r = client.post(
        f"/workspaces/{team.ws_id}/transfer", json={"user_id": team.outsider.id}, headers=team.owner.headers
    )
    assert r.status_code == 404


def test_promoted_member_gains_admin_powers(client, team):
    assert client.get(f"/workspaces/{team.ws_id}/audit", headers=team.member.headers).status_code == 403
    client.patch(
        f"/workspaces/{team.ws_id}/members/{team.member.id}", json={"role": "admin"}, headers=team.admin.headers
    )
    assert client.get(f"/workspaces/{team.ws_id}/audit", headers=team.member.headers).status_code == 200


def test_removed_member_loses_access(client, team):
    assert client.get("/projects", headers=team.target.in_ws(team.ws_id)).status_code == 200
    client.delete(f"/workspaces/{team.ws_id}/members/{team.target.id}", headers=team.admin.headers)
    assert client.get("/projects", headers=team.target.in_ws(team.ws_id)).status_code == 404


def test_personal_workspace_rules(client, new_user):
    u = new_user()
    other = new_user()
    ws = f"/workspaces/{u.personal_workspace_id}"
    assert client.delete(ws, headers=u.headers).status_code == 400
    assert client.post(f"{ws}/members", json={"email": other.email}, headers=u.headers).status_code == 400


def test_duplicate_member_rejected(client, team):
    r = client.post(
        f"/workspaces/{team.ws_id}/members", json={"email": team.member.email}, headers=team.owner.headers
    )
    assert r.status_code == 409


def test_add_unknown_user_404(client, team):
    r = client.post(
        f"/workspaces/{team.ws_id}/members", json={"email": "nobody@example.com"}, headers=team.owner.headers
    )
    assert r.status_code == 404


def test_list_my_workspaces_shows_roles(client, team):
    mine = client.get("/workspaces", headers=team.admin.headers).json()
    assert mine[0]["personal"] is True and mine[0]["role"] == "owner"
    assert {"id": team.ws_id, "role": "admin"}.items() <= next(
        w for w in mine if w["id"] == team.ws_id).items()


def test_deleting_workspace_removes_its_data(client, team):
    assert client.delete(f"/workspaces/{team.ws_id}", headers=team.owner.headers).status_code == 204
    assert client.get(f"/workspaces/{team.ws_id}", headers=team.owner.headers).status_code == 404
    assert client.get("/projects", headers=team.member.in_ws(team.ws_id)).status_code == 404
    ids = [w["id"] for w in client.get("/workspaces", headers=team.member.headers).json()]
    assert team.ws_id not in ids


def test_role_enum_ordering():
    assert Role.member.rank < Role.admin.rank < Role.owner.rank
