"""📷 Magic Camera 2.0 — อ่านหน้าจอเครื่องฟอกไตแบบรู้รุ่น (25 พ.ย. 2569)

profile ต่อรุ่นตั้งจากรูปจริงของหน่วย (โฟลเดอร์ "รูปหน้าจอHD" 7 รุ่น 111 รูป):
  Fresenius 4008B · 4008S (OCM) · 5008S · NIKKISO DBB-07 · DBB-27 · DBB-27C · DBB-EXA
หลักการ:
  * ค่าที่ "เป็นตัวเลขบนจอ" เท่านั้นที่อ่าน — ค่าที่รุ่นนั้นแสดงเป็นแท่ง LED/แท่งกราฟ (เช่น AP/VP/TMP ของ 4008)
    ไม่อ่านเป็นตัวเลข (บอกพยาบาลให้อ่านเอง) เพราะเดาจากแท่งได้ผิด ±50 mmHg
  * หน่วยต่างกันตามรุ่น: NIKKISO แสดง UF volume/goal เป็น L และ UF rate เป็น L/h → แปลงเป็น mL / mL/h
  * ทุกค่ามี confidence 0–1; ต่ำกว่าเกณฑ์ = ไม่ใส่ลงช่อง (ให้พยาบาลพิมพ์เอง) ไม่มี "ผิดเงียบ ๆ"
  * ตัวอย่างทุกครั้งที่พยาบาลยืนยัน → hd_ocr_samples (ตัวเลขอย่างเดียว) → วัดความแม่นรายรุ่นรายฟิลด์จริง
ไม่เก็บรูป · รูปหน้าจอบางรุ่นมีป้ายชื่อผู้ป่วยติดข้างเครื่อง — prompt สั่งไม่อ่าน/ไม่ตอบข้อความใด ๆ นอกจากตัวเลข
"""
import json
import logging
import threading
import uuid
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# ฟิลด์ของ HD Monitoring Sheet (ตรงกับฟอร์มรวม 📝) + ช่วงที่เป็นไปได้ (นอกช่วง = ตัดทิ้ง)
FIELDS = {
    "uf_removed":   {"label": "UF Removed", "unit": "mL",     "range": (0, 8000)},
    "uf_rate":      {"label": "UF Rate",    "unit": "mL/h",   "range": (0, 3000)},
    "uf_goal":      {"label": "UF Goal",    "unit": "mL",     "range": (0, 8000)},
    "time_left_min": {"label": "เวลาเหลือ",  "unit": "นาที",   "range": (0, 600)},
    "bfr":          {"label": "BFR",        "unit": "mL/min", "range": (50, 500)},
    "dfr":          {"label": "DFR",        "unit": "mL/min", "range": (300, 800)},
    "ap":           {"label": "AP",         "unit": "mmHg",   "range": (-300, 300)},
    "vp":           {"label": "VP",         "unit": "mmHg",   "range": (-50, 500)},
    "tmp":          {"label": "TMP",        "unit": "mmHg",   "range": (-100, 500)},
    "conductivity": {"label": "Cond.",      "unit": "mS/cm",  "range": (12, 16)},
    "dialysate_temp": {"label": "Dialysate T", "unit": "°C",  "range": (34, 39)},
    "sbp":          {"label": "SBP",        "unit": "mmHg",   "range": (50, 260)},
    "dbp":          {"label": "DBP",        "unit": "mmHg",   "range": (20, 160)},
    "hr":           {"label": "PR",         "unit": "/min",   "range": (30, 200)},
    "heparin_rate": {"label": "Heparin",    "unit": "mL/h",   "range": (0, 10)},
}

