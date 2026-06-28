from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr


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
