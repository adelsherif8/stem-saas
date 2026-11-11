from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings


def _normalize_db_url(url: str) -> str:
    """Managed hosts (Railway/Heroku) hand out `postgres://` / `postgresql://`,
    which SQLAlchemy maps to psycopg2. We ship psycopg v3, so force its dialect."""
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


DATABASE_URL = _normalize_db_url(settings.database_url)

connect_args = (
    {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
)

engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI dependency that yields a request-scoped DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --- Postgres row-level security --------------------------------------------
# Migration 0003 creates a NOLOGIN role `stemsaas_tenant`, grants it to the app's
# DB user, and adds RLS policies on every workspace-owned table keyed on the
# session setting `app.workspace_id`. Once a request has been authorized for a
# workspace, we switch the transaction to that role and pin the workspace id, so
# even a buggy query (a missing `WHERE workspace_id = ...`) cannot read or write
# another tenant's rows. The app-level checks in deps.py remain the first line
# of defence; this is the database-enforced second line.
RLS_ROLE = "stemsaas_tenant"
_RLS_KEY = "rls_workspace_id"
_rls_cache: dict[str, bool] = {}


def rls_available(db) -> bool:
    mode = settings.rls_enforce.lower()
    if mode == "off" or engine.dialect.name != "postgresql":
        return False
    if mode == "on":
        return True
    if "auto" not in _rls_cache:
        _rls_cache["auto"] = bool(
            db.execute(
                text(
                    "SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :r) "
                    "THEN pg_has_role(current_user, :r, 'MEMBER') ELSE false END"
                ),
                {"r": RLS_ROLE},
            ).scalar()
        )
    return _rls_cache["auto"]


def _apply_rls(connection, workspace_id: int) -> None:
    connection.exec_driver_sql(f"SET LOCAL ROLE {RLS_ROLE}")
    connection.execute(
        text("SELECT set_config('app.workspace_id', :w, true)"), {"w": str(workspace_id)}
    )


def scope_session_to_workspace(db, workspace_id: int) -> None:
    """Pin this session to one workspace for the rest of the request (Postgres
    only; a no-op on SQLite). Re-applied at the start of every transaction."""
    if not rls_available(db):
        return
    db.info[_RLS_KEY] = workspace_id
    _apply_rls(db.connection(), workspace_id)


@event.listens_for(SessionLocal, "after_begin")
def _reapply_rls(session, transaction, connection) -> None:
    workspace_id = session.info.get(_RLS_KEY)
    if workspace_id is not None:
        _apply_rls(connection, workspace_id)