# รุ่นเครื่อง: label, คำบอกใบ้ให้ AI (ตำแหน่ง/หน่วย/รูปแบบ), ฟิลด์ที่มีเป็นตัวเลข, ฟิลด์ที่เป็นแท่ง (ไม่อ่าน)
MACHINE_PROFILES = {
    "fresenius_4008b": {
        "label": "Fresenius 4008B", "brand": "Fresenius", "keywords": ["4008 B", "4008B"],
        "numeric": ["uf_removed", "uf_rate", "uf_goal", "time_left_min", "bfr", "heparin_rate"],
        "bargraph": ["ap", "vp", "tmp", "conductivity"],
        "hint": ("จอตัวเลข 7-segment สีเขียวในกรอบ 'Ultrafiltration' 4 ช่องเรียงจากบนลงล่าง: UF Volume (mL ที่ดึงแล้ว), "
                 "UF Rate (mL/h, แสดง 4 หลักเช่น 0750 = 750), UF Goal (mL), Time Left (รูป H-MM เช่น 0-53 = 53 นาที). "
                 "BFR อยู่จอเล็กซ้ายล่างของตัวเครื่อง (เช่น 300). จอเล็กกลางล่างคือ heparin mL/h (เช่น 02.8 = 2.8). "
                 "Arterial/Venous/TMP/Conductivity เป็นแท่ง LED ไม่ใช่ตัวเลข — ให้ตอบ null เสมอ"),
    },
    "fresenius_4008s": {
        "label": "Fresenius 4008S (OCM)", "brand": "Fresenius", "keywords": ["4008 S", "4008S", "OCM"],
        "numeric": ["uf_removed", "uf_rate", "uf_goal", "time_left_min", "bfr", "heparin_rate"],
        "bargraph": ["ap", "vp", "tmp", "conductivity"],
        "hint": ("จอ LCD สี: คอลัมน์ขวาเป็นกล่องดำตัวเลขขาว เรียงบนลงล่าง UF Volume (mL ดึงแล้ว), UF Time Left (h:min), "
                 "UF Rate (mL/h), UF Goal (mL), Eff. Blood Flow (mL/min — ใช้เป็น BFR ได้ถ้าจอเล็ก 7-segment ล่างซ้ายอ่านไม่ได้; "
                 "ถ้าอ่านได้ทั้งคู่ให้ใช้จอเล็ก 7-segment เป็น bfr), Cum. Blood Vol. (L — ไม่ต้องใช้). "
                 "จอเล็กกลางล่างคือ heparin mL/h (02.8 = 2.8). Arterial/Venous/TMP/Conductivity เป็นแท่ง LED ซ้ายจอ — ตอบ null"),
    },
    "fresenius_5008s": {
        "label": "Fresenius 5008S", "brand": "Fresenius", "keywords": ["5008", "HDF postdilution", "ISO goal"],
        "numeric": ["uf_removed", "uf_rate", "uf_goal", "time_left_min", "bfr", "ap", "vp", "tmp"],
        "bargraph": [],
        "hint": ("จอสีเทา-ฟ้า: แถวบน 'UF goal (mL) · UF time (h:min) · UF rate (mL/h) · UF volume (mL ดึงแล้ว)'. "
                 "ซ้ายบน ART (ตัวเลขติดลบ เช่น -185) ซ้ายล่าง VEN (เช่น 135) กลาง TMP (เช่น 190) — เป็นตัวเลขอ่านได้. "
                 "ขวาบนตัวเลขใหญ่สีเขียวใต้ 'Blood flow' คือ BFR mL/min (เช่น 300). แถว ISO goal/time/rate ไม่ต้องใช้"),
    },
    "nikkiso_dbb07": {
        "label": "NIKKISO DBB-07", "brand": "NIKKISO", "keywords": ["DBB-07", "Treat.time", "Total conduct"],
        "numeric": ["uf_removed", "uf_rate", "time_left_min", "bfr", "conductivity", "dialysate_temp", "vp", "tmp", "heparin_rate"],
        "bargraph": [],
        "hint": ("จอเขียวอ่อน แถวบนช่องฟ้า: 'Treat.time remaining' (hrs:mins), 'UF volume' เป็น **ลิตร** (0.94 = 940 mL), "
                 "'UF rate' เป็น **L/h** (0.43 = 430 mL/h), 'Total conduct.' mS/cm, 'BP1' = BFR mL/min. "
                 "แถวถัดไป 'D. temp.' °C, 'V press.' = VP mmHg, 'TMP' mmHg. ปุ่ม HP x.x mL/h = heparin. ไม่มี AP และ UF goal บนจอนี้ → null"),
    },
    "nikkiso_dbb27": {
        "label": "NIKKISO DBB-27", "brand": "NIKKISO", "keywords": ["DBB-27", "VEN PRESS", "DIALY FLOW"],
        "numeric": ["uf_removed", "uf_rate", "uf_goal", "time_left_min", "bfr", "dfr", "conductivity", "dialysate_temp", "vp", "tmp", "heparin_rate"],
        "bargraph": [],
        "hint": ("จอเขียว: แถวบน 'VEN PRESS' (mmHg), 'TMP' (mmHg อาจติดลบ เช่น -9), 'UF VOLUME' **ลิตร** (0.99 = 990 mL), "
                 "'UF GOAL' **ลิตร** (1.70 = 1700), 'UF RATE' **L/h** (0.44 = 440). ขวา: IP (heparin mL/h), DIALY TEMP °C, "
                 "T CONDUCT mS/cm, DIALY FLOW = DFR mL/min. ล่างขวา 'B. FLW' = BFR mL/min. 'REMAIN TIME h:mm' = เวลาเหลือ. ไม่มี AP → null"),
    },
    "nikkiso_dbb27c": {
        "label": "NIKKISO DBB-27C", "brand": "NIKKISO", "keywords": ["DBB-27C", "27C"],
        "numeric": ["uf_removed", "uf_rate", "uf_goal", "time_left_min", "bfr", "dfr", "conductivity", "dialysate_temp", "vp", "tmp", "heparin_rate"],
        "bargraph": [],
        "hint": ("จอเขียวเข้มเหมือน DBB-27: 'VEN PRESS', 'TMP' (อาจติดลบ), 'UF VOLUME' **ลิตร**, 'UF GOAL' **ลิตร**, 'UF RATE' **L/h** "
                 "(ตัวเลขอาจมีช่องว่างแทนจุด เช่น '0 58' = 0.58). ขวา IP (heparin), DIALY TEMP, T CONDUCT, DIALY FLOW = DFR. "
                 "ล่างขวา 'B. FLW' = BFR. เวลาเหลืออาจไม่แสดง → null. ไม่มี AP → null"),
    },
    "nikkiso_exa": {
        "label": "NIKKISO DBB-EXA", "brand": "NIKKISO", "keywords": ["DBB-EXA", "EXA", "Remain time"],
        "numeric": ["uf_removed", "uf_rate", "uf_goal", "time_left_min", "bfr", "conductivity", "dialysate_temp", "ap", "vp", "tmp", "heparin_rate"],
        "bargraph": [],
        "hint": ("จอขาว-เขียว: แถวบน ART (ติดลบ เช่น -191), VEN, TMP เป็นตัวเลขใหญ่. กล่องเขียวขวา 'UF volume' **ลิตร** (2.38 = 2380) "
                 "และ 'UF rate' **L/h** (1.13 = 1130); ตัวเลขเล็กใต้แถบ (เช่น 4.50) คือ UF goal เป็นลิตร. "
                 "'Remain time h:mm' กลางจอ. ขวา COND mS/cm, TEMP °C (dialysate), Heparin mL/h. ล่างขวา 'BF rate' = BFR mL/min"),
    },
}

