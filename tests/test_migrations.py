"""Alembic migrations against throwaway SQLite files (independent of the app DB)."""
from datetime import datetime, timezone

from alembic import command
from sqlalchemy import create_engine, inspect, text

from app.migrate import alembic_config, upgrade_to_head


def _seed_legacy(url):
    """A database at revision 0001 with two users, one Pro, and some data."""
    command.upgrade(alembic_config(url), "0001_initial")
    eng = create_engine(url)
    now = datetime.now(timezone.utc)
    with eng.begin() as c:
        c.execute(text("INSERT INTO users (id, email, hashed_password, tier, created_at) VALUES "
                       "(1, 'pro@x.com', 'h', 'pro', :n), (2, 'free@x.com', 'h', 'free', :n)"), {"n": now})
        c.execute(text("INSERT INTO projects (id, owner_id, name, original_filename, original_path, created_at) "
                       "VALUES (10, 1, 'a', 'a.mp3', '', :n), (11, 2, 'b', 'b.mp3', '', :n)"), {"n": now})
        c.execute(text("INSERT INTO jobs (id, project_id, owner_id, status, progress) VALUES "
                       "(100, 10, 1, 'completed', 100), (101, 11, 2, 'queued', 0)"))
    return eng


def test_backfill_creates_personal_workspaces(tmp_path):
    url = f"sqlite:///{tmp_path}/legacy.db"
    eng = _seed_legacy(url)
    command.upgrade(alembic_config(url), "head")
    with eng.connect() as c:
        ws = {r.name: r for r in c.execute(text("SELECT * FROM workspaces"))}
        assert ws["pro's workspace"].tier == "pro" and ws["pro's workspace"].personal
        assert ws["free's workspace"].tier == "free"
        members = c.execute(text("SELECT user_id, workspace_id, role FROM memberships ORDER BY user_id")).fetchall()
        assert [m.role for m in members] == ["owner", "owner"]
        ws_of = {m.user_id: m.workspace_id for m in members}
        projects = dict(c.execute(text("SELECT id, workspace_id FROM projects")).fetchall())
        jobs = dict(c.execute(text("SELECT id, workspace_id FROM jobs")).fetchall())
        assert projects == {10: ws_of[1], 11: ws_of[2]}
        assert jobs == {100: ws_of[1], 101: ws_of[2]}
    assert "tier" not in {col["name"] for col in inspect(eng).get_columns("users")}


def test_downgrade_restores_user_tier(tmp_path):
    url = f"sqlite:///{tmp_path}/roundtrip.db"
    eng = _seed_legacy(url)
    cfg = alembic_config(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0001_initial")
    with eng.connect() as c:
        tiers = dict(c.execute(text("SELECT email, tier FROM users")).fetchall())
        assert tiers == {"pro@x.com": "pro", "free@x.com": "free"}
    command.upgrade(cfg, "head")


def test_pre_alembic_database_is_stamped_then_upgraded(tmp_path):
    """Databases created by the old create_all() have tables but no
    alembic_version; upgrade_to_head() stamps them at 0001 first."""
    url = f"sqlite:///{tmp_path}/precreate.db"
    eng = _seed_legacy(url)
    with eng.begin() as c:
        c.execute(text("DROP TABLE alembic_version"))
    upgrade_to_head(url)
    with eng.connect() as c:
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0003_postgres_rls"
        assert c.execute(text("SELECT count(*) FROM workspaces")).scalar() == 2
