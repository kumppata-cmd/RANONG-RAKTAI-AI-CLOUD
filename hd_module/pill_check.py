"""💊 ถ่ายรูปกำยา เช็กให้ (Pill Photo Check) — 28 พ.ย. 2569.

At the clinic a nurse photographs the pills *as dispensed* for each time slot
(เช้า/กลางวัน/เย็น/ก่อนนอน) → reference photo per patient per slot.
At home the patient opens a signed link (from LINE), picks the slot (auto by
clock), photographs the pills in their hand → Claude Vision compares against
(a) the reference photo and (b) the expected list from the medication record
→ "ครบ / อาจขาด … / มีเม็ดที่ไม่ตรง …". One tap sends a text-only note to the
nurse group (no photo, no diagnosis).

Switch ⚙️ pill_check (default off). Tokens: patient_engage.make_token("pill").
Photos stay inside the DB (bytea) — never written to the document store.
"""
import base64
import io
import json
import logging
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

SETTING_KEY = "pill_check"
TOKEN_KIND = "pill"
TOKEN_DAYS = 45
SLOTS = ("morning", "noon", "evening", "bedtime")
SLOT_TH = {"morning": "เช้า", "noon": "กลางวัน", "evening": "เย็น", "bedtime": "ก่อนนอน"}
MAX_IMG = 6 * 1024 * 1024
REF_MAX_PX = 1024

REF_SQL = """CREATE TABLE IF NOT EXISTS hd_pill_reference (
    id SERIAL PRIMARY KEY, patient_id INTEGER, hn VARCHAR(20) NOT NULL, slot VARCHAR(10) NOT NULL,
    image BYTEA NOT NULL, mime VARCHAR(30) DEFAULT 'image/jpeg', items JSONB, note VARCHAR(300),
    created_by VARCHAR(80), created_at TIMESTAMPTZ DEFAULT NOW(), active BOOLEAN DEFAULT TRUE)"""
CHECK_SQL = """CREATE TABLE IF NOT EXISTS hd_pill_checks (
    id SERIAL PRIMARY KEY, hn VARCHAR(20) NOT NULL, slot VARCHAR(10), verdict VARCHAR(12), pills_seen INTEGER,
    detail JSONB, text TEXT, has_reference BOOLEAN, notified BOOLEAN DEFAULT FALSE, created_at TIMESTAMPTZ DEFAULT NOW())"""


def _thai_now():
    return datetime.now(timezone.utc) + timedelta(hours=7)


def is_enabled(get_setting=None):
    try:
        if get_setting is None:
            from .blueprint import get_setting_value as get_setting
        return str(get_setting(SETTING_KEY, "off") or "off").strip().lower() in ("on", "1", "true", "yes")
    except Exception:
        return False


def slot_now(now=None):
    h = (now or _thai_now()).hour
    if h < 10:
        return "morning"
    if h < 15:
        return "noon"
    if h < 20:
        return "evening"
    return "bedtime"


# ───────────────────────── expected list from the medication record ─────────────────────────

def slots_for_freq(freq, dose=""):
    """Frequency text (Thai/English/sig code) → set of slots. Unknown → {'morning'}."""
    f = (str(freq or "") + " " + str(dose or "")).lower().strip()
    if not f.strip():
        return {"morning"}
    try:
        from .med_sheet_reader import parse_sig_code
        sig = parse_sig_code(str(freq or "").strip())
    except Exception:
        sig = None
    times = sig["times"] if sig else None
    timing = (sig or {}).get("timing", "")
    if times is None:
        m = re.search(r"(\d)\s*[x×]\s*(\d)", f)
        if m:
            times = int(m.group(2))
        elif re.search(r"\bqid\b|วันละ\s*4", f):
            times = 4
        elif re.search(r"\btid\b|วันละ\s*3|3\s*ครั้ง|3\s*เวลา", f):
            times = 3
        elif re.search(r"\bbid\b|วันละ\s*2|2\s*ครั้ง|2\s*เวลา", f):
            times = 2
        elif re.search(r"\bod\b|\bqd\b|\bhs\b|วันละ\s*1|1\s*ครั้ง|once|daily|ก่อนนอน", f):
            times = 1
    if re.search(r"\bhs\b|ก่อนนอน|bedtime", f) or "ก่อนนอน" in timing:
        return {"bedtime"} if (times or 1) == 1 else {"morning", "bedtime"}
    if re.search(r"เช้า", f) and times in (None, 1):
        return {"morning"}
    if re.search(r"เย็น", f) and times in (None, 1):
        return {"evening"}
    if re.search(r"\b(tiw|biw|q\s*(mon|tue|wed|thu|fri)|สัปดาห์ละ|weekly|หลังฟอก|post\s*hd|iv|sc)\b", f):
        return set()   # not a home oral slot
    return {1: {"morning"}, 2: {"morning", "evening"}, 3: {"morning", "noon", "evening"}, 4: {"morning", "noon", "evening", "bedtime"}}.get(times or 1, {"morning"})