GENERIC_HINT = ("ถ้าไม่รู้รุ่น: ค่าที่ลงท้าย L หรือ L/h ให้แปลงเป็น mL / mL/h (×1000). "
                "AP/VP/TMP ที่แสดงเป็นแท่งไฟ ไม่ใช่ตัวเลข → null")

CONF_MIN = 0.70          # ต่ำกว่านี้ = ไม่ใส่ลงช่อง (ให้พยาบาลพิมพ์)
LITER_FIELDS = ("uf_removed", "uf_goal", "uf_rate")


def profile_for(model):
    return MACHINE_PROFILES.get((model or "").strip().lower())


def model_choices():
    return [{"id": k, "label": v["label"], "brand": v["brand"]} for k, v in MACHINE_PROFILES.items()]


def guess_model(brand=None, model_text=None):
    """ทะเบียนเครื่อง (brand/model) → รหัสรุ่น; ไม่ตรงอะไรเลย → None"""
    txt = f"{brand or ''} {model_text or ''}".upper().replace("-", "").replace(" ", "")
    if not txt.strip():
        return None
    order = ["nikkiso_dbb27c", "nikkiso_dbb27", "nikkiso_dbb07", "nikkiso_exa",
             "fresenius_5008s", "fresenius_4008s", "fresenius_4008b"]
    for mid in order:
        for kw in MACHINE_PROFILES[mid]["keywords"]:
            if kw.upper().replace("-", "").replace(" ", "") in txt:
                return mid
    if "4008" in txt:
        return "fresenius_4008s" if "S" in txt.split("4008")[-1][:1] else "fresenius_4008b"
    return None


