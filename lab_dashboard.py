#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lab Tracking & Dialysis Dashboard — RANONG RAKTAI AI 2.0

Feature 5: ระบบติดตามผลแล็บและข้อมูลฟอกไต

ฟีเจอร์:
  - Lab Results Viewer: ดูผลแล็บของแต่ละเอกสาร
  - Trend Charts: กราฟค่าแล็บเปลี่ยนแปลงตามเวลา
  - Dialysis Dashboard: สรุปข้อมูลฟอกไต
  - Alert Summary: รายงานค่าผิดปกติ

กฎความปลอดภัย:
  - ใช้ hashed IDs — ไม่แสดง PHI
  - ADMIN_SECRET auth ทุก endpoint
  - Read-only — ไม่แก้ไขข้อมูล
  - ไม่ log เนื้อหาเอกสาร
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)

lab_dashboard_bp = Blueprint("lab_dashboard", __name__)

_storage = None
_doc_index = None
_audit_logger = None

LAB_RESULTS_PREFIX = "_lab_results/"
ALERTS_PREFIX = "_alerts/"
DIALYSIS_FOLDER = "05_DIALYSIS_RECORDS/"


def _check_admin_secret() -> bool:
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

@lab_dashboard_bp.route("/admin/api/v2/lab-tracking/summary", methods=["GET"])
def lab_tracking_summary():
    """สรุปข้อมูลแล็บ + ฟอกไต."""
    if not _check_admin_secret():
        return _unauthorized()

    # Lab reports from GCS
    lab_reports = []
    try:
        files = _storage.list_files(LAB_RESULTS_PREFIX) if _storage else []
        for f in files[:200]:
            try:
                data = _storage.get(f)
                if data:
                    report = json.loads(data)
                    lab_reports.append(report)
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"Lab tracking: error reading lab results: {e}")

    # Dialysis docs from index
    docs = _doc_index._docs if _doc_index else []
    dialysis_docs = [d for d in docs if d.get("category") == "dialysis"]
    lab_docs = [d for d in docs if d.get("category") == "lab"]

    # Alerts
    alert_count = 0
    try:
        alert_files = _storage.list_files(ALERTS_PREFIX) if _storage else []
        alert_count = len(alert_files)
    except Exception:
        pass

    # Count lab report statuses
    pending = sum(1 for r in lab_reports if r.get("status") == "pending_review")
    approved = sum(1 for r in lab_reports if r.get("status") == "approved")
    rejected = sum(1 for r in lab_reports if r.get("status") == "rejected")

    # Count analytes with flags
    flagged_count = 0
    total_analytes = 0
    for r in lab_reports:
        for result in r.get("results", []):
            total_analytes += 1
            if result.get("flags"):
                flagged_count += 1

    return jsonify({
        "lab_reports": {
            "total": len(lab_reports),
            "pending_review": pending,
            "approved": approved,
            "rejected": rejected,
        },
        "lab_documents": len(lab_docs),
        "dialysis_documents": len(dialysis_docs),
        "alerts": alert_count,
        "analytes": {
            "total": total_analytes,
            "flagged": flagged_count,
        },
    }), 200


@lab_dashboard_bp.route("/admin/api/v2/lab-tracking/reports", methods=["GET"])
def lab_tracking_reports():
    """รายการ Lab Reports."""
    if not _check_admin_secret():
        return _unauthorized()

    status_filter = request.args.get("status", "")
    page = max(1, int(request.args.get("page", 1)))
    per_page = min(50, max(5, int(request.args.get("per_page", 20))))

    lab_reports = []
    try:
        files = _storage.list_files(LAB_RESULTS_PREFIX) if _storage else []
        for f in files[:500]:
            try:
                data = _storage.get(f)
                if data:
                    report = json.loads(data)
                    if status_filter and report.get("status") != status_filter:
                        continue
                    lab_reports.append({
                        "source_document_id": report.get("source_document_id", "")[:8],
                        "specimen_date": report.get("specimen_date", "unknown"),
                        "status": report.get("status", "pending_review"),
                        "result_count": report.get("result_count", len(report.get("results", []))),
                        "extraction_method": report.get("extraction_method", ""),
                        "extracted_at": report.get("extracted_at", ""),
                        "reviewed_by": report.get("reviewed_by", ""),
                        "reviewed_at": report.get("reviewed_at", ""),
                        "warnings": report.get("warnings", []),
                        "has_flags": any(
                            r.get("flags") for r in report.get("results", [])
                        ),
                    })
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"Lab tracking reports: {e}")

    # Sort by date desc
    lab_reports.sort(key=lambda x: x.get("extracted_at", ""), reverse=True)

    total = len(lab_reports)
    start = (page - 1) * per_page
    page_items = lab_reports[start:start + per_page]

    return jsonify({
        "items": page_items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": max(1, -(-total // per_page)),
    }), 200


