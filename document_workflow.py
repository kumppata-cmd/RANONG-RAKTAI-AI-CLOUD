#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Document Workflow — Review & Approval System
RANONG RAKTAI AI 2.0

Feature 7: ระบบ Review & Approval สำหรับเอกสาร

ฟีเจอร์:
  - Workflow states: RECEIVED → CLASSIFIED → AWAITING_REVIEW → APPROVED/REJECTED
  - Quarantine at any stage
  - Assignment of reviewers
  - Full audit trail for every transition
  - Thai-language UI with dark theme

กฎความปลอดภัย:
  - ใช้ hashed IDs เท่านั้น — ไม่แสดง PHI
  - ต้องมี ADMIN_SECRET ทุก endpoint
  - ไม่ส่งข้อมูลผู้ป่วยไป External AI
  - Audit ทุก state transition
  - Fail-closed: ถ้า audit ล้มเหลวสำหรับ security-critical actions → reject transition
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)

workflow_bp = Blueprint("document_workflow", __name__)

# Module-level refs — set by register_workflow()
_storage = None
_doc_index = None
_audit_logger = None

# ═══════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════

WORKFLOW_PREFIX = "_workflow/"

VALID_STATUSES = {
    "received", "classified", "awaiting_review",
    "approved", "rejected", "quarantined",
}

VALID_TRANSITIONS = {
    "received": ["classified", "quarantined"],
    "classified": ["awaiting_review", "quarantined"],
    "awaiting_review": ["approved", "rejected", "quarantined"],
    "rejected": ["awaiting_review", "quarantined"],
    # approved and quarantined are terminal states
}

PRIORITY_LEVELS = {"normal", "urgent", "critical"}

STATUS_LABELS_TH = {
    "received": "ได้รับแล้ว",
    "classified": "จัดหมวดแล้ว",
    "awaiting_review": "รอตรวจสอบ",
    "approved": "อนุมัติแล้ว",
    "rejected": "ปฏิเสธ",
    "quarantined": "กักกัน",
}


# ═══════════════════════════════════════════════════════════
# Auth Helper
# ═══════════════════════════════════════════════════════════

def _check_admin_secret() -> bool:
    """Verify ADMIN_SECRET from header or cookie."""
    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    if not admin_secret:
        return False
    auth_header = request.headers.get("X-Admin-Secret", "")
    auth_cookie = request.cookies.get("admin_secret", "")
    return auth_header == admin_secret or auth_cookie == admin_secret


def _unauthorized():
    return jsonify({"error": "Unauthorized"}), 401


def _safe_id(doc_id: str) -> str:
    """Return first 8 chars of doc_id for safe display (no PHI)."""
    return doc_id[:8] if doc_id else "unknown"


# ═══════════════════════════════════════════════════════════
# Workflow Data Access
# ═══════════════════════════════════════════════════════════

def _workflow_path(doc_id: str) -> str:
    """GCS path for a document's workflow state."""
    return f"{WORKFLOW_PREFIX}{doc_id}.json"


def _read_workflow(doc_id: str) -> dict | None:
    """Read workflow state from GCS. Returns None if not found or error."""
    if not _storage:
        return None
    try:
        data = _storage.get(_workflow_path(doc_id))
        if data is None:
            return None
        return json.loads(data.decode("utf-8"))
    except Exception as e:
        logger.error(f"Workflow read error for {_safe_id(doc_id)}: {type(e).__name__}")
        return None


def _write_workflow(doc_id: str, workflow: dict) -> bool:
    """Write workflow state to GCS. Returns True on success."""
    if not _storage:
        return False
    try:
        workflow["updated_at"] = datetime.now().isoformat(timespec="seconds")
        data = json.dumps(workflow, ensure_ascii=False, indent=2).encode("utf-8")
        _storage.put(_workflow_path(doc_id), data, content_type="application/json")
        return True
    except Exception as e:
        logger.error(f"Workflow write error for {_safe_id(doc_id)}: {type(e).__name__}")
        return False


def _init_workflow(doc_id: str, doc_meta: dict | None = None) -> dict:
    """Create initial workflow state for a document."""
    now = datetime.now().isoformat(timespec="seconds")
    return {
        "doc_id": doc_id,
        "current_status": "received",
        "history": [
            {
                "status": "received",
                "actor_hash": "system",
                "timestamp": now,
                "note": "เอกสารเข้าสู่ระบบ",
            }
        ],
        "assigned_reviewer": None,
        "priority": "normal",
        "created_at": now,
        "updated_at": now,
        "doc_type": (doc_meta or {}).get("category", "unknown"),
    }


def _get_or_create_workflow(doc_id: str, doc_meta: dict | None = None) -> dict:
    """Get existing workflow or auto-initialize one."""
    wf = _read_workflow(doc_id)
    if wf is not None:
        return wf
    # Auto-initialize
    wf = _init_workflow(doc_id, doc_meta)
    _write_workflow(doc_id, wf)
    return wf


def _get_all_workflows() -> list[dict]:
    """Get workflow states for all indexed documents."""
    if not _doc_index:
        return []

    results = []
    docs = _doc_index._docs if hasattr(_doc_index, "_docs") else []
    for doc in docs:
        doc_id = doc.get("doc_id", "")
        if not doc_id:
            continue
        wf = _get_or_create_workflow(doc_id, doc)
        # Merge minimal doc metadata for display (no PHI)
        wf["doc_type"] = doc.get("category", wf.get("doc_type", "unknown"))
        results.append(wf)
    return results


