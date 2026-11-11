"""Cross-workspace isolation at the API level (runs on SQLite and Postgres)."""
from .conftest import upload


def test_users_cannot_see_each_others_data(client, new_user):
    alice, bob = new_user("alice"), new_user("bob")
    job = upload(client, alice.headers).json()["job"]["id"]

    assert client.get("/projects", headers=bob.headers).json() == []
    assert client.get(f"/jobs/{job}", headers=bob.headers).status_code == 404
    assert client.get(f"/jobs/{job}/stems", headers=bob.headers).status_code == 404
    assert client.get(f"/jobs/{job}/stems/vocals", headers=bob.headers).status_code == 404


def test_cannot_select_a_workspace_you_are_not_in(client, new_user):
    alice, bob = new_user("alice"), new_user("bob")
    upload(client, alice.headers)
    h = bob.in_ws(alice.personal_workspace_id)
    assert client.get("/projects", headers=h).status_code == 404
    assert upload(client, h).status_code == 404
    assert client.get(f"/workspaces/{alice.personal_workspace_id}", headers=bob.headers).status_code == 404


def test_job_from_other_workspace_hidden_even_for_shared_member(client, team):
    """A member of both workspaces still only sees a job through the workspace
    it belongs to: the active workspace scopes every lookup."""
    member = team.member
    # The team job is invisible from the member's personal workspace context...
    assert client.get(f"/jobs/{team.job_id}", headers=member.headers).status_code == 404
    # ...and visible from the team context.
    assert client.get(f"/jobs/{team.job_id}", headers=member.in_ws(team.ws_id)).status_code == 200


def test_admin_of_one_workspace_has_no_power_in_another(client, team, new_user):
    stranger = new_user("stranger")
    other_ws = client.post("/workspaces", json={"name": "Other"}, headers=stranger.headers).json()["id"]
    for path in ("members", "api-keys", "audit"):
        assert client.get(f"/workspaces/{other_ws}/{path}", headers=team.admin.headers).status_code == 404
    r = client.post(f"/workspaces/{other_ws}/members", json={"email": team.admin.email},
                    headers=team.admin.headers)
    assert r.status_code == 404


def test_api_keys_of_other_workspace_cannot_be_managed(client, team, new_user):
    stranger = new_user("stranger")
    key_id = team.key_id
    # Addressing team's key through a workspace the stranger does own:
    ws = stranger.personal_workspace_id
    assert client.delete(f"/workspaces/{ws}/api-keys/{key_id}", headers=stranger.headers).status_code == 404
    assert client.post(f"/workspaces/{ws}/api-keys/{key_id}/rotate", headers=stranger.headers).status_code == 404


def test_audit_log_only_contains_own_workspace(client, team, new_user):
    stranger = new_user("stranger")
    client.post("/workspaces", json={"name": "Elsewhere"}, headers=stranger.headers)
    entries = client.get(f"/workspaces/{team.ws_id}/audit", headers=team.owner.headers).json()
    assert entries and all(e["workspace_id"] == team.ws_id for e in entries)
    assert {"workspace.create", "member.add", "key.create"} <= {e["action"] for e in entries}


def test_quota_is_per_workspace(client, new_user):
    u = new_user("quota")
    team_ws = client.post("/workspaces", json={"name": "Q"}, headers=u.headers).json()["id"]
    for _ in range(3):
        assert upload(client, u.headers).status_code == 201
    assert upload(client, u.headers).status_code == 402  # personal workspace exhausted
    # A different workspace has its own allowance.
    assert upload(client, u.in_ws(team_ws)).status_code == 201
    q = client.get("/projects/quota", headers=u.in_ws(team_ws)).json()
    assert q["used_this_month"] == 1 and q["remaining"] == 2


def test_quota_is_shared_by_members(client, team):
    # Team fixture already used 1 of 3; member and admin share the rest.
    assert upload(client, team.member.in_ws(team.ws_id)).status_code == 201
    assert upload(client, team.admin.in_ws(team.ws_id)).status_code == 201
    assert upload(client, team.member.in_ws(team.ws_id)).status_code == 402
    # Only the owner can upgrade; then everyone can upload again.
    assert client.post("/billing/mock-pay?session_id=x", headers=team.admin.in_ws(team.ws_id)).status_code == 403
    assert client.post("/billing/mock-pay?session_id=x", headers=team.owner.in_ws(team.ws_id)).status_code == 200
    assert upload(client, team.member.in_ws(team.ws_id)).status_code == 201


def test_member_events_are_audited(client, team):
    client.patch(f"/workspaces/{team.ws_id}/members/{team.target.id}", json={"role": "admin"},
                 headers=team.owner.headers)
    client.delete(f"/workspaces/{team.ws_id}/members/{team.target.id}", headers=team.admin.headers)
    entries = client.get(f"/workspaces/{team.ws_id}/audit", headers=team.owner.headers).json()
    by_action = {e["action"]: e for e in entries}
    assert by_action["member.role_change"]["details"] == {"from": "member", "to": "admin"}
    assert by_action["member.remove"]["actor_user_id"] == team.admin.id
    assert by_action["member.remove"]["target_id"] == str(team.target.id)


def test_workspace_delete_audit_survives_deletion(client, team):
    from app.database import SessionLocal
    from app.models import AuditLog

    client.delete(f"/workspaces/{team.ws_id}", headers=team.owner.headers)
    db = SessionLocal()
    try:
        row = db.query(AuditLog).filter(AuditLog.workspace_id == team.ws_id,
                                        AuditLog.action == "workspace.delete").one()
        assert row.actor_user_id == team.owner.id
    finally:
        db.close()


def test_password_login_is_audited(client):
    client.post("/auth/register", json={"email": "audit@x.com", "password": "pw123456"})
    client.post("/auth/login", data={"username": "audit@x.com", "password": "wrong"})
    token = client.post("/auth/login", data={"username": "audit@x.com", "password": "pw123456"}).json()["access_token"]
    h = {"Authorization": f"Bearer {token}"}
    ws = client.get("/workspaces", headers=h).json()[0]["id"]
    actions = [e["action"] for e in client.get(f"/workspaces/{ws}/audit", headers=h).json()]
    assert actions[:2] == ["auth.login", "auth.login_failed"]


def test_app_sessions_are_pinned_by_rls_on_postgres(client, new_user):
    """When the suite runs on Postgres (TEST_DATABASE_URL), check the app's own
    session wiring: after scope_session_to_workspace() an *unfiltered* ORM query
    only returns that workspace's rows, and the pin survives a commit."""
    import pytest
    from sqlalchemy import text

    from app.database import RLS_ROLE, SessionLocal, engine, rls_available, scope_session_to_workspace
    from app.models import Project

    if engine.dialect.name != "postgresql":
        pytest.skip("RLS is Postgres-only; run with TEST_DATABASE_URL=postgresql://...")
    alice, bob = new_user("alice"), new_user("bob")
    upload(client, alice.headers)
    upload(client, bob.headers)

    db = SessionLocal()
    try:
        if not rls_available(db):
            pytest.skip("this DB user may not assume the RLS role (migration 0003 logged a NOTICE)")
        scope_session_to_workspace(db, alice.personal_workspace_id)
        assert {p.workspace_id for p in db.query(Project).all()} == {alice.personal_workspace_id}
        db.commit()  # new transaction: the after_begin hook must re-apply the pin
        assert db.execute(text("SELECT current_user")).scalar() == RLS_ROLE
        assert {p.workspace_id for p in db.query(Project).all()} == {alice.personal_workspace_id}
    finally:
        db.close()
