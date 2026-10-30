"""รายงานสรุปอัตโนมัติ — รวบรวมข้อมูลจากระบบแล้วส่ง email สรุปให้แพทย์

รายงานรายสัปดาห์/รายเดือน ประกอบด้วย:
- จำนวนเอกสารใหม่ แยกตามประเภท (แล็บ/ยา/ฟอกเลือด/บิล)
- จำนวนค่าวิกฤตที่แจ้งเตือน
- ผู้ป่วยที่มีเอกสารมากที่สุด (ไม่แสดงชื่อ — ใช้ HN เท่านั้น)
- สรุปค่าใช้จ่าย (ถ้าเปิดใช้ expense_tracker)
- สถานะระบบ (GCS, Drive, Sheets, Email)

ไม่ log ข้อมูลผู้ป่วย (PDPA)

ENV VARS:
- AUTO_REPORT_ENABLED: "true" เพื่อเปิดใช้งาน (default: false)
- AUTO_REPORT_SCHEDULE: "weekly" หรือ "monthly" (default: weekly)
"""

from __future__ import annotations

import logging
import os
from collections import Counter
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

AUTO_REPORT_ENABLED = (
    os.getenv("AUTO_REPORT_ENABLED", "").strip().lower() == "true"
)
AUTO_REPORT_SCHEDULE = os.getenv("AUTO_REPORT_SCHEDULE", "weekly").strip().lower()

# Track last sent time (in-memory, resets on deploy)
_last_sent_at: str = ""
_last_result: dict = {}

# ─── Gmail import guard ────────────────────────────────────────

try:
    import gmail_report as _gmail

    _GMAIL_AVAILABLE = True
except ImportError:
    _gmail = None  # type: ignore[assignment]
    _GMAIL_AVAILABLE = False


def is_available() -> bool:
    """ตรวจว่าระบบรายงานอัตโนมัติพร้อมใช้งาน."""
    if not _GMAIL_AVAILABLE:
        return False
    if not _gmail.is_available():
        return False
    return True


def get_status() -> dict:
    """สถานะของระบบรายงานอัตโนมัติ."""
    return {
        "enabled": AUTO_REPORT_ENABLED,
        "schedule": AUTO_REPORT_SCHEDULE,
        "gmail_available": _GMAIL_AVAILABLE,
        "email_connected": is_available(),
        "last_sent": _last_sent_at or None,
        "last_result": _last_result or None,
    }


# ─── Data gathering ────────────────────────────────────────────


