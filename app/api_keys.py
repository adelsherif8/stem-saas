"""API key generation and hashing.

Format: ``sk_<43 url-safe chars>`` (256 bits of entropy). Because the secret is
high-entropy and random, a single fast SHA-256 is the right storage hash (a
slow KDF like bcrypt protects low-entropy passwords; it buys nothing here and
would make every API request slow). Only the hash and a short display prefix
are stored; the full secret is returned exactly once at creation/rotation.
"""
import hashlib
import secrets

KEY_PREFIX = "sk_"


def generate_key() -> tuple[str, str, str]:
    """Return (secret, display_prefix, sha256_hex)."""
    secret = KEY_PREFIX + secrets.token_urlsafe(32)
    return secret, secret[:10], hash_key(secret)


def hash_key(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def looks_like_api_key(token: str) -> bool:
    return token.startswith(KEY_PREFIX)
