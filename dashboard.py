#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Dashboard V2 — RANONG RAKTAI AI 2.0
Web Dashboard สำหรับผู้ดูแลและแพทย์

Features:
  - ภาพรวมระบบ (KPI cards, category chart)
  - จัดการเอกสาร (filter, search, pagination)
  - บิล/OCR (company breakdown)
  - ผลแล็บ (trend charts, dialysis)
  - ตรวจสอบ (audit trail)

กฎความปลอดภัย:
  - ใช้ hashed IDs เท่านั้น — ไม่แสดง PHI
  - ต้องมี ADMIN_SECRET ทุก endpoint
  - Read-only — ไม่แก้ไขข้อมูล
  - ไม่ส่งข้อมูลผู้ป่วยไป External AI
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)

dashboard_bp = Blueprint("dashboard_v2", __name__)

# Module-level refs — set by register_dashboard()
_storage = None
_doc_index = None
_audit_logger = None


def _check_admin_secret() -> bool:
    """Verify ADMIN_SECRET from header or query param."""
    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    if not admin_secret:
        return False
    auth_header = request.headers.get("X-Admin-Secret", "")
    auth_param = request.args.get("secret", "")
    return auth_header == admin_secret or auth_param == admin_secret


def _unauthorized():
    return jsonify({"error": "Unauthorized"}), 401


# ═══════════════════════════════════════════════════════════
# API Endpoints
# ═══════════════════════════════════════════════════════════

@dashboard_bp.route("/admin/api/v2/stats", methods=["GET"])
def api_v2_stats():
    """Enhanced stats — overview KPI."""
    if not _check_admin_secret():
        return _unauthorized()

    docs = _doc_index._docs if _doc_index else []

    # Category counts
    cat_counts: dict[str, int] = {}
    bill_company_counts: dict[str, int] = {}
    workflow_counts = {"received": 0, "classified": 0, "pending_review": 0, "approved": 0}
    total_size = 0

    for doc in docs:
        cat = doc.get("category", "other")
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
        total_size += doc.get("file_size", 0)

        # Bill company
        bc = doc.get("bill_company", "")
        if bc and cat == "bills":
            bill_company_counts[bc] = bill_company_counts.get(bc, 0) + 1

        # Workflow status
        status = doc.get("review_status", "")
        if status == "approved":
            workflow_counts["approved"] += 1
        elif status == "pending_review":
            workflow_counts["pending_review"] += 1
        elif cat and cat != "review":
            workflow_counts["classified"] += 1
        else:
            workflow_counts["received"] += 1

    # GCS info
    gcs_ready = getattr(_storage, "is_ready", False) if _storage else False

    # Alerts from GCS
    alert_count = 0
    try:
        if _storage and hasattr(_storage, '_bucket') and _storage._bucket:
            alert_blobs = list(_storage._bucket.list_blobs(
                prefix="_alerts/", max_results=100
            ))
            alert_count = len(alert_blobs)
    except Exception:
        pass

    # Lab approved count
    approved_lab_count = 0
    pending_lab_count = 0
    try:
        if _storage and hasattr(_storage, '_bucket') and _storage._bucket:
            lab_blobs = list(_storage._bucket.list_blobs(
                prefix="_lab_results/", max_results=500
            ))
            for lb in lab_blobs:
                try:
                    ldata = json.loads(lb.download_as_text())
                    st = ldata.get("status", "pending_review")
                    if st == "approved":
                        approved_lab_count += 1
                    else:
                        pending_lab_count += 1
                except Exception:
                    pass
    except Exception:
        pass

    # Audit write failures
    audit_failures = 0
    if _audit_logger and hasattr(_audit_logger, 'write_failures'):
        audit_failures = _audit_logger.write_failures

    # Recent 5 docs
    recent = []
    sorted_docs = sorted(docs, key=lambda d: d.get("upload_date", ""), reverse=True)[:5]
    for d in sorted_docs:
        recent.append({
            "doc_id": d.get("document_id", "")[:8],
            "category": d.get("category", ""),
            "date": d.get("upload_date", "")[:16],
            "bill_company": d.get("bill_company", ""),
        })

    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    physician_users = set(
        h.strip() for h in os.getenv("PHYSICIAN_USERS", "").split(",") if h.strip()
    )
    admin_users = set(
        h.strip() for h in os.getenv("ADMIN_USERS", "").split(",") if h.strip()
    )
    authorized_users = set(
        h.strip() for h in os.getenv("AUTHORIZED_USERS", "").split(",") if h.strip()
    )

    return jsonify({
        "gcs_ready": gcs_ready,
        "total_docs": len(docs),
        "total_size_mb": round(total_size / (1024 * 1024), 2),
        "categories": cat_counts,
        "bill_companies": bill_company_counts,
        "workflow": workflow_counts,
        "alert_count": alert_count,
        "approved_lab_count": approved_lab_count,
        "pending_lab_count": pending_lab_count,
        "audit_failures": audit_failures,
        "recent_activity": recent,
        "physician_count": len(physician_users),
        "admin_count": len(admin_users),
        "authorized_count": len(authorized_users),
        "app_env": os.getenv("APP_ENV", "production"),
        "server_time": datetime.now().isoformat(timespec="seconds"),
    }), 200


