#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LINE LIFF Mini-App — Phase F
Kidney Clinical Intelligence Platform 2.0

แอปดูผลแล็บสำหรับผู้ป่วย (Read-Only):
  1. ดูผลแล็บล่าสุด
  2. ดูกราฟแนวโน้ม eGFR / Hb / Cr
  3. ดูสรุป CKD Stage

กฎความปลอดภัย:
  - Read-Only — ผู้ป่วยดูได้อย่างเดียว ไม่มีปุ่มแก้ไข
  - PDPA Consent — ต้องยินยอมก่อนเข้าถึง
  - ยืนยันตัวตนด้วย LIFF ID token ที่ตรวจกับ LINE ฝั่ง server เท่านั้น
    แล้วจึงคำนวณ owner_hash จาก sub ที่ยืนยันแล้ว
    ห้ามรับ owner_hash หรือ userId จากฝั่ง client มาใช้กรองข้อมูล
  - แสดงเฉพาะข้อมูลของผู้ป่วยเจ้าของ token
  - ไม่แสดงข้อมูลผู้ป่วยรายอื่น
  - ห้ามส่งข้อมูลผู้ป่วยออกภายนอก

ต้องตั้ง env: LIFF_ID (สำหรับหน้าเว็บ) และ LIFF_CHANNEL_ID
(LINE Login channel ID ใช้ตรวจ aud ของ ID token)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# LIFF Configuration
# ═══════════════════════════════════════════════════════════

LIFF_ID = os.environ.get("LIFF_ID", "")  # Set in Render env

# Channel ID ของ LINE Login channel ที่ผูกกับ LIFF app
# ใช้เป็น client_id ตอนยืนยัน ID token กับ LINE — ต้องตรงกับ aud ใน token
LIFF_CHANNEL_ID = os.environ.get("LIFF_CHANNEL_ID", "").strip()

LINE_VERIFY_URL = "https://api.line.me/oauth2/v2.1/verify"


# ═══════════════════════════════════════════════════════════
# [SEC-1] LIFF Identity Verification
#
# ปัญหาเดิม: /liff/api/my-labs รับ owner_hash จาก query string ตรง ๆ
# แล้วใช้ค่านั้นกรองข้อมูลผู้ป่วย — เท่ากับให้ผู้เรียกประกาศเองว่า
# ตัวเองเป็นใคร (broken authorization / IDOR) และไม่มีการยืนยัน
# LIFF ID token ที่ใดในระบบเลย
#
# พร้อมกันนั้นยังใช้งานไม่ได้จริง เพราะ client ส่ง LINE userId ดิบ
# ขณะที่ทุกที่ในระบบเก็บ owner_hash แบบ salted SHA-256 → เทียบไม่ตรง
# ผู้ป่วยจริงจึงไม่เห็นข้อมูลของตัวเองเลย
#
# แก้: ยืนยัน ID token กับ LINE ที่ฝั่ง server แล้วคำนวณ owner_hash
# จาก sub ที่ยืนยันแล้วเท่านั้น — ไม่รับ owner_hash จากผู้เรียกอีกต่อไป
# ═══════════════════════════════════════════════════════════

class LiffAuthError(Exception):
    """ยืนยันตัวตน LIFF ไม่สำเร็จ

    ข้อความใน exception นี้ใช้สำหรับ log ฝั่ง server เท่านั้น
    ห้ามส่งกลับไปให้ client (กันการรั่วไหลของรายละเอียดภายใน)
    """


