"""Patient Engagement ผ่าน LINE — 4 ฟีเจอร์ WOW (21 พ.ย. 2026).

1. lab_friendly_lines()   — แปลผลแล็บเป็นภาษาบ้าน สีเขียว/เหลือง/แดง รายคน
2. Pre-HD Check-in        — เช้าวันฟอกส่งลิงก์ถาม 3 ข้อ คำตอบขึ้น dashboard ก่อนคนไข้ถึง
3. session_summary_text() — จบรอบฟอก สรุปภาษาคนส่งถึง LINE ที่ผูก (ผู้ป่วย/ผู้ดูแล)
4. Food Coach             — หน้าเว็บถ่ายรูปอาหาร → Claude Vision + แล็บล่าสุดรายคน

ออกแบบ: ทุกอย่างอยู่ใน hd_module — ไม่แตะ server.py; push ผ่าน LINE_PUSH_FN
ที่ server ฉีดให้ใน app.config; ลิงก์ผู้ป่วยใช้ token HMAC ไม่ต้อง login;
ทุกข้อความมีคำกำกับ "ไม่ใช่คำวินิจฉัย"
"""
import hashlib
import hmac
import logging
import os
from datetime import date as _date, datetime, timedelta, timezone

logger = logging.getLogger("hd.patient_engage")

_THAI_TZ = timezone(timedelta(hours=7))
_DAY_LETTER = {0: "จ", 1: "อ", 2: "พ", 3: "พฤ", 4: "ศ", 5: "ส", 6: "อา"}

DISCLAIMER = "\n\n⚠️ ข้อความอัตโนมัติ ไม่ใช่คำวินิจฉัย — มีข้อสงสัยปรึกษาพยาบาล/แพทย์ที่คลินิก"


def _secret():
    return (os.environ.get("HD_JWT_SECRET", "") or os.environ.get("ADMIN_SECRET", "") or "rnrk").encode()


def make_token(kind, hn, day_str):
    """Opaque token: <hn>.<yyyymmdd>.<sig> — stateless, no login needed."""
    sig = hmac.new(_secret(), f"{kind}:{hn}:{day_str}".encode(), hashlib.sha256).hexdigest()[:20]
    return f"{hn}.{day_str}.{sig}"


def parse_token(kind, token, max_age_days=1):
    """คืน (hn, day) เมื่อ token ถูกต้องและไม่หมดอายุ, ไม่งั้น (None, None)."""
    try:
        hn, day_str, sig = token.split(".")
        good = hmac.new(_secret(), f"{kind}:{hn}:{day_str}".encode(), hashlib.sha256).hexdigest()[:20]
        if not hmac.compare_digest(sig, good):
            return None, None
        d = datetime.strptime(day_str, "%Y%m%d").date()
        today = datetime.now(_THAI_TZ).date()
        if today < d or (today - d).days > max_age_days:
            return None, None
        return hn, d
    except Exception:
        return None, None


def _base_url():
    return (os.environ.get("RENDER_EXTERNAL_URL") or "https://ranong-raktai-ai-cloud.onrender.com").rstrip("/")


# อายุลิงก์โค้ชอาหาร (วัน) — แล็บออกราวเดือนละครั้ง ลิงก์ต้องอยู่ถึงแล็บรอบถัดไป
FOOD_TOKEN_DAYS = 45


