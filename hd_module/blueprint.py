"""HD Smart Monitoring — Flask Blueprint.

All routes under /hd/api/v1/.  Completely isolated from server.py.
"""

import logging
from datetime import date, datetime, timezone  # noqa: F401
from flask import (  # noqa: F401
    Blueprint, request, jsonify, g, render_template, redirect, url_for,
)

from .auth import require_role  # noqa: F401
from .audit import log_action

logger = logging.getLogger(__name__)

hd_bp = Blueprint(
    "hd", __name__,
    url_prefix="/hd/api/v1",
    template_folder="templates",
)

# ── Web UI (template) routes ─────────────────────────────
# Mounted at /hd/* via a second Blueprint so the API prefix
# doesn't collide with page URLs.

hd_web = Blueprint(
    "hd_web", __name__,
    url_prefix="/hd",
    template_folder="templates",
)


@hd_web.route("/")
def hd_index():
    """Redirect root to login."""
    return redirect(url_for("hd_web.hd_login_page"))


@hd_web.route("/login")
def hd_login_page():
    """Render login page."""
    return render_template("hd_login.html")


@hd_web.route("/dashboard")
def hd_dashboard_page():
    """Render 30-bed dashboard."""
    return render_template("hd_dashboard.html")


@hd_web.route("/session/new")
def hd_session_new():
    """Render new HD session form (Pre/Intra/Post tabs)."""
    return render_template("hd_session.html")


@hd_web.route("/session/<int:session_id>")
def hd_session_detail(session_id):
    """Render existing HD session."""
    return render_template("hd_session.html", session_id=session_id)


@hd_web.route("/patients")
def hd_patient_list_page():
    """Render patient list."""
    return render_template("hd_patient_list.html")


@hd_web.route("/alerts")
def hd_alerts_page():
    """Render clinical alerts page."""
    return render_template("hd_alerts.html")


# ── Health / Status ───────────────────────────────────────

