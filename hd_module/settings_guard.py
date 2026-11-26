"""🛡 Settings Guard — กันค่าตั้งค่าสำคัญหาย/ผิดรูปแบบ (26 พ.ย. 2569)

เหตุการณ์ 25 พ.ย. 2569 17:52: หน้า ⚙️ โหลดค่าเดิมไม่สำเร็จตอนระบบรีสตาร์ท
→ ช่องขึ้นว่าง → กดบันทึก → nurse_group_id / alert_recipients ถูกทับเป็นค่าว่าง
→ คำสั่งแพทย์ไม่เข้ากลุ่มพยาบาลนาน 16 ชั่วโมงโดยไม่มีใครรู้
26 พ.ย. 07:33: ใส่ Group ID คืนแต่ขาด 1 ตัว → LINE ตอบ 400 "'to' is invalid"

โมดูลนี้เป็นฟังก์ชันล้วน (ไม่แตะ DB) ใช้ร่วมกันโดย
  • POST /hd/api/v1/settings  — ปฏิเสธค่าว่าง/ผิดรูปแบบของค่าสำคัญ
  • ops status / scheduler    — ตรวจค่าสำคัญทุกชั่วโมง แจ้งแพทย์เมื่อหาย
"""
import re

# ค่าที่ถ้าว่างหรือผิด = การแจ้งเตือนทั้งระบบเงียบโดยไม่มีใครรู้
CRITICAL_KEYS = ("nurse_group_id", "alert_recipients", "physician_line_id")

_LINE_USER_RE = re.compile(r"^U[0-9a-f]{32}$")
_LINE_GROUP_RE = re.compile(r"^[CR][0-9a-f]{32}$")   # C = group, R = multi-person room

LABELS = {
    "nurse_group_id": "LINE Group ID กลุ่มพยาบาล",
    "alert_recipients": "LINE User ID ผู้รับ alert",
    "physician_line_id": "LINE User ID แพทย์",
}


def normalize(key, value):
    """ตัดช่องว่าง/ขึ้นบรรทัดใหม่ที่ติดมากับการก๊อป (รวมช่องว่างรอบจุลภาคใน alert_recipients)."""
    v = (value or "").strip()
    if key == "alert_recipients":
        return ",".join(p.strip() for p in v.replace("\n", ",").split(",") if p.strip())
    if key in ("nurse_group_id", "physician_line_id"):
        return "".join(v.split())
    return v


def validate(key, value):
    """→ (ok, error_message). ค่าว่างถือว่า ok ที่นี่ (เรื่อง 'ล้างค่า' ตัดสินแยกด้วย confirm_clear)."""
    v = normalize(key, value)
    if not v:
        return True, None
    if key == "nurse_group_id":
        if not _LINE_GROUP_RE.match(v):
            return False, (f"{LABELS[key]} ต้องขึ้นต้นด้วย C ตามด้วยตัวเลข/ตัวอักษร a–f อีก 32 ตัว (รวม 33 ตัว) "
                           f"— ที่ใส่มามี {len(v)} ตัว · คัดลอกจากข้อความตอบของบอท (พิมพ์ groupid ในกลุ่ม) ทั้งก้อน อย่าพิมพ์เอง")
        return True, None
    if key == "physician_line_id":
        if not _LINE_USER_RE.match(v):
            return False, (f"{LABELS[key]} ต้องขึ้นต้นด้วย U ตามด้วย 32 ตัว (รวม 33 ตัว) — ที่ใส่มามี {len(v)} ตัว")
        return True, None
    if key == "alert_recipients":
        bad = [p for p in v.split(",") if not _LINE_USER_RE.match(p)]
        if bad:
            return False, (f"{LABELS[key]} แต่ละรายการต้องเป็น U + 32 ตัว (คั่นด้วยจุลภาค) — "
                           f"ผิดรูปแบบ {len(bad)} รายการ เช่น '{bad[0][:8]}…' ({len(bad[0])} ตัว)")
        return True, None
    return True, None


def is_critical(key):
    return key in CRITICAL_KEYS


def check_all(get_value):
    """ตรวจค่าสำคัญทั้งหมด → รายการปัญหา (ว่าง = ไม่มีปัญหา).
    get_value(key) → str ปัจจุบันจาก DB/ENV."""
    problems = []
    for key in CRITICAL_KEYS:
        v = normalize(key, get_value(key) or "")
        if not v:
            problems.append(f"⚙️ {LABELS[key]} ({key}) ว่าง — การแจ้งเตือนช่องทางนี้จะเงียบ")
            continue
        ok, err = validate(key, v)
        if not ok:
            problems.append(f"⚙️ {key} ผิดรูปแบบ — {err.split(' — ')[0]}")
    return problems


def physician_warning(problems):
    """ข้อความ LINE ถึงแพทย์เมื่อพบค่าสำคัญหาย (ส่งชั่วโมงละครั้งจน แก้)."""
    if not problems:
        return ""
    return ("🚨 ค่าตั้งค่าสำคัญของ HD Smart Monitoring หาย/ผิด\n"
            + "\n".join("• " + p for p in problems[:5])
            + "\n\n👉 เปิด ⚙️ ตั้งค่าระบบ แล้วใส่ค่าคืน — จนกว่าจะแก้ คำสั่งแพทย์/สรุปเวรจะไม่เข้ากลุ่มพยาบาล")