def food_link_command(cur, body):
    """คำสั่งในกลุ่มพยาบาล: 'ลิงก์อาหาร <HN>' — ออกลิงก์โค้ชอาหาร (Food Coach)
    ของผู้ป่วย อายุ 45 วัน ให้เจ้าหน้าที่ส่งต่อทาง LINE. คืนข้อความตอบเสมอ."""
    import re as _re
    m = _re.search(r"(\d{3,9})", body or "")
    if not m:
        return "ใช้: ลิงก์อาหาร <HN> เช่น ลิงก์อาหาร 68443"
    hn = m.group(1)
    cur.execute("SELECT patient_name FROM hd_patient_mapping WHERE hn = %s", (hn,))
    pr = cur.fetchone()
    if not pr:
        return f"ไม่พบ HN {hn} ในทะเบียนผู้ป่วย"
    day = datetime.now(_THAI_TZ).strftime("%Y%m%d")
    link = f"{_base_url()}/hd/food/{make_token('food', hn, day)}"
    return (f"🍲 ลิงก์โค้ชอาหารของ {pr[0] or hn} (HN {hn})\n{link}\n\n"
            f"ส่งต่อให้ผู้ป่วย/ญาติทาง LINE ได้เลย — เปิดลิงก์ → ถ่ายรูปอาหาร → "
            f"AI แนะนำตามผลแล็บล่าสุดของผู้ป่วยคนนี้ (ใช้ได้ {FOOD_TOKEN_DAYS} วัน ถ่ายได้ไม่จำกัดครั้ง)\n"
            f"⚠️ อย่าส่งรูปอาหารเข้าแชท OA ตรง ๆ — รูปในแชทจะเข้าระบบจัดเก็บเอกสาร")


# ───────────────────────── 1) แล็บฉบับภาษาบ้าน ─────────────────────────

def _band(v, lo, hi, warn_lo=None, warn_hi=None):
    """คืน (emoji, level): 🟢 ปกติ 🟡 เฝ้าระวัง 🔴 ต้องใส่ใจ"""
    if v is None:
        return None, None
    if lo <= v <= hi:
        return "🟢", "ok"
    if (warn_lo is not None and warn_lo <= v < lo) or (warn_hi is not None and hi < v <= warn_hi):
        return "🟡", "warn"
    return "🔴", "bad"


def lab_friendly_lines(row):
    """row: dict ค่า hb,k,phos,ca,albumin,ktv (float|None) → รายการบรรทัดภาษาบ้าน.
    เกณฑ์อิง KDOQI/แนวทางหน่วยไตเทียม — rule-based ไม่มีการเดา"""
    L = []
    hb = row.get("hb")
    e, lv = _band(hb, 10, 12, warn_lo=9, warn_hi=13)
    if e:
        L.append(f"{e} ความเข้มข้นเลือด (Hb) {hb:.1f} — " + (
            "อยู่ในเป้า ดีมาก 💪" if lv == "ok" else
            ("ต่ำกว่าเป้าเล็กน้อย ทีมรักษากำลังดูแลเรื่องยากระตุ้นเลือดให้" if hb < 10 else "สูงกว่าเป้าเล็กน้อย แพทย์จะปรับยาให้เหมาะสม")))
    k = row.get("k")
    e, lv = _band(k, 3.5, 5.0, warn_hi=5.5)
    if e:
        L.append(f"{e} โพแทสเซียม (K) {k:.1f} — " + (
            "ปกติ คุมอาหารได้ดีมาก 👍" if lv == "ok" else
            ("เริ่มสูง: ช่วงนี้เลี่ยง กล้วย ส้ม ทุเรียน มะละกอ มะพร้าว ผลไม้แห้ง" if lv == "warn" and k > 5 else
             ("สูงเกินปลอดภัย ❗ งดผลไม้/ผักสดปริมาณมาก และอย่าขาดฟอกเด็ดขาด" if k and k > 5.5 else "ต่ำกว่าปกติ แจ้งพยาบาลก่อนฟอกครั้งหน้า"))))
    p = row.get("phos")
    e, lv = _band(p, 3.0, 5.5, warn_hi=6.5)
    if e:
        L.append(f"{e} ฟอสฟอรัส (P) {p:.1f} — " + (
            "อยู่ในเกณฑ์ เยี่ยม" if lv == "ok" else
            ("สูง: ลดของหมักดอง เครื่องในสัตว์ นมข้น ไข่แดงเยอะๆ และ **เคี้ยวยาจับฟอสฟอรัสพร้อมข้าวทุกมื้อ**" if p and p > 5.5 else "เริ่มสูง อย่าลืมเคี้ยวยาจับฟอสฟอรัสพร้อมอาหาร")))
    alb = row.get("albumin")
    e, lv = _band(alb, 3.5, 5.2, warn_lo=3.0)
    if e:
        L.append(f"{e} โปรตีนในเลือด (Alb) {alb:.1f} — " + (
            "ดี กินโปรตีนได้เพียงพอ" if lv == "ok" else "ต่ำ: เพิ่มไข่ขาว เนื้อปลา อกไก่ ทุกวัน"))
    ktv = row.get("ktv")
    if ktv is not None:
        e = "🟢" if ktv >= 1.2 else "🟡"
        L.append(f"{e} ประสิทธิภาพการฟอก (Kt/V) {ktv:.2f} — " + (
            "ฟอกได้เต็มที่ ดีมาก" if ktv >= 1.2 else "ยังไม่เต็มเป้า มาฟอกให้ครบเวลาทุกครั้งนะคะ/ครับ"))
    return L


