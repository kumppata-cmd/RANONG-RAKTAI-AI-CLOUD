"""บัตรผู้ป่วย + QR เป็นไฟล์ PDF (A4, บัตรขนาด 105x66 มม. พร้อมเส้นตัด)

ทำไมต้องเป็น PDF: ปุ่ม 🖨 พิมพ์ (window.print) ใช้ไม่ได้บนเบราว์เซอร์มือถือหลายตัว
(Edge/Chrome Android, เบราว์เซอร์ใน LINE) — PDF เปิด/แชร์/สั่งพิมพ์ได้ทุกเครื่อง
และพิมพ์ได้ขนาดจริงเสมอ ไม่ขึ้นกับการตั้งค่าหน้ากระดาษของเบราว์เซอร์

QR ใช้ reportlab.graphics.barcode.qr (มากับ reportlab อยู่แล้ว ไม่ต้องติดตั้งเพิ่ม)
"""
import io

SHIFT_TH = {"morning": "รอบเช้า", "afternoon": "รอบบ่าย", "evening": "รอบค่ำ"}


def card_pdf(p, url, clinic_name="คลินิกระนอง รักษ์ไต"):
    """p: dict(name, hn, hospital_hn, schedule_days, default_shift, vascular_access, dry_weight)
    url: signed patient-portal URL to encode in the QR. Returns PDF bytes."""
    from reportlab.graphics import renderPDF
    from reportlab.graphics.barcode.qr import QrCodeWidget
    from reportlab.graphics.shapes import Drawing
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas
    from .daily_records import register_fonts

    register_fonts()
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(f"บัตรผู้ป่วย HN {p.get('hn', '')}")
    pw, ph = A4
    cw, chh = 105 * mm, 66 * mm
    x0, y0 = (pw - cw) / 2, ph - 30 * mm - chh

    # เส้นตัด (มุมทั้ง 4) + กรอบบัตร
    c.setStrokeColorRGB(0.6, 0.6, 0.6)
    c.setLineWidth(0.4)
    for cx, cy, dx, dy in ((x0, y0, -1, -1), (x0 + cw, y0, 1, -1), (x0, y0 + chh, -1, 1), (x0 + cw, y0 + chh, 1, 1)):
        c.line(cx + dx * 2 * mm, cy, cx + dx * 7 * mm, cy)
        c.line(cx, cy + dy * 2 * mm, cx, cy + dy * 7 * mm)
    c.setStrokeColorRGB(0.62, 0.70, 0.77)
    c.setLineWidth(1)
    c.roundRect(x0, y0, cw, chh, 4 * mm)

    # QR (ขวา)
    q = 32 * mm
    w = QrCodeWidget(url, barLevel="M")
    b = w.getBounds()
    d = Drawing(q, q, transform=[q / (b[2] - b[0]), 0, 0, q / (b[3] - b[1]), 0, 0])
    d.add(w)
    qx, qy = x0 + cw - q - 5 * mm, y0 + (chh - q) / 2 + 3 * mm
    renderPDF.draw(d, c, qx, qy)
    c.setFont("Loma", 7)
    c.setFillColorRGB(0.45, 0.5, 0.55)
    c.drawCentredString(qx + q / 2, qy - 3.5 * mm, "สแกนดูผลเลือดของฉัน")

    # ข้อความ (ซ้าย)
    tx = x0 + 7 * mm
    maxw = qx - tx - 3 * mm
    y = y0 + chh - 10 * mm

    def fit(text, font, size):
        while size > 7 and c.stringWidth(text, font, size) > maxw:
            size -= 0.5
        return size

    c.setFillColorRGB(0.16, 0.5, 0.73)
    c.setFont("Loma-Bold", 9)
    c.drawString(tx, y, clinic_name)
    y -= 9 * mm
    c.setFillColorRGB(0.1, 0.17, 0.24)
    name = p.get("name") or "-"
    c.setFont("Loma-Bold", fit(name, "Loma-Bold", 15))
    c.drawString(tx, y, name)

    rows = [f"HN {p.get('hn', '-')}" + (f"   HN รพ. {p['hospital_hn']}" if p.get("hospital_hn") else "")]
    sched = p.get("schedule_days") or "-"
    sh = SHIFT_TH.get(p.get("default_shift") or "", "")
    rows.append(f"วันฟอก {sched}" + (f"  ·  {sh}" if sh else ""))
    dw = p.get("dry_weight")
    rows.append(f"เส้นฟอก {p.get('vascular_access') or '-'}" + (f"  ·  DW {float(dw):.1f} kg" if dw else ""))
    for t in rows:
        y -= 6.5 * mm
        c.setFont("Loma", fit(t, "Loma", 10.5))
        c.drawString(tx, y, t)

    c.setFillColorRGB(0.45, 0.5, 0.55)
    c.setFont("Loma", 7)
    c.drawString(tx, y0 + 9 * mm, "ข้อมูลส่วนบุคคล — กรุณาไม่ถ่ายรูปหรือส่งต่อให้ผู้อื่น")

    # คำแนะนำนอกบัตร
    c.setFont("Loma", 9)
    c.setFillColorRGB(0.4, 0.4, 0.4)
    c.drawCentredString(pw / 2, y0 - 14 * mm, "พิมพ์ขนาดจริง (100% / Actual size) แล้วตัดตามเส้นมุม · เคลือบหรือสอดซองติดกับบัตรนัด")
    c.showPage()
    c.save()
    return buf.getvalue()
