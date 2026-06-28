# Stem SaaS — async audio-processing backend

A production-shaped FastAPI backend for an audio stem-separation SaaS. Upload a
song, a background worker splits it into **vocals / drums / bass / other**, and
you poll the job and download the stems. Free tier = 3 songs/month; Pro =
unlimited (billing flow included).

It's a prototype, but it's wired the way a real service is: an API process and a
separate **Celery worker** talking over **Redis**, **Postgres** for state, JWT
auth, per-user quotas, structured JSON logging, health checks, tests, and Docker.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/adelsherif8/stem-saas)

---

## Architecture

```
                 ┌─────────────┐      enqueue       ┌──────────────┐
   client ─────▶ │  FastAPI    │ ─────────────────▶ │    Redis     │
   (HTTP/JWT)    │  (api)      │     (broker)       │   broker     │
                 └─────┬───────┘                    └──────┬───────┘
                       │                                   │ consume
                       │ read/write                        ▼
                       │                            ┌──────────────┐
                       ▼                            │   Celery     │
                 ┌─────────────┐   read/write       │   worker     │
                 │  Postgres   │ ◀───────────────── │ (separation) │
                 │ users/      │                    └──────┬───────┘
                 │ projects/   │                           │ writes stems
                 │ jobs        │                           ▼
                 └─────────────┘                    ┌──────────────┐
                                                    │   storage    │
                                                    │ (local disk; │
                                                    │  swap for S3)│
                                                    └──────────────┘
```

The client uploads → API creates a `Project` + `Job` and enqueues a Celery task
→ the worker sets the job to `processing`, writes progress, produces stems, and
marks it `completed` → the client polls `GET /jobs/{id}` and downloads stems.

---

## Quickstart

### Option A — Docker (full stack: Postgres + Redis + API + worker)

```bash
docker compose up --build
# API:  http://localhost:8000
# Docs: http://localhost:8000/docs
```

In another terminal, run the scripted end-to-end demo:

```bash
pip install requests
python scripts/demo.py
```

### Option B — Local, no broker (single process, eager mode)

Runs jobs synchronously in the API process — zero infra, great for a quick look:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
CELERY_EAGER=true uvicorn app.main:app --reload
```

### Option C — Local with a real worker

```bash
# terminal 1
redis-server
# terminal 2
celery -A app.celery_app.celery_app worker --loglevel=info
# terminal 3
uvicorn app.main:app --reload
```

---

## API

| Method | Path                         | Description                          |
|--------|------------------------------|--------------------------------------|
| POST   | `/auth/register`             | Create an account                    |
| POST   | `/auth/login`                | Get a JWT (OAuth2 password flow)     |
| GET    | `/auth/me`                   | Current user                         |
| POST   | `/projects`                  | Upload a song → returns project+job  |
| GET    | `/projects`                  | List your projects                   |
| GET    | `/projects/quota`            | Current usage vs. tier limit         |
| GET    | `/jobs/{id}`                 | Job status + progress                |
| GET    | `/jobs/{id}/stems`           | List produced stems                  |
| GET    | `/jobs/{id}/stems/{name}`    | Download one stem (.wav)             |
| POST   | `/billing/checkout`          | Start (mock) Stripe checkout         |
| POST   | `/billing/webhook`           | (Mock) Stripe webhook → upgrade      |
| POST   | `/billing/mock-pay`          | Demo shortcut: upgrade to Pro        |
| GET    | `/health`                    | DB/Redis/worker health               |

### curl walkthrough

```bash
curl -X POST localhost:8000/auth/register -H 'Content-Type: application/json' \
  -d '{"email":"me@example.com","password":"secret123"}'

TOKEN=$(curl -s -X POST localhost:8000/auth/login \
  -d 'username=me@example.com&password=secret123' | jq -r .access_token)

JOB=$(curl -s -X POST localhost:8000/projects \
  -H "Authorization: Bearer $TOKEN" -F 'file=@song.mp3' | jq .job.id)