def verify_liff_id_token(
    id_token: str,
    http_post=None,
    channel_id: str | None = None,
) -> str:
    """ยืนยัน LIFF ID token กับ LINE แล้วคืน LINE userId (sub)

    Args:
        id_token: ID token จาก liff.getIDToken() ฝั่ง client
        http_post: ฟังก์ชัน post(url, data=..., timeout=...) — ใส่เพื่อทดสอบ
        channel_id: override LIFF_CHANNEL_ID (สำหรับทดสอบ)

    Returns:
        LINE userId ที่ยืนยันแล้ว (claim "sub")

    Raises:
        LiffAuthError: token ไม่ถูกต้อง หมดอายุ aud ไม่ตรง หรือยืนยันไม่ได้

    หมายเหตุด้านความปลอดภัย:
        - ต้องตรวจ aud ว่าตรงกับ channel ของเราเอง มิฉะนั้น token ที่ออก
          ให้ channel อื่นจะใช้เข้าระบบเราได้
        - ไม่ถอดรหัส token เอง ให้ LINE เป็นผู้ตรวจลายเซ็น
    """
    cid = (channel_id if channel_id is not None else LIFF_CHANNEL_ID).strip()
    if not cid:
        raise LiffAuthError("LIFF_CHANNEL_ID not configured")
    if not id_token or not isinstance(id_token, str):
        raise LiffAuthError("missing id_token")
    # กันส่ง payload ใหญ่ผิดปกติไปยัง LINE
    if len(id_token) > 8192:
        raise LiffAuthError("id_token too large")

    if http_post is None:
        import requests as _requests
        http_post = _requests.post

    try:
        resp = http_post(
            LINE_VERIFY_URL,
            data={"id_token": id_token, "client_id": cid},
            timeout=10,
        )
    except Exception as e:
        raise LiffAuthError(
            f"verify request failed: {type(e).__name__}"
        ) from None

    status = getattr(resp, "status_code", None)
    if status != 200:
        raise LiffAuthError(f"verify returned status {status}")

    try:
        payload = resp.json()
    except Exception:
        raise LiffAuthError("verify returned non-JSON") from None

    if not isinstance(payload, dict):
        raise LiffAuthError("verify returned unexpected payload")

    # ตรวจ aud ซ้ำฝั่งเรา ไม่พึ่ง LINE อย่างเดียว
    aud = payload.get("aud")
    aud_list = aud if isinstance(aud, list) else [aud]
    if cid not in [str(a) for a in aud_list if a is not None]:
        raise LiffAuthError("aud mismatch")

    sub = payload.get("sub")
    if not sub or not isinstance(sub, str):
        raise LiffAuthError("missing sub claim")

    return sub


# ═══════════════════════════════════════════════════════════
# Patient Lab Data Retrieval (Read-Only)
# ═══════════════════════════════════════════════════════════

def get_patient_labs_for_liff(
    storage: Any,
    owner_hash: str,
    limit: int = 20,
) -> dict:
    """ดึงผลแล็บของผู้ป่วย (owner_hash) สำหรับ LIFF.

    Args:
        storage: GCS storage instance
        owner_hash: LINE userId hash — ยืนยันตัวตน
        limit: จำนวนผลแล็บสูงสุด

    Returns:
        dict with patient_labs, trends, ckd_info

    Safety:
        - Read-Only — ไม่มีการแก้ไข
        - กรองเฉพาะ owner_hash ของผู้ป่วยเจ้าของ
        - ส่งเฉพาะ approved labs
    """
    result = {
        "owner_hash_prefix": owner_hash[:8] if owner_hash else "",
        "labs": [],
        "trends": {
            "egfr": [],
            "creatinine": [],
            "hemoglobin": [],
            "potassium": [],
        },
        "ckd_info": None,
        "last_visit": None,
    }

    if not owner_hash or not storage:
        return result

    try:
        if not hasattr(storage, '_bucket') or not storage._bucket:
            return result

        # Scan lab results for this patient
        lab_blobs = list(storage._bucket.list_blobs(
            prefix="_lab_results/", max_results=500
        ))

        patient_labs = []
        for lb in lab_blobs:
            try:
                ldata = json.loads(lb.download_as_text())
                # Safety: only show this patient's labs
                if ldata.get("owner_hash") != owner_hash:
                    continue
                # Safety: only show approved labs
                if ldata.get("status") != "approved":
                    continue
                patient_labs.append(ldata)
            except Exception:
                continue

        # Sort by date descending
        patient_labs.sort(
            key=lambda x: x.get("report_date", x.get("upload_date", "")),
            reverse=True,
        )

        # Limit results
        patient_labs = patient_labs[:limit]

        # Build simplified lab list (no raw data)
        for lab in patient_labs:
            lab_date = lab.get("report_date", lab.get("upload_date", ""))
            lab_entry = {
                "date": lab_date,
                "analytes": [],
            }
            for r in lab.get("results", []):
                analyte = r.get("analyte", "")
                value = r.get("value")
                unit = r.get("unit", "")
                ref_range = r.get("reference_range", "")
                flag = r.get("flag", "")
                lab_entry["analytes"].append({
                    "name": analyte,
                    "value": value,
                    "unit": unit,
                    "ref_range": ref_range,
                    "flag": flag,
                })

                # Build trend data
                if value is not None:
                    trend_point = {"date": lab_date, "value": value}
                    upper_name = analyte.upper()
                    if upper_name in ("EGFR", "GFR"):
                        result["trends"]["egfr"].append(trend_point)
                    elif upper_name in ("CR", "CREATININE", "CREA"):
                        result["trends"]["creatinine"].append(trend_point)
                    elif upper_name in ("HB", "HGB", "HEMOGLOBIN"):
                        result["trends"]["hemoglobin"].append(trend_point)
                    elif upper_name in ("K", "K+", "POTASSIUM"):
                        result["trends"]["potassium"].append(trend_point)

            result["labs"].append(lab_entry)

        # Sort trends chronologically
        for key in result["trends"]:
            result["trends"][key].sort(key=lambda x: x["date"])

        # Last visit
        if patient_labs:
            result["last_visit"] = patient_labs[0].get(
                "report_date", patient_labs[0].get("upload_date", "")
            )

        # CKD Info from registry
        try:
            reg_blobs = list(storage._bucket.list_blobs(
                prefix="_patient_master/", max_results=10
            ))
            if reg_blobs:
                # Use latest version
                reg_blobs.sort(key=lambda b: b.name, reverse=True)
                reg_data = json.loads(reg_blobs[0].download_as_text())
                patients = reg_data.get("patients", {})
                for pid, pdata in patients.items():
                    if pdata.get("owner_hash") == owner_hash:
                        result["ckd_info"] = {
                            "ckd_stage": pdata.get("ckd_stage", ""),
                            "latest_egfr": pdata.get("latest_egfr"),
                            "latest_cr": pdata.get("latest_cr"),
                        }
                        break
        except Exception:
            pass

    except Exception as e:
        logger.error(f"LIFF lab retrieval error: {e}")

    return result


