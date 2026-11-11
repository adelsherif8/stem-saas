from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """App configuration, populated from environment variables / .env file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Stem SaaS"
    environment: str = "development"

    # Postgres in docker; SQLite as a zero-setup local fallback.
    database_url: str = "sqlite:///./stemsaas.db"
    redis_url: str = "redis://localhost:6379/0"

    # Run `alembic upgrade head` when the API process starts.
    auto_migrate: bool = True

    # When True, Celery runs tasks synchronously in-process (no broker needed).
    # Handy for tests and a quick `uvicorn`-only demo.
    celery_eager: bool = False

    jwt_secret: str = "dev-secret-change-me-to-a-long-random-value-in-prod"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24

    storage_dir: str = "./storage"

    # Flip to True (DEMUCS_REAL=1) to run real HT-Demucs instead of mock stems.
    demucs_real: bool = False

    # Postgres row-level security: "auto" switches each workspace-scoped request
    # to the RLS role when migration 0003 has granted it; "on" forces it, "off"
    # disables it. Always off on SQLite.
    rls_enforce: str = "auto"

    # --- OAuth2 social login (authorization-code flow + PKCE) ---
    # Leave a provider's client id empty to disable it; its endpoints then
    # answer 503 with a clear message instead of failing obscurely.
    google_client_id: str = ""
    google_client_secret: str = ""
    github_client_id: str = ""
    github_client_secret: str = ""
    # Public base URL of this API; callbacks are {base}/auth/oauth/{provider}/callback
    oauth_redirect_base_url: str = "http://localhost:8000"
    # Where the browser lands after a successful social login. The JWT is
    # appended as a URL fragment (#access_token=...), which never hits server logs.
    oauth_success_redirect: str = "/"

    free_tier_monthly_limit: int = 3
    pro_price_usd: float = 9.0

    @property
    def stem_names(self) -> list[str]:
        return ["vocals", "drums", "bass", "other"]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