@dashboard_bp.route("/admin/api/v2/documents", methods=["GET"])
def api_v2_documents():
    """Paginated document list with filters."""
    if not _check_admin_secret():
        return _unauthorized()

    docs = _doc_index._docs if _doc_index else []

    # Filters
    category = request.args.get("category", "").strip()
    search_q = request.args.get("search", "").strip().lower()
    page = max(1, int(request.args.get("page", 1)))
    per_page = min(100, max(5, int(request.args.get("per_page", 20))))

    filtered = []
    for d in reversed(docs):  # newest first
        if category and d.get("category", "") != category:
            continue
        if search_q:
            searchable = " ".join([
                d.get("document_id", "")[:8],
                d.get("category", ""),
                d.get("upload_date", ""),
                d.get("bill_company", ""),
            ]).lower()
            if search_q not in searchable:
                continue
        filtered.append(d)

    total = len(filtered)
    start = (page - 1) * per_page
    page_docs = filtered[start:start + per_page]

    items = []
    for d in page_docs:
        items.append({
            "doc_id": d.get("document_id", "")[:8],
            "category": d.get("category", ""),
            "upload_date": d.get("upload_date", ""),
            "owner": d.get("owner_hash", "")[:8] or "—",
            "size_kb": round(d.get("file_size", 0) / 1024, 1),
            "ocr_status": d.get("ocr_status", ""),
            "bill_company": d.get("bill_company", ""),
            "review_status": d.get("review_status", ""),
            "mime_type": d.get("mime_type", ""),
        })

    return jsonify({
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": max(1, -(-total // per_page)),
    }), 200


@dashboard_bp.route("/admin/api/v2/bills", methods=["GET"])
def api_v2_bills():
    """Bill breakdown by company."""
    if not _check_admin_secret():
        return _unauthorized()

    docs = _doc_index._docs if _doc_index else []

    companies: dict[str, dict] = {}
    recent_bills = []

    for d in docs:
        if d.get("category") != "bills":
            continue
        bc = d.get("bill_company", "OTHER") or "OTHER"
        if bc not in companies:
            companies[bc] = {"count": 0, "total_size": 0}
        companies[bc]["count"] += 1
        companies[bc]["total_size"] += d.get("file_size", 0)

    # Recent bills (last 20)
    bill_docs = [d for d in docs if d.get("category") == "bills"]
    bill_docs.sort(key=lambda x: x.get("upload_date", ""), reverse=True)
    for d in bill_docs[:20]:
        recent_bills.append({
            "doc_id": d.get("document_id", "")[:8],
            "company": d.get("bill_company", "OTHER") or "OTHER",
            "date": d.get("upload_date", "")[:16],
            "size_kb": round(d.get("file_size", 0) / 1024, 1),
            "ocr_status": d.get("ocr_status", ""),
        })

    return jsonify({
        "companies": companies,
        "total_bills": sum(c["count"] for c in companies.values()),
        "recent_bills": recent_bills,
    }), 200


@dashboard_bp.route("/admin/api/v2/labs", methods=["GET"])
def api_v2_labs():
    """Lab results with trend data."""
    if not _check_admin_secret():
        return _unauthorized()

    lab_results = []
    dialysis_count = 0

    try:
        if _storage and hasattr(_storage, '_bucket') and _storage._bucket:
            # Lab results
            lab_blobs = list(_storage._bucket.list_blobs(
                prefix="_lab_results/", max_results=200
            ))
            for lb in lab_blobs:
                try:
                    ldata = json.loads(lb.download_as_text())
                    lab_results.append({
                        "doc_id": ldata.get("source_document_id", "")[:8],
                        "specimen_date": ldata.get("specimen_date", ""),
                        "status": ldata.get("status", "pending_review"),
                        "result_count": len(ldata.get("results", [])),
                        "extracted_at": ldata.get("extracted_at", "")[:16],
                        "reviewed_by": ldata.get("reviewed_by", "")[:8] or "—",
                        "warnings": len(ldata.get("warnings", [])),
                        "analytes": [
                            r.get("analyte", "") for r in ldata.get("results", [])[:5]
                        ],
                    })
                except Exception:
                    pass

            # Dialysis records count
            dial_blobs = list(_storage._bucket.list_blobs(
                prefix="05_DIALYSIS_RECORDS/", max_results=500
            ))
            dialysis_count = len(dial_blobs)
    except Exception:
        pass

    # Sort by date descending
    lab_results.sort(
        key=lambda x: x.get("extracted_at", ""), reverse=True
    )

    # Summary stats
    approved = sum(1 for r in lab_results if r.get("status") == "approved")
    pending = sum(1 for r in lab_results if r.get("status") == "pending_review")

    return jsonify({
        "lab_results": lab_results[:50],
        "total_labs": len(lab_results),
        "approved": approved,
        "pending": pending,
        "dialysis_count": dialysis_count,
    }), 200


@dashboard_bp.route("/admin/api/v2/audit", methods=["GET"])
def api_v2_audit():
    """Audit trail events."""
    if not _check_admin_secret():
        return _unauthorized()

    month = request.args.get("month", datetime.now().strftime("%Y-%m"))
    action_filter = request.args.get("action", "")
    category_filter = request.args.get("category", "")
    limit = min(200, max(10, int(request.args.get("limit", 50))))

    events = []
    if _audit_logger and hasattr(_audit_logger, 'query'):
        try:
            raw = _audit_logger.query(
                month=month,
                action=action_filter or None,
                limit=limit,
            )
            for e in raw:
                if category_filter and e.get("category", "") != category_filter:
                    continue
                events.append({
                    "event_id": e.get("event_id", "")[:8],
                    "action": e.get("action", ""),
                    "actor": e.get("actor_hash", "")[:8],
                    "timestamp": e.get("timestamp", ""),
                    "category": e.get("category", ""),
                    "outcome": e.get("outcome", ""),
                    "doc_ref": e.get("document_ref", "")[:8] if e.get("document_ref") else "",
                })
        except Exception as exc:
            logger.warning(f"Audit query failed: {exc}")

    return jsonify({
        "events": events,
        "month": month,
        "total": len(events),
    }), 200