# ═══════════════════════════════════════════════════════════
# LIFF HTML Template
# ═══════════════════════════════════════════════════════════

LIFF_HTML = """<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>คลินิกโรคไต — ผลแล็บของฉัน</title>
<script charset="utf-8" src="https://static.line-scdn.net/liff/edge/2/sdk.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
:root {
    --bg: #0f172a;
    --card: #1e293b;
    --border: #334155;
    --text: #e2e8f0;
    --text-muted: #94a3b8;
    --primary: #38bdf8;
    --success: #22c55e;
    --warning: #eab308;
    --danger: #ef4444;
    --accent: #8b5cf6;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    padding-bottom: 80px;
}
.header {
    background: linear-gradient(135deg, #1e3a5f 0%, #0f172a 100%);
    padding: 20px 16px 16px;
    text-align: center;
    border-bottom: 1px solid var(--border);
}
.header h1 {
    font-size: 18px;
    color: var(--primary);
    margin-bottom: 4px;
}
.header .subtitle {
    font-size: 12px;
    color: var(--text-muted);
}
.consent-overlay {
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(15, 23, 42, 0.95);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
    padding: 16px;
}
.consent-card {
    background: var(--card);
    border-radius: 16px;
    padding: 24px;
    max-width: 400px;
    width: 100%;
    border: 1px solid var(--border);
}
.consent-card h2 {
    color: var(--primary);
    font-size: 18px;
    margin-bottom: 16px;
    text-align: center;
}
.consent-card p {
    color: var(--text-muted);
    font-size: 13px;
    line-height: 1.6;
    margin-bottom: 12px;
}
.consent-card ul {
    color: var(--text-muted);
    font-size: 13px;
    margin: 0 0 20px 20px;
    line-height: 1.8;
}
.btn-consent {
    width: 100%;
    padding: 14px;
    background: var(--primary);
    color: #0f172a;
    border: none;
    border-radius: 10px;
    font-size: 16px;
    font-weight: 600;
    cursor: pointer;
}
.btn-consent:active { opacity: 0.8; }
.btn-decline {
    width: 100%;
    padding: 12px;
    background: transparent;
    color: var(--text-muted);
    border: 1px solid var(--border);
    border-radius: 10px;
    font-size: 14px;
    cursor: pointer;
    margin-top: 8px;
}

/* Tabs */
.tab-bar {
    display: flex;
    background: var(--card);
    border-bottom: 1px solid var(--border);
    position: sticky;
    top: 0;
    z-index: 100;
}
.tab-btn {
    flex: 1;
    padding: 12px 8px;
    background: none;
    border: none;
    color: var(--text-muted);
    font-size: 13px;
    cursor: pointer;
    border-bottom: 2px solid transparent;
    transition: all 0.2s;
}
.tab-btn.active {
    color: var(--primary);
    border-bottom-color: var(--primary);
}
.tab-content { display: none; padding: 16px; }
.tab-content.active { display: block; }

/* Cards */
.card {
    background: var(--card);
    border-radius: 12px;
    border: 1px solid var(--border);
    padding: 16px;
    margin-bottom: 12px;
}
.card-title {
    font-size: 14px;
    color: var(--primary);
    margin-bottom: 12px;
    font-weight: 600;
}
.lab-date {
    font-size: 12px;
    color: var(--text-muted);
    margin-bottom: 8px;
}
.lab-row {
    display: flex;
    justify-content: space-between;
    padding: 6px 0;
    border-bottom: 1px solid rgba(51, 65, 85, 0.5);
    font-size: 13px;
}
.lab-row:last-child { border-bottom: none; }
.lab-name { color: var(--text-muted); flex: 2; }
.lab-value { font-weight: 600; flex: 1; text-align: right; }
.lab-unit { color: var(--text-muted); flex: 1; text-align: right; font-size: 12px; }
.lab-value.high { color: var(--danger); }
.lab-value.low { color: var(--warning); }
.lab-value.normal { color: var(--success); }

/* CKD Badge */
.ckd-badge {
    display: inline-block;
    padding: 4px 12px;
    border-radius: 20px;
    font-size: 14px;
    font-weight: 600;
}
.ckd-1, .ckd-2 { background: rgba(34,197,94,0.2); color: #22c55e; }
.ckd-3a, .ckd-3b { background: rgba(234,179,8,0.2); color: #eab308; }
.ckd-4 { background: rgba(249,115,22,0.2); color: #f97316; }
.ckd-5 { background: rgba(239,68,68,0.2); color: #ef4444; }

.chart-container {
    position: relative;
    height: 220px;
    margin-top: 8px;
}
.loading {
    text-align: center;
    padding: 40px;
    color: var(--text-muted);
}
.disclaimer {
    text-align: center;
    padding: 12px;
    font-size: 11px;
    color: var(--danger);
    opacity: 0.8;
}
.empty-state {
    text-align: center;
    padding: 40px 20px;
    color: var(--text-muted);
}
.empty-state .icon { font-size: 40px; margin-bottom: 12px; }
</style>
</head>
<body>

<!-- PDPA Consent Overlay -->
<div class="consent-overlay" id="consentOverlay">
<div class="consent-card">
    <h2>🔒 ข้อมูลส่วนบุคคล</h2>
    <p>แอปนี้จะแสดงผลตรวจแล็บของคุณจากคลินิกโรคไต รพ.ระนอง</p>
    <p><strong>ข้อมูลที่เข้าถึง:</strong></p>
    <ul>
        <li>ผลตรวจแล็บที่แพทย์อนุมัติแล้ว</li>
        <li>แนวโน้มค่าไต (eGFR, Creatinine)</li>
        <li>สถานะ CKD Stage</li>
    </ul>
    <p>ข้อมูลเป็นแบบ <strong>อ่านอย่างเดียว</strong> และแสดงเฉพาะข้อมูลของคุณเท่านั้น
    ตามพระราชบัญญัติคุ้มครองข้อมูลส่วนบุคคล พ.ศ. 2562 (PDPA)</p>
    <button class="btn-consent" onclick="acceptConsent()">ยอมรับและดูผลแล็บ</button>
    <button class="btn-decline" onclick="declineConsent()">ไม่ยอมรับ</button>
</div>
</div>

<div id="mainApp" style="display:none;">
<div class="header">
    <h1>🏥 คลินิกโรคไต — ผลแล็บของฉัน</h1>
    <div class="subtitle" id="patientInfo">กำลังโหลด...</div>
</div>

<!-- Tab Bar -->
<div class="tab-bar">
    <button class="tab-btn active" onclick="switchTab('results')">📋 ผลแล็บ</button>
    <button class="tab-btn" onclick="switchTab('trends')">📈 แนวโน้ม</button>
    <button class="tab-btn" onclick="switchTab('ckd')">🏥 CKD</button>
</div>

<!-- Tab: ผลแล็บล่าสุด -->
<div class="tab-content active" id="tab-results">
    <div class="loading" id="loadingLabs">
        <div>⏳ กำลังโหลดผลแล็บ...</div>
    </div>
    <div id="labList"></div>
</div>

<!-- Tab: แนวโน้ม -->
<div class="tab-content" id="tab-trends">
    <div class="card">
        <div class="card-title">📈 แนวโน้ม eGFR</div>
        <div class="chart-container"><canvas id="chartEgfr"></canvas></div>
    </div>
    <div class="card">
        <div class="card-title">📈 แนวโน้ม Creatinine</div>
        <div class="chart-container"><canvas id="chartCr"></canvas></div>
    </div>
    <div class="card">
        <div class="card-title">📈 แนวโน้ม Hemoglobin</div>
        <div class="chart-container"><canvas id="chartHb"></canvas></div>
    </div>
</div>

<!-- Tab: CKD Info -->
<div class="tab-content" id="tab-ckd">
    <div id="ckdInfo">
        <div class="loading">⏳ กำลังโหลด...</div>
    </div>
</div>

<div class="disclaimer">
    ⚠️ ข้อมูลนี้เป็นข้อมูลประกอบเท่านั้น — กรุณาปรึกษาแพทย์สำหรับการรักษา<br>
    ระบบ Read-Only — ไม่สามารถแก้ไขข้อมูลได้
</div>
</div>

<script>
const LIFF_ID = '__LIFF_ID__';
const API_BASE = '__API_BASE__';
// [SEC-1] เก็บ ID token ไว้ส่งให้ server ยืนยัน — ไม่เก็บ/ไม่ส่ง userId ดิบ
let idToken = null;
let labData = null;

// สถานะ liff.init() — ต้องรู้ว่า SDK พร้อมแล้วหรือยังก่อนเรียก API
let liffReady = false;

// แสดงปัญหาให้ผู้ป่วยเห็นจริง
//
// ข้อความเดิมเขียนลง #loadingLabs เท่านั้น ซึ่งอยู่ใน #mainApp ที่ถูก
// ซ่อนอยู่หลังหน้ายินยอม PDPA ถ้าเกิดปัญหาก่อนกดยอมรับ ผู้ป่วยจึง
// ไม่เห็นอะไรเลย — เห็นแต่หน้ายินยอมที่กดแล้วไม่มีอะไรเกิดขึ้น
function showProblem(msg) {
    const box = document.getElementById('loadingLabs');
    if (box) {
        box.style.display = '';
        box.innerHTML = '<div>' + msg + '</div>';
    }
    const ov = document.getElementById('consentOverlay');
    const card = document.querySelector('.consent-card');
    if (ov && card && getComputedStyle(ov).display !== 'none') {
        card.innerHTML = '<h2>🔒</h2>' +
            '<p style="text-align:center;color:#94a3b8;">' + msg + '</p>';
    }
}

// รับประกันว่ามี ID token ก่อนเรียก API
//
// ทำไมต้องมี: acceptConsent() เดิมเรียก initApp() ทันทีโดยไม่ตรวจว่า
// idToken ถูกตั้งค่าแล้วหรือยัง ถ้าผู้ป่วยเปิดลิงก์จาก browser (ไม่ได้
// เปิดจากแอป LINE) initLiff() จะไปถึง liff.login() แล้ว return ทิ้ง
// idToken เป็น null ไว้ พอกดยอมรับ ระบบจะ POST {id_token: null}
// server ตอบ 400 และผู้ป่วยเห็นข้อความ "ไม่สามารถโหลดข้อมูลได้ HTTP 400"
// ซึ่งไม่บอกว่าต้องทำอะไรต่อ (ยืนยันแล้วจาก production 26 ต.ค. 2026)
async function ensureIdToken() {
    if (!liffReady) return null;
    if (idToken) return idToken;
    try { idToken = liff.getIDToken(); } catch (e) { idToken = null; }
    if (idToken) return idToken;

    if (!liff.isLoggedIn()) {
        // กันวนซ้ำ: ถ้าเพิ่งพยายาม login ไปแล้วรอบนี้ ไม่ redirect อีก
        let tried = false;
        try { tried = sessionStorage.getItem('liff_login_tried') === '1'; } catch (e) {}
        if (!tried) {
            try { sessionStorage.setItem('liff_login_tried', '1'); } catch (e) {}
            liff.login();
            return null;
        }
    }
    return null;
}

// ── PDPA Consent ──
async function acceptConsent() {
    document.getElementById('consentOverlay').style.display = 'none';
    document.getElementById('mainApp').style.display = 'block';
    try { localStorage.setItem('pdpa_consent', 'accepted'); } catch(e) {}
    await initApp();
}
function declineConsent() {
    if (typeof liff !== 'undefined' && liff.isInClient()) {
        liff.closeWindow();
    } else {
        document.querySelector('.consent-card').innerHTML =
            '<h2>🔒</h2><p style="text-align:center;color:#94a3b8;">'+
            'คุณปฏิเสธการเข้าถึงข้อมูล<br>สามารถเปิดใหม่ได้ตลอดเวลา</p>';
    }
}

// ── LIFF Init ──
// [SEC-1] ส่ง ID token ให้ server ยืนยันกับ LINE แทนการส่ง userId ดิบ
// server จะคำนวณ owner_hash จาก token ที่ยืนยันแล้วเอง
// ห้ามส่งตัวระบุตัวตนใด ๆ ที่ client กำหนดเองไปใช้กรองข้อมูลผู้ป่วย
async function initLiff() {
    if (!LIFF_ID || LIFF_ID === '__LIFF_ID__') {
        showProblem('❌ ระบบยังไม่ได้ตั้งค่า LIFF — กรุณาติดต่อเจ้าหน้าที่');
        return;
    }
    try {
        await liff.init({ liffId: LIFF_ID });
        liffReady = true;
        const tok = await ensureIdToken();
        if (!tok) {
            // ensureIdToken อาจสั่ง redirect ไป login อยู่ — ถ้ายังไม่มี
            // token และไม่ได้ redirect ให้บอกให้ชัดว่าต้องทำอะไร
            if (!liff.isInClient()) {
                showProblem('🔑 กรุณาเปิดหน้านี้จากแอป LINE' +
                    '<br><small>หรือเข้าสู่ระบบ LINE ในเบราว์เซอร์นี้ก่อน</small>');
            } else {
                showProblem('❌ ไม่สามารถยืนยันตัวตนได้ กรุณาเข้าใหม่อีกครั้ง');
            }
            return;
        }
        checkConsent();
    } catch (e) {
        console.error('LIFF init error:', e);
        showProblem('❌ ไม่สามารถเชื่อมต่อ LINE ได้');
    }
}

function checkConsent() {
    let consented = false;
    try { consented = localStorage.getItem('pdpa_consent') === 'accepted'; } catch(e) {}
    if (consented) {
        document.getElementById('consentOverlay').style.display = 'none';
        document.getElementById('mainApp').style.display = 'block';
        initApp();
    }
}

// ── Load Data ──
async function initApp() {
    // ห้ามยิง API โดยไม่มี token — server จะตอบ 400 และผู้ป่วยเห็นแต่
    // รหัส HTTP ซึ่งไม่บอกว่าต้องทำอะไรต่อ
    const tok = await ensureIdToken();
    if (!tok) {
        showProblem('🔑 กรุณาเปิดหน้านี้จากแอป LINE' +
            '<br><small>ระบบต้องยืนยันตัวตนกับ LINE ก่อนแสดงผลแล็บ</small>');
        return;
    }
    try {
        // [SEC-1] POST พร้อม ID token — ตัวระบุตัวตนไม่อยู่ใน URL อีกต่อไป
        // (query string ถูกบันทึกลง access log ของ proxy/แพลตฟอร์ม)
        const resp = await fetch(API_BASE + '/liff/api/my-labs', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({id_token: tok}),
        });
        // 400 = ไม่มี token, 401 = token ใช้ไม่ได้ — ผู้ป่วยต้องทำ
        // สิ่งเดียวกันทั้งสองกรณี จึงบอกให้ชัดแบบเดียวกัน
        if (resp.status === 401 || resp.status === 400) {
            idToken = null;
            showProblem('❌ ยืนยันตัวตนไม่สำเร็จ กรุณาเปิดจาก LINE อีกครั้ง');
            return;
        }
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        labData = await resp.json();
        renderLabs();
        renderTrends();
        renderCkd();
        // Patient info
        if (labData.last_visit) {
            document.getElementById('patientInfo').textContent =
                'ข้อมูลล่าสุด: ' + labData.last_visit;
        } else {
            document.getElementById('patientInfo').textContent = 'ยังไม่มีข้อมูล';
        }
    } catch (e) {
        document.getElementById('loadingLabs').innerHTML =
            '<div>❌ ไม่สามารถโหลดข้อมูลได้<br><small>' + e.message + '</small></div>';
    }
}

// ── Render Labs ──
function renderLabs() {
    const container = document.getElementById('labList');
    document.getElementById('loadingLabs').style.display = 'none';

    if (!labData || !labData.labs || labData.labs.length === 0) {
        container.innerHTML = '<div class="empty-state"><div class="icon">📋</div>'+
            '<div>ยังไม่มีผลแล็บที่อนุมัติแล้ว</div></div>';
        return;
    }

    let html = '';
    for (const lab of labData.labs) {
        html += '<div class="card">';
        html += '<div class="lab-date">📅 ' + (lab.date || 'ไม่ระบุวันที่') + '</div>';
        for (const a of lab.analytes) {
            const flagClass = a.flag === 'H' ? 'high' : a.flag === 'L' ? 'low' : 'normal';
            html += '<div class="lab-row">';
            html += '<span class="lab-name">' + a.name + '</span>';
            html += '<span class="lab-value ' + flagClass + '">' + (a.value ?? '-') + '</span>';
            html += '<span class="lab-unit">' + (a.unit || '') + '</span>';
            html += '</div>';
        }
        html += '</div>';
    }
    container.innerHTML = html;
}

// ── Render Trends ──
function renderTrends() {
    if (!labData || !labData.trends) return;

    const chartConfig = (canvas, data, label, color, targetMin, targetMax) => {
        if (!data || data.length === 0) {
            canvas.parentElement.innerHTML = '<div class="empty-state"><div>ยังไม่มีข้อมูล</div></div>';
            return;
        }
        const labels = data.map(d => d.date);
        const values = data.map(d => parseFloat(d.value));
        const datasets = [{
            label: label,
            data: values,
            borderColor: color,
            backgroundColor: color + '33',
            fill: true,
            tension: 0.3,
            pointRadius: 4,
        }];
        // Target range
        if (targetMin !== undefined && targetMax !== undefined) {
            datasets.push({
                label: 'เป้าหมาย (ล่าง)',
                data: Array(values.length).fill(targetMin),
                borderColor: '#22c55e44',
                borderDash: [5, 5],
                pointRadius: 0,
                fill: false,
            });
            datasets.push({
                label: 'เป้าหมาย (บน)',
                data: Array(values.length).fill(targetMax),
                borderColor: '#22c55e44',
                borderDash: [5, 5],
                pointRadius: 0,
                fill: false,
            });
        }
        new Chart(canvas, {
            type: 'line',
            data: { labels, datasets },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: {
                    x: { ticks: { color: '#94a3b8', font: { size: 10 } }, grid: { color: '#33415522' } },
                    y: { ticks: { color: '#94a3b8' }, grid: { color: '#33415522' } },
                },
            },
        });
    };

    chartConfig(document.getElementById('chartEgfr'),
        labData.trends.egfr, 'eGFR', '#38bdf8', 60, 90);
    chartConfig(document.getElementById('chartCr'),
        labData.trends.creatinine, 'Creatinine', '#f97316');
    chartConfig(document.getElementById('chartHb'),
        labData.trends.hemoglobin, 'Hemoglobin', '#ef4444', 10, 12);
}

// ── Render CKD ──
function renderCkd() {
    const container = document.getElementById('ckdInfo');
    if (!labData || !labData.ckd_info) {
        container.innerHTML = '<div class="empty-state"><div class="icon">🏥</div>'+
            '<div>ยังไม่มีข้อมูล CKD Stage</div></div>';
        return;
    }
    const ckd = labData.ckd_info;
    const stage = ckd.ckd_stage || 'ไม่ทราบ';
    const stageClass = 'ckd-' + stage.toLowerCase().replace('g','');

    let html = '<div class="card">';
    html += '<div class="card-title">🏥 สถานะโรคไตเรื้อรัง</div>';
    html += '<div style="text-align:center;margin:16px 0;">';
    html += '<div style="color:var(--text-muted);font-size:13px;margin-bottom:8px;">CKD Stage</div>';
    html += '<span class="ckd-badge ' + stageClass + '">' + stage + '</span>';
    html += '</div>';

    if (ckd.latest_egfr !== null && ckd.latest_egfr !== undefined) {
        html += '<div class="lab-row"><span class="lab-name">eGFR ล่าสุด</span>';
        html += '<span class="lab-value">' + ckd.latest_egfr + '</span>';
        html += '<span class="lab-unit">mL/min/1.73m²</span></div>';
    }
    if (ckd.latest_cr !== null && ckd.latest_cr !== undefined) {
        html += '<div class="lab-row"><span class="lab-name">Creatinine ล่าสุด</span>';
        html += '<span class="lab-value">' + ckd.latest_cr + '</span>';
        html += '<span class="lab-unit">mg/dL</span></div>';
    }

    html += '<div style="margin-top:16px;padding:12px;background:rgba(56,189,248,0.1);border-radius:8px;font-size:12px;color:var(--text-muted);">';
    html += '💡 <strong>CKD Stage คืออะไร?</strong><br>';
    html += 'ระดับการทำงานของไต — Stage 1 ปกติ, Stage 5 ไตวายต้องฟอกไต<br>';
    html += 'ติดตามค่า eGFR สม่ำเสมอตามคำแนะนำของแพทย์';
    html += '</div>';
    html += '</div>';
    container.innerHTML = html;
}

// ── Tab Switching ──
function switchTab(name) {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    document.getElementById('tab-' + name).classList.add('active');
    event.target.classList.add('active');
}

// ── Init ──
document.addEventListener('DOMContentLoaded', initLiff);
</script>
</body>
</html>"""