def build_lab_friendly_message(cur, hn, kind="ผลเลือด"):
    """สร้างข้อความแล็บภาษาบ้านของคนไข้ 1 คนจาก hd_labs ล่าสุด; คืน None ถ้าไม่มีข้อมูล."""
    try:
        cur.execute("""SELECT l.lab_date, l.hb, l.k, l.phos, l.ca, l.albumin, l.ktv, p.patient_name
                       FROM hd_labs l JOIN hd_patient_mapping p ON p.id = l.patient_id
                       WHERE p.hn = %s AND l.lab_date IS NOT NULL
                         AND (l.hb IS NOT NULL OR l.k IS NOT NULL OR l.phos IS NOT NULL OR l.ktv IS NOT NULL)
                       ORDER BY l.lab_date DESC LIMIT 1""", (hn,))
        r = cur.fetchone()
    except Exception as e:
        logger.warning("lab friendly query %s: %s", hn, e)
        return None
    if not r:
        return None
    row = {"hb": float(r[1]) if r[1] is not None else None,
           "k": float(r[2]) if r[2] is not None else None,
           "phos": float(r[3]) if r[3] is not None else None,
           "ca": float(r[4]) if r[4] is not None else None,
           "albumin": float(r[5]) if r[5] is not None else None,
           "ktv": float(r[6]) if r[6] is not None else None}
    lines = lab_friendly_lines(row)
    if not lines:
        return None
    first = (r[7] or "").split()
    nick = first[-1] if first else ""
    head = f"🩸 {kind}ของคุณ{nick} ({r[0].strftime('%d/%m/%Y')})\n━━━━━━━━━━━━\n"
    return head + "\n".join(lines) + DISCLAIMER


