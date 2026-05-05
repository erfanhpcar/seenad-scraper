from __future__ import annotations

from app.celery_app import celery_app
from app.external_inspector import ExternalDbInspector


@celery_app.task(name="tasks.test_db_connection")
def test_db_connection() -> dict[str, object]:
    """
    Single lightweight check: can the worker reach the external SQL Server (Divar)?
    Run from Flower without enqueueing the full ingestion pipeline.
    """
    return ExternalDbInspector().ping_connection()
