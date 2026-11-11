"""OAuth2 login with mocked Google/GitHub.

Provider HTTP is served by an httpx.MockTransport, so the real token-exchange
and userinfo code paths run (including PKCE verification by the 'provider').
"""
import base64
import hashlib
import uuid
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app import oauth
from app.config import settings
from app.database import SessionLocal
from app.models import OAuthState, utcnow


class FakeProvider:
    """Plays Google + GitHub. Validates the token request like a real IdP."""

    def __init__(self):
        self.codes: dict[str, str] = {}  # code -> code_challenge
        self.google_user = {"sub": "g-1", "email": "g@example.com", "email_verified": True}
        self.github_user = {"id": 42, "login": "octo", "email": None}
        self.github_emails = [{"email": "octo@example.com", "primary": True, "verified": True}]
        self.token_error = None
        self.requests: list[httpx.Request] = []

    def issue_code(self, challenge: str) -> str:
        code = uuid.uuid4().hex
        self.codes[code] = challenge
        return code

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url in (oauth.PROVIDERS["google"].token_url, oauth.PROVIDERS["github"].token_url):
            form = parse_qs(request.content.decode())
            if self.token_error:
                return httpx.Response(200, json={"error": self.token_error})
            code, verifier = form["code"][0], form["code_verifier"][0]
            expected = self.codes.pop(code, None)
            actual = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            if expected is None or expected != actual or form["grant_type"] != ["authorization_code"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "provider-token", "token_type": "bearer"})
        if request.headers.get("Authorization") != "Bearer provider-token":
            return httpx.Response(401)
        if url == oauth.GOOGLE_USERINFO_URL:
            return httpx.Response(200, json=self.google_user)
        if url == oauth.GITHUB_USER_URL:
            return httpx.Response(200, json=self.github_user)
        if url == oauth.GITHUB_EMAILS_URL:
            return httpx.Response(200, json=self.github_emails)
        return httpx.Response(404)


@pytest.fixture
def idp(monkeypatch):
    fake = FakeProvider()
    monkeypatch.setattr(settings, "google_client_id", "google-id")
    monkeypatch.setattr(settings, "google_client_secret", "google-secret")
    monkeypatch.setattr(settings, "github_client_id", "github-id")
    monkeypatch.setattr(settings, "github_client_secret", "github-secret")
    monkeypatch.setattr(oauth, "_client_factory", lambda: httpx.Client(transport=httpx.MockTransport(fake.handler)))
    return fake


def _start(client, provider):
    r = client.get(f"/auth/oauth/{provider}/authorize", follow_redirects=False)
    assert r.status_code == 307, r.text
    q = parse_qs(urlparse(r.headers["location"]).query)
    return q["state"][0], q["code_challenge"][0]


def _login(client, idp, provider):
    state, challenge = _start(client, provider)
    code = idp.issue_code(challenge)
    return client.get(f"/auth/oauth/{provider}/callback", params={"code": code, "state": state},
                      follow_redirects=False)


def _token_from(resp):
    assert resp.status_code == 303, resp.text
    fragment = parse_qs(urlparse(resp.headers["location"]).fragment)
    return fragment["access_token"][0]


# --- disabled by default --------------------------------------------------------

@pytest.mark.parametrize("provider", ["google", "github"])
def test_disabled_provider_returns_503(client, provider):
    r = client.get(f"/auth/oauth/{provider}/authorize", follow_redirects=False)
    assert r.status_code == 503
    assert "not configured" in r.json()["detail"]
    assert f"{provider.upper()}_CLIENT_ID" in r.json()["detail"]
    r = client.get(f"/auth/oauth/{provider}/callback?code=x&state=y")
    assert r.status_code == 503


def test_providers_endpoint_reports_status(client, monkeypatch):
    assert all(not p["enabled"] for p in client.get("/auth/oauth/providers").json())
    monkeypatch.setattr(settings, "github_client_id", "id")
    monkeypatch.setattr(settings, "github_client_secret", "secret")
    status = {p["name"]: p["enabled"] for p in client.get("/auth/oauth/providers").json()}
    assert status == {"google": False, "github": True}