def build_prompt(model):
    p = profile_for(model)
    keys = ", ".join(f'"{k}"' for k in FIELDS)
    lines = [
        "นี่คือรูปหน้าจอเครื่องฟอกไตในหน่วยไตเทียม อ่าน **เฉพาะตัวเลขบนจอ** แล้วตอบเป็น JSON เท่านั้น ห้ามมีข้อความอื่น",
        "ห้ามอ่านหรือตอบชื่อคน ป้าย สติกเกอร์ หรือข้อความใด ๆ นอกจากตัวเลขของเครื่อง",
        "รูปแบบ: {\"model_seen\": <ชื่อรุ่นที่เห็นบนเครื่อง หรือ null>, \"values\": {<field>: <number หรือ null>}, "
        "\"confidence\": {<field>: 0.0–1.0}, \"unreadable_reason\": <ข้อความสั้น ๆ หรือ null>}",
        f"field ที่รับ: {keys}",
        "หน่วยที่ต้องการ: uf_removed/uf_goal = mL, uf_rate = mL/h, time_left_min = นาที (H:MM → นาที), bfr/dfr = mL/min, "
        "ap/vp/tmp = mmHg (ติดลบได้), conductivity = mS/cm, dialysate_temp = °C, heparin_rate = mL/h",
        "ตัวเลข 7-segment สับได้ง่าย 8↔0, 6↔5, 1↔7, 3↔9 — ถ้าไม่มั่นใจให้ลด confidence ต่ำกว่า 0.7 หรือใส่ null ห้ามเดา",
        "ค่าที่ไม่มีบนจอรุ่นนี้ หรือแสดงเป็นแท่งไฟ/แท่งกราฟ ให้ใส่ null (ห้ามประมาณจากแท่ง)",
    ]
    if p:
        lines.append(f"รุ่นเครื่อง (พยาบาลเลือก): {p['label']} — {p['hint']}")
        if p["bargraph"]:
            lines.append("รุ่นนี้ฟิลด์ต่อไปนี้เป็นแท่งไฟ ตอบ null เสมอ: " + ", ".join(p["bargraph"]))
    else:
        lines.append(GENERIC_HINT)
    return "\n".join(lines)