def lab_friendly_command(cur, body):
    """คำสั่งในกลุ่มพยาบาล:
    'แล็บบ้าน <HN>'   — ดูตัวอย่างข้อความแล็บภาษาบ้านของผู้ป่วย (ไม่ส่ง)
    'ส่งแล็บบ้าน <HN>' — ส่งข้อความนั้นเข้า LINE ผู้ป่วยทันที (เคารพ PDPA)
    คืนข้อความตอบเสมอ."""
    import re as _re
    low = (body or "").strip().lower()
    send = low.startswith("ส่ง")
    m = _re.search(r"(\d{3,9})", body or "")
    if not m:
        return "ใช้: แล็บบ้าน <HN> (ดูตัวอย่าง) หรือ ส่งแล็บบ้าน <HN> (ส่งเข้า LINE ผู้ป่วย)"
    hn = m.group(1)
    fm = build_lab_friendly_message(cur, hn)
    if not fm:
        return (f"ยังไม่มีผลแล็บของ HN {hn} ในระบบ HD — "
                f"ข้อความภาษาบ้านสร้างจากแล็บล่าสุด (Hb/K/P/Alb/Kt/V) ต้อง import หรือบันทึกแล็บก่อน")
    if not send:
        return ("📋 ตัวอย่างข้อความที่ผู้ป่วยจะได้รับ (ยังไม่ส่ง):\n\n" + fm +
                f"\n\n👉 ส่งจริง: @รักษ์ไต ส่งแล็บบ้าน {hn}")
    # ── ส่งจริง: PDPA → LINE id → push พร้อมลิงก์โค้ชอาหาร (เหมือน push อัตโนมัติ) ──
    try:
        from .pdpa import status_for
        st = (status_for(cur, hn).get("line_messaging") or {}).get("status")
        if st == "withdrawn":
            return f"⛔ ผู้ป่วย HN {hn} ถอนความยินยอมรับข้อความ LINE แล้ว — ไม่ส่ง"
    except Exception as e:
        logger.warning("lab friendly pdpa %s: %s", hn, e)
    from .blueprint import _resolve_patient_line_id
    lid = _resolve_patient_line_id(hn)
    if not lid:
        return f"ผู้ป่วย HN {hn} ยังไม่ได้ผูก LINE — ให้ผู้ป่วยพิมพ์ 'ผูก <รหัส>' กับ OA ก่อน"
    try:
        from flask import current_app
        push_fn = current_app.config.get("LINE_PUSH_FN")
    except Exception:
        push_fn = None
    if not push_fn:
        return "ระบบส่ง LINE ยังไม่พร้อม — ลองใหม่อีกครั้ง"
    day = datetime.now(_THAI_TZ).strftime("%Y%m%d")
    food_url = f"{_base_url()}/hd/food/{make_token('food', hn, day)}"
    msg = fm + f"\n\n🍲 ไม่แน่ใจว่ามื้อไหนกินได้ไหม? ถ่ายรูปถามโค้ชอาหาร: {food_url}"
    try:
        if push_fn(lid, msg[:4900]) is False:
            return "ส่งไม่สำเร็จ (อาจติดโควตาข้อความ LINE ประจำเดือน)"
    except Exception as e:
        logger.warning("lab friendly push %s: %s", hn, e)
        return "ส่งไม่สำเร็จ — ลองใหม่อีกครั้ง"
    return f"✅ ส่งแล็บภาษาบ้าน + ลิงก์โค้ชอาหาร เข้า LINE ผู้ป่วย HN {hn} แล้ว"


# ───────────────────────── 2) Pre-HD Check-in ─────────────────────────

RED_FLAG_WORDS = ("เจ็บอก", "แน่นหน้าอก", "เหนื่อยมาก", "หอบ", "หมดสติ", "ใจสั่น")


