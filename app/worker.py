"""CLI entry for `celery -A app.worker worker --loglevel=info`."""

from app.celery_app import celery_app

celery = celery_app
