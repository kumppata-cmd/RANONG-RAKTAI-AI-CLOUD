"""HD Smart Monitoring Module — Flask Blueprint Registration.

Feature Flag: HD_MODULE_ENABLED (env var, default "false")
When disabled, the Blueprint is NOT registered and the entire
HD module has zero impact on the existing application.
"""

import logging
import os

logger = logging.getLogger(__name__)

HD_MODULE_ENABLED = os.environ.get("HD_MODULE_ENABLED", "false").lower() == "true"


def register_hd_module(app):
    """Register the HD Smart Monitoring Blueprint with a Flask app.

    Does nothing if HD_MODULE_ENABLED is false.
    This is the ONLY integration point with server.py.

    Usage in server.py (1 line):
        from hd_module import register_hd_module
        register_hd_module(app)
    """
    if not HD_MODULE_ENABLED:
        logger.info("HD Smart Monitoring module is DISABLED (HD_MODULE_ENABLED=false)")
        return False

    try:
        from .blueprint import hd_bp, hd_web
        app.register_blueprint(hd_bp)
        app.register_blueprint(hd_web)
        logger.info("HD Smart Monitoring module registered at /hd/api/v1/ + /hd/ (web)")

        # Auto-run database migrations (idempotent — CREATE IF NOT EXISTS)
        _run_hd_migrations()

        return True
    except Exception as e:
        logger.error(f"Failed to register HD module: {e}")
        return False


def _run_hd_migrations():
    """Run HD database migrations if PostgreSQL is available.

    Uses psycopg2 directly (same as database.py) — no SQLAlchemy needed.
    Safe to run every boot: all CREATE statements use IF NOT EXISTS.
    """
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        logger.info("HD migrations: skipped — no database URL configured")
        return

    try:
        import psycopg2
    except ImportError:
        logger.warning("HD migrations: skipped — psycopg2 not available")
        return

    try:
        from .migrations import MIGRATION_UP, SCHEMA_VERSION
        conn = psycopg2.connect(db_url, connect_timeout=10)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(MIGRATION_UP)
        conn.close()
        logger.info("HD migrations: applied schema v%s successfully", SCHEMA_VERSION)
    except Exception as e:
        logger.error("HD migrations: failed — %s", e)
