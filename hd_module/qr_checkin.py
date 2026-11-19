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
    """สร้างหน้า HTML สำหรับพิมพ์ QR Code ทุกเตียง — ใช้ JS qrcode library"""
    base_url = os.environ.get("RENDER_EXTERNAL_URL",
                               "https://ranong-raktai-ai-cloud.onrender.com")

    beds_json = []
    for bed in range(1, total_beds + 1):
        qr = generate_bed_qr_data(bed, base_url)
        beds_json.append({"bed": bed, "url": qr["url"]})

    import json
    beds_str = json.dumps(beds_json)
    today = time.strftime("%d/%m/%Y")

    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>QR Check-in — คลินิกระนอง รักษ์ไต</title>
<script src="https://cdn.jsdelivr.net/npm/qrcode-generator@1.4.4/qrcode.min.js"></script>
<style>
@page{{margin:8mm}}
body{{font-family:'Sarabun',sans-serif;margin:0;padding:16px;background:#fff}}
.header{{text-align:center;margin-bottom:16px}}
.header h1{{color:#1e40af;margin:0;font-size:1.5rem}}
.header p{{color:#6b7280;margin:4px 0;font-size:.85rem}}
.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}
.bed-card{{text-align:center;padding:12px 8px;border:2px solid #1e40af;border-radius:12px;page-break-inside:avoid}}
.bed-num{{font-size:1.4rem;font-weight:800;color:#1e40af;margin-bottom:4px}}
.qr-box{{margin:4px auto}}
.qr-box canvas,.qr-box img{{max-width:120px;height:auto}}
.scan-txt{{font-size:.7rem;color:#6b7280;margin-top:4px}}
.footer{{text-align:center;margin-top:12px;font-size:.7rem;color:#9ca3af}}
@media print{{.no-print{{display:none!important}}}}
@media(max-width:600px){{.grid{{grid-template-columns:repeat(2,1fr)}}}}
</style></head><body>
<div class="no-print" style="text-align:right;margin-bottom:12px">
  <button onclick="window.print()" style="padding:10px 20px;background:#1e40af;color:#fff;border:none;border-radius:8px;font-size:14px;cursor:pointer">🖨 พิมพ์ QR ทุกเตียง</button>
</div>
<div class="header">
  <h1>🏥 QR Check-in — คลินิกระนอง รักษ์ไต</h1>
  <p>สแกน QR Code เพื่อเริ่มรอบฟอก · วันที่ {today}</p>
</div>
<div class="grid" id="qrGrid"></div>
<div class="footer">Token ใช้ได้ภายในวันนี้ ({today}) · HD Smart Monitoring v2.2</div>
<script>
const beds = {beds_str};
const grid = document.getElementById('qrGrid');
beds.forEach(b => {{
  const card = document.createElement('div');
  card.className = 'bed-card';
  card.innerHTML = '<div class="bed-num">เตียง '+b.bed+'</div><div class="qr-box" id="qr'+b.bed+'"></div><div class="scan-txt">📱 สแกนเพื่อเช็คอิน</div>';
  grid.appendChild(card);
  try {{
    const qr = qrcode(0, 'M');
    qr.addData(b.url);
    qr.make();
    document.getElementById('qr'+b.bed).innerHTML = qr.createSvgTag(3, 0);
  }} catch(e) {{
    document.getElementById('qr'+b.bed).innerHTML = '<div style="font-size:3rem">📱</div>';
  }}
}});
</script></body></html>"""
