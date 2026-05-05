"""Loads all task modules so Celery autodiscover_tasks picks them up."""

import app.tasks.discovery  # noqa: F401
import app.tasks.ingestion  # noqa: F401
import app.tasks.mapping  # noqa: F401
import app.tasks.normalization  # noqa: F401
import app.tasks.test_connection  # noqa: F401
