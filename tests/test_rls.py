"""Postgres row-level security: the database itself keeps workspaces apart.

Needs a real Postgres. Set TEST_POSTGRES_URL to an empty scratch database,
for example:

    TEST_POSTGRES_URL=postgresql://postgres@127.0.0.1:5432/stemsaas_rls pytest tests/test_rls.py

The database's `public` schema is dropped and rebuilt with `alembic upgrade
head`. Without TEST_POSTGRES_URL these tests are skipped.
"""
import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from app.database import RLS_ROLE, _apply_rls, _normalize_db_url
from app.migrate import upgrade_to_head

PG_URL = os.environ.get("TEST_POSTGRES_URL")
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not PG_URL, reason="needs Postgres: set TEST_POSTGRES_URL"),
]


@pytest.fixture(scope="module")
def pg():
    url = _normalize_db_url(PG_URL)
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    upgrade_to_head(url)

    now = datetime.now(timezone.utc)
    # Seed as the table owner (not subject to RLS), like the app's migrations/worker.
    with eng.begin() as c:
        c.execute(text("INSERT INTO users (id, email, created_at) VALUES (1, 'a@x.com', :n), (2, 'b@x.com', :n)"), {"n": now})
        c.execute(text("INSERT INTO workspaces (id, name, personal, tier, created_at) VALUES "
                       "(1, 'A', true, 'free', :n), (2, 'B', true, 'free', :n)"), {"n": now})
        c.execute(text("INSERT INTO memberships (workspace_id, user_id, role) VALUES (1, 1, 'owner'), (2, 2, 'owner')"))
        c.execute(text("INSERT INTO projects (id, workspace_id, owner_id, name, original_filename, original_path, created_at) VALUES "
                       "(1, 1, 1, 'a-song', 'a.mp3', '', :n), (2, 2, 2, 'b-song', 'b.mp3', '', :n)"), {"n": now})
        c.execute(text("INSERT INTO jobs (id, workspace_id, project_id, owner_id, status, progress) VALUES "
                       "(1, 1, 1, 1, 'completed', 100), (2, 2, 2, 2, 'completed', 100)"))
        c.execute(text("INSERT INTO api_keys (id, workspace_id, name, prefix, key_hash, scopes, created_by_id) VALUES "
                       "(1, 1, 'a', 'sk_a', 'hash-a', 'jobs:read', 1), (2, 2, 'b', 'sk_b', 'hash-b', 'jobs:read', 2)"))
        c.execute(text("INSERT INTO audit_logs (workspace_id, action, created_at) VALUES (1, 'a.event', :n), (2, 'b.event', :n)"), {"n": now})
    yield eng
    eng.dispose()


def _as_tenant(conn, workspace_id):
    """Exactly what the app does after authorizing a request."""
    _apply_rls(conn, workspace_id)


TABLES = ["projects", "jobs", "api_keys", "audit_logs"]


def test_policies_installed(pg):
    with pg.connect() as c:
        rls = dict(c.execute(text(
            "SELECT relname, relrowsecurity FROM pg_class WHERE relname = ANY(:t)"), {"t": TABLES}).fetchall())
        assert rls == {t: True for t in TABLES}
        assert c.execute(text("SELECT count(*) FROM pg_policies WHERE policyname = 'workspace_isolation'")).scalar() == 4
        assert c.execute(text("SELECT pg_has_role(current_user, :r, 'MEMBER')"), {"r": RLS_ROLE}).scalar()


@pytest.mark.parametrize("table", TABLES)
def test_workspace_sees_only_its_own_rows(pg, table):
    for ws in (1, 2):
        with pg.begin() as c:
            _as_tenant(c, ws)
            ids = {r[0] for r in c.execute(text(f"SELECT workspace_id FROM {table}"))}
            assert ids == {ws}


def test_unscoped_query_cannot_reach_other_workspace(pg):
    """Even a query that forgets its WHERE clause, or asks for the other
    tenant's id directly, gets nothing back."""
    with pg.begin() as c:
        _as_tenant(c, 1)
        assert c.execute(text("SELECT count(*) FROM projects")).scalar() == 1
        assert c.execute(text("SELECT * FROM projects WHERE id = 2")).fetchall() == []
        assert c.execute(text("SELECT * FROM jobs WHERE workspace_id = 2")).fetchall() == []


def test_fails_closed_without_workspace_setting(pg):
    with pg.begin() as c:
        c.exec_driver_sql(f"SET LOCAL ROLE {RLS_ROLE}")
        for table in TABLES:
            assert c.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 0


def test_cannot_write_into_another_workspace(pg):
    with pytest.raises(DBAPIError, match="row-level security"):
        with pg.begin() as c:
            _as_tenant(c, 1)
            c.execute(text("INSERT INTO projects (workspace_id, owner_id, name, original_filename, original_path) "
                           "VALUES (2, 1, 'smuggled', 'x.mp3', '')"))


def test_cannot_move_row_into_another_workspace(pg):
    with pytest.raises(DBAPIError, match="row-level security"):
        with pg.begin() as c:
            _as_tenant(c, 1)
            c.execute(text("UPDATE projects SET workspace_id = 2 WHERE id = 1"))


def test_cannot_update_or_delete_other_workspace_rows(pg):
    with pg.begin() as c:
        _as_tenant(c, 1)
        assert c.execute(text("UPDATE jobs SET progress = 0 WHERE id = 2")).rowcount == 0
        assert c.execute(text("DELETE FROM api_keys WHERE id = 2")).rowcount == 0
    with pg.connect() as c:  # owner view: B's rows untouched
        assert c.execute(text("SELECT progress FROM jobs WHERE id = 2")).scalar() == 100
        assert c.execute(text("SELECT count(*) FROM api_keys WHERE id = 2")).scalar() == 1


def test_own_workspace_writes_allowed(pg):
    with pg.begin() as c:
        _as_tenant(c, 1)
        c.execute(text("INSERT INTO audit_logs (workspace_id, action) VALUES (1, 'ok.event')"))
        assert c.execute(text("UPDATE jobs SET progress = 99 WHERE id = 1")).rowcount == 1
        c.execute(text("UPDATE jobs SET progress = 100 WHERE id = 1"))


def test_setting_is_transaction_scoped(pg):
    """SET LOCAL: once the transaction ends the connection is back to normal,
    so a pooled connection never leaks one tenant's scope to the next request."""
    with pg.connect() as c:
        with c.begin():
            _as_tenant(c, 1)
            assert c.execute(text("SELECT current_user")).scalar() == RLS_ROLE
        assert c.execute(text("SELECT current_user")).scalar() != RLS_ROLE
        assert c.execute(text("SELECT count(*) FROM projects")).scalar() == 2  # owner again
