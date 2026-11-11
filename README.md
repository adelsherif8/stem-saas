# Stem SaaS — async audio-processing backend

A production-shaped FastAPI backend for an audio stem-separation SaaS. Upload a
song, a background worker splits it into **vocals / drums / bass / other**, and
you poll the job and download the stems. Free tier = 3 songs/month; Pro =
unlimited (billing flow included).

It's a prototype, but it's wired the way a real service is: an API process and a
separate **Celery worker** talking over **Redis**, **Postgres** for state,
**multi-tenant workspaces with role-based access control**, email/password +
**Google/GitHub OAuth2** login, **scoped API keys**, an **audit log**,
**Postgres row-level security**, **Alembic** migrations, per-workspace quotas,
structured JSON logging, health checks, tests, and Docker.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/adelsherif8/stem-saas)

---

## Architecture

```
                 ┌─────────────┐      enqueue       ┌──────────────┐
   client ─────▶ │  FastAPI    │ ─────────────────▶ │    Redis     │
 (JWT / sk_ key) │  (api)      │     (broker)       │   broker     │
                 │ authn+authz │                    └──────┬───────┘
                 └─────┬───────┘                           │
                       │                                   │ consume
                       │ read/write (RLS-pinned)           ▼
                       │                            ┌──────────────┐
                       ▼                            │   Celery     │
                 ┌─────────────┐   read/write       │   worker     │
                 │  Postgres   │ ◀───────────────── │ (separation) │
                 │ users,      │                    └──────┬───────┘
                 │ workspaces, │                           │ writes stems
                 │ memberships,│                           ▼
                 │ projects,   │                    ┌──────────────┐
                 │ jobs, keys, │                    │   storage    │
                 │ audit_logs  │                    │ (local disk; │
                 └─────────────┘                    │  swap for S3)│
                                                    └──────────────┘
```

The client uploads → API creates a `Project` + `Job` and enqueues a Celery task
→ the worker sets the job to `processing`, writes progress, produces stems, and
marks it `completed` → the client polls `GET /jobs/{id}` and downloads stems.

Every request goes through two steps before it touches data:

1. **Authentication** (`app/deps.py::get_principal`): a JWT (from password or
   OAuth login) identifies a *user*; an `sk_...` key identifies an *API key*.
2. **Authorization** (`app/deps.py::authorize`): the endpoint names an action
   (`projects:create`, `keys:manage`, ...). Users need a minimum role in the
   active workspace; keys need the matching scope. On Postgres the DB session
   is then pinned to that workspace with row-level security, so the database
   itself refuses cross-tenant reads and writes.

Code map:

| Path | Role |
|------|------|
| `app/permissions.py` | The action -> (minimum role, API-key scope) table. One source of truth. |
| `app/deps.py` | Principal resolution (JWT / API key) and `require(...)` dependencies |
| `app/oauth.py`, `app/routers/oauth.py` | Google/GitHub authorization-code flow with PKCE |
| `app/routers/workspaces.py` | Workspaces, members, ownership transfer, API keys, audit log |
| `app/database.py` | Engine/session plus the RLS session pinning |
| `migrations/` | Alembic: `0001` baseline, `0002` workspaces/RBAC + data backfill, `0003` Postgres RLS |

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

Data endpoints act in the workspace given by the `X-Workspace-ID` header
(default: your personal workspace). API keys always act in their own workspace.

| Method | Path                         | Description                          |
|--------|------------------------------|--------------------------------------|
| POST   | `/auth/register`             | Create an account (+ personal workspace) |
| POST   | `/auth/login`                | Get a JWT (OAuth2 password flow)     |
| GET    | `/auth/me`                   | Current user                         |
| GET    | `/auth/oauth/providers`      | Which social logins are enabled      |
| GET    | `/auth/oauth/{google,github}/authorize` | Start social login (redirect) |
| GET    | `/auth/oauth/{google,github}/callback`  | Provider callback -> JWT      |
| POST   | `/projects`                  | Upload a song → returns project+job  |
| GET    | `/projects`                  | List the workspace's projects        |
| GET    | `/projects/quota`            | Workspace usage vs. plan limit       |
| GET    | `/jobs/{id}`                 | Job status + progress                |
| GET    | `/jobs/{id}/stems`           | List produced stems                  |
| GET    | `/jobs/{id}/stems/{name}`    | Download one stem (.wav)             |
| POST   | `/billing/checkout`          | Start (mock) Stripe checkout         |
| POST   | `/billing/webhook`           | (Mock) Stripe webhook → upgrade      |
| POST   | `/billing/mock-pay`          | Demo shortcut: upgrade to Pro        |
| GET    | `/workspaces`                | Workspaces you belong to, with your role |
| POST   | `/workspaces`                | Create a team workspace (you = owner) |
| GET/PATCH/DELETE | `/workspaces/{id}` | View / rename / delete              |
| POST   | `/workspaces/{id}/transfer`  | Transfer ownership to a member       |
| GET/POST | `/workspaces/{id}/members` | List / add members                   |
| PATCH/DELETE | `/workspaces/{id}/members/{user_id}` | Change role / remove   |
| GET/POST | `/workspaces/{id}/api-keys` | List / create API keys              |
| DELETE | `/workspaces/{id}/api-keys/{key_id}` | Revoke a key                 |
| POST   | `/workspaces/{id}/api-keys/{key_id}/rotate` | Rotate a key's secret |
| GET    | `/workspaces/{id}/audit`     | Audit log (filter with `?action=`)   |
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

