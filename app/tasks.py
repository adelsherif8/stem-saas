import json
import os
import struct
import time
import wave
from typing import Callable

from .celery_app import celery_app
from .config import settings
from .database import SessionLocal
from .logging_conf import get_logger
from .models import Job, JobStatus, Project
from .storage import stem_dir

logger = get_logger(__name__)


def _write_silence(path: str, seconds: float = 1.0, framerate: int = 44100) -> None:
    """Write a tiny valid mono WAV file (used as a stand-in stem in mock mode)."""
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(framerate)
        silence = struct.pack("<h", 0) * int(framerate * seconds)
        w.writeframes(silence)


def _mock_separate(out_dir: str, update: Callable[[int], None]) -> list[dict]:
    names = settings.stem_names
    stems = []
    for i, name in enumerate(names):
        path = os.path.join(out_dir, f"{name}.wav")
        _write_silence(path)
        stems.append({"name": name, "path": path})
        if not settings.celery_eager:
            time.sleep(0.5)  # simulate per-stem work for a realistic demo
        update(int((i + 1) / len(names) * 100))
    return stems


def _real_separate(
    original_path: str, out_dir: str, update: Callable[[int], None]
) -> list[dict]:
    import subprocess

    update(10)
    subprocess.run(
        ["demucs", "-n", "htdemucs", "-o", out_dir, original_path], check=True
    )
    stems = []
    for root, _, files in os.walk(out_dir):
        for fname in files:
            if fname.endswith(".wav"):
                name = os.path.splitext(fname)[0]
                stems.append({"name": name, "path": os.path.join(root, fname)})
    update(100)
    return stems


@celery_app.task(bind=True, name="separate_stems")
def separate_stems(self, job_id: int) -> dict:
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None:
            logger.warning("job_missing", extra={"job_id": job_id})
            return {"job_id": job_id, "status": "missing"}

        project = db.get(Project, job.project_id)
        job.status = JobStatus.processing
        job.progress = 0
        db.commit()
        logger.info("job_started", extra={"job_id": job_id})

        out_dir = stem_dir(job_id)

        def update(progress: int) -> None:
            job.progress = progress
            db.commit()
            logger.info("job_progress", extra={"job_id": job_id, "progress": progress})

        if settings.demucs_real:
            stems = _real_separate(project.original_path, out_dir, update)
        else:
            stems = _mock_separate(out_dir, update)

        job.stems = json.dumps(stems)
        job.status = JobStatus.completed
        job.progress = 100
        db.commit()
        logger.info("job_completed", extra={"job_id": job_id, "stems": len(stems)})
        return {"job_id": job_id, "status": "completed", "stems": len(stems)}
    except Exception as exc:  # noqa: BLE001 - we want to record any failure
        logger.exception("job_failed", extra={"job_id": job_id})
        job = db.get(Job, job_id)
        if job is not None:
            job.status = JobStatus.failed
            job.error = str(exc)
            db.commit()
        raise
    finally:
        db.close()
