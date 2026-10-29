"""Gmail report — ส่งรายงานสรุปทาง Email

ใช้ Service Account ตัวเดียวกับ GCS (ADC)
ต้องเปิด Gmail API ใน Google Cloud Console
Service Account ต้องมี domain-wide delegation หรือใช้ SMTP relay

หมายเหตุ: Gmail API ด้วย Service Account ต้องใช้ domain-wide delegation
ถ้าไม่มี ให้ใช้ SMTP relay แทน (ตั้ง SMTP_* env vars)

ENV VARS:
- GMAIL_ENABLED: "true" เพื่อเปิดใช้งาน
- GMAIL_SENDER: email ผู้ส่ง (ต้องเป็น domain ที่ delegate ให้ SA)
- GMAIL_RECIPIENT: email ผู้รับรายงาน
- SMTP_HOST: SMTP server (fallback ถ้าไม่ใช้ Gmail API)
- SMTP_PORT: SMTP port (default 587)
- SMTP_USER: SMTP username
- SMTP_PASS: SMTP password

ไม่ log ข้อมูลผู้ป่วย (PDPA)
"""

from __future__ import annotations

import logging
import os
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

GMAIL_ENABLED = os.getenv("GMAIL_ENABLED", "").strip().lower() == "true"
GMAIL_SENDER = os.getenv("GMAIL_SENDER", "").strip()
GMAIL_RECIPIENT = os.getenv("GMAIL_RECIPIENT", "").strip()

# SMTP fallback
SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587").strip() or "587")
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASS = os.getenv("SMTP_PASS", "").strip()


def is_available() -> bool:
    """ตรวจว่า Email พร้อมใช้งาน."""
    if not GMAIL_ENABLED:
        return False
    if not GMAIL_RECIPIENT:
        return False
    # ต้องมี sender + (Gmail API หรือ SMTP)
    return bool(GMAIL_SENDER) and (bool(SMTP_HOST) or _gmail_api_available())


def _gmail_api_available() -> bool:
    """ตรวจว่า Gmail API พร้อมใช้งาน."""
    try:
        import google.auth
        from googleapiclient.discovery import build

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/gmail.send"]
        )
        return True
    except Exception:
        return False


def get_status() -> dict:
    """สถานะการเชื่อมต่อ Email."""
    return {
        "enabled": GMAIL_ENABLED,
        "sender": GMAIL_SENDER[:3] + "..." if GMAIL_SENDER else "",
        "recipient": GMAIL_RECIPIENT[:3] + "..." if GMAIL_RECIPIENT else "",
        "method": "smtp" if SMTP_HOST else "gmail_api",
        "connected": is_available(),
    }


# ─── Send helpers ────────────────────────────────────────────


def _send_via_smtp(subject: str, html_body: str) -> dict:
    """ส่ง email ผ่าน SMTP."""
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = GMAIL_SENDER
        msg["To"] = GMAIL_RECIPIENT
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            if SMTP_USER and SMTP_PASS:
                server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(GMAIL_SENDER, GMAIL_RECIPIENT, msg.as_string())

        logger.info("Email sent via SMTP")
        return {"success": True, "method": "smtp"}
    except Exception as e:
        logger.warning("SMTP send failed: %s", type(e).__name__)
        return {"error": f"ส่ง email ไม่สำเร็จ: {type(e).__name__}"}


def _send_via_gmail_api(subject: str, html_body: str) -> dict:
    """ส่ง email ผ่าน Gmail API."""
    try:
        import base64

        import google.auth
        from googleapiclient.discovery import build

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/gmail.send"]
        )
        # Domain-wide delegation: impersonate sender
        credentials = credentials.with_subject(GMAIL_SENDER)
        service = build(
            "gmail", "v1", credentials=credentials, cache_discovery=False
        )

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = GMAIL_SENDER
        msg["To"] = GMAIL_RECIPIENT
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        body = {"raw": raw}

        service.users().messages().send(
            userId="me", body=body
        ).execute()

        logger.info("Email sent via Gmail API")
        return {"success": True, "method": "gmail_api"}
    except Exception as e:
        logger.warning("Gmail API send failed: %s", type(e).__name__)
        return {"error": f"ส่ง email ไม่สำเร็จ: {type(e).__name__}"}


def _send_email(subject: str, html_body: str) -> dict:
    """ส่ง email — เลือก method อัตโนมัติ."""
    if not is_available():
        return {"error": "Email ไม่ได้เปิดใช้งาน"}

    if SMTP_HOST:
        return _send_via_smtp(subject, html_body)
    else:
        return _send_via_gmail_api(subject, html_body)


# ─── Report functions ────────────────────────────────────────