def _count_docs_in_period(doc_index, days: int) -> dict:
    """นับเอกสารในช่วงเวลาที่กำหนด (ไม่ log ข้อมูลผู้ป่วย).

    Returns:
        {
            "total_docs": int,
            "new_docs": int,
            "by_category": {"lab_result": N, "prescription": N, ...},
            "critical_alerts": int,
            "top_hn": [("HN", count), ...],  # top 5 by doc count
            "needs_review": int,
        }
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    total = 0
    new_count = 0
    category_counter: Counter = Counter()
    hn_counter: Counter = Counter()
    critical = 0
    needs_review = 0

    try:
        doc_index._ensure_fresh()
    except Exception:
        pass

    for doc in getattr(doc_index, "_docs", []):
        total += 1

        upload_date = doc.get("upload_date", "")
        if not upload_date:
            continue

        # ตรวจว่าอยู่ในช่วงเวลา
        date_part = upload_date[:10] if len(upload_date) >= 10 else upload_date
        if date_part >= cutoff_str:
            new_count += 1

            category = doc.get("category", "unknown")
            category_counter[category] += 1

            hn = doc.get("patient_hn_folder", "")
            if hn:
                # ใช้แค่ HN ส่วนแรก (ก่อน _) เพื่อไม่เปิดเผยชื่อ
                hn_short = hn.split("_")[0] if "_" in hn else hn
                hn_counter[hn_short] += 1

            # นับค่าวิกฤต
            if doc.get("has_critical_values"):
                critical += 1

            # นับ needs_review
            if doc.get("review_status") == "needs_review":
                needs_review += 1

    return {
        "total_docs": total,
        "new_docs": new_count,
        "by_category": dict(category_counter.most_common(10)),
        "critical_alerts": critical,
        "top_hn": hn_counter.most_common(5),
        "needs_review": needs_review,
    }


def generate_weekly_summary(storage, doc_index, master_registry) -> dict:
    """สร้างสรุปรายสัปดาห์ (7 วัน)."""
    stats = _count_docs_in_period(doc_index, days=7)

    now = datetime.now(timezone.utc)
    week_start = (now - timedelta(days=7)).strftime("%d/%m/%Y")
    week_end = now.strftime("%d/%m/%Y")

    patient_count = 0
    try:
        patient_count = master_registry.count() if master_registry else 0
    except Exception:
        pass

    stats["period"] = "weekly"
    stats["period_label"] = f"{week_start} — {week_end}"
    stats["patient_count"] = patient_count
    stats["gcs_ready"] = storage.is_ready if storage else False
    stats["generated_at"] = now.isoformat()

    return stats


def generate_monthly_summary(
    storage, doc_index, master_registry, expense_tracker_mod=None
) -> dict:
    """สร้างสรุปรายเดือน (30 วัน)."""
    stats = _count_docs_in_period(doc_index, days=30)

    now = datetime.now(timezone.utc)
    month_start = (now - timedelta(days=30)).strftime("%d/%m/%Y")
    month_end = now.strftime("%d/%m/%Y")

    patient_count = 0
    try:
        patient_count = master_registry.count() if master_registry else 0
    except Exception:
        pass

    stats["period"] = "monthly"
    stats["period_label"] = f"{month_start} — {month_end}"
    stats["patient_count"] = patient_count
    stats["gcs_ready"] = storage.is_ready if storage else False
    stats["generated_at"] = now.isoformat()

    # ข้อมูลค่าใช้จ่าย (ถ้ามี)
    if expense_tracker_mod:
        try:
            year = now.year
            month = now.month
            expense_summary = expense_tracker_mod.get_monthly_summary(year, month)
            if expense_summary:
                stats["expense_summary"] = {
                    "total": expense_summary.get("total", 0),
                    "vendor_count": len(expense_summary.get("by_vendor", {})),
                    "invoice_count": expense_summary.get("invoice_count", 0),
                }
        except Exception:
            pass

    return stats


# ─── HTML formatting ────────────────────────────────────────────


def _format_category_thai(cat: str) -> str:
    """แปลชื่อ category เป็นภาษาไทย."""
    mapping = {
        "lab_result": "🔬 ผลแล็บ",
        "prescription": "💊 ใบสั่งยา",
        "medication_record": "💊 บันทึกยา",
        "dialysis_record": "🩸 ใบฟอกเลือด",
        "bill": "🧾 บิล/ใบเสร็จ",
        "receipt": "🧾 ใบเสร็จ",
        "medical_certificate": "📄 ใบรับรองแพทย์",
        "referral": "📋 ใบส่งตัว",
        "unknown": "❓ ไม่ระบุ",
    }
    return mapping.get(cat, f"📄 {cat}")


def format_report_html(summary: dict, period: str = "weekly") -> str:
    """จัดรูปแบบ HTML สำหรับ email รายงาน."""
    period_label = summary.get("period_label", "")
    period_title = "สัปดาห์" if period == "weekly" else "เดือน"
    emoji = "📊" if period == "weekly" else "📈"

    # Category rows
    cat_rows = ""
    by_cat = summary.get("by_category", {})
    for cat, count in sorted(by_cat.items(), key=lambda x: -x[1]):
        cat_thai = _format_category_thai(cat)
        cat_rows += (
            f'<tr><td style="padding:8px;border:1px solid #e0e0e0;">'
            f"{cat_thai}</td>"
            f'<td style="padding:8px;border:1px solid #e0e0e0;'
            f'text-align:center;font-weight:bold;">{count}</td></tr>\n'
        )

    # Top HN rows (ไม่แสดงชื่อ — PDPA)
    top_rows = ""
    for hn, count in summary.get("top_hn", []):
        top_rows += (
            f'<tr><td style="padding:6px;border:1px solid #e0e0e0;">'
            f"HN {hn}</td>"
            f'<td style="padding:6px;border:1px solid #e0e0e0;'
            f'text-align:center;">{count}</td></tr>\n'
        )

    # Expense section
    expense_html = ""
    exp = summary.get("expense_summary")
    if exp:
        expense_html = f"""
        <h3 style="color:#2e7d32;margin-top:24px;">💰 ค่าใช้จ่าย</h3>
        <table style="border-collapse:collapse;width:100%;max-width:400px;">
        <tr style="background:#e8f5e9;">
            <td style="padding:8px;border:1px solid #e0e0e0;">ยอดรวม</td>
            <td style="padding:8px;border:1px solid #e0e0e0;text-align:right;
                font-weight:bold;">฿{exp.get('total', 0):,.2f}</td>
        </tr>
        <tr>
            <td style="padding:8px;border:1px solid #e0e0e0;">จำนวน invoice</td>
            <td style="padding:8px;border:1px solid #e0e0e0;text-align:center;">
                {exp.get('invoice_count', 0)}</td>
        </tr>
        <tr style="background:#f5f5f5;">
            <td style="padding:8px;border:1px solid #e0e0e0;">บริษัทคู่ค้า</td>
            <td style="padding:8px;border:1px solid #e0e0e0;text-align:center;">
                {exp.get('vendor_count', 0)}</td>
        </tr>
        </table>
        """

    critical = summary.get("critical_alerts", 0)
    critical_color = "#c62828" if critical > 0 else "#2e7d32"
    critical_bg = "#ffebee" if critical > 0 else "#e8f5e9"

    needs_review = summary.get("needs_review", 0)

    html = f"""<html>
<body style="font-family:-apple-system,Helvetica,sans-serif;padding:20px;
    background:#fafafa;color:#333;">
