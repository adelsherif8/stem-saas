def _upload(client, headers, name="song.mp3"):
    files = {"file": (name, b"fake-audio-bytes", "audio/mpeg")}
    return client.post("/projects", files=files, headers=headers)


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] in {"ok", "degraded"}
    assert body["celery_eager"] is True


def test_register_login_me(client):
    assert client.post(
        "/auth/register", json={"email": "x@y.com", "password": "pw123456"}
    ).status_code == 201
    token = client.post(
        "/auth/login", data={"username": "x@y.com", "password": "pw123456"}
    ).json()["access_token"]
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
    assert me["email"] == "x@y.com"
    assert me["tier"] == "free"


def test_upload_runs_job_to_completion(client, auth_headers):
    # Eager Celery means the job finishes synchronously during the request.
    resp = _upload(client, auth_headers)
    assert resp.status_code == 201
    job_id = resp.json()["job"]["id"]

    job = client.get(f"/jobs/{job_id}", headers=auth_headers).json()
    assert job["status"] == "completed"
    assert job["progress"] == 100

    stems = client.get(f"/jobs/{job_id}/stems", headers=auth_headers).json()
    assert {s["name"] for s in stems} == {"vocals", "drums", "bass", "other"}

    dl = client.get(f"/jobs/{job_id}/stems/vocals", headers=auth_headers)
    assert dl.status_code == 200
    assert dl.content[:4] == b"RIFF"  # valid WAV header


def test_rejects_unsupported_filetype(client, auth_headers):
    assert _upload(client, auth_headers, name="notes.txt").status_code == 400


def test_requires_auth(client):
    assert _upload(client, {}).status_code == 401


def test_free_tier_quota_enforced(client):
    client.post("/auth/register", json={"email": "q@z.com", "password": "pw123456"})
    token = client.post(
        "/auth/login", data={"username": "q@z.com", "password": "pw123456"}
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    for _ in range(3):  # FREE_TIER_MONTHLY_LIMIT
        assert _upload(client, headers).status_code == 201
    assert _upload(client, headers).status_code == 402  # 4th blocked

    # Upgrade to pro, then it works again.
    client.post("/billing/mock-pay?session_id=cs_test", headers=headers)
    assert _upload(client, headers).status_code == 201
