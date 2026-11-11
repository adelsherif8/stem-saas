from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserCreate(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    tier: str
    created_at: datetime


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    original_filename: str
    created_at: datetime


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    status: str
    progress: int
    error: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class StemOut(BaseModel):
    name: str
    path: str


class UploadResponse(BaseModel):
    project: ProjectOut
    job: JobOut


class QuotaOut(BaseModel):
    tier: str
    used_this_month: int
    limit: Optional[int] = None  # None == unlimited (pro)
    remaining: Optional[int] = None


# --- workspaces / RBAC ------------------------------------------------------

class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class WorkspaceUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class WorkspaceOut(BaseModel):
    id: int
    name: str
    personal: bool
    tier: str
    role: str  # the caller's role in this workspace
    created_at: datetime


class MemberAdd(BaseModel):
    email: EmailStr
    role: Literal["admin", "member"] = "member"


class MemberRoleUpdate(BaseModel):
    role: Literal["admin", "member"]


class MemberOut(BaseModel):
    user_id: int
    email: EmailStr
    role: str
    created_at: datetime


class OwnershipTransfer(BaseModel):
    user_id: int


# --- API keys ---------------------------------------------------------------

class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[str] = Field(min_length=1)


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    workspace_id: int
    name: str
    prefix: str
    scopes: list[str]
    created_by_id: int
    created_at: datetime
    last_used_at: Optional[datetime] = None
    rotated_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None


class ApiKeyWithSecret(ApiKeyOut):
    secret: str  # shown exactly once


# --- audit ------------------------------------------------------------------

class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    workspace_id: Optional[int] = None
    actor_user_id: Optional[int] = None
    actor_api_key_id: Optional[int] = None
    action: str
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    details: Optional[dict] = None
    ip: Optional[str] = None
    created_at: datetime
