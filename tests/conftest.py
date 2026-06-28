import os
import tempfile

# Configure a fully self-contained test environment BEFORE the app imports its
# settings: in-process Celery, a throwaway SQLite DB, and a temp storage dir.
_tmp = tempfile.mkdtemp(prefix="stemsaas-test-")
os.environ["CELERY_EAGER"] = "true"
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["STORAGE_DIR"] = f"{_tmp}/storage"
os.environ["FREE_TIER_MONTHLY_LIMIT"] = "3"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


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