## Auth & permissions

### Workspaces and roles

A **workspace** is the tenant boundary: projects, jobs, API keys, the plan and
the monthly quota all belong to one. Every user gets a **personal workspace**
on sign-up (they are its owner; it cannot be deleted or shared) and can create
any number of team workspaces and invite existing users into them.

| Action (`app/permissions.py`) | What it covers | member | admin | owner | API key scope |
|---|---|:-:|:-:|:-:|---|
| `projects:create` | Upload a song / start a job | yes | yes | yes | `jobs:write` |
| `projects:read` | List projects, read quota | yes | yes | yes | `jobs:read` |
| `jobs:read` | Job status and progress | yes | yes | yes | `jobs:read` |
| `stems:read` | List and download stems | yes | yes | yes | `stems:read` |
| `workspace:read` | View workspace and members | yes | yes | yes | not allowed |
| `workspace:update` | Rename workspace | - | yes | yes | not allowed |
| `members:manage` | Add/remove members, change roles | - | yes | yes | not allowed |
| `keys:manage` | Create/list/revoke/rotate API keys | - | yes | yes | not allowed |
| `audit:read` | Read the audit log | - | yes | yes | not allowed |
| `billing:manage` | Upgrade/cancel the plan | - | - | yes | not allowed |
| `workspace:transfer` | Transfer ownership | - | - | yes | not allowed |
| `workspace:delete` | Delete the workspace | - | - | yes | not allowed |

Rules on top of the matrix:

- Non-members get **404**, not 403, so workspace and job ids cannot be probed.
- Exactly one owner per workspace, enforced by a partial unique index. The
  owner cannot be removed or demoted; ownership moves only via
  `POST /workspaces/{id}/transfer` (the old owner becomes admin).
- Roles are set to `member` or `admin` only; `owner` is never assignable directly.
- Workspace administration is for signed-in humans; API keys are data-plane only.

The matrix above is asserted in `tests/test_rbac.py` against both
`app/permissions.py` and real HTTP calls for every role x action.

### API keys

```bash
# as an admin/owner of workspace 7
curl -X POST localhost:8000/workspaces/7/api-keys -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"name":"ci","scopes":["jobs:write","jobs:read"]}'
# -> {"id": 1, "prefix": "sk_AbC123x", "secret": "sk_AbC123x...", ...}   (secret shown once)

curl localhost:8000/projects -H "Authorization: Bearer sk_..."   # or: -H "X-API-Key: sk_..."
```

- Secrets are `sk_` + 256 random bits. Only a SHA-256 hash and a short display
  prefix are stored (a slow KDF is unnecessary for high-entropy random secrets
  and would slow every request).
- Scopes: `jobs:read`, `jobs:write`, `stems:read`. Missing scope -> 403.
- `last_used_at` is updated on every authenticated request.
- Revoke (`DELETE .../api-keys/{id}`) is immediate; rotate issues a new secret
  for the same key id and scopes and invalidates the old one at once.

### Social login (Google and GitHub)

Authorization-code flow with **PKCE (S256)** and a `state` that is single-use,
expires after 10 minutes, and must match an HttpOnly cookie set on the same
browser (login-CSRF defence). Tokens are exchanged server-side; only a
**verified** email is trusted. An identity is matched by provider user id first,
then linked to an existing account by verified email, otherwise a new user and
personal workspace are created. The browser is redirected to
`OAUTH_SUCCESS_REDIRECT#access_token=<jwt>` (a fragment, so it never reaches
server logs).

If a provider's client id/secret are not set, its endpoints answer
`503 "Google login is not configured on this server (set GOOGLE_CLIENT_ID and
GOOGLE_CLIENT_SECRET)."` and the web UI hides the button.

Setup:

| Provider | Where to create the app | Callback URL to register |
|---|---|---|
| Google | Google Cloud Console -> APIs & Services -> Credentials -> Create credentials -> OAuth client ID -> *Web application* (configure the OAuth consent screen first) | `http://localhost:8000/auth/oauth/google/callback` and `https://<your-host>/auth/oauth/google/callback` |
| GitHub | GitHub -> Settings -> Developer settings -> OAuth Apps -> New OAuth App | `https://<your-host>/auth/oauth/github/callback` (one callback per app; create a second app for localhost) |