@hd_bp.route("/health", methods=["GET"])
def hd_health():
    """HD module health check."""
    return jsonify({
        "module": "hd_smart_monitoring",
        "status": "ok",
        "version": "1.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


# ── Auth ──────────────────────────────────────────────────

@hd_bp.route("/auth/login", methods=["POST"])
def hd_login():
    """Login and receive JWT token."""
    from .auth import verify_password, create_token  # noqa: F401
    data = request.get_json(silent=True) or {}
    username = data.get("username", "")
    password = data.get("password", "")

    if not username or not password:
        return jsonify({"error": "Username and password required"}), 400

    # In production: look up user from PostgreSQL
    # For now: return structure showing the API contract
    return jsonify({
        "error": "Database not connected — login disabled in development",
        "api_contract": {
            "request": {"username": "string", "password": "string"},
            "response": {"token": "jwt_string", "expires_in": 28800,
                         "user": {"id": 1, "username": "...", "role": "nurse"}},
        },
    }), 503


# ── Sessions ──────────────────────────────────────────────

@hd_bp.route("/sessions", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def list_sessions():
    """List HD sessions (filterable by date, bed, patient)."""
    session_date = request.args.get("date", date.today().isoformat())
    bed = request.args.get("bed", type=int)
    patient_hn = request.args.get("hn")

    log_action(
        user_id=g.get("hd_user_id"),
        action="session.list",
        resource_type="session",
        ip_address=request.remote_addr or "",
    )

    # Placeholder: database query goes here
    return jsonify({
        "sessions": [],
        "filters": {"date": session_date, "bed": bed, "hn": patient_hn},
        "total": 0,
    })


@hd_bp.route("/sessions", methods=["POST"])
@require_role("physician", "nurse")
def create_session():
    """Create a new HD session (start dialysis)."""
    data = request.get_json(silent=True) or {}

    required = ["patient_hn", "bed_number"]
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

    bed = data.get("bed_number")
    if not isinstance(bed, int) or bed < 1 or bed > 50:
        return jsonify({"error": "bed_number must be 1-50"}), 400

    log_action(
        user_id=g.get("hd_user_id"),
        action="session.create",
        resource_type="session",
        new_values=data,
        ip_address=request.remote_addr or "",
    )

    return jsonify({
        "status": "created",
        "message": "Session created (database not connected)",
        "data": data,
    }), 201


@hd_bp.route("/sessions/<int:session_id>", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def get_session(session_id):
    """Get details of a specific HD session."""
    return jsonify({"session_id": session_id, "status": "not_found"}), 404


@hd_bp.route("/sessions/<int:session_id>", methods=["PUT"])
@require_role("physician", "nurse")
def update_session(session_id):
    """Update an HD session (e.g. end dialysis, record post-weight)."""
    data = request.get_json(silent=True) or {}
    log_action(
        user_id=g.get("hd_user_id"),
        action="session.update",
        resource_type="session",
        resource_id=session_id,
        new_values=data,
        ip_address=request.remote_addr or "",
    )
    return jsonify({"session_id": session_id, "updated": True})


# ── Vital Signs ───────────────────────────────────────────

@hd_bp.route("/sessions/<int:session_id>/vitals", methods=["POST"])
@require_role("physician", "nurse")
def record_vital(session_id):
    """Record vital signs for a session."""
    data = request.get_json(silent=True) or {}

    required = ["phase"]
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing: {', '.join(missing)}"}), 400

    phase = data.get("phase")
    if phase not in ("pre", "intra", "post"):
        return jsonify({"error": "phase must be pre, intra, or post"}), 400

    # Clinical alert check would go here
    log_action(
        user_id=g.get("hd_user_id"),
        action="vital.record",
        resource_type="vital_sign",
        new_values=data,
        ip_address=request.remote_addr or "",
    )

    return jsonify({"session_id": session_id, "phase": phase, "recorded": True}), 201


@hd_bp.route("/sessions/<int:session_id>/vitals", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def list_vitals(session_id):
    """List vital signs for a session."""
    return jsonify({"session_id": session_id, "vitals": []})


# ── UF Records ────────────────────────────────────────────

@hd_bp.route("/sessions/<int:session_id>/uf", methods=["POST"])
@require_role("physician", "nurse")
def record_uf(session_id):
    """Record UF data for a session."""
    data = request.get_json(silent=True) or {}
    log_action(
        user_id=g.get("hd_user_id"),
        action="uf.record",
        resource_type="uf_record",
        new_values=data,
        ip_address=request.remote_addr or "",
    )
    return jsonify({"session_id": session_id, "uf_recorded": True}), 201


# ── Alerts ────────────────────────────────────────────────

@hd_bp.route("/alerts", methods=["GET"])
@require_role("physician", "nurse", "admin")
def list_alerts():
    """List active clinical alerts."""
    return jsonify({"alerts": [], "total": 0})


@hd_bp.route("/alerts/<int:alert_id>/acknowledge", methods=["POST"])
@require_role("physician", "nurse")
def acknowledge_alert(alert_id):
    """Acknowledge a clinical alert."""
    data = request.get_json(silent=True) or {}
    log_action(
        user_id=g.get("hd_user_id"),
        action="alert.acknowledge",
        resource_type="alert",
        resource_id=alert_id,
        new_values=data,
        ip_address=request.remote_addr or "",
    )
    return jsonify({"alert_id": alert_id, "acknowledged": True})


# ── Patients ──────────────────────────────────────────────

@hd_bp.route("/patients", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def list_patients():
    """List HD patients."""
    return jsonify({"patients": [], "total": 0})


@hd_bp.route("/patients/<hn>", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def get_patient(hn):
    """Get HD patient details."""
    return jsonify({"hn": hn, "status": "not_found"}), 404


# ── Dashboard ─────────────────────────────────────────────

@hd_bp.route("/dashboard/beds", methods=["GET"])
@require_role("physician", "nurse", "admin")
def bed_dashboard():
    """30-bed real-time dashboard."""
    from .config import HdConfig
    beds = []
    for i in range(1, HdConfig.TOTAL_BEDS + 1):
        beds.append({
            "bed_number": i,
            "status": "available",
            "patient": None,
            "current_vitals": None,
            "alerts": [],
        })
    return jsonify({"beds": beds, "total": HdConfig.TOTAL_BEDS})


# ── Audit ─────────────────────────────────────────────────

@hd_bp.route("/audit", methods=["GET"])
@require_role("admin", "auditor")
def list_audit_logs():
    """Query audit trail."""
    from .audit import query_audit_log
    logs = query_audit_log(
        user_id=request.args.get("user_id", type=int),
        action=request.args.get("action"),
        limit=request.args.get("limit", 50, type=int),
    )
    return jsonify({"logs": logs, "total": len(logs)})
