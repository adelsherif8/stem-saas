import os
import tempfile
import uuid

# Configure a fully self-contained test environment BEFORE the app imports its
# settings: in-process Celery, a throwaway database, and a temp storage dir.
#
# By default the suite runs on a fresh SQLite file. Set TEST_DATABASE_URL to a
# Postgres database to run the whole suite there instead (with RLS active);
# that database's `public` schema is DROPPED and recreated first.
_tmp = tempfile.mkdtemp(prefix="stemsaas-test-")
_db_url = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{_tmp}/test.db"
os.environ["CELERY_EAGER"] = "true"
os.environ["DATABASE_URL"] = _db_url
os.environ["STORAGE_DIR"] = f"{_tmp}/storage"
os.environ["FREE_TIER_MONTHLY_LIMIT"] = "3"
os.environ["BCRYPT_ROUNDS"] = "4"
for _var in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET"):
    os.environ[_var] = ""  # OAuth starts disabled; tests enable it explicitly

if _db_url.startswith("postgres"):
    from sqlalchemy import create_engine, text

    from app.database import _normalize_db_url

    _eng = create_engine(_normalize_db_url(_db_url))
    with _eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    _eng.dispose()

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402  (runs migrations to head)
from app.security import create_access_token  # noqa: E402
from app.services import create_user_with_workspace  # noqa: E402


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def auth_headers(client):
    client.post("/auth/register", json={"email": "a@b.com", "password": "pw123456"})
    token = client.post(
        "/auth/login", data={"username": "a@b.com", "password": "pw123456"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


class TestUser:
    __test__ = False

    def __init__(self, id: int, email: str, personal_workspace_id: int):
        self.id = id
        self.email = email
        self.personal_workspace_id = personal_workspace_id
        self.headers = {"Authorization": f"Bearer {create_access_token(email)}"}

    def in_ws(self, workspace_id: int) -> dict:
        return {**self.headers, "X-Workspace-ID": str(workspace_id)}


def make_user(prefix: str = "user") -> TestUser:
    """Create a user (+ personal workspace) directly in the DB and mint a JWT."""
    email = f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"
    db = SessionLocal()
    try:
        user = create_user_with_workspace(db, email, hashed_password=None)
        db.commit()
        return TestUser(user.id, email, user.personal_workspace.id)
    finally:
        db.close()


@pytest.fixture
def new_user():
    return make_user


def upload(client, headers, name="song.mp3"):
    files = {"file": (name, b"fake-audio-bytes", "audio/mpeg")}
    return client.post("/projects", files=files, headers=headers)


class Team:
    """A team workspace with one user per role, an outsider, a spare member to
    act upon, a completed job and an API key."""

    def __init__(self, client):
        self.owner = make_user("owner")
        self.admin = make_user("admin")
        self.member = make_user("member")
        self.target = make_user("target")
        self.outsider = make_user("outsider")
        ws = client.post("/workspaces", json={"name": "Team"}, headers=self.owner.headers)
        assert ws.status_code == 201, ws.text
        self.ws_id = ws.json()["id"]
        for u, role in ((self.admin, "admin"), (self.member, "member"), (self.target, "member")):
            r = client.post(
                f"/workspaces/{self.ws_id}/members",
                json={"email": u.email, "role": role},
                headers=self.owner.headers,
            )
            assert r.status_code == 201, r.text
        r = upload(client, self.owner.in_ws(self.ws_id))
        assert r.status_code == 201, r.text
        self.job_id = r.json()["job"]["id"]
        r = client.post(
            f"/workspaces/{self.ws_id}/api-keys",
            json={"name": "ci", "scopes": ["jobs:read"]},
            headers=self.owner.headers,
        )
        assert r.status_code == 201, r.text
        self.key_id = r.json()["id"]

    def user(self, role: str) -> TestUser:
        return getattr(self, role)


@pytest.fixture
def team(client):
    return Team(client)
