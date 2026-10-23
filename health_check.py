#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Health Monitoring — RANONG-RAKTAI-AI 2.0

/health/live  — application process สามารถรับ request ได้หรือไม่
/health/ready — dependencies พร้อมใช้งานหรือไม่

กฎความปลอดภัย:
  - ไม่เปิดเผย credentials หรือ sensitive info
  - ใช้ HTTP status codes ที่เหมาะสม
  - ไม่ปิดระบบตรวจสอบ GCS เพื่อทำให้ /health ผ่านโดยหลอก ๆ
"""

from __future__ import annotations

import time
import logging
from datetime import datetime
from flask import Flask, jsonify

logger = logging.getLogger(__name__)

# In-memory health state
_health_state = {
    "started_at": None,
    "last_gcs_check": None,
    "gcs_available": None,
    "last_error": None,
}


def init_health(app_start_time: str | None = None):
    """Initialize health state at application startup."""
    _health_state["started_at"] = (
        app_start_time or datetime.now().isoformat(timespec="seconds")
    )


def register_health_routes(app: Flask, storage=None):
    """Register health check endpoints on Flask app.

    /health/live  — liveness: can process serve requests?
    /health/ready — readiness: are dependencies available?
    """

    @app.route("/health/live", methods=["GET"])
    def health_live():
        """Liveness probe.

        200 = process is alive and can serve requests
        503 = process is unhealthy
        """
        try:
            return jsonify({
                "status": "alive",
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "uptime_since": _health_state.get("started_at", "unknown"),
            }), 200
        except Exception:
            return jsonify({"status": "unhealthy"}), 503

    @app.route("/health/ready", methods=["GET"])
    def health_ready():
        """Readiness probe.

        200 = dependencies available, ready to accept traffic
        503 = dependencies unavailable

        ไม่เปิดเผย: credentials, bucket name, service account,
        internal paths, error details ที่อาจมีข้อมูลภายใน
        """
        checks = {}
        all_ready = True

        # Check GCS storage
        if storage is not None:
            gcs_ok = _check_storage(storage)
            checks["storage"] = "ok" if gcs_ok else "unavailable"
            if not gcs_ok:
                all_ready = False
        else:
            checks["storage"] = "not_configured"

        status_code = 200 if all_ready else 503
        status_text = "ready" if all_ready else "not_ready"

        return jsonify({
            "status": status_text,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "checks": checks,
        }), status_code

    # Legacy /health endpoint for backward compatibility
    @app.route("/health", methods=["GET"])
    def health_legacy():
        """Legacy health endpoint — redirect to /health/live."""
        return health_live()


def _check_storage(storage) -> bool:
    """ตรวจสอบว่า storage พร้อมใช้งาน.

    ไม่ fake ผล — ตรวจสอบจริง
    Cache result สั้น ๆ (30 วินาที) เพื่อไม่ให้ GCS โดน rate limit
    """
    now = time.time()
    last_check = _health_state.get("last_gcs_check")

    # Use cached result if recent (< 30 seconds)
    if last_check and (now - last_check) < 30:
        cached = _health_state.get("gcs_available")
        if cached is not None:
            return cached

    try:
        # ใช้ list_files เพื่อตรวจสอบว่าเชื่อมต่อได้
        # prefix ที่ไม่มีข้อมูลจริง — ไม่เปิดเผยข้อมูลผู้ป่วย
        storage.list_files("_health_check/")
        _health_state["gcs_available"] = True
        _health_state["last_gcs_check"] = now
        _health_state["last_error"] = None
        return True
    except Exception as e:
        _health_state["gcs_available"] = False
        _health_state["last_gcs_check"] = now
        # ไม่ log ข้อมูลภายในของ error
        _health_state["last_error"] = type(e).__name__
        logger.warning(f"Storage health check failed: {type(e).__name__}")
        return False


def get_health_summary() -> dict:
    """สรุปสถานะสำหรับ internal use (ไม่เปิดเผยภายนอก)."""
    return {
        "started_at": _health_state.get("started_at"),
        "gcs_available": _health_state.get("gcs_available"),
        "last_check": _health_state.get("last_gcs_check"),
    }