def sanitize(raw, model):
    """ผล JSON จาก AI → {values, confidence, uncertain, dropped, model_seen, model_mismatch, reason}
    - แปลงลิตร→mL เมื่อค่าเล็กผิดปกติ (NIKKISO ส่ง 0.94) · ตัดค่านอกช่วง · ค่า confidence < CONF_MIN → uncertain (ไม่ใส่ช่อง)
    - ฟิลด์ที่รุ่นนั้นเป็นแท่ง → ตัดทิ้งเสมอ"""
    p = profile_for(model)
    vals = (raw or {}).get("values") or {}
    confs = (raw or {}).get("confidence") or {}
    out, conf_out, uncertain, dropped = {}, {}, [], []
    for k, spec in FIELDS.items():
        v = vals.get(k)
        if v is None or v == "":
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            dropped.append(k)
            continue
        if p and k in p["bargraph"]:
            dropped.append(k)
            continue
        # ลิตร → mL (NIKKISO / EXA): UF ที่น้อยกว่า 20 ไม่มีทางเป็น mL จริง
        if k in LITER_FIELDS and 0 < abs(v) < 20:
            v = v * 1000
        if k == "time_left_min" and 0 < v < 10 and float(v) != int(v):
            v = int(v) * 60 + round((v - int(v)) * 100)   # 1.47 (h.mm) → 107
        lo, hi = spec["range"]
        if not (lo <= v <= hi):
            dropped.append(k)
            continue
        c = confs.get(k)
        try:
            c = max(0.0, min(1.0, float(c))) if c is not None else 0.5
        except (TypeError, ValueError):
            c = 0.5
        v = round(v, 1) if k in ("conductivity", "dialysate_temp", "heparin_rate") else round(v)
        if c < CONF_MIN:
            uncertain.append({"field": k, "value": v, "confidence": round(c, 2)})
            continue
        out[k] = v
        conf_out[k] = round(c, 2)
    seen = (raw or {}).get("model_seen")
    mismatch = False
    if seen and p:
        g = guess_model(model_text=str(seen))
        mismatch = bool(g and g != model)
    return {"values": out, "confidence": conf_out, "uncertain": uncertain, "dropped": dropped,
            "model_seen": seen, "model_mismatch": mismatch,
            "reason": (raw or {}).get("unreadable_reason")}


# ── กัน 502: อ่านรูปพร้อมกันได้จำกัด (Claude Vision ค้าง thread ได้นานสุด = timeout) ─────────────
VISION_SLOTS = threading.BoundedSemaphore(2)
VISION_TIMEOUT_S = 25


def read_with_claude(img_bytes, model, api_key, ai_model_id, post_fn=None):
    """เรียก Claude Vision 1 ครั้ง → sanitize(). คืน None ถ้าไม่มี key / คิวเต็ม (raise RuntimeError('busy')) / อ่านไม่ได้"""
    import base64
    import io
    if not api_key:
        return None
    try:
        from PIL import Image, ImageOps
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(img_bytes))).convert("RGB")
        if max(im.size) > 1568:
            im.thumbnail((1568, 1568))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        img_bytes = buf.getvalue()
    except Exception:
        pass
    body = json.dumps({
        "model": ai_model_id, "max_tokens": 1200,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.b64encode(img_bytes).decode()}},
            {"type": "text", "text": build_prompt(model)},
        ]}],
    }).encode("utf-8")
    if not VISION_SLOTS.acquire(blocking=False):
        raise RuntimeError("busy")
    try:
        if post_fn is None:
            import requests as _rq
            post_fn = lambda b: _rq.post("https://api.anthropic.com/v1/messages",
                                         headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                                                  "content-type": "application/json"},
                                         data=b, timeout=VISION_TIMEOUT_S)
        resp = post_fn(body)
    finally:
        VISION_SLOTS.release()
    if getattr(resp, "status_code", 0) != 200:
        logger.info("machine-screen claude HTTP %s", getattr(resp, "status_code", "?"))
        return None
    txt = "".join(x.get("text", "") for x in (resp.json().get("content") or []) if x.get("type") == "text").strip()
    if txt.startswith("```"):
        txt = txt.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        raw = json.loads(txt)
    except Exception:
        return None
    if isinstance(raw, dict) and "values" not in raw:        # รูปแบบเก่า (flat) → ห่อ
        raw = {"values": raw, "confidence": {}}
    return sanitize(raw, model)