def expected_for(cur, hn, slot):
    """Active confirmed oral meds whose schedule includes `slot` → [{drug, dose, freq, tabs}]."""
    cur.execute("""SELECT m.drug_name, m.dose, m.frequency, m.route FROM hd_medication_orders m
                   JOIN hd_patient_mapping p ON p.id = m.patient_id
                   WHERE p.hn = %s AND m.is_active = TRUE
                     AND COALESCE(m.verify_status,'confirmed') IN ('confirmed','merged')
                   ORDER BY m.drug_name""", (hn,))
    out = []
    for drug, dose, freq, route in cur.fetchall():
        if (route or "").upper() in ("IV", "SC", "IM", "TOPICAL"):
            continue
        if slot in slots_for_freq(freq, dose):
            tabs = None
            try:
                from .med_sheet_reader import parse_sig_code
                sig = parse_sig_code(str(freq or "").strip())
                tabs = sig["tabs"] if sig else None
            except Exception:
                pass
            if tabs is None:
                m = re.search(r"(\d+(?:\.\d+)?)\s*(?:เม็ด|tab|แคป|cap)", f"{dose or ''} {freq or ''}".lower())
                tabs = float(m.group(1)) if m else None
            out.append({"drug": drug, "dose": dose, "freq": freq, "tabs": tabs})
    return out


# ───────────────────────── reference photos ─────────────────────────

def _shrink(img_bytes, max_px=REF_MAX_PX, quality=82):
    try:
        from PIL import Image, ImageOps
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(img_bytes))).convert("RGB")
        if max(im.size) > max_px:
            im.thumbnail((max_px, max_px))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality)
        return buf.getvalue()
    except Exception:
        return img_bytes