@lab_dashboard_bp.route("/admin/api/v2/lab-tracking/report/<doc_id_prefix>", methods=["GET"])
def lab_tracking_report_detail(doc_id_prefix: str):
    """รายละเอียด Lab Report + ค่าแล็บทั้งหมด."""
    if not _check_admin_secret():
        return _unauthorized()

    try:
        files = _storage.list_files(LAB_RESULTS_PREFIX) if _storage else []
        for f in files:
            try:
                data = _storage.get(f)
                if data:
                    report = json.loads(data)
                    if report.get("source_document_id", "").startswith(doc_id_prefix):
                        # Truncate IDs for safety
                        report["source_document_id"] = report.get("source_document_id", "")[:8]
                        if "reviewed_by" in report and report["reviewed_by"]:
                            report["reviewed_by"] = report["reviewed_by"][:8]
                        return jsonify(report), 200
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"Lab report detail: {e}")

    return jsonify({"error": "ไม่พบ Lab Report"}), 404


@lab_dashboard_bp.route("/admin/api/v2/lab-tracking/trends", methods=["GET"])
def lab_tracking_trends():
    """ข้อมูล trend ค่าแล็บ สำหรับกราฟ."""
    if not _check_admin_secret():
        return _unauthorized()

    analyte_filter = request.args.get("analyte", "")

    # Collect all lab results with dates
    trends = {}  # analyte -> [(date, value)]

    try:
        files = _storage.list_files(LAB_RESULTS_PREFIX) if _storage else []
        for f in files[:500]:
            try:
                data = _storage.get(f)
                if data:
                    report = json.loads(data)
                    date = report.get("specimen_date", "unknown")
                    if date == "unknown":
                        date = report.get("extracted_at", "")[:10]

                    for result in report.get("results", []):
                        analyte = result.get("analyte", "")
                        if analyte_filter and analyte != analyte_filter:
                            continue
                        if analyte not in trends:
                            trends[analyte] = []
                        trends[analyte].append({
                            "date": date,
                            "value": result.get("value"),
                            "unit": result.get("unit", ""),
                            "flags": result.get("flags", []),
                            "doc_id": report.get("source_document_id", "")[:8],
                        })
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"Lab trends: {e}")

    # Sort each analyte by date
    for analyte in trends:
        trends[analyte].sort(key=lambda x: x.get("date", ""))

    return jsonify({
        "trends": trends,
        "analytes": list(trends.keys()),
    }), 200


@lab_dashboard_bp.route("/admin/api/v2/lab-tracking/dialysis", methods=["GET"])
def lab_tracking_dialysis():
    """สรุปข้อมูลฟอกไต."""
    if not _check_admin_secret():
        return _unauthorized()

    docs = _doc_index._docs if _doc_index else []
    dialysis_docs = [d for d in docs if d.get("category") == "dialysis"]

    # Group by month
    by_month = {}
    for d in dialysis_docs:
        date = d.get("upload_date", "")
        month = date[:7] if date else "unknown"
        by_month.setdefault(month, []).append({
            "doc_id": d.get("document_id", "")[:8],
            "upload_date": date,
            "ocr_status": d.get("ocr_status", ""),
            "has_text": bool(d.get("extracted_text", "").strip()),
            "size_kb": round(d.get("file_size", 0) / 1024, 1),
        })

    # Sort months
    sorted_months = sorted(by_month.keys(), reverse=True)

    return jsonify({
        "total_records": len(dialysis_docs),
        "months": {m: by_month[m] for m in sorted_months},
        "month_counts": {m: len(by_month[m]) for m in sorted_months},
    }), 200