@dashboard_bp.route("/admin/api/v2/errors", methods=["GET"])
def api_v2_errors():
    """Recent error events from audit."""
    if not _check_admin_secret():
        return _unauthorized()

    errors = []
    if _audit_logger and hasattr(_audit_logger, 'query'):
        try:
            month = datetime.now().strftime("%Y-%m")
            raw = _audit_logger.query(month=month, limit=200)
            for e in raw:
                if e.get("outcome") in ("failure", "error"):
                    errors.append({
                        "event_id": e.get("event_id", "")[:8],
                        "action": e.get("action", ""),
                        "timestamp": e.get("timestamp", ""),
                        "category": e.get("category", ""),
                        "details": {
                            k: v for k, v in e.get("details", {}).items()
                            if k not in ("password", "token", "secret", "credential")
                        },
                    })
        except Exception as exc:
            logger.warning(f"Error query failed: {exc}")

    errors.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

    return jsonify({
        "errors": errors[:50],
        "total": len(errors),
    }), 200


@dashboard_bp.route("/admin/api/v2/workflow", methods=["GET"])
def api_v2_workflow():
    """Document workflow status breakdown."""
    if not _check_admin_secret():
        return _unauthorized()

    docs = _doc_index._docs if _doc_index else []

    pipeline = {
        "received": [],
        "classified": [],
        "pending_review": [],
        "approved": [],
    }

    for d in docs:
        status = d.get("review_status", "")
        cat = d.get("category", "review")
        entry = {
            "doc_id": d.get("document_id", "")[:8],
            "category": cat,
            "date": d.get("upload_date", "")[:16],
        }

        if status == "approved":
            pipeline["approved"].append(entry)
        elif status == "pending_review":
            pipeline["pending_review"].append(entry)
        elif cat and cat != "review":
            pipeline["classified"].append(entry)
        else:
            pipeline["received"].append(entry)

    return jsonify({
        "counts": {k: len(v) for k, v in pipeline.items()},
        "recent_pending": sorted(
            pipeline["pending_review"],
            key=lambda x: x.get("date", ""), reverse=True
        )[:10],
        "recent_approved": sorted(
            pipeline["approved"],
            key=lambda x: x.get("date", ""), reverse=True
        )[:10],
    }), 200


# ═══════════════════════════════════════════════════════════
# Dashboard V2 HTML
# ═══════════════════════════════════════════════════════════

DASHBOARD_V2_HTML = r"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>RANONG RAKTAI AI — Dashboard V2</title>
<style>
:root {
  --bg: #0f172a; --bg2: #1e293b; --bg3: #334155;
  --border: #475569; --text: #e2e8f0; --muted: #94a3b8;
  --accent: #38bdf8; --green: #22c55e; --red: #ef4444;
  --yellow: #eab308; --purple: #a78bfa; --pink: #f472b6;
  --orange: #fb923c; --teal: #2dd4bf;
  --card-shadow: 0 1px 3px rgba(0,0,0,.3);
  --radius: 12px;
}
* { margin:0; padding:0; box-sizing:border-box; }
body {
  background: var(--bg); color: var(--text);
  font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
  min-height: 100vh;
}
a { color: var(--accent); text-decoration: none; }

/* ── Header ── */
.header {
  background: var(--bg2); border-bottom: 1px solid var(--bg3);
  padding: 16px 24px; display: flex; align-items: center;
  justify-content: space-between; flex-wrap: wrap; gap: 12px;
}
.header h1 { font-size: 1.3rem; color: var(--accent); display: flex; align-items: center; gap: 8px; }
.header .meta { font-size: 0.8rem; color: var(--muted); display: flex; gap: 16px; align-items: center; }
.header .meta .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.header .meta .dot.ok { background: var(--green); }
.header .meta .dot.err { background: var(--red); }

/* ── Tabs ── */
.tabs {
  display: flex; background: var(--bg2); border-bottom: 1px solid var(--bg3);
  padding: 0 16px; overflow-x: auto; -webkit-overflow-scrolling: touch;
}
.tab {
  padding: 12px 20px; cursor: pointer; font-size: 0.9rem;
  color: var(--muted); border-bottom: 2px solid transparent;
  white-space: nowrap; transition: color .2s, border-color .2s;
  user-select: none;
}
.tab:hover { color: var(--text); }
.tab.active { color: var(--accent); border-bottom-color: var(--accent); }

/* ── Content ── */
.content { max-width: 1400px; margin: 0 auto; padding: 20px 16px; }
.panel { display: none; }
.panel.active { display: block; }

/* ── KPI Grid ── */
.kpi-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px; margin-bottom: 24px;
}
.kpi {
  background: var(--bg2); border: 1px solid var(--bg3); border-radius: var(--radius);
  padding: 16px; box-shadow: var(--card-shadow);
}
.kpi .label { font-size: 0.75rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 6px; }
.kpi .value { font-size: 1.8rem; font-weight: 700; }
.kpi .value.ok { color: var(--green); }
.kpi .value.warn { color: var(--yellow); }
.kpi .value.err { color: var(--red); }
.kpi .sub { font-size: 0.75rem; color: var(--muted); margin-top: 4px; }

/* ── Cards / Sections ── */
.section {
  background: var(--bg2); border: 1px solid var(--bg3);
  border-radius: var(--radius); padding: 20px; margin-bottom: 20px;
  box-shadow: var(--card-shadow);
}
.section h2 {
  font-size: 1rem; color: var(--accent); margin-bottom: 16px;
  padding-bottom: 10px; border-bottom: 1px solid var(--bg3);
  display: flex; align-items: center; gap: 8px;
}

/* ── Table ── */
table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
th {
  text-align: left; color: var(--muted); padding: 8px 10px;
  border-bottom: 1px solid var(--bg3); font-weight: 500;
  font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.3px;
}
td { padding: 8px 10px; border-bottom: 1px solid rgba(71,85,105,.3); }
tr:hover td { background: rgba(56,189,248,.03); }