<div style="max-width:600px;margin:0 auto;background:#fff;border-radius:12px;
    padding:24px;box-shadow:0 2px 8px rgba(0,0,0,0.08);">

<h2 style="margin:0 0 4px;color:#1565c0;">
    {emoji} รายงานสรุปราย{period_title}</h2>
<p style="color:#666;margin:0 0 20px;">
    คลินิกระนอง รักษ์ไต &middot; {period_label}</p>

<div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:20px;">
    <div style="flex:1;min-width:120px;background:#e3f2fd;
        border-radius:8px;padding:16px;text-align:center;">
        <div style="font-size:28px;font-weight:bold;color:#1565c0;">
            {summary.get('new_docs', 0)}</div>
        <div style="color:#666;font-size:13px;">เอกสารใหม่</div>
    </div>
    <div style="flex:1;min-width:120px;background:{critical_bg};
        border-radius:8px;padding:16px;text-align:center;">
        <div style="font-size:28px;font-weight:bold;color:{critical_color};">
            {critical}</div>
        <div style="color:#666;font-size:13px;">ค่าวิกฤต</div>
    </div>
    <div style="flex:1;min-width:120px;background:#e8f5e9;
        border-radius:8px;padding:16px;text-align:center;">
        <div style="font-size:28px;font-weight:bold;color:#2e7d32;">
            {summary.get('patient_count', 0)}</div>
        <div style="color:#666;font-size:13px;">ผู้ป่วยทั้งหมด</div>
    </div>
</div>

{"<h3 style='color:#1565c0;'>📂 แยกตามประเภท</h3>" if cat_rows else ""}
{"<table style='border-collapse:collapse;width:100%;max-width:400px;'>" + cat_rows + "</table>" if cat_rows else ""}

{"<h3 style='color:#1565c0;margin-top:24px;'>🏥 ผู้ป่วยที่มีเอกสารมากที่สุด</h3>" if top_rows else ""}
{"<table style='border-collapse:collapse;width:100%;max-width:300px;'><tr style='background:#e3f2fd;'><th style='padding:6px;border:1px solid #e0e0e0;text-align:left;'>HN</th><th style='padding:6px;border:1px solid #e0e0e0;'>จำนวน</th></tr>" + top_rows + "</table>" if top_rows else ""}

{expense_html}

{"<p style='margin-top:16px;padding:10px;background:#fff3e0;border-radius:6px;'>⚠️ เอกสารรอตรวจสอบ: <b>" + str(needs_review) + "</b> รายการ</p>" if needs_review > 0 else ""}

<hr style="border:none;border-top:1px solid #eee;margin:24px 0 12px;">
<p style="color:#999;font-size:11px;margin:0;">
    เอกสารทั้งหมดในระบบ: {summary.get('total_docs', 0)} &middot;
    GCS: {'✅' if summary.get('gcs_ready') else '❌'} &middot;
    สร้างเมื่อ: {summary.get('generated_at', '')[:19]}
</p>
<p style="color:#bbb;font-size:10px;margin:4px 0 0;">
    ส่งอัตโนมัติจาก RANONG RAKTAI AI — ไม่มีข้อมูลส่วนบุคคลของผู้ป่วยในรายงานนี้</p>
</div>
</body></html>"""

    return html


# ─── Send orchestrator ────────────────────────────────────────


def send_scheduled_report(
    storage, doc_index, master_registry, period: str = "weekly",
    expense_tracker_mod=None,
) -> dict:
    """สร้างรายงาน → จัด HTML → ส่ง email.

    Returns:
        {"success": True/False, ...}
    """
    global _last_sent_at, _last_result

    if not is_available():
        result = {"error": "Email ไม่พร้อม — ตรวจ GMAIL_ENABLED และ SMTP settings"}
        _last_result = result
        return result

    try:
        # Generate summary
        if period == "monthly":
            summary = generate_monthly_summary(
                storage, doc_index, master_registry, expense_tracker_mod
            )
        else:
            summary = generate_weekly_summary(storage, doc_index, master_registry)

        # Format HTML
        html = format_report_html(summary, period)

        # Send
        period_title = "สัปดาห์" if period == "weekly" else "เดือน"
        emoji = "📊" if period == "weekly" else "📈"
        period_label = summary.get("period_label", "")

        subject = (
            f"{emoji} รายงานสรุปราย{period_title} {period_label} "
            f"— ระนอง รักษ์ไต"
        )

        result = _gmail._send_email(subject, html)

        now = datetime.now(timezone.utc).isoformat()
        _last_sent_at = now
        _last_result = result

        if result.get("success"):
            logger.info(
                "Auto report sent: period=%s, docs=%d",
                period, summary.get("new_docs", 0),
            )
        else:
            logger.warning(
                "Auto report failed: %s", result.get("error", "unknown")
            )

        return result

    except Exception as e:
        logger.warning("Auto report error: %s", type(e).__name__)
        result = {"error": f"สร้างรายงานล้มเหลว: {type(e).__name__}"}
        _last_result = result
        return result