Then set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GITHUB_CLIENT_ID`,
`GITHUB_CLIENT_SECRET` and `OAUTH_REDIRECT_BASE_URL` (the public base URL the
callbacks are built from).

### Audit log

`GET /workspaces/{id}/audit` (admin+) returns who did what, when and from which
IP: `auth.login`, `auth.login_failed` (recorded in the user's personal
workspace), `workspace.create/rename/transfer/delete`, `member.add/remove/
role_change`, `key.create/revoke/rotate`, `billing.upgrade/cancel`. Entries are
written in the same transaction as the change they describe.
`audit_logs.workspace_id` has no foreign key on purpose, so the record of a
workspace's deletion outlives the workspace. Secrets are never logged.

### Row-level security (Postgres)

Migration `0003` enables RLS on `projects`, `jobs`, `api_keys` and
`audit_logs` with one policy each:

```sql
USING      (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::integer)
WITH CHECK (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::integer)
```

It also creates a `NOLOGIN` role `stemsaas_tenant` granted to the app user.
After a request is authorized for a workspace, the app runs `SET LOCAL ROLE
stemsaas_tenant` and sets `app.workspace_id` for the transaction (re-applied at
the start of every transaction by a SQLAlchemy `after_begin` hook). With the
setting missing the policy matches nothing, so it fails closed. The table
owner (migrations, the Celery worker, login and API-key lookup) is not subject
to the policies. `memberships` is deliberately not under RLS: it is the table
the app reads to decide which workspace to pin.

If the database user is not allowed to create roles, the migration logs a
NOTICE and the app detects that it cannot switch role (`RLS_ENFORCE=auto`); the
application-level checks still apply. On SQLite the migration is a no-op.

### Migrations

Alembic owns the schema. The API runs `alembic upgrade head` on start
(`AUTO_MIGRATE=true`); databases created before Alembic are stamped at `0001`
first. `0002` gives every existing user a personal workspace, moves their tier
onto it, and moves their projects and jobs into it. Downgrades are implemented.

```bash
alembic upgrade head      # or let the API do it on boot
alembic downgrade 0001_initial
```

**Quota decision:** quotas moved to the workspace. A team on Pro shares one
unlimited plan; a free workspace's 3 songs/month are shared by its members.
Solo users see no change because their personal workspace inherits their old
tier.

---

## Tests

```bash
pip install -r requirements.txt
pytest -q
```

Tests run in eager mode against a throwaway SQLite DB and temp storage — no
Redis/Postgres needed. They cover auth, the full upload→job→download flow, file
validation, auth enforcement, free-tier quota + upgrade, and:

- `test_rbac.py`: every role (owner, admin, member, outsider) x every protected action
- `test_api_keys.py`: secret-once/hash-only storage, a scope x endpoint table, revoke, rotate, `last_used_at`
- `test_isolation.py`: cross-workspace reads/writes, per-workspace quota, audit scoping
- `test_oauth.py`: Google/GitHub against a mocked provider (`httpx.MockTransport`), PKCE, state handling, email linking
- `test_migrations.py`: backfill of legacy data, downgrade round-trip, pre-Alembic stamping
- `test_rls.py`: Postgres RLS isolation (needs a real Postgres, skipped otherwise)

Postgres runs (the target database's `public` schema is dropped and recreated):

```bash
# RLS policy tests
TEST_POSTGRES_URL=postgresql://postgres@localhost:5432/stemsaas_rls pytest tests/test_rls.py
# the whole suite on Postgres, with RLS session pinning active
TEST_DATABASE_URL=postgresql://postgres@localhost:5432/stemsaas_test pytest
```

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
| `AUTO_MIGRATE`            | `true`                   | Run `alembic upgrade head` on API start         |
| `RLS_ENFORCE`             | `auto`                   | Postgres RLS role switching: `auto`/`on`/`off`  |
| `OAUTH_REDIRECT_BASE_URL` | `http://localhost:8000`  | Public base URL for OAuth callbacks             |
| `OAUTH_SUCCESS_REDIRECT`  | `/`                      | Where the browser lands after social login      |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | empty  | Empty = Google login disabled (503)          |
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` | empty  | Empty = GitHub login disabled (503)          |

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
- **OAuth2 social login** (Google, GitHub): authorization code + PKCE, state bound to the browser
- **Multi-tenant RBAC**: workspaces, owner/admin/member roles, one permission table, full matrix tests
- **Scoped API keys** with hashed storage, rotation, revocation and usage tracking
- **Audit logging** of security-relevant events
- **Postgres row-level security** as a database-enforced second line of tenant isolation
- **Alembic migrations** including a data migration of existing users
- **Async task queue** (Celery + Redis) with real **job status + progress tracking**
- **Postgres + SQLAlchemy** ORM modelling users / projects / jobs
- **Large-file upload** handling and an **ML/audio pipeline** seam (Demucs)
- **Stripe-shaped subscription billing** with tiered **quota enforcement**
- **Structured JSON logging**, request IDs, and a **health check**
- **Docker Compose** multi-service deploy + a **pytest** suite

## Next steps (production hardening)

- Email invitations for users without an account; SCIM 2.0 provisioning
- Refresh tokens / session revocation for JWTs
- Real S3 + presigned download URLs; real Stripe SDK + signature verification
- Rate limiting (login, API keys) and per-job retry/backoff
- Object-storage-backed stem cleanup + lifecycle policies

---

Built by **Adel Atya**, AI engineer: AI voice agents, agentic systems and automation for businesses. [adelatya.com](https://adelatya.com)