# ── ตัวอย่างเฉลย: AI อ่าน vs พยาบาลยืนยัน ───────────────────────────────────────────────
TOLERANCE = {"uf_removed": 10, "uf_goal": 10, "uf_rate": 10, "time_left_min": 2, "bfr": 5, "dfr": 10,
             "ap": 5, "vp": 5, "tmp": 5, "conductivity": 0.15, "dialysate_temp": 0.15,
             "sbp": 2, "dbp": 2, "hr": 2, "heparin_rate": 0.15}


def outcome_of(field, ai_value, nurse_value):
    """ok = ตรง (ภายใน tolerance) · wrong = AI ให้ค่าแต่พยาบาลแก้ · blank = AI ไม่ให้ค่า พยาบาลกรอกเอง · unused = AI ให้ค่า พยาบาลไม่ใช้"""
    if ai_value is None and nurse_value is None:
        return None
    if ai_value is None:
        return "blank"
    if nurse_value is None:
        return "unused"
    return "ok" if abs(float(ai_value) - float(nurse_value)) <= TOLERANCE.get(field, 0) else "wrong"


def record_samples(cur, session_id, model, ai_values, ai_conf, nurse_values, read_id, nurse_id=None):
    """บันทึกตัวอย่างต่อฟิลด์ (ตัวเลขอย่างเดียว) → จำนวนแถวที่บันทึก"""
    n = 0
    for k in FIELDS:
        a = ai_values.get(k) if ai_values else None
        v = nurse_values.get(k) if nurse_values else None
        oc = outcome_of(k, a, v)
        if oc is None:
            continue
        cur.execute("""INSERT INTO hd_ocr_samples (session_id, machine_model, field, ai_value, confidence, nurse_value,
                                                   outcome, read_id, nurse_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (session_id, (model or "")[:40] or None, k, a, (ai_conf or {}).get(k), v, oc, (read_id or "")[:40] or None, nurse_id))
        n += 1
    return n


def accuracy_report(cur, days=30):
    """สรุป ok/wrong/blank/unused ต่อรุ่นต่อฟิลด์ (เฉพาะที่พยาบาลยืนยันแล้ว)"""
    since = datetime.utcnow() - timedelta(days=days)
    cur.execute("""SELECT COALESCE(machine_model,'unknown'), field, outcome, COUNT(*)
                   FROM hd_ocr_samples WHERE created_at >= %s AND outcome IS NOT NULL
                   GROUP BY 1, 2, 3""", (since,))
    rep = {}
    for model, field, oc, n in cur.fetchall():
        m = rep.setdefault(model, {"label": (profile_for(model) or {}).get("label", model), "fields": {}, "reads": 0})
        f = m["fields"].setdefault(field, {"ok": 0, "wrong": 0, "blank": 0, "unused": 0})
        f[oc] = f.get(oc, 0) + int(n)
    for m in rep.values():
        for f in m["fields"].values():
            tot = f["ok"] + f["wrong"] + f["blank"] + f["unused"]
            f["n"] = tot
            given = f["ok"] + f["wrong"] + f["unused"]
            f["precision"] = round(f["ok"] / given, 3) if given else None     # เมื่อ AI ให้ค่า ถูกกี่ %
            f["coverage"] = round(given / tot, 3) if tot else None            # AI ให้ค่าได้กี่ % ของครั้งที่พยาบาลกรอก
    cur.execute("""SELECT COALESCE(machine_model,'unknown'), COUNT(DISTINCT read_id) FROM hd_ocr_samples
                   WHERE created_at >= %s GROUP BY 1""", (since,))
    for model, n in cur.fetchall():
        if model in rep:
            rep[model]["reads"] = int(n)
    return {"days": days, "models": rep}


def new_read_id():
    return uuid.uuid4().hex[:16]