@lab_dashboard_bp.route("/admin/api/v2/lab-tracking/alerts", methods=["GET"])
def lab_tracking_alerts():
    """รายการ alerts ค่าผิดปกติ."""
    if not _check_admin_secret():
        return _unauthorized()

    alerts = []
    try:
        files = _storage.list_files(ALERTS_PREFIX) if _storage else []
        for f in files[:100]:
            try:
                data = _storage.get(f)
                if data:
                    alert = json.loads(data)
                    # Truncate sensitive IDs
                    if "document_id" in alert:
                        alert["document_id"] = alert["document_id"][:8]
                    if "patient_hash" in alert:
                        alert["patient_hash"] = alert["patient_hash"][:8]
                    alerts.append(alert)
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"Lab alerts: {e}")

    # Sort by date desc
    alerts.sort(key=lambda x: x.get("created_at", ""), reverse=True)

    return jsonify({
        "alerts": alerts[:50],
        "total": len(alerts),
    }), 200


# ═══════════════════════════════════════════════════════════
# Lab Dashboard UI
# ═══════════════════════════════════════════════════════════

LAB_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Lab & Dialysis — RANONG RAKTAI AI</title>
<style>
:root {
  --bg:#0f172a; --bg2:#1e293b; --bg3:#334155;
  --border:#475569; --text:#e2e8f0; --muted:#94a3b8;
  --accent:#38bdf8; --green:#22c55e; --red:#ef4444;
  --yellow:#eab308; --purple:#a78bfa; --orange:#fb923c;
  --teal:#2dd4bf; --pink:#f472b6;
  --radius:12px;
}
*{margin:0;padding:0;box-sizing:border-box;}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh;}

.header{background:var(--bg2);border-bottom:1px solid var(--bg3);padding:16px 24px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;}
.header h1{font-size:1.2rem;color:var(--accent);}
.header-nav{display:flex;gap:12px;}
.header-nav a{color:var(--muted);font-size:.85rem;text-decoration:none;padding:4px 10px;border-radius:6px;transition:all .15s;}
.header-nav a:hover{color:var(--accent);background:rgba(56,189,248,.1);}

.container{max-width:1200px;margin:0 auto;padding:24px;}

/* Tabs */
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--bg3);margin-bottom:24px;}
.tab{padding:10px 20px;font-size:.9rem;color:var(--muted);cursor:pointer;border-bottom:2px solid transparent;transition:all .15s;}
.tab:hover{color:var(--text);}
.tab.active{color:var(--accent);border-bottom-color:var(--accent);}

/* Summary Cards */
.summary-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:16px;margin-bottom:24px;}
.summary-card{background:var(--bg2);border:1px solid var(--bg3);border-radius:var(--radius);padding:16px;text-align:center;}
.summary-card .s-value{font-size:2rem;font-weight:700;}
.summary-card .s-label{font-size:.75rem;color:var(--muted);margin-top:4px;}

/* Section */
.section{background:var(--bg2);border:1px solid var(--bg3);border-radius:var(--radius);padding:20px;margin-bottom:20px;}
.section h3{font-size:.95rem;color:var(--accent);margin-bottom:16px;padding-bottom:8px;border-bottom:1px solid var(--bg3);}

/* Table */
.data-table{width:100%;border-collapse:collapse;font-size:.85rem;}
.data-table th{text-align:left;color:var(--muted);padding:8px 12px;border-bottom:1px solid var(--bg3);font-size:.75rem;text-transform:uppercase;}
.data-table td{padding:8px 12px;border-bottom:1px solid rgba(71,85,105,.3);}
.data-table tr:hover td{background:rgba(56,189,248,.03);}