# ═══════════════════════════════════════════════════════════
# API Endpoints
# ═══════════════════════════════════════════════════════════

@workflow_bp.route("/admin/api/v2/workflow/queue", methods=["GET"])
def api_workflow_queue():
    """List documents by workflow status."""
    if not _check_admin_secret():
        return _unauthorized()

    status_filter = request.args.get("status", "").strip().lower()
    priority_filter = request.args.get("priority", "").strip().lower()
    limit = min(int(request.args.get("limit", "50")), 200)

    try:
        all_wf = _get_all_workflows()

        # Apply filters
        if status_filter and status_filter in VALID_STATUSES:
            all_wf = [w for w in all_wf if w.get("current_status") == status_filter]
        if priority_filter and priority_filter in PRIORITY_LEVELS:
            all_wf = [w for w in all_wf if w.get("priority") == priority_filter]

        # Sort by updated_at descending
        all_wf.sort(key=lambda w: w.get("updated_at", ""), reverse=True)

        # Apply limit
        all_wf = all_wf[:limit]

        # Redact for safe display
        safe_items = []
        for w in all_wf:
            safe_items.append({
                "doc_id_prefix": _safe_id(w.get("doc_id", "")),
                "doc_id": w.get("doc_id", ""),
                "current_status": w.get("current_status", "received"),
                "priority": w.get("priority", "normal"),
                "assigned_reviewer": _safe_id(w.get("assigned_reviewer", "") or ""),
                "doc_type": w.get("doc_type", "unknown"),
                "created_at": w.get("created_at", ""),
                "updated_at": w.get("updated_at", ""),
                "history_count": len(w.get("history", [])),
            })

        return jsonify({
            "ok": True,
            "items": safe_items,
            "total": len(safe_items),
        })
    except Exception as e:
        logger.error(f"Workflow queue error: {type(e).__name__}: {e}")
        return jsonify({"ok": False, "error": "Internal error"}), 500


@workflow_bp.route("/admin/api/v2/workflow/document/<doc_id_prefix>", methods=["GET"])
def api_workflow_document(doc_id_prefix: str):
    """Get full workflow for a single document (lookup by prefix)."""
    if not _check_admin_secret():
        return _unauthorized()

    try:
        # Find doc by prefix match
        docs = _doc_index._docs if _doc_index and hasattr(_doc_index, "_docs") else []
        matched_doc = None
        for doc in docs:
            did = doc.get("doc_id", "")
            if did.startswith(doc_id_prefix):
                matched_doc = doc
                break

        if not matched_doc:
            return jsonify({"ok": False, "error": "ไม่พบเอกสาร"}), 404

        doc_id = matched_doc.get("doc_id", "")
        wf = _get_or_create_workflow(doc_id, matched_doc)

        # Safe response
        safe_wf = {
            "doc_id_prefix": _safe_id(doc_id),
            "doc_id": doc_id,
            "current_status": wf.get("current_status", "received"),
            "priority": wf.get("priority", "normal"),
            "assigned_reviewer": _safe_id(wf.get("assigned_reviewer", "") or ""),
            "doc_type": matched_doc.get("category", "unknown"),
            "created_at": wf.get("created_at", ""),
            "updated_at": wf.get("updated_at", ""),
            "history": [
                {
                    "status": h.get("status", ""),
                    "actor_hash": _safe_id(h.get("actor_hash", "")),
                    "timestamp": h.get("timestamp", ""),
                    "note": h.get("note", ""),
                }
                for h in wf.get("history", [])
            ],
        }

        return jsonify({"ok": True, "workflow": safe_wf})
    except Exception as e:
        logger.error(f"Workflow document error: {type(e).__name__}: {e}")
        return jsonify({"ok": False, "error": "Internal error"}), 500