def save_reference(cur, hn, slot, img_bytes, items=None, note="", who=""):
    if slot not in SLOTS:
        raise ValueError("slot")
    cur.execute(REF_SQL)
    cur.execute("SELECT id FROM hd_patient_mapping WHERE hn = %s", (hn,))
    r = cur.fetchone()
    pid = r[0] if r else None
    cur.execute("UPDATE hd_pill_reference SET active = FALSE WHERE hn = %s AND slot = %s AND active = TRUE", (hn, slot))
    cur.execute("""INSERT INTO hd_pill_reference (patient_id, hn, slot, image, items, note, created_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (pid, hn, slot, _shrink(img_bytes), json.dumps(items or [], ensure_ascii=False), (note or "")[:300], (who or "")[:80]))
    return cur.fetchone()[0]


def get_reference(cur, hn, slot, with_image=True):
    cur.execute(REF_SQL)
    cur.execute(f"""SELECT id, {'image' if with_image else 'NULL'}, items, note, created_by, created_at FROM hd_pill_reference
                    WHERE hn = %s AND slot = %s AND active = TRUE ORDER BY id DESC LIMIT 1""", (hn, slot))
    r = cur.fetchone()
    if not r:
        return None
    return {"id": r[0], "image": bytes(r[1]) if r[1] is not None else None, "items": r[2] or [], "note": r[3],
            "created_by": r[4], "created_at": r[5].isoformat() if r[5] else None}


def list_references(cur, hn):
    cur.execute(REF_SQL)
    cur.execute("""SELECT slot, id, items, note, created_by, created_at, octet_length(image) FROM hd_pill_reference
                   WHERE hn = %s AND active = TRUE ORDER BY slot""", (hn,))
    return [{"slot": r[0], "slot_th": SLOT_TH.get(r[0], r[0]), "id": r[1], "items": r[2] or [], "note": r[3], "created_by": r[4],
             "created_at": r[5].isoformat() if r[5] else None, "bytes": r[6]} for r in cur.fetchall()]


# ───────────────────────── AI comparison ─────────────────────────

def build_prompt(expected, has_ref, slot):
    exp = "\n".join(f"- {e['drug']} {e.get('dose') or ''} ({e.get('freq') or ''})" + (f" ≈ {e['tabs']:g} เม็ด" if e.get("tabs") else "")
                    for e in expected) or "- (ไม่มีรายการยาในระบบสำหรับมื้อนี้)"
    ref = ("รูปที่ 1 = รูปอ้างอิงที่พยาบาลถ่ายไว้ตอนจัดยามื้อนี้ (ชุดที่ถูกต้อง) · รูปที่ 2 = รูปที่ผู้ป่วยถ่ายวันนี้ "
           "ให้เทียบรูปที่ 2 กับรูปที่ 1 เป็นหลัก (จำนวน สี รูปทรง ขนาด ตัวพิมพ์บนเม็ด)" if has_ref
           else "มีรูปเดียว = รูปที่ผู้ป่วยถ่ายวันนี้ ให้เทียบกับรายการยาที่คาดหวังด้านล่าง (นับจำนวนและลักษณะเม็ด)")
    return (
        f"คุณช่วยตรวจ 'กำยา' มื้อ{SLOT_TH.get(slot, slot)} ของผู้ป่วยฟอกไตที่บ้าน {ref}\n"
        f"รายการยาที่คาดหวังสำหรับมื้อนี้:\n{exp}\n\n"
        "ตอบเป็น JSON เท่านั้น: {\"pills_seen\": จำนวนเม็ดที่เห็นในรูปผู้ป่วย, "
        "\"items\": [{\"desc\": \"ลักษณะเม็ด เช่น เม็ดกลมขาวเล็ก\", \"count\": n, \"match\": \"ชื่อยาที่น่าจะใช่ หรือ null\"}], "
        "\"missing\": [\"ยา/เม็ดที่น่าจะขาดเมื่อเทียบกับอ้างอิง\"], \"extra\": [\"เม็ดที่ไม่ควรมี/ไม่ตรงอ้างอิง\"], "
        "\"verdict\": \"ok|missing|extra|unsure\", \"confidence\": 0-1, \"note\": \"เหตุผลสั้น ๆ ภาษาไทย\"}\n"
        "กติกา: ไม่แน่ใจให้ตอบ unsure ดีกว่าเดา · ถ้ารูปมืด/เบลอ/ไม่เห็นเม็ดชัด → unsure · ห้ามแนะนำให้กินหรือไม่กินยา"
    )


def analyze(patient_img, expected, ref_img=None, slot="morning", api_key=None, model_id=None, post_fn=None):
    """→ dict(result) หรือ dict(error=...) · uses machine_ocr.VISION_SLOTS."""
    import os
    from . import machine_ocr as mo
    from .ai_text import extract_json
    key = api_key if api_key is not None else (os.environ.get("ANTHROPIC_API_KEY", "") or "").strip()
    if not key and post_fn is None:
        return {"error": "no_api_key"}
    if not model_id:
        try:
            from .patient_engage import ai_model
            model_id = ai_model()
        except Exception:
            model_id = "claude-haiku-5-5"
    content = []
    if ref_img:
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(_shrink(ref_img, 1280)).decode()}})
    content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(_shrink(patient_img, 1280)).decode()}})
    content.append({"type": "text", "text": build_prompt(expected, bool(ref_img), slot)})
    body = json.dumps({"model": model_id, "max_tokens": 1200, "messages": [{"role": "user", "content": content}]}).encode("utf-8")
    if not mo.VISION_SLOTS.acquire(blocking=False):
        return {"error": "busy"}
    try:
        if post_fn is None:
            import requests as _rq
            post_fn = lambda b: _rq.post("https://api.anthropic.com/v1/messages",  # noqa: E731
                                         headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                                         data=b, timeout=60)
        try:
            resp = post_fn(body)
        except Exception as e:  # noqa: BLE001
            return {"error": f"net:{type(e).__name__}"}
    finally:
        mo.VISION_SLOTS.release()
    code = getattr(resp, "status_code", 200)
    if code != 200:
        txt = getattr(resp, "text", "") or ""
        return {"error": "no_credit" if (code == 400 and "credit balance" in txt) else f"api:{code}"}
    try:
        rj = resp.json()
        txt = "".join(x.get("text", "") for x in (rj.get("content") or []) if x.get("type") == "text")
    except Exception:
        return {"error": "bad_json"}
    js = extract_json(txt)
    if not isinstance(js, dict):
        return {"error": "empty"}
    v = str(js.get("verdict") or "unsure").lower()
    if v not in ("ok", "missing", "extra", "unsure"):
        v = "unsure"
    try:
        conf = float(js.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    if conf < 0.5 and v == "ok":
        v = "unsure"
    return {"verdict": v, "pills_seen": js.get("pills_seen"), "items": js.get("items") or [], "missing": js.get("missing") or [],
            "extra": js.get("extra") or [], "confidence": conf, "note": js.get("note") or ""}


def patient_text(res, slot, expected, has_ref):
    """Plain-language result for the patient page."""
    sl = SLOT_TH.get(slot, slot)
    n = res.get("pills_seen")
    exp_n = sum((e.get("tabs") or 1) for e in expected) if expected else None
    base = f"มื้อ{sl}" + (f" · เห็น {n} เม็ด" if n is not None else "") + (f" (คลินิกจัด ≈ {exp_n:g} เม็ด)" if exp_n else "")
    v = res.get("verdict")
    if v == "ok":
        return "✅ ดูครบตามที่คลินิกจัดไว้\n" + base + ("\nเทียบกับรูปอ้างอิงของพยาบาลแล้ว" if has_ref else "")
    if v == "missing":
        return "⚠️ อาจขาด: " + ", ".join(res.get("missing") or ["บางเม็ด"]) + "\n" + base + "\nถ้าไม่แน่ใจ อย่าเพิ่งกิน — กด 'แจ้งพยาบาล' ด้านล่าง"
    if v == "extra":
        return "⚠️ มีเม็ดที่ไม่ตรงกับที่คลินิกจัด: " + ", ".join(res.get("extra") or ["บางเม็ด"]) + "\n" + base + "\nอย่าเพิ่งกินเม็ดที่ไม่แน่ใจ — กด 'แจ้งพยาบาล'"
    return "🤔 ระบบยังดูไม่ชัด (" + (res.get("note") or "รูปอาจมืด/เบลอ") + ")\n" + base + "\nลองถ่ายใหม่ในที่สว่าง วางเม็ดยาแยกกันบนฝ่ามือหรือกระดาษขาว"


def record_check(cur, hn, slot, res, text, has_ref):
    cur.execute(CHECK_SQL)
    cur.execute("""INSERT INTO hd_pill_checks (hn, slot, verdict, pills_seen, detail, text, has_reference)
                   VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (hn, slot, res.get("verdict"), res.get("pills_seen") if isinstance(res.get("pills_seen"), int) else None,
                 json.dumps({k: res.get(k) for k in ("items", "missing", "extra", "confidence", "note")}, ensure_ascii=False),
                 text, bool(has_ref)))
    return cur.fetchone()[0]