.badge{display:inline-block;padding:2px 8px;border-radius:9999px;font-size:.65rem;font-weight:600;}
.badge-pending{background:#713f12;color:#fde047;}
.badge-approved{background:#14532d;color:#86efac;}
.badge-rejected{background:#450a0a;color:#fca5a5;}
.badge-flag{background:#450a0a;color:#fca5a5;}

/* Chart */
.chart-container{position:relative;height:300px;margin:16px 0;}
.chart-svg{width:100%;height:100%;}
.chart-label{font-size:.7rem;fill:var(--muted);}
.chart-line{fill:none;stroke-width:2;}
.chart-dot{r:4;cursor:pointer;}
.chart-dot:hover{r:6;}
.chart-grid{stroke:var(--bg3);stroke-width:1;stroke-dasharray:4,4;}
.chart-tooltip{position:absolute;background:var(--bg2);border:1px solid var(--accent);border-radius:8px;padding:8px 12px;font-size:.8rem;pointer-events:none;display:none;z-index:10;}

/* Analyte selector */
.analyte-selector{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:16px;}
.analyte-btn{padding:4px 12px;border-radius:8px;border:1px solid var(--bg3);background:var(--bg);color:var(--text);cursor:pointer;font-size:.8rem;transition:all .15s;}
.analyte-btn:hover{border-color:var(--accent);}
.analyte-btn.active{background:rgba(56,189,248,.15);border-color:var(--accent);color:var(--accent);}

/* Dialysis month */
.month-block{margin-bottom:16px;}
.month-header{font-size:.85rem;color:var(--accent);margin-bottom:8px;display:flex;align-items:center;gap:8px;}
.month-count{background:var(--bg3);border-radius:9999px;padding:2px 8px;font-size:.7rem;color:var(--text);}

/* Alert */
.alert-item{padding:10px 14px;border-radius:8px;border-left:3px solid var(--red);background:rgba(239,68,68,.05);margin-bottom:8px;}
.alert-item .a-type{font-size:.8rem;color:var(--red);font-weight:600;}
.alert-item .a-detail{font-size:.8rem;color:var(--muted);margin-top:4px;}

.loading{text-align:center;padding:30px;color:var(--muted);}
.spinner{display:inline-block;width:20px;height:20px;border:3px solid var(--bg3);border-top-color:var(--accent);border-radius:50%;animation:spin .8s linear infinite;}
@keyframes spin{to{transform:rotate(360deg);}}

.pagination{display:flex;align-items:center;justify-content:center;gap:8px;padding:12px 0;}
.pagination button{padding:5px 12px;border-radius:6px;border:1px solid var(--bg3);background:var(--bg);color:var(--text);cursor:pointer;font-size:.8rem;}
.pagination button:disabled{opacity:.3;cursor:default;}

@media(max-width:768px){
  .container{padding:16px;}
  .summary-grid{grid-template-columns:repeat(2,1fr);}
  .chart-container{height:200px;}
}
</style>
</head>
<body>

<div class="header">
  <h1>🔬 Lab & Dialysis Tracking</h1>
  <div class="header-nav">
    <a href="#" id="link-dashboard">← Dashboard</a>
    <a href="#" id="link-ocr">🔍 OCR Review</a>
  </div>
</div>

<div class="container">
  <!-- Tabs -->
  <div class="tabs" id="tabs">
    <div class="tab active" data-tab="overview">📊 ภาพรวม</div>
    <div class="tab" data-tab="reports">📋 รายงานแล็บ</div>
    <div class="tab" data-tab="trends">📈 กราฟค่าแล็บ</div>
    <div class="tab" data-tab="dialysis">🫁 ฟอกไต</div>
    <div class="tab" data-tab="alerts">🚨 ค่าผิดปกติ</div>
  </div>

  <!-- Tab content -->
  <div id="tab-overview" class="tab-content"></div>
  <div id="tab-reports" class="tab-content" style="display:none"></div>
  <div id="tab-trends" class="tab-content" style="display:none"></div>
  <div id="tab-dialysis" class="tab-content" style="display:none"></div>
  <div id="tab-alerts" class="tab-content" style="display:none"></div>
</div>

<script>
const SECRET = new URLSearchParams(location.search).get('secret') || '';
const API = '/admin/api/v2/lab-tracking';
document.getElementById('link-dashboard').href = '/admin/v2/dashboard?secret=' + encodeURIComponent(SECRET);
document.getElementById('link-ocr').href = '/admin/v2/ocr-review?secret=' + encodeURIComponent(SECRET);

// Tab logic
const tabEls = document.querySelectorAll('.tab');
tabEls.forEach(t => t.addEventListener('click', () => {
  tabEls.forEach(x => x.classList.remove('active'));
  t.classList.add('active');
  document.querySelectorAll('.tab-content').forEach(c => c.style.display = 'none');
  document.getElementById('tab-' + t.dataset.tab).style.display = 'block';
  location.hash = t.dataset.tab;
  loadTab(t.dataset.tab);
}));

// Restore hash
const initTab = location.hash.replace('#','') || 'overview';
const initEl = document.querySelector('[data-tab="'+initTab+'"]');
if (initEl) initEl.click();

async function apiFetch(path) {
  const sep = path.includes('?') ? '&' : '?';
  const r = await fetch(API + path + sep + 'secret=' + encodeURIComponent(SECRET));
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}

function statusBadge(status) {
  const cls = {pending_review:'badge-pending',approved:'badge-approved',rejected:'badge-rejected'};
  const names = {pending_review:'รอตรวจสอบ',approved:'อนุมัติแล้ว',rejected:'ปฏิเสธ'};
  return '<span class="badge '+(cls[status]||'badge-pending')+'">'+(names[status]||status)+'</span>';
}

// ── Overview ──
async function loadOverview() {
  const el = document.getElementById('tab-overview');
  el.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
  try {
    const d = await apiFetch('/summary');
    const lr = d.lab_reports || {};
    const an = d.analytes || {};
    el.innerHTML = '<div class="summary-grid">'
      + card(lr.total || 0, 'Lab Reports', '--accent')
      + card(lr.pending_review || 0, 'รอตรวจสอบ', '--yellow')
      + card(lr.approved || 0, 'อนุมัติแล้ว', '--green')
      + card(d.dialysis_documents || 0, 'เอกสารฟอกไต', '--teal')
      + card(d.alerts || 0, 'Alerts', '--red')
      + card(an.flagged || 0, 'ค่าผิดปกติ', '--orange')
      + '</div>';
  } catch (e) {
    el.innerHTML = '<div style="color:var(--red);padding:20px">โหลดไม่สำเร็จ: '+e.message+'</div>';
  }
}

function card(val, label, color) {
  return '<div class="summary-card"><div class="s-value" style="color:var('+color+')">'+val+'</div><div class="s-label">'+label+'</div></div>';
}

// ── Reports ──
let reportsPage = 1;
async function loadReports() {
  const el = document.getElementById('tab-reports');
  el.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
  try {
    const d = await apiFetch('/reports?page='+reportsPage+'&per_page=20');
    const items = d.items || [];
    let html = '<div class="section"><h3>📋 รายงานผลแล็บ ('+d.total+' รายการ)</h3>';
    if (!items.length) {
      html += '<div style="text-align:center;padding:20px;color:var(--muted)">ไม่พบ Lab Reports ใน GCS</div>';
    } else {
      html += '<table class="data-table"><thead><tr><th>เอกสาร</th><th>วันที่</th><th>สถานะ</th><th>ค่าแล็บ</th><th>Flags</th><th>วิธีดึง</th></tr></thead><tbody>';
      items.forEach(r => {
        html += '<tr>'
          + '<td style="font-family:monospace;color:var(--accent)">'+r.source_document_id+'</td>'
          + '<td>'+r.specimen_date+'</td>'
          + '<td>'+statusBadge(r.status)+'</td>'
          + '<td>'+r.result_count+' รายการ</td>'
          + '<td>'+(r.has_flags ? '<span class="badge badge-flag">⚠ มี</span>' : '—')+'</td>'
          + '<td style="font-size:.75rem;color:var(--muted)">'+r.extraction_method+'</td>'
          + '</tr>';
      });
      html += '</tbody></table>';
    }
    html += '</div>';
    // Pagination
    html += '<div class="pagination">'
      + '<button '+(d.page<=1?'disabled':'')+' onclick="reportsPage--;loadReports()">← ก่อนหน้า</button>'
      + '<span style="font-size:.8rem;color:var(--muted)">'+d.page+'/'+d.total_pages+'</span>'
      + '<button '+(d.page>=d.total_pages?'disabled':'')+' onclick="reportsPage++;loadReports()">ถัดไป →</button>'
      + '</div>';
    el.innerHTML = html;
  } catch (e) {
    el.innerHTML = '<div style="color:var(--red);padding:20px">โหลดไม่สำเร็จ: '+e.message+'</div>';
  }
}

// ── Trends ──
let selectedAnalyte = 'Creatinine';
async function loadTrends() {
  const el = document.getElementById('tab-trends');
  el.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
  try {
    const d = await apiFetch('/trends');
    const analytes = d.analytes || [];
    const trends = d.trends || {};

    let html = '<div class="section"><h3>📈 กราฟค่าแล็บตามเวลา</h3>';

    // Analyte selector
    html += '<div class="analyte-selector">';
    if (!analytes.length) {
      html += '<span style="color:var(--muted)">ไม่มีข้อมูลแล็บ — จะแสดงกราฟเมื่อมี Lab Reports</span>';
    } else {
      analytes.forEach(a => {
        const active = a === selectedAnalyte ? ' active' : '';
        html += '<button class="analyte-btn'+active+'" onclick="selectedAnalyte=\''+a+'\';renderChart()">'+a+'</button>';
      });
    }
    html += '</div>';

    // Chart area
    html += '<div class="chart-container" id="trend-chart"></div>';
    html += '<div class="chart-tooltip" id="chart-tooltip"></div>';
    html += '</div>';

    el.innerHTML = html;
    window._trendData = trends;

    if (analytes.length && !trends[selectedAnalyte]) {
      selectedAnalyte = analytes[0];
    }
    if (analytes.length) renderChart();
  } catch (e) {
    el.innerHTML = '<div style="color:var(--red);padding:20px">โหลดไม่สำเร็จ: '+e.message+'</div>';
  }
}

function renderChart() {
  const container = document.getElementById('trend-chart');
  if (!container || !window._trendData) return;

  const data = window._trendData[selectedAnalyte] || [];
  if (!data.length) {
    container.innerHTML = '<div style="text-align:center;padding:60px;color:var(--muted)">ไม่มีข้อมูล '+selectedAnalyte+'</div>';
    return;
  }

  // Update active button
  document.querySelectorAll('.analyte-btn').forEach(b => {
    b.classList.toggle('active', b.textContent === selectedAnalyte);
  });

  const W = container.clientWidth;
  const H = container.clientHeight;
  const pad = {top:20,right:30,bottom:40,left:60};
  const cw = W - pad.left - pad.right;
  const ch = H - pad.top - pad.bottom;

  const values = data.map(d => d.value);
  const minV = Math.min(...values) * 0.9;
  const maxV = Math.max(...values) * 1.1;
  const rangeV = maxV - minV || 1;

  const unit = data[0].unit || '';

  // Build SVG
  let svg = '<svg class="chart-svg" viewBox="0 0 '+W+' '+H+'" xmlns="http://www.w3.org/2000/svg">';

  // Grid lines
  for (let i = 0; i <= 4; i++) {
    const y = pad.top + ch - (i/4)*ch;
    const val = (minV + (i/4)*rangeV).toFixed(1);
    svg += '<line x1="'+pad.left+'" y1="'+y+'" x2="'+(W-pad.right)+'" y2="'+y+'" class="chart-grid"/>';
    svg += '<text x="'+(pad.left-8)+'" y="'+(y+4)+'" class="chart-label" text-anchor="end">'+val+'</text>';
  }

  // Points + line
  const points = data.map((d, i) => {
    const x = pad.left + (i / Math.max(data.length-1,1)) * cw;
    const y = pad.top + ch - ((d.value - minV) / rangeV) * ch;
    return {x, y, ...d};
  });

  // Line path
  const pathD = points.map((p, i) => (i === 0 ? 'M' : 'L') + p.x.toFixed(1) + ',' + p.y.toFixed(1)).join(' ');
  svg += '<path d="'+pathD+'" class="chart-line" stroke="var(--accent)"/>';

  // Area
  svg += '<path d="'+pathD+' L'+points[points.length-1].x+','+( pad.top+ch)+' L'+points[0].x+','+(pad.top+ch)+' Z" fill="var(--accent)" opacity="0.08"/>';

  // Dots
  points.forEach((p, i) => {
    const color = p.flags && p.flags.length ? 'var(--red)' : 'var(--accent)';
    svg += '<circle cx="'+p.x.toFixed(1)+'" cy="'+p.y.toFixed(1)+'" class="chart-dot" fill="'+color+'" '
      + 'onmouseenter="showTip(evt,\''+p.date+'\\n'+p.value+' '+unit+(p.flags&&p.flags.length?'\\n⚠ '+p.flags.join(', '):'')+'\','+p.x+','+p.y+')" '
      + 'onmouseleave="hideTip()"/>';
  });

  // X-axis labels
  const maxLabels = Math.min(data.length, 8);
  const step = Math.max(1, Math.floor(data.length / maxLabels));
  for (let i = 0; i < data.length; i += step) {
    const p = points[i];
    svg += '<text x="'+p.x.toFixed(1)+'" y="'+(H-8)+'" class="chart-label" text-anchor="middle">'+p.date+'</text>';
  }

  // Title
  svg += '<text x="'+(W/2)+'" y="14" class="chart-label" text-anchor="middle" font-size="12" fill="var(--accent)">'+selectedAnalyte+(unit?' ('+unit+')':'')+'</text>';

  svg += '</svg>';
  container.innerHTML = svg;
}

function showTip(evt, text, x, y) {
  const tip = document.getElementById('chart-tooltip');
  tip.innerHTML = text.replace(/\\n/g, '<br>');
  tip.style.display = 'block';
  tip.style.left = x + 'px';
  tip.style.top = (y - 60) + 'px';
}
function hideTip() {
  document.getElementById('chart-tooltip').style.display = 'none';
}

// ── Dialysis ──
async function loadDialysis() {
  const el = document.getElementById('tab-dialysis');
  el.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
  try {
    const d = await apiFetch('/dialysis');
    let html = '<div class="summary-grid">'
      + card(d.total_records || 0, 'เอกสารฟอกไตทั้งหมด', '--teal')
      + card(Object.keys(d.months || {}).length, 'เดือนที่มีข้อมูล', '--accent')
      + '</div>';

    // Monthly chart
    const mc = d.month_counts || {};
    const months = Object.keys(mc).sort();
    if (months.length) {
      html += '<div class="section"><h3>🫁 จำนวนเอกสารฟอกไตรายเดือน</h3>';
      const maxC = Math.max(...Object.values(mc), 1);
      html += '<div style="display:flex;align-items:flex-end;gap:8px;height:200px;padding:10px 0">';
      months.forEach(m => {
        const c = mc[m];
        const pct = (c / maxC * 100).toFixed(0);
        html += '<div style="flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;height:100%">'
          + '<div style="font-size:.75rem;color:var(--accent);margin-bottom:4px">'+c+'</div>'
          + '<div style="width:100%;max-width:50px;height:'+pct+'%;background:var(--teal);border-radius:6px 6px 0 0;min-height:4px"></div>'
          + '<div style="font-size:.65rem;color:var(--muted);margin-top:4px;white-space:nowrap">'+m+'</div>'
          + '</div>';
      });
      html += '</div></div>';
    }

    // Monthly details
    const allMonths = d.months || {};
    Object.keys(allMonths).sort().reverse().forEach(m => {
      const docs = allMonths[m];
      html += '<div class="month-block"><div class="month-header">📅 '+m+' <span class="month-count">'+docs.length+' เอกสาร</span></div>';
      html += '<table class="data-table"><thead><tr><th>ID</th><th>วันที่</th><th>OCR</th><th>ขนาด</th></tr></thead><tbody>';
      docs.forEach(doc => {
        html += '<tr><td style="font-family:monospace;color:var(--accent)">'+doc.doc_id+'</td>'
          + '<td>'+doc.upload_date+'</td>'
          + '<td>'+(doc.has_text ? '<span class="badge badge-approved">มีข้อความ</span>' : '<span class="badge badge-pending">ไม่มี</span>')+'</td>'
          + '<td>'+doc.size_kb+' KB</td></tr>';
      });
      html += '</tbody></table></div>';
    });

    if (!Object.keys(allMonths).length) {
      html += '<div style="text-align:center;padding:40px;color:var(--muted)">ไม่มีเอกสารฟอกไตในระบบ</div>';
    }

    el.innerHTML = html;
  } catch (e) {
    el.innerHTML = '<div style="color:var(--red);padding:20px">โหลดไม่สำเร็จ: '+e.message+'</div>';
  }
}

// ── Alerts ──
async function loadAlerts() {
  const el = document.getElementById('tab-alerts');
  el.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
  try {
    const d = await apiFetch('/alerts');
    const alerts = d.alerts || [];
    let html = '<div class="section"><h3>🚨 ค่าผิดปกติ ('+d.total+' รายการ)</h3>';
    if (!alerts.length) {
      html += '<div style="text-align:center;padding:40px;color:var(--muted)">ไม่มี Alert ในระบบ — จะแสดงเมื่อตรวจพบค่าแล็บผิดปกติ</div>';
    } else {
      alerts.forEach(a => {
        html += '<div class="alert-item">'
          + '<div class="a-type">'+(a.alert_type||a.type||'alert')+' — '+(a.analyte||'')+'</div>'
          + '<div class="a-detail">'
          + 'เอกสาร: '+(a.document_id||'—')
          + (a.value ? ' | ค่า: '+a.value : '')
          + (a.created_at ? ' | '+a.created_at : '')
          + '</div></div>';
      });
    }
    html += '</div>';
    el.innerHTML = html;
  } catch (e) {
    el.innerHTML = '<div style="color:var(--red);padding:20px">โหลดไม่สำเร็จ: '+e.message+'</div>';
  }
}

function loadTab(tab) {
  switch(tab) {
    case 'overview': loadOverview(); break;
    case 'reports': loadReports(); break;
    case 'trends': loadTrends(); break;
    case 'dialysis': loadDialysis(); break;
    case 'alerts': loadAlerts(); break;
  }
}

// Initial load
loadOverview();
</script>
</body>
</html>"""


@lab_dashboard_bp.route("/admin/v2/lab-dashboard", methods=["GET"])
def lab_dashboard_page():
    """Lab & Dialysis Dashboard page."""
    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    auth_param = request.args.get("secret", "")
    auth_header = request.headers.get("X-Admin-Secret", "")
    if not admin_secret or (auth_header != admin_secret and auth_param != admin_secret):
        return (
            "<html><body style='font-family:sans-serif;text-align:center;"
            "padding:60px;background:#0f172a;color:#e2e8f0'>"
            "<h2>🔒 ต้องการรหัสผู้ดูแล</h2>"
            "<p style='color:#94a3b8'>เข้าถึง: /admin/v2/lab-dashboard?secret=YOUR_ADMIN_SECRET</p>"
            "</body></html>"
        ), 401
    return LAB_DASHBOARD_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


def register_lab_dashboard(app, storage_ref, doc_index_ref, audit_ref):
    """Register lab dashboard blueprint."""
    global _storage, _doc_index, _audit_logger
    _storage = storage_ref
    _doc_index = doc_index_ref
    _audit_logger = audit_ref
    app.register_blueprint(lab_dashboard_bp)
    logger.info("Lab Dashboard: registered on /admin/v2/lab-dashboard")