@workflow_bp.route("/admin/api/v2/workflow/transition", methods=["POST"])
def api_workflow_transition():
    """Transition a document to a new workflow state."""
    if not _check_admin_secret():
        return _unauthorized()

    try:
        body = request.get_json(silent=True) or {}
        doc_id = body.get("doc_id", "").strip()
        new_status = body.get("new_status", "").strip().lower()
        actor_hash = body.get("actor_hash", "").strip()
        note = body.get("note", "").strip()

        # Validate inputs
        if not doc_id:
            return jsonify({"ok": False, "error": "ต้องระบุ doc_id"}), 400
        if not new_status or new_status not in VALID_STATUSES:
            return jsonify({"ok": False, "error": f"สถานะไม่ถูกต้อง: {new_status}"}), 400
        if not actor_hash:
            return jsonify({"ok": False, "error": "ต้องระบุ actor_hash"}), 400

        # Get current workflow
        wf = _read_workflow(doc_id)
        if wf is None:
            # Auto-initialize if doesn't exist
            wf = _init_workflow(doc_id)

        current_status = wf.get("current_status", "received")

        # Validate transition
        allowed = VALID_TRANSITIONS.get(current_status, [])
        if new_status not in allowed:
            return jsonify({
                "ok": False,
                "error": f"ไม่สามารถเปลี่ยนจาก '{STATUS_LABELS_TH.get(current_status, current_status)}' "
                         f"เป็น '{STATUS_LABELS_TH.get(new_status, new_status)}' ได้",
                "current_status": current_status,
                "allowed_transitions": allowed,
            }), 400

        # Audit trail — fail-closed for security-critical transitions
        audit_ok = False
        if _audit_logger:
            try:
                _audit_logger.log(
                    action="workflow_transition",
                    actor_hash=_safe_id(actor_hash),
                    details={
                        "from_status": current_status,
                        "to_status": new_status,
                        "note": note[:200] if note else "",
                    },
                    document_ref=_safe_id(doc_id),
                    category="document",
                    outcome="success",
                )
                audit_ok = True
            except Exception as e:
                logger.error(f"Audit failed for transition {_safe_id(doc_id)}: {type(e).__name__}")
        else:
            # No audit logger configured — allow non-critical transitions
            audit_ok = True

        # Fail-closed: if audit fails, reject transition
        if not audit_ok:
            return jsonify({
                "ok": False,
                "error": "ไม่สามารถบันทึก audit trail — ปฏิเสธการเปลี่ยนสถานะ",
            }), 500

        # Apply transition
        now = datetime.now().isoformat(timespec="seconds")
        wf["current_status"] = new_status
        wf["history"].append({
            "status": new_status,
            "actor_hash": _safe_id(actor_hash),
            "timestamp": now,
            "note": note[:200] if note else "",
        })
        wf["updated_at"] = now

        if not _write_workflow(doc_id, wf):
            return jsonify({"ok": False, "error": "ไม่สามารถบันทึกสถานะได้"}), 500

        logger.info(
            f"Workflow transition: {_safe_id(doc_id)} "
            f"{current_status} → {new_status} by {_safe_id(actor_hash)}"
        )

        return jsonify({
            "ok": True,
            "doc_id_prefix": _safe_id(doc_id),
            "previous_status": current_status,
            "current_status": new_status,
            "updated_at": now,
        })
    except Exception as e:
        logger.error(f"Workflow transition error: {type(e).__name__}: {e}")
        return jsonify({"ok": False, "error": "Internal error"}), 500


@workflow_bp.route("/admin/api/v2/workflow/assign", methods=["POST"])
def api_workflow_assign():
    """Assign a reviewer to a document."""
    if not _check_admin_secret():
        return _unauthorized()

    try:
        body = request.get_json(silent=True) or {}
        doc_id = body.get("doc_id", "").strip()
        reviewer_hash = body.get("reviewer_hash", "").strip()

        if not doc_id:
            return jsonify({"ok": False, "error": "ต้องระบุ doc_id"}), 400
        if not reviewer_hash:
            return jsonify({"ok": False, "error": "ต้องระบุ reviewer_hash"}), 400

        wf = _read_workflow(doc_id)
        if wf is None:
            wf = _init_workflow(doc_id)

        wf["assigned_reviewer"] = reviewer_hash

        # Audit
        if _audit_logger:
            try:
                _audit_logger.log(
                    action="workflow_assign_reviewer",
                    actor_hash=_safe_id(reviewer_hash),
                    details={"assigned_to": _safe_id(reviewer_hash)},
                    document_ref=_safe_id(doc_id),
                    category="document",
                    outcome="success",
                )
            except Exception as e:
                logger.warning(f"Audit failed for assign {_safe_id(doc_id)}: {type(e).__name__}")

        if not _write_workflow(doc_id, wf):
            return jsonify({"ok": False, "error": "ไม่สามารถบันทึกได้"}), 500

        return jsonify({
            "ok": True,
            "doc_id_prefix": _safe_id(doc_id),
            "assigned_reviewer": _safe_id(reviewer_hash),
        })
    except Exception as e:
        logger.error(f"Workflow assign error: {type(e).__name__}: {e}")
        return jsonify({"ok": False, "error": "Internal error"}), 500


@workflow_bp.route("/admin/api/v2/workflow/stats", methods=["GET"])
def api_workflow_stats():
    """Workflow statistics — counts by status, average times."""
    if not _check_admin_secret():
        return _unauthorized()

    try:
        all_wf = _get_all_workflows()

        # Count by status
        status_counts = {}
        for s in VALID_STATUSES:
            status_counts[s] = 0
        for w in all_wf:
            st = w.get("current_status", "received")
            status_counts[st] = status_counts.get(st, 0) + 1

        # Priority counts
        priority_counts = {"normal": 0, "urgent": 0, "critical": 0}
        for w in all_wf:
            p = w.get("priority", "normal")
            priority_counts[p] = priority_counts.get(p, 0) + 1

        # Average time in each stage (seconds)
        stage_times: dict[str, list[float]] = {}
        for w in all_wf:
            history = w.get("history", [])
            for i in range(1, len(history)):
                prev = history[i - 1]
                curr = history[i]
                try:
                    t0 = datetime.fromisoformat(prev["timestamp"])
                    t1 = datetime.fromisoformat(curr["timestamp"])
                    delta = (t1 - t0).total_seconds()
                    stage = prev["status"]
                    if stage not in stage_times:
                        stage_times[stage] = []
                    stage_times[stage].append(delta)
                except (KeyError, ValueError):
                    continue

        avg_stage_time = {}
        for stage, times in stage_times.items():
            if times:
                avg_seconds = sum(times) / len(times)
                avg_stage_time[stage] = {
                    "avg_seconds": round(avg_seconds, 1),
                    "avg_display": _format_duration(avg_seconds),
                    "count": len(times),
                }

        # Pending review count
        pending_review = status_counts.get("awaiting_review", 0)

        return jsonify({
            "ok": True,
            "total_documents": len(all_wf),
            "by_status": status_counts,
            "by_priority": priority_counts,
            "avg_stage_time": avg_stage_time,
            "pending_review": pending_review,
        })
    except Exception as e:
        logger.error(f"Workflow stats error: {type(e).__name__}: {e}")
        return jsonify({"ok": False, "error": "Internal error"}), 500


