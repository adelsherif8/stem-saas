import json
import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user
from ..models import Job, JobStatus, User
from ..schemas import JobOut, StemOut

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _get_owned_job(job_id: int, db: Session, user: User) -> Job:
    job = db.get(Job, job_id)
    if job is None or job.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.get("/{job_id}", response_model=JobOut)
def get_job(
    job_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return _get_owned_job(job_id, db, user)


@router.get("/{job_id}/stems", response_model=list[StemOut])
def list_stems(
    job_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    job = _get_owned_job(job_id, db, user)
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
    user: User = Depends(get_current_user),
):
    job = _get_owned_job(job_id, db, user)
    if job.status != JobStatus.completed:
        raise HTTPException(status_code=409, detail="Job not completed")
    stems = {s["name"]: s["path"] for s in json.loads(job.stems or "[]")}
    path = stems.get(stem_name)
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Stem not found")
    return FileResponse(path, filename=f"{stem_name}.wav", media_type="audio/wav")
