"""Workspaces, memberships, OAuth identities, scoped API keys, audit log.

Data migration: every existing user gets a personal workspace (named after
their email) where they are `owner`; their tier moves onto that workspace and
all of their projects/jobs move into it. Quotas are enforced per workspace
from here on.

Revision ID: 0002_workspaces_rbac
Revises: 0001_initial
Create Date: 2026-11-11
"""
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_workspaces_rbac"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

# The `tier` enum type already exists on Postgres (created with users.tier).
TIER = sa.Enum("free", "pro", name="tier").with_variant(
    postgresql.ENUM("free", "pro", name="tier", create_type=False), "postgresql"
)


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("personal", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("tier", TIER, nullable=False, server_default="free"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "memberships",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Integer(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("workspace_id", "user_id", name="uq_memberships_workspace_user"),
        sa.CheckConstraint("role IN ('owner', 'admin', 'member')", name="ck_memberships_role"),
    )
    op.create_index("ix_memberships_workspace_id", "memberships", ["workspace_id"])
    op.create_index("ix_memberships_user_id", "memberships", ["user_id"])
    # At most one owner per workspace, enforced by the database.
    op.create_index(
        "uq_memberships_one_owner",
        "memberships",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("role = 'owner'"),
        sqlite_where=sa.text("role = 'owner'"),
    )

    op.create_table(
        "oauth_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("provider", "subject", name="uq_oauth_accounts_provider_subject"),
    )
    op.create_index("ix_oauth_accounts_user_id", "oauth_accounts", ["user_id"])

    op.create_table(
        "oauth_states",
        sa.Column("state", sa.String(128), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("code_verifier", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "api_keys",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Integer(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("prefix", sa.String(16), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("scopes", sa.String(), nullable=False),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_api_keys_workspace_id", "api_keys", ["workspace_id"])
    op.create_index("ix_api_keys_key_hash", "api_keys", ["key_hash"], unique=True)

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), nullable=True),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("actor_api_key_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=True),
        sa.Column("target_id", sa.String(64), nullable=True),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_audit_logs_workspace_id", "audit_logs", ["workspace_id"])
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])

    # Nullable first so existing rows can be backfilled.
    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("workspace_id", sa.Integer(), nullable=True))
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("workspace_id", sa.Integer(), nullable=True))

    _backfill_personal_workspaces()

    with op.batch_alter_table("projects") as batch:
        batch.alter_column("workspace_id", existing_type=sa.Integer(), nullable=False)
        batch.create_foreign_key(
            "fk_projects_workspace_id", "workspaces", ["workspace_id"], ["id"], ondelete="CASCADE"
        )
        batch.create_index("ix_projects_workspace_id", ["workspace_id"])
    with op.batch_alter_table("jobs") as batch:
        batch.alter_column("workspace_id", existing_type=sa.Integer(), nullable=False)
        batch.create_foreign_key(
            "fk_jobs_workspace_id", "workspaces", ["workspace_id"], ["id"], ondelete="CASCADE"
        )
        batch.create_index("ix_jobs_workspace_id", ["workspace_id"])

    with op.batch_alter_table("users") as batch:
        batch.alter_column("hashed_password", existing_type=sa.String(), nullable=True)
        batch.drop_column("tier")


def _backfill_personal_workspaces() -> None:
    bind = op.get_bind()
    now = datetime.now(timezone.utc)
    users = bind.execute(sa.text("SELECT id, email, tier FROM users ORDER BY id")).fetchall()
    workspaces = sa.table(
        "workspaces",
        sa.column("id", sa.Integer),
        sa.column("name", sa.String),
        sa.column("personal", sa.Boolean),
        sa.column("tier", sa.String),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    for user_id, email, tier in users:
        tier_value = getattr(tier, "value", tier) or "free"
        ws_id = bind.execute(
            workspaces.insert()
            .values(
                name=f"{email.split('@')[0]}'s workspace",
                personal=True,
                tier=sa.cast(sa.literal(tier_value), TIER) if bind.dialect.name == "postgresql" else tier_value,
                created_at=now,
            )
            .returning(workspaces.c.id)
        ).scalar_one()
        bind.execute(
            sa.text(
                "INSERT INTO memberships (workspace_id, user_id, role, created_at) "
                "VALUES (:ws, :uid, 'owner', :now)"
            ),
            {"ws": ws_id, "uid": user_id, "now": now},
        )
        bind.execute(
            sa.text("UPDATE projects SET workspace_id = :ws WHERE owner_id = :uid"),
            {"ws": ws_id, "uid": user_id},
        )
    bind.execute(
        sa.text(
            "UPDATE jobs SET workspace_id = "
            "(SELECT p.workspace_id FROM projects p WHERE p.id = jobs.project_id)"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("tier", TIER, nullable=False, server_default="free"))
    # Carry the personal workspace's tier back onto the user.
    bind.execute(
        sa.text(
            "UPDATE users SET tier = (SELECT w.tier FROM workspaces w "
            "JOIN memberships m ON m.workspace_id = w.id "
            "WHERE m.user_id = users.id AND m.role = 'owner' AND w.personal = :t) "
            "WHERE EXISTS (SELECT 1 FROM memberships m JOIN workspaces w ON w.id = m.workspace_id "
            "WHERE m.user_id = users.id AND m.role = 'owner' AND w.personal = :t)"
        ),
        {"t": True},
    )
    # Users created via OAuth have no password; the old schema requires one.
    bind.execute(sa.text("UPDATE users SET hashed_password = '!' WHERE hashed_password IS NULL"))
    with op.batch_alter_table("users") as batch:
        batch.alter_column("hashed_password", existing_type=sa.String(), nullable=False)

    with op.batch_alter_table("jobs") as batch:
        batch.drop_index("ix_jobs_workspace_id")
        batch.drop_constraint("fk_jobs_workspace_id", type_="foreignkey")
        batch.drop_column("workspace_id")
    with op.batch_alter_table("projects") as batch:
        batch.drop_index("ix_projects_workspace_id")
        batch.drop_constraint("fk_projects_workspace_id", type_="foreignkey")
        batch.drop_column("workspace_id")

    op.drop_table("audit_logs")
    op.drop_table("api_keys")
    op.drop_table("oauth_states")
    op.drop_table("oauth_accounts")
    op.drop_table("memberships")
    op.drop_table("workspaces")