def _format_duration(seconds: float) -> str:
    """Format seconds into Thai-friendly duration string."""
    if seconds < 60:
        return f"{int(seconds)} วินาที"
    elif seconds < 3600:
        return f"{int(seconds // 60)} นาที"
    elif seconds < 86400:
        hours = seconds / 3600
        return f"{hours:.1f} ชั่วโมง"
    else:
        days = seconds / 86400
        return f"{days:.1f} วัน"


# ═══════════════════════════════════════════════════════════
# HTML UI
# ═══════════════════════════════════════════════════════════

@workflow_bp.route("/admin/v2/workflow", methods=["GET"])
def workflow_ui():
    """Full Thai-language workflow management UI."""
    if not _check_admin_secret():
        return _render_auth_page()

    return _render_workflow_page()


def _render_auth_page() -> str:
    """Auth guard page."""
    return """<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ระบบอนุมัติเอกสาร — เข้าสู่ระบบ</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0f172a;color:#e2e8f0;font-family:'Segoe UI',system-ui,sans-serif;
  display:flex;justify-content:center;align-items:center;min-height:100vh}
.login-box{background:#1e293b;border-radius:12px;padding:40px;max-width:400px;width:90%;
  box-shadow:0 4px 24px rgba(0,0,0,.4)}
h1{font-size:1.4rem;margin-bottom:8px;text-align:center}
p{color:#94a3b8;font-size:.9rem;margin-bottom:24px;text-align:center}
input{width:100%;padding:12px 16px;background:#0f172a;border:1px solid #334155;
  border-radius:8px;color:#e2e8f0;font-size:1rem;margin-bottom:16px}
input:focus{outline:none;border-color:#3b82f6}
button{width:100%;padding:12px;background:#3b82f6;color:#fff;border:none;
  border-radius:8px;font-size:1rem;cursor:pointer;font-weight:600}
button:hover{background:#2563eb}
.icon{text-align:center;font-size:2.5rem;margin-bottom:16px}
</style>
</head>
<body>
<div class="login-box">
  <div class="icon">
    <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="#3b82f6" stroke-width="2">
      <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/>
      <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
    </svg>
  </div>
  <h1>ระบบอนุมัติเอกสาร</h1>
  <p>คลินิกระนอง รักษ์ไต — กรุณาใส่รหัสผ่านผู้ดูแล</p>
  <form onsubmit="go(event)">
    <input type="password" id="secret" placeholder="Admin Secret" autocomplete="off">
    <button type="submit">เข้าสู่ระบบ</button>
  </form>
</div>
<script>
function go(e){
  e.preventDefault();
  var s=document.getElementById('secret').value.trim();
  if(s) location.href='?secret='+encodeURIComponent(s);
}
</script>
</body>
</html>""", 200, {"Content-Type": "text/html; charset=utf-8"}


