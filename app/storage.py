import os
import shutil
from typing import BinaryIO

from .config import settings

# Local-disk storage abstraction. Swap these four functions for boto3 S3 calls
# (put_object / generate_presigned_url) to go to real object storage — the rest
# of the app only talks to this module.


def _ensure_parent(path: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def upload_path(project_id: int, filename: str) -> str:
    return _ensure_parent(
        os.path.join(settings.storage_dir, "uploads", str(project_id), filename)
    )


def stem_dir(job_id: int) -> str:
    path = os.path.join(settings.storage_dir, "stems", str(job_id))
    os.makedirs(path, exist_ok=True)
    return path


def save_upload(project_id: int, filename: str, fileobj: BinaryIO) -> str:
    dest = upload_path(project_id, filename)
    with open(dest, "wb") as out:
        shutil.copyfileobj(fileobj, out)
    return dest