def send_daily_summary(stats: dict) -> dict:
    """ส่งสรุปเอกสารประจำวัน.

    Args:
        stats: {"total_docs": N, "new_today": N, "by_category": {...},
                "alerts": N, "date": "YYYY-MM-DD"}
    """
    date = stats.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    total = stats.get("total_docs", 0)
    new = stats.get("new_today", 0)
    alerts = stats.get("alerts", 0)
    by_cat = stats.get("by_category", {})

    cat_rows = ""
    for cat, count in by_cat.items():
        cat_rows += f"<tr><td>{cat}</td><td>{count}</td></tr>"

    html = f"""
    <html>
    <body style="font-family: -apple-system, sans-serif; padding: 20px;">
    <h2>📋 สรุปเอกสารประจำวัน — {date}</h2>
    <p>คลินิกระนอง รักษ์ไต</p>
    <table style="border-collapse: collapse; width: 100%;">
    <tr style="background: #f5f5f5;">
        <td style="padding: 8px; border: 1px solid #ddd;"><b>เอกสารทั้งหมด</b></td>
        <td style="padding: 8px; border: 1px solid #ddd;">{total}</td>
    </tr>
    <tr>
        <td style="padding: 8px; border: 1px solid #ddd;"><b>เอกสารใหม่วันนี้</b></td>
        <td style="padding: 8px; border: 1px solid #ddd;">{new}</td>
    </tr>
    <tr style="background: #f5f5f5;">
        <td style="padding: 8px; border: 1px solid #ddd;"><b>แจ้งเตือนค่าวิกฤต</b></td>
        <td style="padding: 8px; border: 1px solid #ddd;">{alerts}</td>
    </tr>
    </table>
    {f'<h3>แยกตามประเภท</h3><table style="border-collapse:collapse;">{cat_rows}</table>' if cat_rows else ''}
    <p style="color: #999; font-size: 12px; margin-top: 20px;">
    ส่งอัตโนมัติจาก RANONG RAKTAI AI</p>
    </body></html>
    """

    return _send_email(
        f"📋 สรุปเอกสาร {date} — ระนอง รักษ์ไต",
        html,
    )


def send_weekly_report(stats: dict) -> dict:
    """ส่งสรุปรายสัปดาห์.

    Args:
        stats: {"week": "YYYY-WNN", "total_docs": N, "new_this_week": N,
                "patients": N, "lab_count": N, "rx_count": N,
                "dialysis_count": N, "alerts": N}
    """
    week = stats.get("week", "")
    html = f"""
    <html>
    <body style="font-family: -apple-system, sans-serif; padding: 20px;">
    <h2>📊 สรุปรายสัปดาห์ — {week}</h2>
    <p>คลินิกระนอง รักษ์ไต</p>
    <table style="border-collapse: collapse; width: 100%;">
    <tr style="background: #e3f2fd;">
        <td style="padding: 10px; border: 1px solid #ddd;"><b>เอกสารใหม่</b></td>
        <td style="padding: 10px; border: 1px solid #ddd;">{stats.get('new_this_week', 0)}</td>
    </tr>
    <tr>
        <td style="padding: 10px; border: 1px solid #ddd;">ผลแล็บ</td>
        <td style="padding: 10px; border: 1px solid #ddd;">{stats.get('lab_count', 0)}</td>
    </tr>
    <tr style="background: #f5f5f5;">
        <td style="padding: 10px; border: 1px solid #ddd;">ใบยา</td>
        <td style="padding: 10px; border: 1px solid #ddd;">{stats.get('rx_count', 0)}</td>
    </tr>
    <tr>
        <td style="padding: 10px; border: 1px solid #ddd;">ฟอกเลือด</td>
        <td style="padding: 10px; border: 1px solid #ddd;">{stats.get('dialysis_count', 0)}</td>
    </tr>
    <tr style="background: #f5f5f5;">
        <td style="padding: 10px; border: 1px solid #ddd;">แจ้งเตือนวิกฤต</td>
        <td style="padding: 10px; border: 1px solid #ddd;">{stats.get('alerts', 0)}</td>
    </tr>
    <tr style="background: #e8f5e9;">
        <td style="padding: 10px; border: 1px solid #ddd;"><b>ผู้ป่วยทั้งหมด</b></td>
        <td style="padding: 10px; border: 1px solid #ddd;">{stats.get('patients', 0)}</td>
    </tr>
    </table>
    <p style="color: #999; font-size: 12px; margin-top: 20px;">
    ส่งอัตโนมัติจาก RANONG RAKTAI AI</p>
    </body></html>
    """

    return _send_email(
        f"📊 สรุปรายสัปดาห์ {week} — ระนอง รักษ์ไต",
        html,
    )


def send_alert_email(subject: str, body: str) -> dict:
    """ส่งแจ้งเตือนฉุกเฉินทาง email.

    ไม่ใส่ข้อมูลผู้ป่วยใน email body (PDPA)
    """
    if not subject:
        return {"error": "ไม่มีหัวข้อ"}

    html = f"""
    <html>
    <body style="font-family: -apple-system, sans-serif; padding: 20px;">
    <div style="background: #ffebee; padding: 16px; border-radius: 8px;
                border-left: 4px solid #c62828;">
    <h2 style="color: #c62828; margin: 0 0 8px;">⚠️ {subject}</h2>
    <p>{body}</p>
    </div>
    <p style="color: #999; font-size: 12px; margin-top: 20px;">
    ส่งอัตโนมัติจาก RANONG RAKTAI AI</p>
    </body></html>
    """

    return _send_email(f"⚠️ {subject}", html)