curl -s localhost:8000/jobs/$JOB -H "Authorization: Bearer $TOKEN"
curl -s localhost:8000/jobs/$JOB/stems -H "Authorization: Bearer $TOKEN"
curl -s localhost:8000/jobs/$JOB/stems/vocals -H "Authorization: Bearer $TOKEN" -o vocals.wav
```

---

## Tests

```bash
pip install -r requirements.txt
pytest -q
```

Tests run in eager mode against a throwaway SQLite DB and temp storage — no
Redis/Postgres needed. They cover auth, the full upload→job→download flow, file
validation, auth enforcement, and free-tier quota + upgrade.

---

## Configuration

All via environment variables (see `.env.example`). Key flags:

| Var                       | Default                  | Notes                                           |
|---------------------------|--------------------------|-------------------------------------------------|
| `DATABASE_URL`            | `sqlite:///./stemsaas.db`| Postgres in Docker                              |
| `REDIS_URL`               | `redis://localhost:6379/0`| Celery broker + result backend                 |
| `CELERY_EAGER`            | `false`                  | `true` = run jobs in-process, no broker         |
| `DEMUCS_REAL`             | `false`                  | `true` = real HT-Demucs (see "Running real stem separation") |
| `FREE_TIER_MONTHLY_LIMIT` | `3`                      | Songs/month on the free tier                    |
| `JWT_SECRET`              | `dev-secret-change-me`   | **Change in production**                         |

### Mock vs. real

- **Stem separation** — mock mode writes valid placeholder WAV stems instantly so
  the demo runs anywhere. Set `DEMUCS_REAL=true` to run actual HT-Demucs; only
  `app/tasks.py::_real_separate` changes, the rest of the system is identical.
- **Storage** — local disk behind `app/storage.py`. Swap those four functions for
  boto3 `put_object` / presigned URLs to move to S3; nothing else changes.
- **Billing** — mock Stripe endpoints with the real shape (checkout session +
  webhook + `client_reference_id`). Drop in the `stripe` SDK to go live.

### Running real stem separation (Demucs)

Mock mode runs anywhere (the hosted demo uses it — real Demucs needs ~2 GB +
several GB of RAM, more than a free cloud box). To actually separate audio,
run it **locally**:

```bash
# install the ML extras on top of the base deps (~2 GB; needs ffmpeg installed)
pip install -r requirements.txt -r requirements-ml.txt

# run a job synchronously with real HT-Demucs (no broker needed)
CELERY_EAGER=true DEMUCS_REAL=true uvicorn app.main:app
```

Upload a song (or hit **Try a sample track**) and the worker runs the real
`htdemucs` model — a 30s clip separates in ~15 s on CPU. The first run downloads
the model (~80 MB). Only `app/tasks.py::_real_separate` differs from mock; the
queue, DB, API, and UI are identical. To run real Demucs in the cloud, deploy
the `worker` service on an instance with enough RAM.

The bundled sample track is *"Homesick"* by **kizzylotus** (ccMixter,
[CC BY 3.0](http://creativecommons.org/licenses/by/3.0/)).

---

## What this demonstrates

- **FastAPI** REST API with JWT auth (OAuth2 password flow) and Pydantic schemas
- **Async task queue** (Celery + Redis) with real **job status + progress tracking**
- **Postgres + SQLAlchemy** ORM modelling users / projects / jobs
- **Large-file upload** handling and an **ML/audio pipeline** seam (Demucs)
- **Stripe-shaped subscription billing** with tiered **quota enforcement**
- **Structured JSON logging**, request IDs, and a **health check**
- **Docker Compose** multi-service deploy + a **pytest** suite

## Next steps (production hardening)

- Alembic migrations (currently `create_all` on startup)
- Real S3 + presigned download URLs; real Stripe SDK + signature verification
- Rate limiting, refresh tokens, and per-job retry/backoff
- Object-storage-backed stem cleanup + lifecycle policies
