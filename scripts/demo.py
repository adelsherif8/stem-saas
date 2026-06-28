"""End-to-end demo against a running server.

Usage:
    python scripts/demo.py            # uses http://localhost:8000

Walks the full flow: register -> login -> upload -> poll job -> list/download
stems -> hit the free-tier limit -> upgrade -> upload again.
"""

import io
import sys
import time

import requests

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
EMAIL = f"demo_{int(time.time())}@example.com"
PASSWORD = "demo-password"


def main() -> None:
    print(f"Server: {BASE}")
    print("health:", requests.get(f"{BASE}/health").json())

    requests.post(f"{BASE}/auth/register", json={"email": EMAIL, "password": PASSWORD})
    token = requests.post(
        f"{BASE}/auth/login", data={"username": EMAIL, "password": PASSWORD}
    ).json()["access_token"]
    h = {"Authorization": f"Bearer {token}"}
    print("logged in as", EMAIL)

    def upload(n):
        files = {"file": (f"song{n}.mp3", io.BytesIO(b"fake-audio"), "audio/mpeg")}
        return requests.post(f"{BASE}/projects", files=files, headers=h)

    r = upload(1)
    job_id = r.json()["job"]["id"]
    print("uploaded -> job", job_id)

    while True:
        job = requests.get(f"{BASE}/jobs/{job_id}", headers=h).json()
        print(f"  job {job_id}: {job['status']} ({job['progress']}%)")
        if job["status"] in {"completed", "failed"}:
            break
        time.sleep(1)

    stems = requests.get(f"{BASE}/jobs/{job_id}/stems", headers=h).json()
    print("stems:", [s["name"] for s in stems])

    print("quota:", requests.get(f"{BASE}/projects/quota", headers=h).json())

    # Burn through the rest of the free tier, then expect a 402.
    upload(2)
    upload(3)
    blocked = upload(4)
    print("4th upload status (expect 402):", blocked.status_code)

    requests.post(f"{BASE}/billing/mock-pay?session_id=cs_demo", headers=h)
    print("upgraded; quota:", requests.get(f"{BASE}/projects/quota", headers=h).json())
    print("post-upgrade upload status (expect 201):", upload(5).status_code)


if __name__ == "__main__":
    main()
