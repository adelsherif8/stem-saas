import pytest

from app.api_keys import hash_key
from app.database import SessionLocal
from app.models import ApiKey

from .conftest import upload


def _create_key(client, user, ws_id, scopes, name="key"):
    r = client.post(
        f"/workspaces/{ws_id}/api-keys", json={"name": name, "scopes": scopes}, headers=user.headers
    )
    assert r.status_code == 201, r.text
    return r.json()


def _bearer(secret):
    return {"Authorization": f"Bearer {secret}"}


@pytest.fixture
def ws(client, new_user):
    """An owner with a completed job in their personal workspace."""
    owner = new_user("keys")
    job_id = upload(client, owner.headers).json()["job"]["id"]
    return owner, owner.personal_workspace_id, job_id


def test_secret_shown_once_and_only_hash_stored(client, ws):
    owner, ws_id, _ = ws
    key = _create_key(client, owner, ws_id, ["jobs:read"])
    secret = key["secret"]
    assert secret.startswith("sk_") and len(secret) > 40
    assert key["prefix"] == secret[:10]

    listed = client.get(f"/workspaces/{ws_id}/api-keys", headers=owner.headers).json()
    assert all("secret" not in k for k in listed)

    db = SessionLocal()
    try:
        row = db.get(ApiKey, key["id"])
        assert row.key_hash == hash_key(secret)
        assert secret not in (row.key_hash, row.prefix, row.scopes)
    finally:
        db.close()


def test_both_header_styles_authenticate(client, ws):
    owner, ws_id, _ = ws
    secret = _create_key(client, owner, ws_id, ["jobs:read"])["secret"]
    assert client.get("/projects", headers=_bearer(secret)).status_code == 200
    assert client.get("/projects", headers={"X-API-Key": secret}).status_code == 200


# (scope granted, endpoint, expected status)
SCOPE_CASES = [
    ("jobs:read", "list_projects", 200),
    ("jobs:read", "quota", 200),
    ("jobs:read", "job", 200),
    ("jobs:read", "stems", 403),
    ("jobs:read", "download", 403),
    ("jobs:read", "upload", 403),
    ("jobs:write", "upload", 201),
    ("jobs:write", "list_projects", 403),
    ("jobs:write", "job", 403),
    ("jobs:write", "stems", 403),
    ("stems:read", "stems", 200),
    ("stems:read", "download", 200),
    ("stems:read", "job", 403),
    ("stems:read", "upload", 403),
]


def _call(client, endpoint, headers, job_id):
    return {
        "list_projects": lambda: client.get("/projects", headers=headers),
        "quota": lambda: client.get("/projects/quota", headers=headers),
        "job": lambda: client.get(f"/jobs/{job_id}", headers=headers),
        "stems": lambda: client.get(f"/jobs/{job_id}/stems", headers=headers),
        "download": lambda: client.get(f"/jobs/{job_id}/stems/vocals", headers=headers),
        "upload": lambda: upload(client, headers),
    }[endpoint]()


@pytest.mark.parametrize("scope,endpoint,expected", SCOPE_CASES)
def test_scope_enforcement(client, ws, scope, endpoint, expected):
    owner, ws_id, job_id = ws
    secret = _create_key(client, owner, ws_id, [scope])["secret"]
    resp = _call(client, endpoint, _bearer(secret), job_id)
    assert resp.status_code == expected, resp.text
    if expected == 403:
        assert "scope" in resp.json()["detail"]


def test_api_key_upload_is_attributed_to_key_creator(client, ws):
    owner, ws_id, _ = ws
    secret = _create_key(client, owner, ws_id, ["jobs:write", "jobs:read"])["secret"]
    assert upload(client, _bearer(secret)).status_code == 201
    audit = client.get(f"/workspaces/{ws_id}/audit?action=key.create", headers=owner.headers).json()
    assert audit[0]["actor_user_id"] == owner.id


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/auth/me"),
        ("get", "/workspaces"),
        ("get", "/workspaces/{ws}/members"),
        ("get", "/workspaces/{ws}/api-keys"),
        ("post", "/workspaces/{ws}/api-keys"),
        ("get", "/workspaces/{ws}/audit"),
        ("post", "/billing/mock-pay?session_id=x"),
        ("delete", "/workspaces/{ws}"),
    ],
)
def test_api_keys_cannot_administer(client, ws, method, path):
    owner, ws_id, _ = ws
    secret = _create_key(client, owner, ws_id, ["jobs:read", "jobs:write", "stems:read"])["secret"]
    kwargs = {"headers": _bearer(secret)}
    if method == "post" and "api-keys" in path:
        kwargs["json"] = {"name": "escalate", "scopes": ["jobs:read"]}
    resp = getattr(client, method)(path.format(ws=ws_id), **kwargs)
    assert resp.status_code == 403, resp.text


