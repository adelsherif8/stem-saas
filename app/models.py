import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Tier(str, enum.Enum):
    free = "free"
    pro = "pro"


class JobStatus(str, enum.Enum):
    queued = "queued"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class Role(str, enum.Enum):
    """Workspace roles, lowest to highest privilege."""

    member = "member"
    admin = "admin"
    owner = "owner"

    @property
    def rank(self) -> int:
        return {"member": 1, "admin": 2, "owner": 3}[self.value]


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, index=True, nullable=False)
    # NULL for accounts that only ever signed in with Google/GitHub.
    hashed_password = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)

    memberships = relationship(
        "Membership", back_populates="user", cascade="all, delete-orphan"
    )
    oauth_accounts = relationship(
        "OAuthAccount", back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def personal_workspace(self) -> "Workspace | None":
        for m in self.memberships:
            if m.workspace.personal and m.role == Role.owner:
                return m.workspace
        return None

    @property
    def tier(self) -> str:
        """Tier of the user's personal workspace (quotas live on workspaces)."""
        ws = self.personal_workspace
        return ws.tier.value if ws else Tier.free.value


class Workspace(Base):
    """The tenant boundary. Projects, jobs, API keys and quota belong here."""

    __tablename__ = "workspaces"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    # Every user gets exactly one personal workspace; it cannot be deleted.
    personal = Column(Boolean, nullable=False, default=False)
    tier = Column(Enum(Tier), default=Tier.free, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow)

    memberships = relationship(
        "Membership", back_populates="workspace", cascade="all, delete-orphan"
    )
    projects = relationship(
        "Project", back_populates="workspace", cascade="all, delete-orphan"
    )
    api_keys = relationship(
        "ApiKey", back_populates="workspace", cascade="all, delete-orphan"
    )


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="uq_memberships_workspace_user"),
    )

    id = Column(Integer, primary_key=True)
    workspace_id = Column(
        Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Stored as a plain string (validated by the Role enum in Python) to keep
    # migrations portable across SQLite and Postgres.
    role = Column(String(16), nullable=False, default=Role.member.value)
    created_at = Column(DateTime(timezone=True), default=utcnow)

    workspace = relationship("Workspace", back_populates="memberships")
    user = relationship("User", back_populates="memberships")

    @property
    def role_enum(self) -> Role:
        return Role(self.role)


class OAuthAccount(Base):
    """A Google/GitHub identity linked to a local user."""

    __tablename__ = "oauth_accounts"
    __table_args__ = (
        UniqueConstraint("provider", "subject", name="uq_oauth_accounts_provider_subject"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider = Column(String(32), nullable=False)
    subject = Column(String, nullable=False)  # provider's stable user id
    email = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)

    user = relationship("User", back_populates="oauth_accounts")


class OAuthState(Base):
    """Single-use authorization-code-flow state + PKCE verifier (10 min TTL)."""

    __tablename__ = "oauth_states"

    state = Column(String(128), primary_key=True)
    provider = Column(String(32), nullable=False)
    code_verifier = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class ApiKey(Base):
    """Workspace-scoped API key. Only a SHA-256 hash of the secret is stored."""

    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True)
    workspace_id = Column(
        Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name = Column(String, nullable=False)
    prefix = Column(String(16), nullable=False)  # first chars, safe to display
    key_hash = Column(String(64), nullable=False, unique=True, index=True)
    scopes = Column(String, nullable=False, default="")  # space-separated
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    rotated_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    workspace = relationship("Workspace", back_populates="api_keys")

    @property
    def scope_list(self) -> list[str]:
        return [s for s in (self.scopes or "").split() if s]


class AuditLog(Base):
    """Append-only security log.

    `workspace_id` deliberately has no foreign key: the record of a workspace
    being deleted must survive the workspace itself.
    """

    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True)
    workspace_id = Column(Integer, nullable=True, index=True)
    actor_user_id = Column(Integer, nullable=True)
    actor_api_key_id = Column(Integer, nullable=True)
    action = Column(String(64), nullable=False, index=True)
    target_type = Column(String(32), nullable=True)
    target_id = Column(String(64), nullable=True)
    details = Column(Text, nullable=True)  # JSON
    ip = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, index=True)


class Project(Base):
    """One uploaded song."""

    __tablename__ = "projects"

    id = Column(Integer, primary_key=True)
    workspace_id = Column(
        Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The user who uploaded it (for API-key uploads: the key's creator).
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    original_filename = Column(String, nullable=False)
    original_path = Column(String, nullable=False, default="")
    created_at = Column(DateTime(timezone=True), default=utcnow)

    workspace = relationship("Workspace", back_populates="projects")
    jobs = relationship("Job", back_populates="project", cascade="all, delete-orphan")


class Job(Base):
    """One stem-separation run for a project."""

    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True)
    workspace_id = Column(
        Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    celery_task_id = Column(String, nullable=True)
    status = Column(Enum(JobStatus), default=JobStatus.queued, nullable=False)
    progress = Column(Integer, default=0)
    error = Column(Text, nullable=True)
    stems = Column(Text, nullable=True)  # JSON: [{"name": ..., "path": ...}]
    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    project = relationship("Project", back_populates="jobs")
