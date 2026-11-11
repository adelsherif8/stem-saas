"""Postgres row-level security: workspace isolation enforced by the database.

Applied only on PostgreSQL (a no-op on SQLite).

* Enables RLS on every table that carries tenant data (projects, jobs,
  api_keys, audit_logs) with one policy each:
      workspace_id = current_setting('app.workspace_id')::int
  for both reading (USING) and writing (WITH CHECK). If the setting is
  missing the comparison is NULL, so the policy fails closed: no rows.
* Creates a NOLOGIN role `stemsaas_tenant`, grants it table privileges, and
  grants it to the migrating (app) user. After authorizing a request for a
  workspace, the app runs `SET LOCAL ROLE stemsaas_tenant` and sets
  `app.workspace_id` for that transaction (see app/database.py).
* The table owner (the app's own login role: migrations, Celery worker,
  login/API-key lookup) is not subject to RLS because it is not FORCEd; the
  policies bind every other role, including the tenant role.

`memberships` is intentionally not under RLS: it is the authorization table
the app consults *before* it knows which workspace to pin.

If the database user may not create roles (some managed hosts), the role
steps are skipped with a NOTICE; the policies are still installed and the
app detects that it cannot switch roles (RLS_ENFORCE=auto).

Revision ID: 0003_postgres_rls
Revises: 0002_workspaces_rbac
Create Date: 2026-11-11
"""
from alembic import op

revision = "0003_postgres_rls"
down_revision = "0002_workspaces_rbac"
branch_labels = None
depends_on = None

ROLE = "stemsaas_tenant"
TABLES = ("projects", "jobs", "api_keys", "audit_logs")
POLICY = "workspace_isolation"
PREDICATE = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::integer"


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN
                CREATE ROLE {ROLE} NOLOGIN;
            END IF;
        EXCEPTION WHEN insufficient_privilege THEN
            RAISE NOTICE 'stem-saas: cannot create role {ROLE}; RLS role switching disabled';
        END
        $$;
        """
    )
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN
                EXECUTE format('GRANT {ROLE} TO %I', current_user);
                GRANT USAGE ON SCHEMA public TO {ROLE};
                GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {ROLE};
                GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {ROLE};
                ALTER DEFAULT PRIVILEGES IN SCHEMA public
                    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {ROLE};
                ALTER DEFAULT PRIVILEGES IN SCHEMA public
                    GRANT USAGE, SELECT ON SEQUENCES TO {ROLE};
            END IF;
        EXCEPTION WHEN insufficient_privilege THEN
            RAISE NOTICE 'stem-saas: cannot grant {ROLE}; RLS role switching disabled';
        END
        $$;
        """
    )
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {POLICY} ON {table} USING ({PREDICATE}) WITH CHECK ({PREDICATE})"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in TABLES:
        op.execute(f"DROP POLICY IF EXISTS {POLICY} ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN
                ALTER DEFAULT PRIVILEGES IN SCHEMA public
                    REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {ROLE};
                ALTER DEFAULT PRIVILEGES IN SCHEMA public
                    REVOKE USAGE, SELECT ON SEQUENCES FROM {ROLE};
                REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {ROLE};
                REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {ROLE};
                REVOKE USAGE ON SCHEMA public FROM {ROLE};
                -- Shared across databases on the cluster, so keep it if still in use.
                BEGIN
                    DROP ROLE {ROLE};
                EXCEPTION WHEN dependent_objects_still_exist OR insufficient_privilege THEN
                    RAISE NOTICE 'stem-saas: role {ROLE} kept (still referenced)';
                END;
            END IF;
        END
        $$;
        """
    )
