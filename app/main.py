import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from sqlalchemy import text

from .config import settings
from .database import Base, SessionLocal, engine
from .logging_conf import get_logger, setup_logging
from .routers import auth, billing, jobs, projects

setup_logging()
logger = get_logger("app")

# Prototype-friendly: create tables on startup. Swap for Alembic migrations
# before anything resembling production.
Base.metadata.create_all(bind=engine)

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


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


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
