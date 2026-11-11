import os
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from .config import settings
from .database import SessionLocal
from .logging_conf import get_logger, setup_logging
from .routers import auth, billing, jobs, projects, workspaces

setup_logging()
logger = get_logger("app")

# Schema is owned by Alembic (migrations/). Apply pending migrations on boot so
# a fresh deploy (or a pre-Alembic database) comes up at head. Set
# AUTO_MIGRATE=false to run `alembic upgrade head` as a separate release step.
if settings.auto_migrate:
    from .migrate import upgrade_to_head

    upgrade_to_head()

app = FastAPI(title=settings.app_name, version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_logging(request: Request, call_next):
    request_id = uuid.uuid4().hex[:8]
    start = time.time()
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "request",
        extra={
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "ms": round((time.time() - start) * 1000, 1),
        },
    )
    return response


STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def root():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/health", tags=["meta"])
def health():
    db_ok = True
    try:
        session = SessionLocal()
        session.execute(text("SELECT 1"))
        session.close()
    except Exception:
        db_ok = False

    redis_ok = False
    try:
        import redis

        redis_ok = bool(redis.from_url(settings.redis_url).ping())
    except Exception:
        redis_ok = False

    return {
        "status": "ok" if db_ok else "degraded",
        "db": db_ok,
        "redis": redis_ok,
        "celery_eager": settings.celery_eager,
        "demucs_real": settings.demucs_real,
    }


app.include_router(auth.router)
app.include_router(projects.router)
app.include_router(jobs.router)
app.include_router(billing.router)
app.include_router(workspaces.router)
