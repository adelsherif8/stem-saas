import os

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from ..billing import can_create, quota
from ..database import get_db
from ..deps import get_current_user
from ..logging_conf import get_logger
from ..models import Job, JobStatus, Project, User
from ..schemas import ProjectOut, QuotaOut, UploadResponse
from ..storage import save_upload
from ..tasks import separate_stems

router = APIRouter(prefix="/projects", tags=["projects"])
logger = get_logger(__name__)

ALLOWED_EXT = {".mp3", ".wav", ".flac", ".m4a", ".ogg"}


@router.post("", response_model=UploadResponse, status_code=201)
def upload_song(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {ext}")

    if not can_create(db, user):
        raise HTTPException(
            status_code=402,
            detail="Monthly free-tier limit reached. Upgrade to Pro.",
        )

    project = Project(
        owner_id=user.id,
        name=file.filename,
        original_filename=file.filename,
    )
    db.add(project)
    db.commit()
    db.refresh(project)

    project.original_path = save_upload(project.id, file.filename, file.file)
    db.commit()

    job = Job(project_id=project.id, owner_id=user.id, status=JobStatus.queued)
    db.add(job)
    db.commit()
    db.refresh(job)

    async_result = separate_stems.delay(job.id)
    job.celery_task_id = async_result.id
    db.commit()
    db.refresh(job)

    logger.info(
        "song_uploaded",
        extra={"user_id": user.id, "project_id": project.id, "job_id": job.id},
    )
    return UploadResponse(project=project, job=job)


@router.get("", response_model=list[ProjectOut])
def list_projects(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    return (
        db.query(Project)
        .filter(Project.owner_id == user.id)
        .order_by(Project.created_at.desc())
        .all()
    )


@router.get("/quota", response_model=QuotaOut)
def get_quota(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    return quota(db, user)