def _render_workflow_page() -> str:
    """Main workflow management page — Thai language, dark theme."""
    html = """<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ระบบอนุมัติเอกสาร — ระนอง รักษ์ไต</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0f172a;color:#e2e8f0;font-family:'Segoe UI',system-ui,sans-serif;
  padding:16px;min-height:100vh}
a{color:#60a5fa;text-decoration:none}
a:hover{text-decoration:underline}

.header{display:flex;align-items:center;justify-content:space-between;
  margin-bottom:24px;flex-wrap:wrap;gap:12px}
.header h1{font-size:1.5rem;display:flex;align-items:center;gap:8px}
.header-links{display:flex;gap:12px;font-size:.85rem}

.tabs{display:flex;gap:4px;margin-bottom:20px;border-bottom:1px solid #334155;
  padding-bottom:0}
.tab{padding:10px 20px;cursor:pointer;border-radius:8px 8px 0 0;
  background:transparent;color:#94a3b8;font-size:.95rem;border:none;
  transition:all .2s}
.tab:hover{background:#1e293b;color:#e2e8f0}
.tab.active{background:#1e293b;color:#60a5fa;font-weight:600;
  border-bottom:2px solid #3b82f6}

.panel{display:none}
.panel.active{display:block}

.filter-bar{display:flex;gap:12px;margin-bottom:16px;flex-wrap:wrap;align-items:center}
.filter-bar select,.filter-bar input{
  padding:8px 12px;background:#1e293b;border:1px solid #334155;
  border-radius:6px;color:#e2e8f0;font-size:.9rem}
.filter-bar select:focus,.filter-bar input:focus{outline:none;border-color:#3b82f6}

.card{background:#1e293b;border-radius:10px;padding:16px;margin-bottom:12px;
  border:1px solid #334155;transition:border-color .2s}
.card:hover{border-color:#475569}
.card-header{display:flex;justify-content:space-between;align-items:center;
  margin-bottom:8px;flex-wrap:wrap;gap:8px}
.card-title{font-weight:600;font-size:1rem;display:flex;align-items:center;gap:8px}
.card-meta{color:#94a3b8;font-size:.8rem}
.card-body{margin-top:8px}
.card-actions{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap}

.badge{display:inline-flex;align-items:center;padding:3px 10px;border-radius:12px;
  font-size:.75rem;font-weight:600;gap:4px}
.badge-received{background:#1e3a5f;color:#60a5fa}
.badge-classified{background:#3b3214;color:#fbbf24}
.badge-awaiting_review{background:#3b2814;color:#fb923c}
.badge-approved{background:#14332b;color:#34d399}
.badge-rejected{background:#3b1414;color:#f87171}
.badge-quarantined{background:#27272a;color:#a1a1aa}

.badge-normal{background:#1e293b;color:#94a3b8}
.badge-urgent{background:#3b2814;color:#fb923c}
.badge-critical{background:#3b1414;color:#f87171}

.btn{padding:6px 14px;border-radius:6px;font-size:.8rem;border:none;
  cursor:pointer;font-weight:500;transition:all .15s;color:#fff}
.btn:hover{filter:brightness(1.15)}
.btn-classified{background:#ca8a04}
.btn-awaiting_review{background:#ea580c}
.btn-approved{background:#16a34a}
.btn-rejected{background:#dc2626}
.btn-quarantined{background:#52525b}
.btn-assign{background:#2563eb}
.btn-sm{padding:4px 10px;font-size:.75rem}

.timeline{margin-top:12px;padding-left:20px;border-left:2px solid #334155}
.timeline-item{position:relative;padding:8px 0 8px 16px;font-size:.85rem}
.timeline-item::before{content:'';position:absolute;left:-25px;top:12px;
  width:10px;height:10px;border-radius:50%;background:#3b82f6;border:2px solid #1e293b}
.timeline-item .time{color:#64748b;font-size:.75rem}
.timeline-item .note{color:#94a3b8;font-style:italic;margin-top:2px}

.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));
  gap:12px;margin-bottom:24px}
.stat-card{background:#1e293b;border-radius:10px;padding:20px;text-align:center;
  border:1px solid #334155}
.stat-value{font-size:2rem;font-weight:700}
.stat-label{color:#94a3b8;font-size:.85rem;margin-top:4px}

.avg-table{width:100%;border-collapse:collapse;margin-top:16px}
.avg-table th,.avg-table td{padding:10px 14px;text-align:left;
  border-bottom:1px solid #334155;font-size:.9rem}
.avg-table th{color:#94a3b8;font-weight:500}

.loading{text-align:center;padding:40px;color:#64748b}
.spinner{display:inline-block;width:24px;height:24px;border:3px solid #334155;
  border-top-color:#3b82f6;border-radius:50%;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

.empty{text-align:center;padding:40px;color:#475569}

.modal-overlay{display:none;position:fixed;top:0;left:0;width:100%;height:100%;
  background:rgba(0,0,0,.6);z-index:100;justify-content:center;align-items:center}
.modal-overlay.show{display:flex}
.modal{background:#1e293b;border-radius:12px;padding:24px;max-width:480px;width:90%;
  border:1px solid #334155}
.modal h3{margin-bottom:16px;font-size:1.1rem}
.modal textarea{width:100%;padding:10px;background:#0f172a;border:1px solid #334155;
  border-radius:6px;color:#e2e8f0;font-size:.9rem;resize:vertical;min-height:60px}
.modal textarea:focus{outline:none;border-color:#3b82f6}
.modal-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:16px}
.modal .btn-cancel{background:#475569}

.footer{text-align:center;color:#475569;font-size:.8rem;margin-top:24px;padding:16px 0}

.back-link{font-size:.85rem;color:#60a5fa}

@media(max-width:640px){
  .header h1{font-size:1.2rem}
  .stats-grid{grid-template-columns:1fr 1fr}
  .card-header{flex-direction:column;align-items:flex-start}
}
</style>
</head>
<body>

<div class="header">
  <h1>
    <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#3b82f6" stroke-width="2">
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
      <polyline points="14 2 14 8 20 8"/>
      <line x1="9" y1="15" x2="15" y2="15"/>
      <line x1="12" y1="12" x2="12" y2="18"/>
    </svg>
    ระบบอนุมัติเอกสาร
  </h1>
  <div class="header-links">
    <a class="back-link" id="dashboard-link" href="#">← แดชบอร์ดหลัก</a>
  </div>
</div>

<div class="tabs">
  <button class="tab active" onclick="switchTab('pending')">รอดำเนินการ</button>
  <button class="tab" onclick="switchTab('all')">ทั้งหมด</button>
  <button class="tab" onclick="switchTab('stats')">สถิติ</button>
</div>

<div class="panel active" id="panel-pending">
  <div class="filter-bar">
    <select id="filter-status-pending" onchange="loadQueue()">
      <option value="awaiting_review">รอตรวจสอบ</option>
      <option value="received">ได้รับแล้ว</option>
      <option value="classified">จัดหมวดแล้ว</option>
    </select>
    <select id="filter-priority-pending" onchange="loadQueue()">
      <option value="">ทุกความสำคัญ</option>
      <option value="critical">วิกฤต</option>
      <option value="urgent">เร่งด่วน</option>
      <option value="normal">ปกติ</option>
    </select>
  </div>
  <div id="queue-list"><div class="loading"><div class="spinner"></div></div></div>
</div>

<div class="panel" id="panel-all">
  <div class="filter-bar">
    <select id="filter-status-all" onchange="loadAll()">
      <option value="">ทุกสถานะ</option>
      <option value="received">ได้รับแล้ว</option>
      <option value="classified">จัดหมวดแล้ว</option>
      <option value="awaiting_review">รอตรวจสอบ</option>
      <option value="approved">อนุมัติแล้ว</option>
      <option value="rejected">ปฏิเสธ</option>
      <option value="quarantined">กักกัน</option>
    </select>
    <input type="text" id="search-doc-id" placeholder="ค้นหา doc_id..." oninput="loadAll()">
  </div>
  <div id="all-list"><div class="loading"><div class="spinner"></div></div></div>
</div>

<div class="panel" id="panel-stats">
  <div id="stats-content"><div class="loading"><div class="spinner"></div></div></div>
</div>

<!-- Transition Modal -->
<div class="modal-overlay" id="transition-modal">
  <div class="modal">
    <h3 id="modal-title">ยืนยันการเปลี่ยนสถานะ</h3>
    <p id="modal-desc" style="color:#94a3b8;font-size:.9rem;margin-bottom:12px"></p>
    <textarea id="modal-note" placeholder="หมายเหตุ (ไม่บังคับ)"></textarea>
    <div class="modal-actions">
      <button class="btn btn-cancel" onclick="closeModal()">ยกเลิก</button>
      <button class="btn" id="modal-confirm" onclick="confirmTransition()">ยืนยัน</button>
    </div>
  </div>
</div>

<!-- Detail Modal -->
<div class="modal-overlay" id="detail-modal">
  <div class="modal" style="max-width:560px">
    <h3 id="detail-title">ประวัติเอกสาร</h3>
    <div id="detail-content"></div>
    <div class="modal-actions">
      <button class="btn btn-cancel" onclick="closeDetailModal()">ปิด</button>
    </div>
  </div>
</div>

<div class="footer">
  อัปเดตอัตโนมัติทุก 30 วินาที — <span id="last-update">—</span>
</div>

<script>
const SECRET = new URLSearchParams(location.search).get('secret') || '';
const API = '/admin/api/v2/workflow';
const ACTOR = 'admin_' + Date.now().toString(36).slice(-8);

document.getElementById('dashboard-link').href = '/admin/v2/dashboard?secret=' + encodeURIComponent(SECRET);

const STATUS_TH = {
  received: 'ได้รับแล้ว',
  classified: 'จัดหมวดแล้ว',
  awaiting_review: 'รอตรวจสอบ',
  approved: 'อนุมัติแล้ว',
  rejected: 'ปฏิเสธ',
  quarantined: 'กักกัน'
};

const PRIORITY_TH = {normal:'ปกติ', urgent:'เร่งด่วน', critical:'วิกฤต'};

const TRANSITIONS = {
  received: ['classified', 'quarantined'],
  classified: ['awaiting_review', 'quarantined'],
  awaiting_review: ['approved', 'rejected', 'quarantined'],
  rejected: ['awaiting_review', 'quarantined']
};

const TRANSITION_TH = {
  classified: 'จัดหมวด',
  awaiting_review: 'ส่งตรวจสอบ',
  approved: 'อนุมัติ',
  rejected: 'ปฏิเสธ',
  quarantined: 'กักกัน'
};

const CAT_TH = {
  lab:'ผลแล็บ', rx:'ใบสั่งยา', bills:'ใบเสร็จ', inbox:'กล่องข้อความ',
  dialysis:'ฟอกไต', review:'รอตรวจสอบ', other:'อื่นๆ', unknown:'ไม่ทราบ'
};

let _pendingTransition = null;

function apiFetch(path, opts) {
  const sep = path.includes('?') ? '&' : '?';
  return fetch(API + path + sep + 'secret=' + encodeURIComponent(SECRET), opts);
}

function switchTab(name) {
  document.querySelectorAll('.tab').forEach((t, i) => {
    t.classList.toggle('active', ['pending','all','stats'][i] === name);
  });
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.getElementById('panel-' + name).classList.add('active');
  if (name === 'pending') loadQueue();
  else if (name === 'all') loadAll();
  else if (name === 'stats') loadStats();
}

function renderCard(item) {
  const st = item.current_status;
  const transitions = TRANSITIONS[st] || [];
  let btns = transitions.map(t =>
    '<button class="btn btn-' + t + ' btn-sm" onclick="openTransition(\'' +
    item.doc_id + "','" + st + "','" + t + '\')">' +
    (TRANSITION_TH[t] || t) + '</button>'
  ).join('');

  return '<div class="card">' +
    '<div class="card-header">' +
      '<div class="card-title">' +
        '<span style="cursor:pointer" onclick="showDetail(\'' + item.doc_id + '\')">' +
          item.doc_id_prefix + '</span> ' +
        '<span class="badge badge-' + st + '">' + (STATUS_TH[st] || st) + '</span> ' +
        '<span class="badge badge-' + item.priority + '">' +
          (PRIORITY_TH[item.priority] || item.priority) + '</span>' +
      '</div>' +
      '<div class="card-meta">' + (CAT_TH[item.doc_type] || item.doc_type) + '</div>' +
    '</div>' +
    (item.assigned_reviewer && item.assigned_reviewer !== 'unknown' ?
      '<div class="card-meta">ผู้ตรวจ: ' + item.assigned_reviewer + '</div>' : '') +
    '<div class="card-meta">อัปเดต: ' + formatDate(item.updated_at) + '</div>' +
    '<div class="card-actions">' + btns +
      '<button class="btn btn-assign btn-sm" onclick="assignReviewer(\'' +
        item.doc_id + '\')">มอบหมาย</button>' +
      '<button class="btn btn-sm" style="background:#334155" onclick="showDetail(\'' +
        item.doc_id + '\')">ประวัติ</button>' +
    '</div>' +
  '</div>';
}

function formatDate(iso) {
  if (!iso) return '—';
  try {
    const d = new Date(iso);
    return d.toLocaleDateString('th-TH', {day:'numeric',month:'short',year:'numeric'}) +
      ' ' + d.toLocaleTimeString('th-TH', {hour:'2-digit',minute:'2-digit'});
  } catch(e) { return iso; }
}

async function loadQueue() {
  const el = document.getElementById('queue-list');
  el.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
  const st = document.getElementById('filter-status-pending').value;
  const pr = document.getElementById('filter-priority-pending').value;
  let url = '/queue?status=' + st;
  if (pr) url += '&priority=' + pr;
  try {
    const res = await apiFetch(url);
    const data = await res.json();
    if (!data.ok || !data.items.length) {
      el.innerHTML = '<div class="empty">ไม่มีเอกสาร' +
        (STATUS_TH[st] ? ' (' + STATUS_TH[st] + ')' : '') + '</div>';
      return;
    }
    el.innerHTML = data.items.map(renderCard).join('');
  } catch(e) {
    el.innerHTML = '<div class="empty">เกิดข้อผิดพลาด: ' + e.message + '</div>';
  }
  updateTimestamp();
}

async function loadAll() {
  const el = document.getElementById('all-list');
  el.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
  const st = document.getElementById('filter-status-all').value;
  const search = document.getElementById('search-doc-id').value.trim();
  let url = '/queue?limit=100';
  if (st) url += '&status=' + st;
  try {
    const res = await apiFetch(url);
    const data = await res.json();
    if (!data.ok) { el.innerHTML = '<div class="empty">เกิดข้อผิดพลาด</div>'; return; }
    let items = data.items || [];
    if (search) {
      items = items.filter(i => i.doc_id_prefix.includes(search) || i.doc_id.includes(search));
    }
    if (!items.length) {
      el.innerHTML = '<div class="empty">ไม่พบเอกสาร</div>';
      return;
    }
    el.innerHTML = items.map(renderCard).join('');
  } catch(e) {
    el.innerHTML = '<div class="empty">เกิดข้อผิดพลาด: ' + e.message + '</div>';
  }
  updateTimestamp();
}

async function loadStats() {
  const el = document.getElementById('stats-content');
  el.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
  try {
    const res = await apiFetch('/stats');
    const data = await res.json();
    if (!data.ok) { el.innerHTML = '<div class="empty">เกิดข้อผิดพลาด</div>'; return; }

    const bs = data.by_status || {};
    const bp = data.by_priority || {};
    let html = '<div class="stats-grid">';
    html += statCard(data.total_documents, 'เอกสารทั้งหมด', '#3b82f6');
    html += statCard(data.pending_review, 'รอตรวจสอบ', '#fb923c');
    html += statCard(bs.approved || 0, 'อนุมัติแล้ว', '#34d399');
    html += statCard(bs.rejected || 0, 'ปฏิเสธ', '#f87171');
    html += statCard(bs.quarantined || 0, 'กักกัน', '#a1a1aa');
    html += statCard(bp.critical || 0, 'วิกฤต', '#ef4444');
    html += '</div>';

    // Status breakdown
    html += '<div class="card"><div class="card-title" style="margin-bottom:12px">' +
      'จำนวนเอกสารตามสถานะ</div>';
    html += '<table class="avg-table"><tr><th>สถานะ</th><th>จำนวน</th></tr>';
    for (const [k,v] of Object.entries(bs)) {
      html += '<tr><td><span class="badge badge-' + k + '">' +
        (STATUS_TH[k]||k) + '</span></td><td>' + v + '</td></tr>';
    }
    html += '</table></div>';

    // Avg stage time
    if (data.avg_stage_time && Object.keys(data.avg_stage_time).length) {
      html += '<div class="card"><div class="card-title" style="margin-bottom:12px">' +
        'เวลาเฉลี่ยในแต่ละขั้นตอน</div>';
      html += '<table class="avg-table"><tr><th>ขั้นตอน</th><th>เวลาเฉลี่ย</th><th>จำนวน</th></tr>';
      for (const [k,v] of Object.entries(data.avg_stage_time)) {
        html += '<tr><td>' + (STATUS_TH[k]||k) + '</td><td>' +
          v.avg_display + '</td><td>' + v.count + '</td></tr>';
      }
      html += '</table></div>';
    }

    el.innerHTML = html;
  } catch(e) {
    el.innerHTML = '<div class="empty">เกิดข้อผิดพลาด: ' + e.message + '</div>';
  }
  updateTimestamp();
}

function statCard(val, label, color) {
  return '<div class="stat-card"><div class="stat-value" style="color:' +
    color + '">' + val + '</div><div class="stat-label">' + label + '</div></div>';
}

function openTransition(docId, fromStatus, toStatus) {
  _pendingTransition = {docId, fromStatus, toStatus};
  document.getElementById('modal-title').textContent =
    'ยืนยัน: ' + (STATUS_TH[fromStatus]||fromStatus) + ' → ' + (STATUS_TH[toStatus]||toStatus);
  document.getElementById('modal-desc').textContent =
    'เอกสาร: ' + docId.slice(0,8);
  document.getElementById('modal-note').value = '';
  const confirmBtn = document.getElementById('modal-confirm');
  confirmBtn.className = 'btn btn-' + toStatus;
  confirmBtn.textContent = TRANSITION_TH[toStatus] || toStatus;
  document.getElementById('transition-modal').classList.add('show');
}

function closeModal() {
  document.getElementById('transition-modal').classList.remove('show');
  _pendingTransition = null;
}

async function confirmTransition() {
  if (!_pendingTransition) return;
  const {docId, toStatus} = _pendingTransition;
  const note = document.getElementById('modal-note').value.trim();
  const confirmBtn = document.getElementById('modal-confirm');
  confirmBtn.disabled = true;
  confirmBtn.textContent = 'กำลังดำเนินการ...';

  try {
    const res = await apiFetch('/transition', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        doc_id: docId,
        new_status: toStatus,
        actor_hash: ACTOR,
        note: note
      })
    });
    const data = await res.json();
    if (data.ok) {
      closeModal();
      refreshCurrent();
    } else {
      alert('ข้อผิดพลาด: ' + (data.error || 'Unknown'));
      confirmBtn.disabled = false;
      confirmBtn.textContent = TRANSITION_TH[toStatus] || toStatus;
    }
  } catch(e) {
    alert('เกิดข้อผิดพลาด: ' + e.message);
    confirmBtn.disabled = false;
    confirmBtn.textContent = TRANSITION_TH[toStatus] || toStatus;
  }
}

async function assignReviewer(docId) {
  const reviewer = prompt('ใส่ reviewer hash สำหรับเอกสาร ' + docId.slice(0,8) + ':');
  if (!reviewer || !reviewer.trim()) return;
  try {
    const res = await apiFetch('/assign', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({doc_id: docId, reviewer_hash: reviewer.trim()})
    });
    const data = await res.json();
    if (data.ok) refreshCurrent();
    else alert('ข้อผิดพลาด: ' + (data.error || 'Unknown'));
  } catch(e) { alert('เกิดข้อผิดพลาด: ' + e.message); }
}

async function showDetail(docId) {
  const el = document.getElementById('detail-content');
  el.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
  document.getElementById('detail-title').textContent = 'ประวัติเอกสาร ' + docId.slice(0,8);
  document.getElementById('detail-modal').classList.add('show');

  try {
    const res = await apiFetch('/document/' + docId);
    const data = await res.json();
    if (!data.ok) { el.innerHTML = '<div class="empty">ไม่พบข้อมูล</div>'; return; }
    const wf = data.workflow;
    let html = '<div style="margin-bottom:12px">' +
      '<span class="badge badge-' + wf.current_status + '">' +
        (STATUS_TH[wf.current_status]||wf.current_status) + '</span> ' +
      '<span class="card-meta">ประเภท: ' + (CAT_TH[wf.doc_type]||wf.doc_type) + '</span>' +
    '</div>';
    if (wf.assigned_reviewer && wf.assigned_reviewer !== 'unknown') {
      html += '<div class="card-meta" style="margin-bottom:8px">ผู้ตรวจ: ' +
        wf.assigned_reviewer + '</div>';
    }
    html += '<div class="timeline">';
    const history = (wf.history || []).slice().reverse();
    for (const h of history) {
      html += '<div class="timeline-item">' +
        '<span class="badge badge-' + h.status + '" style="font-size:.7rem">' +
          (STATUS_TH[h.status]||h.status) + '</span> ' +
        '<span style="font-size:.8rem">โดย ' + h.actor_hash + '</span>' +
        '<div class="time">' + formatDate(h.timestamp) + '</div>' +
        (h.note ? '<div class="note">' + escHtml(h.note) + '</div>' : '') +
      '</div>';
    }
    html += '</div>';
    el.innerHTML = html;
  } catch(e) {
    el.innerHTML = '<div class="empty">เกิดข้อผิดพลาด: ' + e.message + '</div>';
  }
}

function closeDetailModal() {
  document.getElementById('detail-modal').classList.remove('show');
}

function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function updateTimestamp() {
  document.getElementById('last-update').textContent =
    new Date().toLocaleTimeString('th-TH');
}

function refreshCurrent() {
  const active = document.querySelector('.tab.active');
  if (!active) return;
  const tabs = document.querySelectorAll('.tab');
  if (tabs[0] === active) loadQueue();
  else if (tabs[1] === active) loadAll();
  else if (tabs[2] === active) loadStats();
}

// Initial load
loadQueue();

// Auto-refresh every 30 seconds
setInterval(refreshCurrent, 30000);
</script>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


# ═══════════════════════════════════════════════════════════
# Registration
# ═══════════════════════════════════════════════════════════

def register_workflow(app, storage_ref, doc_index_ref, audit_ref):
    """Register the Document Workflow blueprint.

    Args:
        app: Flask app instance
        storage_ref: GCS/Local storage instance (put/get interface)
        doc_index_ref: DocumentIndex instance
        audit_ref: EnhancedAuditLogger / PersistentAuditTrail instance
    """
    global _storage, _doc_index, _audit_logger
    _storage = storage_ref
    _doc_index = doc_index_ref
    _audit_logger = audit_ref
    app.register_blueprint(workflow_bp)
    logger.info("Document Workflow: registered on /admin/v2/workflow")
