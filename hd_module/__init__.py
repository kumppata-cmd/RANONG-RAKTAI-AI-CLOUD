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
        return True
    except Exception as e:
        logger.error(f"Failed to register HD module: {e}")
        return False