def get_liff_html(api_base: str = "") -> str:
    """สร้าง LIFF HTML พร้อม config.

    Args:
        api_base: base URL ของ API (e.g., https://ranong-raktai-ai.onrender.com)

    Safety: HTML เป็น read-only — ไม่มีปุ่มแก้ไขข้อมูล
    """
    html = LIFF_HTML
    # แทนค่าเฉพาะบรรทัดประกาศตัวแปร ไม่ใช่ทุกจุดที่มี placeholder
    #
    # บั๊กเดิม (ยืนยันจาก production 26 ต.ค. 2026): ใช้
    #     html.replace("__LIFF_ID__", LIFF_ID)
    # ซึ่ง str.replace ของ Python แทนค่า "ทุกจุด" รวมถึงบรรทัดที่หน้าเว็บ
    # ใช้ตรวจว่ายังไม่ได้ตั้งค่า หน้าเว็บที่ส่งออกไปจึงกลายเป็น
    #     const LIFF_ID = '2012225688-Qu71sdil';
    #     if (!LIFF_ID || LIFF_ID === '2012225688-Qu71sdil') {   // จริงเสมอ
    #         ❌ ระบบยังไม่ได้ตั้งค่า LIFF
    # ผลคือ liff.init() ไม่ถูกเรียกเลย หน้าผลแล็บใช้งานไม่ได้กับผู้ป่วย
    # ทุกคน แม้ env จะถูกต้องครบ และข้อความที่แสดงยังชี้ไปผิดทางด้วย
    #
    # json.dumps ป้องกันค่าที่มี quote หรือ backslash หลุดออกนอก string
    html = html.replace(
        "const LIFF_ID = '__LIFF_ID__';",
        "const LIFF_ID = " + json.dumps(LIFF_ID or "") + ";",
    )
    html = html.replace(
        "const API_BASE = '__API_BASE__';",
        "const API_BASE = " + json.dumps(api_base or "") + ";",
    )

    # ถ้าบรรทัดประกาศยังไม่ถูกแทน แปลว่า template ถูกแก้จนรูปแบบไม่ตรง
    # ปล่อยผ่านไม่ได้ เพราะหน้าเว็บจะพังแบบเงียบ ๆ อีกครั้ง
    #
    # ตรวจที่ "บรรทัดประกาศ" เท่านั้น — ตัว __LIFF_ID__ ยังต้องเหลืออยู่
    # ในบรรทัดที่ JS ใช้ตรวจว่ายังไม่ได้ตั้งค่า ซึ่งถูกต้องแล้ว
    for decl in ("const LIFF_ID = '__LIFF_ID__';",
                 "const API_BASE = '__API_BASE__';"):
        if decl in html:
            logger.error(
                "LIFF: แทนค่าบรรทัด %r ไม่สำเร็จ — รูปแบบ template ไม่ตรง",
                decl,
            )
    return html