def test_key_is_bound_to_its_workspace(client, ws, new_user):
    owner, ws_id, _ = ws
    secret = _create_key(client, owner, ws_id, ["jobs:read"])["secret"]
    other = new_user("other")
    other_job = upload(client, other.headers).json()["job"]["id"]
    # Explicitly asking for another workspace is refused...
    r = client.get("/projects", headers={**_bearer(secret), "X-Workspace-ID": str(other.personal_workspace_id)})
    assert r.status_code == 404
    # ...and another workspace's job is invisible.
    assert client.get(f"/jobs/{other_job}", headers=_bearer(secret)).status_code == 404


def test_revoked_key_rejected(client, ws):
    owner, ws_id, _ = ws
    key = _create_key(client, owner, ws_id, ["jobs:read"])
    assert client.get("/projects", headers=_bearer(key["secret"])).status_code == 200
    r = client.delete(f"/workspaces/{ws_id}/api-keys/{key['id']}", headers=owner.headers)
    assert r.status_code == 200 and r.json()["revoked_at"]
    assert client.get("/projects", headers=_bearer(key["secret"])).status_code == 401
    # Revoked keys cannot be rotated back to life.
    r = client.post(f"/workspaces/{ws_id}/api-keys/{key['id']}/rotate", headers=owner.headers)
    assert r.status_code == 409


def test_rotate_invalidates_old_secret(client, ws):
    owner, ws_id, _ = ws
    key = _create_key(client, owner, ws_id, ["jobs:read"])
    r = client.post(f"/workspaces/{ws_id}/api-keys/{key['id']}/rotate", headers=owner.headers)
    assert r.status_code == 200
    rotated = r.json()
    assert rotated["id"] == key["id"] and rotated["scopes"] == ["jobs:read"]
    assert rotated["secret"] != key["secret"] and rotated["rotated_at"]
    assert client.get("/projects", headers=_bearer(key["secret"])).status_code == 401
    assert client.get("/projects", headers=_bearer(rotated["secret"])).status_code == 200


def test_last_used_at_tracked(client, ws):
    owner, ws_id, _ = ws
    key = _create_key(client, owner, ws_id, ["jobs:read"])
    assert key["last_used_at"] is None
    client.get("/projects", headers=_bearer(key["secret"]))
    listed = client.get(f"/workspaces/{ws_id}/api-keys", headers=owner.headers).json()
    assert next(k for k in listed if k["id"] == key["id"])["last_used_at"] is not None


def test_unknown_scope_rejected(client, ws):
    owner, ws_id, _ = ws
    r = client.post(
        f"/workspaces/{ws_id}/api-keys", json={"name": "x", "scopes": ["admin:all"]}, headers=owner.headers
    )
    assert r.status_code == 422


def test_garbage_key_rejected(client):
    assert client.get("/projects", headers=_bearer("sk_not-a-real-key")).status_code == 401
    assert client.get("/projects", headers={"X-API-Key": "sk_nope"}).status_code == 401


def test_key_lifecycle_is_audited(client, ws):
    owner, ws_id, _ = ws
    key = _create_key(client, owner, ws_id, ["jobs:read"])
    client.post(f"/workspaces/{ws_id}/api-keys/{key['id']}/rotate", headers=owner.headers)
    client.delete(f"/workspaces/{ws_id}/api-keys/{key['id']}", headers=owner.headers)
    actions = [e["action"] for e in client.get(f"/workspaces/{ws_id}/audit", headers=owner.headers).json()]
    assert actions[:3] == ["key.revoke", "key.rotate", "key.create"]
    # The secret never appears in the audit trail.
    body = client.get(f"/workspaces/{ws_id}/audit", headers=owner.headers).text
    assert key["secret"] not in body
