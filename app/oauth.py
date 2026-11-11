"""OAuth2 social login (Google, GitHub): authorization-code flow with PKCE.

Flow
  1. GET /auth/oauth/{provider}/authorize
     - creates a random `state` and PKCE `code_verifier`, stores them in
       `oauth_states` (single use, 10 min TTL) and also sets the state in an
       HttpOnly cookie, binding the flow to this browser (login-CSRF defence);
     - redirects to the provider with `code_challenge = S256(verifier)`.
  2. Provider redirects to GET /auth/oauth/{provider}/callback?code&state
     - state must match both the DB row and the cookie; the row is consumed;
     - code + verifier are exchanged for an access token (server-to-server);
     - the provider's user info is fetched; only a *verified* email is used;
     - the identity is linked to an existing user (by provider id, then by
       verified email) or a new user + personal workspace is created;
     - the browser is redirected to OAUTH_SUCCESS_REDIRECT#access_token=<jwt>.

All provider HTTP goes through `http_client()`, which tests replace with an
httpx.MockTransport so the real request/response handling is exercised.
"""
import base64
import hashlib
import secrets
from dataclasses import dataclass
from typing import Callable, Optional

import httpx

from .config import settings


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    label: str
    authorize_url: str
    token_url: str
    scope: str
    client_id_setting: str
    client_secret_setting: str

    @property
    def client_id(self) -> str:
        return getattr(settings, self.client_id_setting)

    @property
    def client_secret(self) -> str:
        return getattr(settings, self.client_secret_setting)

    @property
    def enabled(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def redirect_uri(self) -> str:
        base = settings.oauth_redirect_base_url.rstrip("/")
        return f"{base}/auth/oauth/{self.name}/callback"

    @property
    def env_hint(self) -> str:
        return f"{self.client_id_setting.upper()} and {self.client_secret_setting.upper()}"


PROVIDERS: dict[str, ProviderConfig] = {
    "google": ProviderConfig(
        name="google",
        label="Google",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scope="openid email profile",
        client_id_setting="google_client_id",
        client_secret_setting="google_client_secret",
    ),
    "github": ProviderConfig(
        name="github",
        label="GitHub",
        authorize_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",
        scope="read:user user:email",
        client_id_setting="github_client_id",
        client_secret_setting="github_client_secret",
    ),
}

GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GITHUB_USER_URL = "https://api.github.com/user"
GITHUB_EMAILS_URL = "https://api.github.com/user/emails"


class OAuthError(Exception):
    """Provider-side failure; surfaced to the client as a 400."""


@dataclass
class ProviderIdentity:
    subject: str  # the provider's stable user id
    email: Optional[str]  # a verified email, or None


# Swappable for tests (httpx.MockTransport).
_client_factory: Callable[[], httpx.Client] = lambda: httpx.Client(timeout=10.0)


def http_client() -> httpx.Client:
    return _client_factory()


def set_http_client_factory(factory: Callable[[], httpx.Client]) -> None:
    global _client_factory
    _client_factory = factory


def new_state() -> str:
    return secrets.token_urlsafe(32)


def new_code_verifier() -> str:
    # RFC 7636: 43-128 chars from the unreserved set; token_urlsafe(64) -> 86.
    return secrets.token_urlsafe(64)


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorization_url(provider: ProviderConfig, state: str, verifier: str) -> str:
    params = {
        "client_id": provider.client_id,
        "redirect_uri": provider.redirect_uri,
        "response_type": "code",
        "scope": provider.scope,
        "state": state,
        "code_challenge": code_challenge(verifier),
        "code_challenge_method": "S256",
    }
    if provider.name == "google":
        params["prompt"] = "select_account"
    return str(httpx.URL(provider.authorize_url, params=params))


def exchange_code(provider: ProviderConfig, code: str, verifier: str) -> str:
    """Trade the authorization code (+ PKCE verifier) for an access token."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": provider.redirect_uri,
        "client_id": provider.client_id,
        "client_secret": provider.client_secret,
        "code_verifier": verifier,
    }
    with http_client() as client:
        resp = client.post(provider.token_url, data=data, headers={"Accept": "application/json"})
    try:
        body = resp.json()
    except ValueError:
        body = {}
    # GitHub reports errors with HTTP 200 and an `error` field.
    if resp.status_code != 200 or "error" in body or "access_token" not in body:
        reason = body.get("error_description") or body.get("error") or f"HTTP {resp.status_code}"
        raise OAuthError(f"{provider.label} token exchange failed: {reason}")
    return body["access_token"]


def fetch_identity(provider: ProviderConfig, access_token: str) -> ProviderIdentity:
    auth = {"Authorization": f"Bearer {access_token}"}
    with http_client() as client:
        if provider.name == "google":
            resp = client.get(GOOGLE_USERINFO_URL, headers=auth)
            if resp.status_code != 200:
                raise OAuthError(f"Google userinfo failed: HTTP {resp.status_code}")
            info = resp.json()
            email = info.get("email") if info.get("email_verified") is True else None
            return ProviderIdentity(subject=str(info["sub"]), email=email)

        if provider.name == "github":
            gh = {
                **auth,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "stem-saas",
            }
            resp = client.get(GITHUB_USER_URL, headers=gh)
            if resp.status_code != 200:
                raise OAuthError(f"GitHub user lookup failed: HTTP {resp.status_code}")
            user = resp.json()
            # The public profile email may be absent or unverified; ask for
            # the primary *verified* address explicitly.
            emails = client.get(GITHUB_EMAILS_URL, headers=gh)
            email = None
            if emails.status_code == 200:
                for e in emails.json():
                    if e.get("primary") and e.get("verified"):
                        email = e.get("email")
                        break
            return ProviderIdentity(subject=str(user["id"]), email=email)

    raise OAuthError(f"Unsupported provider {provider.name}")