def send_prehd_checkins(push_fn, db_url, get_setting=None):
    """06:30 เช้าวันฟอก: ส่งลิงก์เช็คอิน 3 ข้อให้คนไข้ที่มีคิววันนี้และผูก LINE แล้ว.
    คืนสถิติ; ไม่ throw. PDPA: withdrawn = ไม่ส่งเสมอ."""
    import psycopg2
    from .blueprint import _resolve_patient_line_id
    out = {"sent": 0, "no_line": 0, "blocked": 0}
    today = datetime.now(_THAI_TZ).date()
    day_letter = _DAY_LETTER[today.weekday()]
    day_str = today.strftime("%Y%m%d")
    conn = psycopg2.connect(db_url, connect_timeout=5)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT hn, patient_name FROM hd_patient_mapping
                           WHERE is_active = TRUE AND schedule_days IS NOT NULL
                             AND schedule_days LIKE %s ORDER BY patient_name""",
                        (f"%{day_letter}%",))
            roster = cur.fetchall()
            # กันส่งซ้ำ: ใครเช็คอินแล้ววันนี้ ข้าม
            cur.execute("SELECT hn FROM hd_prehd_checkins WHERE checkin_date = %s", (today,))
            done = {r[0] for r in cur.fetchall()}
            consent = {}
            try:
                from .pdpa import status_for
                for hn, _ in roster:
                    consent[hn] = (status_for(cur, hn).get("line_messaging") or {}).get("status")
            except Exception:
                pass
    finally:
        conn.close()
    base = _base_url()
    for hn, name in roster:
        if hn in done:
            continue
        if consent.get(hn) == "withdrawn":
            out["blocked"] += 1
            continue
        lid = _resolve_patient_line_id(hn)
        if not lid:
            out["no_line"] += 1
            continue
        url = f"{base}/hd/checkin/{make_token('checkin', hn, day_str)}"
        msg = ("🌅 สวัสดีตอนเช้า — วันนี้มีนัดฟอกเลือดนะคะ/ครับ\n"
               "ช่วยตอบ 3 ข้อสั้นๆ ก่อนมา เพื่อให้พยาบาลเตรียมพร้อมรับ:\n" + url +
               "\n(ใช้เวลาไม่ถึง 1 นาที)" + DISCLAIMER)
        try:
            if push_fn(lid, msg) is not False:
                out["sent"] += 1
        except Exception as e:
            logger.warning("prehd push %s: %s", hn, e)
    logger.info("prehd checkin: %s", out)
    return out


def checkin_is_flagged(symptoms_text):
    t = (symptoms_text or "")
    return any(w in t for w in RED_FLAG_WORDS)


# ── 3) สรุปจบรอบฟอกถึง LINE ผู้ป่วย: มีแล้วในระบบ (blueprint.send_patient_summary — ว้าว #7) ──


# ───────────────────────── 4) Food Coach (Claude Vision + แล็บรายคน) ─────────────────────────

def analyze_food_photo(img_bytes, labs):
    """รูปอาหาร + แล็บล่าสุดของคนไข้ → คำแนะนำสั้นภาษาไทย. คืน str หรือ None."""
    import io, json as _json, base64
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None
    try:
        from PIL import Image, ImageOps
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(img_bytes))).convert("RGB")
        if max(im.size) > 1280:
            im.thumbnail((1280, 1280))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82)
        img_bytes = buf.getvalue()
    except Exception:
        pass
    lab_txt = ", ".join(f"{k}={v}" for k, v in labs.items() if v is not None) or "ไม่มีผลแล็บล่าสุด"
    prompt = (
        "คุณคือโค้ชโภชนาการของหน่วยไตเทียม ดูรูปอาหารแล้วตอบผู้ป่วยฟอกไตเป็นภาษาไทย อบอุ่น ไม่ดุ\n"
        f"ผลเลือดล่าสุดของผู้ป่วยคนนี้: {lab_txt} (K หน่วย mEq/L เป้า 3.5-5.0, P หน่วย mg/dL เป้า 3.0-5.5)\n"
        "ตอบ 4 ส่วนสั้นๆ: 1) เมนูที่เห็น 2) ระดับความเหมาะกับโรคไต 🟢/🟡/🔴 พร้อมเหตุผล (โพแทสเซียม ฟอสฟอรัส โซเดียม น้ำ) "
        "โยงกับค่าเลือดของเขาถ้าเกี่ยว เช่น K กำลังสูงให้เตือนหนักขึ้น 3) กินได้แค่ไหน/ปรับยังไง 4) กำลังใจ 1 ประโยค\n"
        "รวมไม่เกิน 110 คำ ห้ามวินิจฉัย/สั่งยา ไม่แน่ใจว่าเป็นเมนูอะไรให้ถามกลับ"
    )
    import requests as _rq
    body = _json.dumps({
        "model": "claude-sonnet-4-20250514", "max_tokens": 450,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.b64encode(img_bytes).decode()}},
            {"type": "text", "text": prompt}]}],
    }).encode("utf-8")
    resp = _rq.post("https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                             "content-type": "application/json"},
                    data=body, timeout=45)
    if resp.status_code != 200:
        logger.info("food coach HTTP %s", resp.status_code)
        return None
    txt = (resp.json().get("content") or [{}])[0].get("text", "").strip()
    return txt or None


# ───────────────────────── 3b) Family Link — ญาติรับสรุปหลังฟอก ─────────────────────────

def family_code_command(cur, conn, body):
    """คำสั่งในกลุ่มพยาบาล: 'รหัสญาติ <HN>' ออกรหัสผูกญาติ (48 ชม. ใช้ครั้งเดียว),
    'ยกเลิกญาติ <HN>' ถอนญาติทั้งหมดของ HN นั้น. คืนข้อความตอบเสมอ."""
    import re as _re
    import secrets
    low = body.strip().lower()
    m = _re.search(r"(\d{3,9})", body)
    if not m:
        return "ใช้: รหัสญาติ <HN> หรือ ยกเลิกญาติ <HN>"
    hn = m.group(1)
    cur.execute("SELECT patient_name FROM hd_patient_mapping WHERE hn = %s", (hn,))
    pr = cur.fetchone()
    if not pr:
        return f"ไม่พบ HN {hn} ในทะเบียนผู้ป่วย"
    name = pr[0] or hn
    if low.startswith("ยกเลิกญาติ"):
        cur.execute("DELETE FROM hd_family_links WHERE hn = %s", (hn,))
        n = cur.rowcount
        cur.execute("DELETE FROM hd_family_codes WHERE hn = %s AND used_at IS NULL", (hn,))
        conn.commit()
        return f"✅ ถอนญาติของ {name} (HN {hn}) แล้ว {n} ราย และยกเลิกรหัสที่ค้างอยู่"
    # ออกรหัสใหม่ (เจ้าหน้าที่ยืนยันความยินยอมของผู้ป่วยที่เคาน์เตอร์ก่อนออกรหัส)
    code = f"{secrets.randbelow(900000) + 100000}"
    cur.execute("""INSERT INTO hd_family_codes (code, hn, expires_at)
                   VALUES (%s, %s, NOW() + INTERVAL '48 hours')""", (code, hn))
    conn.commit()
    liff_id = ""
    try:
        from .blueprint import get_setting_value
        liff_id = (get_setting_value("family_liff_id", "") or os.environ.get("HD_FAMILY_LIFF_ID", "")
                   or "2012225688-BPqtXCLI").strip()
    except Exception:
        liff_id = (os.environ.get("HD_FAMILY_LIFF_ID", "") or "2012225688-BPqtXCLI").strip()
    link = (f"https://liff.line.me/{liff_id}?code={code}" if liff_id
            else f"{_base_url()}/hd/family?code={code}")
    out = (f"👨‍👩‍👧 รหัสผูกญาติของ {name} (HN {hn})\n"
           f"🔑 รหัส: {code} (ใช้ได้ 48 ชม. ครั้งเดียว)\n"
           f"ให้ญาติเปิดลิงก์นี้ใน LINE แล้วใส่รหัส:\n{link}\n"
           f"ผูกแล้วญาติจะได้รับสรุปทุกครั้งที่ฟอกเสร็จ")
    if not liff_id:
        out += "\n⚠️ ยังไม่ตั้งค่า family_liff_id — ลิงก์นี้เปิดนอก LINE จะผูกไม่ได้ (ต้องเปิดผ่าน LIFF)"
    return out


def register_family(cur, conn, code, line_user_id, display_name="", label=""):
    """ผูกญาติด้วยรหัส — คืน (ok, message)."""
    cur.execute("""SELECT hn FROM hd_family_codes
                   WHERE code = %s AND used_at IS NULL AND expires_at > NOW()""", (str(code).strip(),))
    r = cur.fetchone()
    if not r:
        return False, "รหัสไม่ถูกต้องหรือหมดอายุ — ขอรหัสใหม่จากเจ้าหน้าที่ที่คลินิก"
    hn = r[0]
    cur.execute("SELECT patient_name FROM hd_patient_mapping WHERE hn = %s", (hn,))
    pr = cur.fetchone()
    name = (pr[0] if pr else "") or hn
    cur.execute("""INSERT INTO hd_family_links (hn, line_user_id, label, display_name)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (hn, line_user_id) DO UPDATE SET label = EXCLUDED.label""",
                (hn, line_user_id, (label or "")[:40], (display_name or "")[:60]))
    cur.execute("UPDATE hd_family_codes SET used_at = NOW() WHERE code = %s", (str(code).strip(),))
    conn.commit()
    first = name.split()
    nick = first[-1] if first else name
    return True, f"ผูกสำเร็จ 💚 คุณจะได้รับข้อความสรุปทุกครั้งที่คุณ{nick}ฟอกเลือดเสร็จ"


def _family_summary_text(cur, session_id):
    """ข้อความสรุปจบรอบฉบับครอบครัว (สั้น อุ่นใจ ไม่มีรายละเอียดทางคลินิกลึก)."""
    cur.execute("""SELECT p.hn, p.patient_name, s.started_at, s.ended_at, s.pre_weight, s.post_weight
                   FROM hd_sessions s JOIN hd_patient_mapping p ON p.id = s.patient_id
                   WHERE s.id = %s AND s.status = 'completed'""", (session_id,))
    r = cur.fetchone()
    if not r:
        return None, None
    hn, name = r[0], r[1] or ""
    first = name.split()
    nick = first[-1] if first else name
    lines = [f"💚 วันนี้คุณ{nick}ฟอกเลือดเสร็จเรียบร้อยแล้วค่ะ/ครับ"]
    try:
        if r[2] and r[3]:
            mins = int((r[3] - r[2]).total_seconds() // 60)
            if 0 < mins < 24 * 60:
                lines.append(f"⏱ ใช้เวลา {mins // 60} ชม. {mins % 60} นาที ราบรื่นดี")
    except Exception:
        pass
    try:
        if r[4] and r[5]:
            lines.append(f"⚖️ น้ำหนักหลังฟอก {float(r[5]):.1f} กก.")
    except Exception:
        pass
    lines.append("ขอบคุณที่ดูแลกันนะคะ/ครับ 🙏 — คลินิกระนอง รักษ์ไต")
    return hn, "\n".join(lines) + DISCLAIMER


def push_family_summaries(app_config, db_url, session_id):
    """ส่งสรุปจบรอบให้ญาติทุกคนที่ผูกกับ HN ของ session นี้. never raises."""
    import psycopg2
    out = {"sent": 0, "links": 0}
    try:
        push_fn = app_config.get("LINE_PUSH_FN")
        if not push_fn:
            return {"off": True}
        try:
            from .blueprint import get_setting_value
            if (get_setting_value("family_summary_push", "on") or "on").lower() in ("off", "0", "false"):
                return {"off": True}
        except Exception:
            pass
        conn = psycopg2.connect(db_url, connect_timeout=5)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                hn, text = _family_summary_text(cur, session_id)
                if not hn or not text:
                    return out
                cur.execute("SELECT line_user_id FROM hd_family_links WHERE hn = %s", (hn,))
                uids = [r[0] for r in cur.fetchall()]
        finally:
            conn.close()
        out["links"] = len(uids)
        for uid in uids[:5]:
            try:
                if push_fn(uid, text[:4900]) is not False:
                    out["sent"] += 1
            except Exception as e:
                logger.warning("family push: %s", e)
        return out
    except Exception as e:
        logger.warning("push_family_summaries: %s", e)
        return {"error": str(e)[:80]}
