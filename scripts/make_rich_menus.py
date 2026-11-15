"""สร้างรูป Rich Menu ทั้ง 3 ชุด (Staff / Patient / Sub) — ขนาด 2500x843

ออกแบบให้อ่านง่ายบนมือถือ: แถบหัวบอกชนิดเมนูชัดเจน, ตัวหนังสือใหญ่,
ไม่ใช้ emoji (ฟอนต์บนเซิร์ฟเวอร์ไม่มี emoji → กลายเป็นสี่เหลี่ยม)

ตำแหน่งปุ่มต้องตรงกับ RM_* ใน server.py (ส่วน "ตั้งค่าเมนู"):
  แถบหัว y 0..BANNER (ไม่มีปุ่ม), แถวบน BANNER..BANNER+ROW1, แถวล่าง ..843

รัน:  python scripts/make_rich_menus.py
"""
import os
from PIL import Image, ImageDraw, ImageFont

W, H = 2500, 843
BANNER = 110
ROW1 = (H - BANNER) // 2          # 366
COL = W // 3                      # 833
GAP = 10
FONT = "/usr/share/fonts/opentype/tlwg/Loma-Bold.otf"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MENUS = {
    "rich_menu_staff.png": {
        "bg": "#0f2a44", "badge": "#ffd54f",
        "title": "เมนูเจ้าหน้าที่", "tag": "STAFF",
        "tiles": [
            ("#2e9e63", "ดูผลแล็บ", "ผลแล็บ / เอกสารผู้ป่วย"),
            ("#1e88e5", "ส่งเอกสาร", "ส่งใบแล็บ / ใบยา"),
            ("#d32f2f", "รอบฟอก", "ตารางฟอกวันนี้"),
            ("#6a3fb5", "วิเคราะห์", "AI วิเคราะห์แล็บ"),
            ("#ef6c00", "ดูนัด", "นัดหมายผู้ป่วย"),
            ("#00796b", "เพิ่มเติม  >", "คำสั่งและเครื่องมือ"),
        ],
    },
    "rich_menu_sub.png": {
        "bg": "#0f2a44", "badge": "#ffd54f",
        "title": "เมนูเพิ่มเติม", "tag": "STAFF",
        "tiles": [
            ("#546e7a", "<  กลับ", "เมนูหลัก"),
            ("#00897b", "คำสั่ง", "ดูคำสั่งทั้งหมด"),
            ("#7b1fa2", "ลงทะเบียน", "ผูก HN กับ LINE"),
            ("#1e88e5", "ค้นหา", "ค้นหาเอกสาร"),
            ("#ef6c00", "KPI", "ดัชนีคุณภาพ"),
            ("#37474f", "ระบบ", "ตรวจสอบระบบ"),
        ],
    },
    "rich_menu_patient.png": {
        "bg": "#0d4f8b", "badge": "#ffffff",
        "title": "คลินิกระนอง รักษ์ไต", "tag": "เมนูผู้ป่วย",
        "tiles": [
            ("#d32f2f", "ผลเลือด HD", "Hb · Kt/V · กราฟ"),
            ("#1e88e5", "ดูผลแล็บ", "ผลตรวจ / เอกสาร"),
            ("#7b1fa2", "ลงทะเบียน", "ผูก HN กับ LINE"),
            ("#ef6c00", "นัดหมาย", "นัดฟอก / นัดตรวจ"),
            ("#00897b", "ติดต่อ", "เจ้าหน้าที่คลินิก"),
            ("#546e7a", "ช่วยเหลือ", "คำสั่งทั้งหมด"),
        ],
    },
}


def render(spec):
    img = Image.new("RGB", (W, H), spec["bg"])
    d = ImageDraw.Draw(img)
    f_title = ImageFont.truetype(FONT, 66)
    f_tag = ImageFont.truetype(FONT, 52)
    f_label = ImageFont.truetype(FONT, 104)
    f_sub = ImageFont.truetype(FONT, 54)

    # แถบหัว: ชื่อเมนู (ซ้าย) + ป้ายชนิดเมนู (ขวา) ตัวใหญ่ อ่านออก
    cy = BANNER // 2 + 2
    d.text((48, cy), spec["title"], fill="#ffffff", font=f_title, anchor="lm")
    tw = d.textlength(spec["tag"], font=f_tag)
    x1 = W - 40
    x0 = x1 - tw - 64
    d.rounded_rectangle([x0, cy - 40, x1, cy + 40], radius=40, fill=spec["badge"])
    d.text(((x0 + x1) / 2, cy + 2), spec["tag"], fill=spec["bg"], font=f_tag, anchor="mm")

    rows = [(BANNER, BANNER + ROW1), (BANNER + ROW1, H)]
    for i, (color, label, sub) in enumerate(spec["tiles"]):
        c, r = i % 3, i // 3
        x0 = c * COL + GAP
        x1 = (c + 1) * COL - GAP if c < 2 else W - GAP
        y0, y1 = rows[r][0] + GAP, rows[r][1] - GAP
        d.rounded_rectangle([x0, y0, x1, y1], radius=28, fill=color)
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        d.text((mx, my - 34), label, fill="#ffffff", font=f_label, anchor="mm")
        d.text((mx, my + 72), sub, fill="#e8f0fe", font=f_sub, anchor="mm")
    return img


if __name__ == "__main__":
    for name, spec in MENUS.items():
        render(spec).save(os.path.join(ROOT, name), optimize=True)
        print("saved", name)