def recent_checks(cur, hn=None, days=14, limit=100):
    cur.execute(CHECK_SQL)
    if hn:
        cur.execute("SELECT id, hn, slot, verdict, pills_seen, detail, created_at, notified FROM hd_pill_checks WHERE hn = %s AND created_at >= NOW() - (%s || ' days')::interval ORDER BY id DESC LIMIT %s", (hn, str(days), limit))
    else:
        cur.execute("""SELECT c.id, c.hn, c.slot, c.verdict, c.pills_seen, c.detail, c.created_at, c.notified, p.patient_name
                       FROM hd_pill_checks c LEFT JOIN hd_patient_mapping p ON p.hn = c.hn
                       WHERE c.created_at >= NOW() - (%s || ' days')::interval ORDER BY c.id DESC LIMIT %s""", (str(days), limit))
    out = []
    for r in cur.fetchall():
        out.append({"id": r[0], "hn": r[1], "slot": r[2], "slot_th": SLOT_TH.get(r[2], r[2]), "verdict": r[3], "pills_seen": r[4],
                    "detail": r[5] or {}, "at": r[6].isoformat() if r[6] else None, "notified": r[7], "name": r[8] if len(r) > 8 else None})
    return out


def link_for(hn, base_url):
    from .patient_engage import make_token
    day = _thai_now().strftime("%Y%m%d")
    return f"{base_url.rstrip('/')}/hd/pill/{make_token(TOKEN_KIND, hn, day)}?openExternalBrowser=1"


def link_command(cur, body, base_url):
    """กลุ่มพยาบาล: 'ลิงก์ยา <HN>' → ลิงก์ตรวจกำยา."""
    m = re.search(r"(\d{3,9})", body or "")
    if not m:
        return "ใช้: ลิงก์ยา <HN> เช่น ลิงก์ยา 68443"
    hn = m.group(1)
    cur.execute("SELECT patient_name FROM hd_patient_mapping WHERE hn = %s", (hn,))
    r = cur.fetchone()
    if not r:
        return f"ไม่พบ HN {hn}"
    refs = list_references(cur, hn)
    return (f"💊 ลิงก์ตรวจกำยาของ {r[0] or hn} (HN {hn})\n{link_for(hn, base_url)}\n\n"
            f"ส่งต่อให้ผู้ป่วย/ญาติ — เปิดลิงก์ → เลือกมื้อ → ถ่ายรูปยาในมือ → ระบบเทียบกับที่คลินิกจัด (ใช้ได้ {TOKEN_DAYS} วัน)\n"
            + (f"รูปอ้างอิงที่มี: {', '.join(x['slot_th'] for x in refs)}" if refs else
               f"⚠️ ยังไม่มีรูปอ้างอิง — ถ่ายได้ที่ {base_url.rstrip('/')}/hd/pill-ref/{hn} (ระบบจะเทียบกับรายการยาอย่างเดียวไปก่อน)"))
