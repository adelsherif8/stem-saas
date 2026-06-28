#!/usr/bin/env sh
# Railway runs the API and the Celery worker co-located in one service.
# (docker-compose.yml shows the "proper" split into separate containers.)
# Both talk over the managed Redis broker, so this is still real async — not eager mode.
set -e

celery -A app.celery_app.celery_app worker --loglevel=info --concurrency=2 &

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
