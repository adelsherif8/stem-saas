import json
import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import WorkspaceContext, require
from ..models import Job, JobStatus
from ..schemas import JobOut, StemOut

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _get_workspace_job(job_id: int, db: Session, ctx: WorkspaceContext) -> Job:
    job = db.get(Job, job_id)
    # Jobs in other workspaces are indistinguishable from missing ones.
    if job is None or job.workspace_id != ctx.workspace.id:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.get("/{job_id}", response_model=JobOut)
def get_job(
    job_id: int,
    db: Session = Depends(get_db),
    ctx: WorkspaceContext = Depends(require("jobs:read")),
):
    return _get_workspace_job(job_id, db, ctx)


@router.get("/{job_id}/stems", response_model=list[StemOut])
def list_stems(
    job_id: int,
    db: Session = Depends(get_db),
    ctx: WorkspaceContext = Depends(require("stems:read")),
):
    job = _get_workspace_job(job_id, db, ctx)
    if job.status != JobStatus.completed:
        raise HTTPException(
            status_code=409,
            detail=f"Job not completed (status={job.status.value})",
        )
    return [StemOut(**s) for s in json.loads(job.stems or "[]")]


@router.get("/{job_id}/stems/{stem_name}")
def download_stem(
    job_id: int,
    stem_name: str,
    db: Session = Depends(get_db),
    ctx: WorkspaceContext = Depends(require("stems:read")),
):
    job = _get_workspace_job(job_id, db, ctx)
    if job.status != JobStatus.completed:
        raise HTTPException(status_code=409, detail="Job not completed")
    stems = {s["name"]: s["path"] for s in json.loads(job.stems or "[]")}
    path = stems.get(stem_name)
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Stem not found")
    return FileResponse(path, filename=f"{stem_name}.wav", media_type="audio/wav")