/* ── Badges ── */
.badge {
  display: inline-block; padding: 2px 10px; border-radius: 9999px;
  font-size: 0.7rem; font-weight: 600;
}
.badge-lab { background: #164e63; color: #22d3ee; }
.badge-rx { background: #4a1d96; color: #c4b5fd; }
.badge-bills { background: #713f12; color: #fde047; }
.badge-inbox { background: #1e3a5f; color: #93c5fd; }
.badge-dialysis { background: #14532d; color: #86efac; }
.badge-review { background: #3f3f46; color: #d4d4d8; }
.badge-approved { background: #14532d; color: #86efac; }
.badge-pending { background: #713f12; color: #fde047; }
.badge-success { background: #14532d; color: #86efac; }
.badge-failure { background: #450a0a; color: #fca5a5; }

/* ── Category chips ── */
.chips { display: flex; flex-wrap: wrap; gap: 8px; }
.chip {
  display: flex; align-items: center; gap: 8px;
  padding: 10px 16px; border-radius: 10px;
  background: var(--bg); border: 1px solid var(--bg3);
  font-size: 0.85rem;
}
.chip .cnt { font-weight: 700; color: var(--accent); font-size: 1.1rem; }

/* ── Company cards ── */
.company-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 12px;
}
.company-card {
  background: var(--bg); border: 1px solid var(--bg3);
  border-radius: 10px; padding: 14px; text-align: center;
}
.company-card .name { font-size: 0.8rem; color: var(--muted); margin-bottom: 6px; }
.company-card .count { font-size: 1.6rem; font-weight: 700; color: var(--accent); }
.company-card .size { font-size: 0.7rem; color: var(--muted); }

/* ── SVG Chart ── */
.chart-container { width: 100%; overflow-x: auto; }
.chart-container svg { display: block; margin: 0 auto; }

/* ── Workflow pipeline ── */
.pipeline { display: flex; gap: 4px; margin-bottom: 20px; flex-wrap: wrap; }
.pipe-step {
  flex: 1; min-width: 120px; text-align: center;
  padding: 14px 10px; border-radius: 10px;
  background: var(--bg); border: 1px solid var(--bg3);
  position: relative;
}
.pipe-step .step-label { font-size: 0.7rem; color: var(--muted); text-transform: uppercase; }
.pipe-step .step-count { font-size: 1.6rem; font-weight: 700; margin: 4px 0; }
.pipe-step.s-received .step-count { color: var(--muted); }
.pipe-step.s-classified .step-count { color: var(--accent); }
.pipe-step.s-pending .step-count { color: var(--yellow); }
.pipe-step.s-approved .step-count { color: var(--green); }

/* ── Pagination ── */
.pagination {
  display: flex; align-items: center; justify-content: center;
  gap: 8px; margin-top: 16px;
}
.pagination button {
  padding: 6px 14px; border-radius: 8px; border: 1px solid var(--bg3);
  background: var(--bg); color: var(--text); cursor: pointer; font-size: 0.85rem;
}
.pagination button:hover { border-color: var(--accent); }
.pagination button:disabled { opacity: .4; cursor: default; }
.pagination .page-info { font-size: 0.85rem; color: var(--muted); }

/* ── Filter bar ── */
.filter-bar {
  display: flex; gap: 10px; margin-bottom: 16px; flex-wrap: wrap; align-items: center;
}
.filter-bar select, .filter-bar input {
  padding: 8px 12px; border-radius: 8px; border: 1px solid var(--bg3);
  background: var(--bg); color: var(--text); font-size: 0.85rem;
}
.filter-bar select:focus, .filter-bar input:focus {
  outline: none; border-color: var(--accent);
}

/* ── Loading / Error ── */
.loading { text-align: center; padding: 40px; color: var(--muted); }
.loading .spinner {
  display: inline-block; width: 24px; height: 24px; border: 3px solid var(--bg3);
  border-top-color: var(--accent); border-radius: 50%;
  animation: spin .8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.error-box {
  background: #450a0a; border: 1px solid var(--red);
  border-radius: 8px; padding: 12px; margin-bottom: 16px; color: #fca5a5;
  display: none;
}

/* ── Footer ── */
.footer {
  text-align: center; padding: 16px; font-size: 0.75rem; color: var(--muted);
}

/* ── Responsive ── */
@media (max-width: 600px) {
  .header { padding: 12px 16px; }
  .header h1 { font-size: 1.1rem; }
  .kpi .value { font-size: 1.4rem; }
  .tab { padding: 10px 14px; font-size: 0.8rem; }
  .content { padding: 12px 8px; }
  .pipeline { flex-direction: column; }
}
</style>
</head>
<body>

<!-- Header -->
<div class="header">
  <h1>🏥 RANONG RAKTAI AI</h1>
  <div class="meta">
    <a href="#" id="lab-dash-link" style="color:var(--teal);font-size:.85rem;font-weight:600;">🔬 Lab & Dialysis</a>
    <a href="#" id="ocr-review-link" style="color:var(--orange);font-size:.85rem;font-weight:600;">🔍 OCR Review</a>
    <span>GCS: <span class="dot" id="gcs-dot"></span> <span id="gcs-label">—</span></span>
    <span>โหมด: <span id="env-label">—</span></span>
    <span id="time-label">—</span>
  </div>
</div>

<!-- Tabs -->
<div class="tabs">
  <div class="tab active" data-tab="overview">📊 ภาพรวม</div>
  <div class="tab" data-tab="documents">📁 เอกสาร</div>
  <div class="tab" data-tab="bills">🧾 บิล/OCR</div>
  <div class="tab" data-tab="labs">🔬 ผลแล็บ</div>
  <div class="tab" data-tab="audit">📋 ตรวจสอบ</div>
</div>

<div class="content">
  <div id="global-error" class="error-box"></div>

  <!-- ═══ OVERVIEW ═══ -->
  <div class="panel active" id="panel-overview">
    <div class="kpi-grid" id="kpi-grid"></div>

    <div class="section">
      <h2>📊 เอกสารตามหมวดหมู่</h2>
      <div class="chart-container" id="cat-chart"></div>
      <div class="chips" id="cat-chips" style="margin-top:16px"></div>
    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
      <div class="section">
        <h2>⚡ กิจกรรมล่าสุด</h2>
        <div id="recent-activity"><div class="loading"><div class="spinner"></div></div></div>
      </div>
      <div class="section">
        <h2>📋 Workflow สถานะเอกสาร</h2>
        <div class="pipeline" id="workflow-pipeline"></div>
      </div>
    </div>
  </div>

  <!-- ═══ DOCUMENTS ═══ -->
  <div class="panel" id="panel-documents">
    <div class="section">
      <h2>📁 รายการเอกสารทั้งหมด</h2>
      <div class="filter-bar">
        <select id="doc-cat-filter">
          <option value="">ทุกหมวด</option>
          <option value="lab">ผลแล็บ</option>
          <option value="bills">ใบเสร็จ</option>
          <option value="rx">ใบสั่งยา</option>
          <option value="dialysis">ฟอกไต</option>
          <option value="inbox">กล่องข้อความ</option>
          <option value="review">รอตรวจสอบ</option>
        </select>
        <input type="text" id="doc-search" placeholder="ค้นหา รหัส/วันที่/บริษัท..." style="flex:1;min-width:180px">
      </div>
      <div id="doc-table-wrap"><div class="loading"><div class="spinner"></div></div></div>
      <div class="pagination" id="doc-pagination"></div>
    </div>
  </div>

  <!-- ═══ BILLS ═══ -->
  <div class="panel" id="panel-bills">
    <div class="section">
      <h2>🏢 สรุปบิลตามบริษัท</h2>
      <div class="company-grid" id="company-grid"></div>
    </div>
    <div class="section">
      <h2>🧾 บิลล่าสุด</h2>
      <div id="bills-table"><div class="loading"><div class="spinner"></div></div></div>
    </div>
  </div>

  <!-- ═══ LABS ═══ -->
  <div class="panel" id="panel-labs">
    <div class="kpi-grid" id="lab-kpi-grid"></div>
    <div class="section">
      <h2>🔬 ผลแล็บล่าสุด</h2>
      <div id="labs-table"><div class="loading"><div class="spinner"></div></div></div>
    </div>
  </div>

  <!-- ═══ AUDIT ═══ -->
  <div class="panel" id="panel-audit">
    <div class="section">
      <h2>📋 ประวัติการดำเนินการ</h2>
      <div class="filter-bar">
        <input type="month" id="audit-month" style="color-scheme:dark">
        <select id="audit-category">
          <option value="">ทุกหมวด</option>
          <option value="security">ความปลอดภัย</option>
          <option value="clinical">ทางคลินิก</option>
          <option value="document">เอกสาร</option>
          <option value="patient">ผู้ป่วย</option>
          <option value="admin">ผู้ดูแล</option>
          <option value="system">ระบบ</option>
        </select>
      </div>
      <div id="audit-table"><div class="loading"><div class="spinner"></div></div></div>
    </div>
    <div class="section">
      <h2>❌ รายการข้อผิดพลาดล่าสุด</h2>
      <div id="errors-table"><div class="loading"><div class="spinner"></div></div></div>
    </div>
  </div>
</div>

<div class="footer">
  อัปเดตอัตโนมัติทุก 30 วินาที — <span id="last-update">—</span>
</div>

<script>
const SECRET = new URLSearchParams(location.search).get('secret') || '';
const API = '/admin/api/v2';
document.getElementById('ocr-review-link').href = '/admin/v2/ocr-review?secret=' + encodeURIComponent(SECRET);
document.getElementById('lab-dash-link').href = '/admin/v2/lab-dashboard?secret=' + encodeURIComponent(SECRET);
const CAT_NAMES = {
  lab:'ผลแล็บ', rx:'ใบสั่งยา', bills:'ใบเสร็จ', inbox:'กล่องข้อความ',
  dialysis:'ฟอกไต', review:'รอตรวจสอบ', other:'อื่นๆ'
};
const CAT_COLORS = {
  lab:'#22d3ee', rx:'#c4b5fd', bills:'#fde047', inbox:'#93c5fd',
  dialysis:'#86efac', review:'#d4d4d8', other:'#fb923c'
};
const CAT_BADGE = {
  lab:'badge-lab', rx:'badge-rx', bills:'badge-bills',
  inbox:'badge-inbox', dialysis:'badge-dialysis', review:'badge-review'
};

let docPage = 1;

// ── Tabs ──
document.querySelectorAll('.tab').forEach(t => {
  t.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    document.getElementById('panel-' + t.dataset.tab).classList.add('active');
    location.hash = t.dataset.tab;
    loadTab(t.dataset.tab);
  });
});
// Restore tab from hash
if (location.hash) {
  const h = location.hash.slice(1);
  const t = document.querySelector('.tab[data-tab="' + h + '"]');
  if (t) t.click();
}

async function apiFetch(path) {
  const r = await fetch(API + path + (path.includes('?') ? '&' : '?') + 'secret=' + encodeURIComponent(SECRET));
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}

function showError(msg) {
  const el = document.getElementById('global-error');
  el.textContent = msg; el.style.display = 'block';
  setTimeout(() => { el.style.display = 'none'; }, 8000);
}

function badge(cat) {
  return '<span class="badge ' + (CAT_BADGE[cat] || 'badge-review') + '">' + (CAT_NAMES[cat] || cat) + '</span>';
}

function statusBadge(s) {
  if (s === 'approved') return '<span class="badge badge-approved">อนุมัติแล้ว</span>';
  if (s === 'pending_review') return '<span class="badge badge-pending">รอตรวจ</span>';
  return '<span class="badge badge-review">' + (s || '—') + '</span>';
}

// ═══ OVERVIEW ═══
async function loadOverview() {
  try {
    const [stats, wf] = await Promise.all([
      apiFetch('/stats'),
      apiFetch('/workflow'),
    ]);

    // KPI cards
    const kpis = [
      { label: 'เอกสารทั้งหมด', value: stats.total_docs, cls: '' },
      { label: 'สถานะ GCS', value: stats.gcs_ready ? 'พร้อม' : 'ไม่พร้อม', cls: stats.gcs_ready ? 'ok' : 'err' },
      { label: 'แจ้งเตือนวิกฤต', value: stats.alert_count, cls: stats.alert_count > 0 ? 'warn' : 'ok' },
      { label: 'แล็บอนุมัติ', value: stats.approved_lab_count, cls: 'ok' },
      { label: 'แล็บรอตรวจ', value: stats.pending_lab_count, cls: stats.pending_lab_count > 0 ? 'warn' : 'ok' },
      { label: 'ขนาดรวม', value: stats.total_size_mb + ' MB', cls: '' },
      { label: 'แพทย์', value: stats.physician_count, cls: '' },
      { label: 'ผู้ใช้อนุมัติ', value: stats.authorized_count, cls: '' },
    ];
    document.getElementById('kpi-grid').innerHTML = kpis.map(k =>
      '<div class="kpi"><div class="label">' + k.label + '</div><div class="value ' + k.cls + '">' + k.value + '</div></div>'
    ).join('');

    // Header
    document.getElementById('gcs-dot').className = 'dot ' + (stats.gcs_ready ? 'ok' : 'err');
    document.getElementById('gcs-label').textContent = stats.gcs_ready ? 'พร้อม' : 'ไม่พร้อม';
    document.getElementById('env-label').textContent = stats.app_env;
    document.getElementById('time-label').textContent = stats.server_time;

    // Category chart (SVG bar chart)
    const cats = Object.entries(stats.categories || {});
    if (cats.length) {
      const maxVal = Math.max(...cats.map(c => c[1]));
      const barH = 28, gap = 6, chartW = 500, labelW = 90;
      const svgH = cats.length * (barH + gap);
      let svg = '<svg viewBox="0 0 ' + (chartW + 60) + ' ' + svgH + '" width="100%" style="max-width:600px">';
      cats.sort((a, b) => b[1] - a[1]);
      cats.forEach(([cat, cnt], i) => {
        const y = i * (barH + gap);
        const w = maxVal > 0 ? (cnt / maxVal) * (chartW - labelW) : 0;
        const color = CAT_COLORS[cat] || '#94a3b8';
        svg += '<text x="0" y="' + (y + barH / 2 + 4) + '" fill="' + color + '" font-size="12">' + (CAT_NAMES[cat] || cat) + '</text>';
        svg += '<rect x="' + labelW + '" y="' + y + '" width="' + w + '" height="' + barH + '" rx="4" fill="' + color + '" opacity=".7"/>';
        svg += '<text x="' + (labelW + w + 6) + '" y="' + (y + barH / 2 + 4) + '" fill="' + color + '" font-size="12" font-weight="700">' + cnt + '</text>';
      });
      svg += '</svg>';
      document.getElementById('cat-chart').innerHTML = svg;
    }

    // Category chips
    document.getElementById('cat-chips').innerHTML = cats.map(([cat, cnt]) =>
      '<div class="chip">' + badge(cat) + '<span class="cnt">' + cnt + '</span></div>'
    ).join('');

    // Recent activity
    const ra = stats.recent_activity || [];
    if (ra.length) {
      document.getElementById('recent-activity').innerHTML = ra.map(a =>
        '<div style="padding:6px 0;border-bottom:1px solid rgba(71,85,105,.3);font-size:.85rem">'
        + '<span style="color:var(--muted)">' + a.date + '</span> '
        + badge(a.category) + ' '
        + '<span style="font-family:monospace;color:var(--accent)">' + a.doc_id + '</span>'
        + (a.bill_company ? ' <span style="color:var(--orange)">🏢 ' + a.bill_company + '</span>' : '')
        + '</div>'
      ).join('');
    } else {
      document.getElementById('recent-activity').innerHTML = '<div style="color:var(--muted);padding:20px;text-align:center">ไม่มีกิจกรรม</div>';
    }

    // Workflow pipeline
    const wfCounts = wf.counts || {};
    const steps = [
      { key: 'received', label: 'รับแล้ว', cls: 's-received' },
      { key: 'classified', label: 'จำแนกแล้ว', cls: 's-classified' },
      { key: 'pending_review', label: 'รอตรวจ', cls: 's-pending' },
      { key: 'approved', label: 'อนุมัติ', cls: 's-approved' },
    ];
    document.getElementById('workflow-pipeline').innerHTML = steps.map(s =>
      '<div class="pipe-step ' + s.cls + '">'
      + '<div class="step-label">' + s.label + '</div>'
      + '<div class="step-count">' + (wfCounts[s.key] || 0) + '</div>'
      + '</div>'
    ).join('<div style="display:flex;align-items:center;color:var(--bg3);font-size:1.2rem">→</div>');

    document.getElementById('last-update').textContent = new Date().toLocaleTimeString('th-TH');
    document.getElementById('global-error').style.display = 'none';
  } catch (e) {
    showError('โหลดข้อมูลภาพรวมไม่สำเร็จ: ' + e.message);
  }
}

// ═══ DOCUMENTS ═══
async function loadDocuments() {
  try {
    const cat = document.getElementById('doc-cat-filter').value;
    const search = document.getElementById('doc-search').value;
    let url = '/documents?page=' + docPage + '&per_page=20';
    if (cat) url += '&category=' + cat;
    if (search) url += '&search=' + encodeURIComponent(search);

    const data = await apiFetch(url);
    const items = data.items || [];

    if (!items.length) {
      document.getElementById('doc-table-wrap').innerHTML = '<div style="text-align:center;padding:30px;color:var(--muted)">ไม่พบเอกสาร</div>';
      document.getElementById('doc-pagination').innerHTML = '';
      return;
    }

    let html = '<table><thead><tr><th>รหัส</th><th>หมวด</th><th>วันที่</th><th>เจ้าของ</th><th>ขนาด</th><th>OCR</th><th>บริษัท</th><th>สถานะ</th></tr></thead><tbody>';
    items.forEach(d => {
      html += '<tr><td style="font-family:monospace;color:var(--accent)">' + d.doc_id + '</td>'
        + '<td>' + badge(d.category) + '</td>'
        + '<td>' + (d.upload_date || '—') + '</td>'
        + '<td>' + d.owner + '</td>'
        + '<td>' + d.size_kb + ' KB</td>'
        + '<td>' + (d.ocr_status || '—') + '</td>'
        + '<td>' + (d.bill_company ? '<span style="color:var(--orange)">' + d.bill_company + '</span>' : '—') + '</td>'
        + '<td>' + statusBadge(d.review_status) + '</td></tr>';
    });
    html += '</tbody></table>';
    document.getElementById('doc-table-wrap').innerHTML = html;

    // Pagination
    const pg = document.getElementById('doc-pagination');
    pg.innerHTML = '<button ' + (data.page <= 1 ? 'disabled' : '') + ' onclick="docPage--;loadDocuments()">← ก่อนหน้า</button>'
      + '<span class="page-info">หน้า ' + data.page + ' / ' + data.total_pages + ' (ทั้งหมด ' + data.total + ')</span>'
      + '<button ' + (data.page >= data.total_pages ? 'disabled' : '') + ' onclick="docPage++;loadDocuments()">ถัดไป →</button>';
  } catch (e) {
    showError('โหลดเอกสารไม่สำเร็จ: ' + e.message);
  }
}
// Filters
document.getElementById('doc-cat-filter').addEventListener('change', () => { docPage = 1; loadDocuments(); });
let _searchTimer;
document.getElementById('doc-search').addEventListener('input', () => {
  clearTimeout(_searchTimer);
  _searchTimer = setTimeout(() => { docPage = 1; loadDocuments(); }, 400);
});

// ═══ BILLS ═══
async function loadBills() {
  try {
    const data = await apiFetch('/bills');
    const companies = data.companies || {};

    // Company grid
    const entries = Object.entries(companies).sort((a, b) => b[1].count - a[1].count);
    if (entries.length) {
      document.getElementById('company-grid').innerHTML = entries.map(([name, info]) =>
        '<div class="company-card">'
        + '<div class="name">' + name + '</div>'
        + '<div class="count">' + info.count + '</div>'
        + '<div class="size">' + (info.total_size / 1024).toFixed(1) + ' KB</div>'
        + '</div>'
      ).join('');
    } else {
      document.getElementById('company-grid').innerHTML = '<div style="text-align:center;padding:20px;color:var(--muted)">ไม่มีข้อมูลบิล</div>';
    }

    // Recent bills table
    const bills = data.recent_bills || [];
    if (bills.length) {
      let html = '<table><thead><tr><th>รหัส</th><th>บริษัท</th><th>วันที่</th><th>ขนาด</th><th>OCR</th></tr></thead><tbody>';
      bills.forEach(b => {
        html += '<tr><td style="font-family:monospace;color:var(--accent)">' + b.doc_id + '</td>'
          + '<td style="color:var(--orange);font-weight:600">' + b.company + '</td>'
          + '<td>' + b.date + '</td>'
          + '<td>' + b.size_kb + ' KB</td>'
          + '<td>' + (b.ocr_status || '—') + '</td></tr>';
      });
      html += '</tbody></table>';
      document.getElementById('bills-table').innerHTML = html;
    } else {
      document.getElementById('bills-table').innerHTML = '<div style="text-align:center;padding:20px;color:var(--muted)">ไม่มีบิลล่าสุด</div>';
    }
  } catch (e) {
    showError('โหลดข้อมูลบิลไม่สำเร็จ: ' + e.message);
  }
}

// ═══ LABS ═══
async function loadLabs() {
  try {
    const data = await apiFetch('/labs');

    // KPI
    const kpis = [
      { label: 'ผลแล็บทั้งหมด', value: data.total_labs, cls: '' },
      { label: 'อนุมัติแล้ว', value: data.approved, cls: 'ok' },
      { label: 'รอตรวจ', value: data.pending, cls: data.pending > 0 ? 'warn' : 'ok' },
      { label: 'บันทึกฟอกไต', value: data.dialysis_count, cls: '' },
    ];
    document.getElementById('lab-kpi-grid').innerHTML = kpis.map(k =>
      '<div class="kpi"><div class="label">' + k.label + '</div><div class="value ' + k.cls + '">' + k.value + '</div></div>'
    ).join('');

    // Lab table
    const labs = data.lab_results || [];
    if (labs.length) {
      let html = '<table><thead><tr><th>รหัสเอกสาร</th><th>วันเก็บตัวอย่าง</th><th>จำนวนรายการ</th><th>ค่าตรวจ</th><th>สถานะ</th><th>ตรวจสอบโดย</th><th>คำเตือน</th></tr></thead><tbody>';
      labs.forEach(l => {
        const analytes = (l.analytes || []).join(', ') || '—';
        html += '<tr><td style="font-family:monospace;color:var(--accent)">' + l.doc_id + '</td>'
          + '<td>' + (l.specimen_date || '—') + '</td>'
          + '<td style="text-align:center">' + l.result_count + '</td>'
          + '<td style="font-size:.8rem;max-width:200px;overflow:hidden;text-overflow:ellipsis">' + analytes + '</td>'
          + '<td>' + statusBadge(l.status) + '</td>'
          + '<td>' + l.reviewed_by + '</td>'
          + '<td>' + (l.warnings > 0 ? '<span style="color:var(--yellow)">⚠ ' + l.warnings + '</span>' : '—') + '</td></tr>';
      });
      html += '</tbody></table>';
      document.getElementById('labs-table').innerHTML = html;
    } else {
      document.getElementById('labs-table').innerHTML = '<div style="text-align:center;padding:30px;color:var(--muted)">ไม่มีข้อมูลผลแล็บ</div>';
    }
  } catch (e) {
    showError('โหลดข้อมูลผลแล็บไม่สำเร็จ: ' + e.message);
  }
}

// ═══ AUDIT ═══
async function loadAudit() {
  try {
    const monthEl = document.getElementById('audit-month');
    if (!monthEl.value) monthEl.value = new Date().toISOString().slice(0, 7);
    const cat = document.getElementById('audit-category').value;

    const [auditData, errData] = await Promise.all([
      apiFetch('/audit?month=' + monthEl.value + (cat ? '&category=' + cat : '') + '&limit=100'),
      apiFetch('/errors'),
    ]);

    // Audit table
    const events = auditData.events || [];
    if (events.length) {
      let html = '<table><thead><tr><th>เวลา</th><th>การดำเนินการ</th><th>ผู้กระทำ</th><th>หมวด</th><th>ผลลัพธ์</th><th>เอกสาร</th></tr></thead><tbody>';
      events.forEach(e => {
        const outBadge = e.outcome === 'success' ? 'badge-success' : 'badge-failure';
        html += '<tr><td style="font-size:.8rem">' + (e.timestamp || '—') + '</td>'
          + '<td>' + e.action + '</td>'
          + '<td style="font-family:monospace">' + e.actor + '</td>'
          + '<td>' + (e.category || '—') + '</td>'
          + '<td><span class="badge ' + outBadge + '">' + e.outcome + '</span></td>'
          + '<td style="font-family:monospace;color:var(--accent)">' + (e.doc_ref || '—') + '</td></tr>';
      });
      html += '</tbody></table>';
      document.getElementById('audit-table').innerHTML = html;
    } else {
      document.getElementById('audit-table').innerHTML = '<div style="text-align:center;padding:30px;color:var(--muted)">ไม่มีรายการตรวจสอบ</div>';
    }

    // Errors table
    const errors = errData.errors || [];
    if (errors.length) {
      let html = '<table><thead><tr><th>เวลา</th><th>การดำเนินการ</th><th>หมวด</th><th>รายละเอียด</th></tr></thead><tbody>';
      errors.forEach(e => {
        const details = Object.entries(e.details || {}).map(([k, v]) => k + ': ' + v).join(', ') || '—';
        html += '<tr><td style="font-size:.8rem">' + (e.timestamp || '—') + '</td>'
          + '<td style="color:var(--red)">' + e.action + '</td>'
          + '<td>' + (e.category || '—') + '</td>'
          + '<td style="font-size:.8rem;max-width:300px;overflow:hidden;text-overflow:ellipsis">' + details + '</td></tr>';
      });
      html += '</tbody></table>';
      document.getElementById('errors-table').innerHTML = html;
    } else {
      document.getElementById('errors-table').innerHTML = '<div style="text-align:center;padding:20px;color:var(--green)">✓ ไม่มีข้อผิดพลาด</div>';
    }
  } catch (e) {
    showError('โหลดข้อมูลตรวจสอบไม่สำเร็จ: ' + e.message);
  }
}
document.getElementById('audit-month').addEventListener('change', loadAudit);
document.getElementById('audit-category').addEventListener('change', loadAudit);

// ── Tab loader ──
function loadTab(tab) {
  switch (tab) {
    case 'overview': loadOverview(); break;
    case 'documents': loadDocuments(); break;
    case 'bills': loadBills(); break;
    case 'labs': loadLabs(); break;
    case 'audit': loadAudit(); break;
  }
}

// ── Initial load + auto-refresh ──
loadOverview();
setInterval(() => {
  const active = document.querySelector('.tab.active');
  if (active) loadTab(active.dataset.tab);
}, 30000);
</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════
# Dashboard V2 route
# ═══════════════════════════════════════════════════════════

@dashboard_bp.route("/admin/v2/dashboard", methods=["GET"])
def dashboard_v2_page():
    """Dashboard V2 — comprehensive admin UI."""
    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    auth_param = request.args.get("secret", "")
    auth_header = request.headers.get("X-Admin-Secret", "")
    if not admin_secret or (auth_header != admin_secret and auth_param != admin_secret):
        return (
            "<html><body style='font-family:sans-serif;text-align:center;"
            "padding:60px;background:#0f172a;color:#e2e8f0'>"
            "<h2>🔒 ต้องการรหัสผู้ดูแล</h2>"
            "<p style='color:#94a3b8'>เข้าถึง: /admin/v2/dashboard?secret=YOUR_ADMIN_SECRET</p>"
            "</body></html>"
        ), 401
    return DASHBOARD_V2_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


def register_dashboard(app, storage_ref, doc_index_ref, audit_ref):
    """Register dashboard blueprint on the Flask app."""
    global _storage, _doc_index, _audit_logger
    _storage = storage_ref
    _doc_index = doc_index_ref
    _audit_logger = audit_ref
    app.register_blueprint(dashboard_bp)
    logger.info("Dashboard V2: registered on /admin/v2/dashboard")