def test_unknown_provider_404(client):
    assert client.get("/auth/oauth/myspace/authorize").status_code == 404


# --- authorize ------------------------------------------------------------------

def test_authorize_redirect_uses_pkce_and_state(client, idp):
    r = client.get("/auth/oauth/google/authorize", follow_redirects=False)
    loc = urlparse(r.headers["location"])
    q = parse_qs(loc.query)
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == oauth.PROVIDERS["google"].authorize_url
    assert q["client_id"] == ["google-id"]
    assert q["response_type"] == ["code"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["redirect_uri"] == ["http://localhost:8000/auth/oauth/google/callback"]
    assert "openid" in q["scope"][0] and "email" in q["scope"][0]
    state = q["state"][0]
    assert r.cookies.get("oauth_state") == state
    # Verifier is stored server-side and matches the challenge sent.
    db = SessionLocal()
    try:
        row = db.get(OAuthState, state)
        assert oauth.code_challenge(row.code_verifier) == q["code_challenge"][0]
    finally:
        db.close()


def test_authorize_json_mode(client, idp):
    r = client.get("/auth/oauth/github/authorize?redirect=false")
    assert r.status_code == 200
    assert r.json()["authorization_url"].startswith(oauth.PROVIDERS["github"].authorize_url)


# --- callback -------------------------------------------------------------------

def test_google_login_creates_user_with_personal_workspace(client, idp):
    idp.google_user = {"sub": "g-new", "email": "newg@example.com", "email_verified": True}
    token = _token_from(_login(client, idp, "google"))
    h = {"Authorization": f"Bearer {token}"}
    assert client.get("/auth/me", headers=h).json()["email"] == "newg@example.com"
    workspaces = client.get("/workspaces", headers=h).json()
    assert len(workspaces) == 1 and workspaces[0]["personal"] and workspaces[0]["role"] == "owner"
    audit = client.get(f"/workspaces/{workspaces[0]['id']}/audit?action=auth.login", headers=h).json()
    assert audit[0]["details"] == {"method": "google", "new_user": True}
    # The token request carried our client credentials and the PKCE verifier.
    token_req = next(r for r in idp.requests if str(r.url) == oauth.PROVIDERS["google"].token_url)
    form = parse_qs(token_req.content.decode())
    assert form["client_secret"] == ["google-secret"] and "code_verifier" in form


def test_repeat_login_maps_to_same_user(client, idp):
    idp.google_user = {"sub": "g-repeat", "email": "repeat@example.com", "email_verified": True}
    t1 = _token_from(_login(client, idp, "google"))
    # Email changed at Google: still the same account (matched by subject).
    idp.google_user = {"sub": "g-repeat", "email": "changed@example.com", "email_verified": True}
    t2 = _token_from(_login(client, idp, "google"))
    me1 = client.get("/auth/me", headers={"Authorization": f"Bearer {t1}"}).json()
    me2 = client.get("/auth/me", headers={"Authorization": f"Bearer {t2}"}).json()
    assert me1["id"] == me2["id"]


def test_github_login_uses_primary_verified_email(client, idp):
    idp.github_user = {"id": 777, "login": "dev", "email": "public-unverified@example.com"}
    idp.github_emails = [
        {"email": "old@example.com", "primary": False, "verified": True},
        {"email": "dev@example.com", "primary": True, "verified": True},
    ]
    token = _token_from(_login(client, idp, "github"))
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
    assert me["email"] == "dev@example.com"


def test_github_without_verified_email_rejected(client, idp):
    idp.github_user = {"id": 778, "login": "anon", "email": None}
    idp.github_emails = [{"email": "x@example.com", "primary": True, "verified": False}]
    r = _login(client, idp, "github")
    assert r.status_code == 400 and "verified email" in r.json()["detail"]


def test_google_unverified_email_rejected(client, idp):
    idp.google_user = {"sub": "g-unv", "email": "unv@example.com", "email_verified": False}
    assert _login(client, idp, "google").status_code == 400


def test_links_to_existing_password_account(client, idp):
    client.post("/auth/register", json={"email": "both@example.com", "password": "pw123456"})
    idp.google_user = {"sub": "g-both", "email": "both@example.com", "email_verified": True}
    token = _token_from(_login(client, idp, "google"))
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
    pw = client.post("/auth/login", data={"username": "both@example.com", "password": "pw123456"})
    me_pw = client.get("/auth/me", headers={"Authorization": f"Bearer {pw.json()['access_token']}"}).json()
    assert me["id"] == me_pw["id"]


def test_oauth_only_user_cannot_password_login(client, idp):
    idp.google_user = {"sub": "g-nopw", "email": "nopw@example.com", "email_verified": True}
    _token_from(_login(client, idp, "google"))
    r = client.post("/auth/login", data={"username": "nopw@example.com", "password": ""})
    assert r.status_code in (401, 422)
    r = client.post("/auth/login", data={"username": "nopw@example.com", "password": "anything"})
    assert r.status_code == 401


def test_invalid_state_rejected(client, idp):
    _start(client, "google")
    r = client.get("/auth/oauth/google/callback", params={"code": "c", "state": "forged"})
    assert r.status_code == 400 and "state" in r.json()["detail"]


def test_state_is_single_use(client, idp):
    state, challenge = _start(client, "google")
    code = idp.issue_code(challenge)
    params = {"code": code, "state": state}
    assert client.get("/auth/oauth/google/callback", params=params, follow_redirects=False).status_code == 303
    client.cookies.set("oauth_state", state, path="/auth/oauth")
    assert client.get("/auth/oauth/google/callback", params=params).status_code == 400


def test_state_must_match_browser_cookie(client, idp):
    """Login CSRF: a callback link for someone else's flow must not log you in."""
    state, challenge = _start(client, "google")
    code = idp.issue_code(challenge)
    victim = client.__class__(client.app)  # fresh browser, no state cookie
    r = victim.get("/auth/oauth/google/callback", params={"code": code, "state": state})
    assert r.status_code == 400


def test_state_bound_to_provider(client, idp):
    state, challenge = _start(client, "google")
    code = idp.issue_code(challenge)
    r = client.get("/auth/oauth/github/callback", params={"code": code, "state": state})
    assert r.status_code == 400


def test_expired_state_rejected(client, idp):
    state, challenge = _start(client, "google")
    db = SessionLocal()
    try:
        db.get(OAuthState, state).created_at = utcnow() - timedelta(minutes=11)
        db.commit()
    finally:
        db.close()
    r = client.get("/auth/oauth/google/callback", params={"code": idp.issue_code(challenge), "state": state})
    assert r.status_code == 400


def test_wrong_pkce_verifier_fails_exchange(client, idp):
    state, _ = _start(client, "google")
    code = idp.issue_code("not-the-right-challenge")
    r = client.get("/auth/oauth/google/callback", params={"code": code, "state": state})
    assert r.status_code == 400 and "token exchange failed" in r.json()["detail"]


def test_provider_error_in_token_response(client, idp):
    idp.token_error = "bad_verification_code"
    r = _login(client, idp, "github")
    assert r.status_code == 400 and "bad_verification_code" in r.json()["detail"]


def test_user_denied_consent(client, idp):
    r = client.get("/auth/oauth/google/callback", params={"error": "access_denied"})
    assert r.status_code == 400 and "access_denied" in r.json()["detail"]


def test_redirect_base_url_configurable(client, idp, monkeypatch):
    monkeypatch.setattr(settings, "oauth_redirect_base_url", "https://stem.example.com/")
    r = client.get("/auth/oauth/github/authorize", follow_redirects=False)
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["redirect_uri"] == ["https://stem.example.com/auth/oauth/github/callback"]
    assert "secure" in r.headers["set-cookie"].lower()
