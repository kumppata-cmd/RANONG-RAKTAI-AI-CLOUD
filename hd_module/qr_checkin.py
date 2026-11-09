"""F5: QR Code Check-in — ผู้ป่วยแสกน QR เมื่อมาถึง

Flow:
1. ระบบสร้าง QR Code สำหรับแต่ละเตียง (URL → /hd/checkin?bed=X)
2. ผู้ป่วยหรือพยาบาลแสกน QR ที่เตียง
3. ระบบ auto-assign ผู้ป่วย → เริ่ม Pre-HD flow

QR content: https://ranong-raktai-ai-cloud.onrender.com/hd/checkin?bed=1&token=xxx
"""

import logging
import hashlib
import time
import os

logger = logging.getLogger(__name__)


def generate_bed_qr_data(bed_number: int, base_url: str = "") -> dict:
    """สร้างข้อมูล QR Code สำหรับเตียง

    Returns: {"bed": 1, "url": "https://...", "token": "abc123"}
    """
    if not base_url:
        base_url = os.environ.get("RENDER_EXTERNAL_URL", "https://ranong-raktai-ai-cloud.onrender.com")

    # Simple token: hash of bed + date (rotates daily)
    today = time.strftime("%Y-%m-%d")
    secret = os.environ.get("ADMIN_SECRET", "hd-qr-fallback")
    token = hashlib.sha256(f"{bed_number}:{today}:{secret}".encode()).hexdigest()[:12]

    url = f"{base_url}/hd/checkin?bed={bed_number}&token={token}"

    return {
        "bed": bed_number,
        "url": url,
        "token": token,
        "date": today,
    }


def verify_qr_token(bed_number: int, token: str) -> bool:
    """ตรวจสอบ QR token ว่าถูกต้อง (ตรงกับวันนี้)"""
    today = time.strftime("%Y-%m-%d")
    secret = os.environ.get("ADMIN_SECRET", "hd-qr-fallback")
    expected = hashlib.sha256(f"{bed_number}:{today}:{secret}".encode()).hexdigest()[:12]
    return token == expected


def generate_qr_svg(data: str, size: int = 200) -> str:
    """สร้าง QR Code เป็น inline SVG (ไม่ต้องพึ่ง library ภายนอก)

    ใช้ simple text-based approach — สำหรับ production ควรใช้ qrcode library
    แต่เพื่อไม่ต้องเพิ่ม dependency ใช้ HTML + URL แทน
    """
    # Return a simple styled div with the URL + instruction
    return f"""<div style="text-align:center;padding:20px;background:#fff;
        border-radius:12px;border:2px solid #2980b9;display:inline-block;">
        <div style="font-size:3rem;margin-bottom:8px;">🏥</div>
        <div style="font-weight:700;font-size:1.2rem;color:#2980b9;">
            สแกน QR เพื่อเช็คอิน
        </div>
        <div style="margin:12px 0;padding:12px;background:#f8f9fa;
            border-radius:8px;font-family:monospace;font-size:.75rem;
            word-break:break-all;">
            {data}
        </div>
        <div style="font-size:.8rem;color:#666;">
            ⚡ แสกน QR Code นี้ด้วยกล้องมือถือ
        </div>
    </div>"""


def generate_all_bed_qr_page(total_beds: int = 25) -> str:
    """สร้างหน้า HTML สำหรับพิมพ์ QR Code ทุกเตียง"""
    base_url = os.environ.get("RENDER_EXTERNAL_URL",
                               "https://ranong-raktai-ai-cloud.onrender.com")
    html_parts = [
        "<!DOCTYPE html><html><head><meta charset='UTF-8'>",
        "<title>QR Check-in — ทุกเตียง</title>",
        "<style>",
        "@page{margin:10mm}",
        "body{font-family:sans-serif;margin:0;padding:20px}",
        ".grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}",
        ".bed-qr{text-align:center;padding:16px;border:2px solid #2980b9;",
        "border-radius:12px;page-break-inside:avoid}",
        ".bed-num{font-size:2rem;font-weight:700;color:#2980b9}",
        ".qr-url{font-size:.55rem;color:#666;word-break:break-all;margin-top:8px}",
        "@media print{.no-print{display:none}}",
        "</style></head><body>",
        "<div class='no-print' style='text-align:right;margin-bottom:16px'>",
        "<button onclick='window.print()' style='padding:10px 24px;",
        "background:#2980b9;color:#fff;border:none;border-radius:8px;",
        "font-size:14px;cursor:pointer'>🖨 พิมพ์</button></div>",
        "<h1 style='text-align:center;color:#2980b9'>🏥 QR Check-in — คลินิกระนอง รักษ์ไต</h1>",
        "<div class='grid'>",
    ]

    for bed in range(1, total_beds + 1):
        qr = generate_bed_qr_data(bed, base_url)
        html_parts.append(f"""
            <div class='bed-qr'>
                <div class='bed-num'>เตียง {bed}</div>
                <div style='font-size:4rem;margin:8px 0'>📱</div>
                <div style='font-size:.85rem;font-weight:600'>สแกนเพื่อเช็คอิน</div>
                <div class='qr-url'>{qr['url']}</div>
            </div>
        """)

    html_parts.append("</div>")
    html_parts.append(f"<p style='text-align:center;margin-top:20px;color:#999;font-size:.75rem'>")
    html_parts.append(f"Token ใช้ได้ภายในวันนี้ ({qr['date']}) — พิมพ์ใหม่ทุกวัน</p>")
    html_parts.append("</body></html>")

    return "".join(html_parts)
