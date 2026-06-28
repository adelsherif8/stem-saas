import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
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


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    tier = Column(Enum(Tier), default=Tier.free, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow)

    projects = relationship(
        "Project", back_populates="owner", cascade="all, delete-orphan"
    )


class Project(Base):
    """One uploaded song."""

    __tablename__ = "projects"

    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    original_filename = Column(String, nullable=False)
    original_path = Column(String, nullable=False, default="")
    created_at = Column(DateTime(timezone=True), default=utcnow)

    owner = relationship("User", back_populates="projects")
    jobs = relationship("Job", back_populates="project", cascade="all, delete-orphan")


class Job(Base):
    """One stem-separation run for a project."""

    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True)
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
