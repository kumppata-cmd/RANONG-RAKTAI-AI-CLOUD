"""Daily RO water check — Hardness · Free Cl · Total Cl (ตรวจระบบน้ำประจำวัน).

Flow (main channel = nurse LINE group):
    1. nurse sends 1–3 photos of the test vials in the group → each photo is
       stored as-is in GCS  water-check/_inbox/<date>/<user>_<msgid>.jpg and
       a row in hd_water_inbox (NOT the patient-document pipeline, no OCR)
    2. nurse types  "น้ำRO H 0 F 0.1 T 0"  (with or without @รักษ์ไต, any
       order, Thai or English keys) → the values are evaluated against the
       limits in ⚙️, one row goes into hd_water_daily, the inbox photos of the
       same sender from the last 20 minutes are attached and copied to
       water-check/YYYY/YYYY-MM/YYYY-MM-DD/… + Drive
       "ตรวจระบบน้ำประจำวัน/YYYY/YYYY-MM/", the bot replies ✅/❌ in the group
    3. ❌ → physician LINE as well

The check date is the LINE send date (Thai time) unless the text names one.
Limits default to ISO 23500 / AAMI for water after the carbon tanks:
    total chlorine < 0.1 ppm · free chlorine < 0.5 ppm · hardness 0 on the
    0/10/25/50 colour chart. Nurses read the chart; photos are the evidence.
Nothing here orders treatment or changes machine settings.
"""
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

DEFAULTS = {"water_total_cl_max": 0.1, "water_free_cl_max": 0.5, "water_hardness_max": 0,
            "water_interval_days": 7, "water_sample_point": "หลังถังคาร์บอน"}
GCS_ROOT = "water-check"
DRIVE_ROOT = "ตรวจระบบน้ำประจำวัน"
SHIFT_TH = {"morning": "เช้า", "afternoon": "บ่าย", "evening": "ค่ำ"}

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS hd_water_daily (
    id SERIAL PRIMARY KEY,
    check_date DATE NOT NULL,
    shift VARCHAR(20),
    sample_point VARCHAR(80),
    hardness_ppm NUMERIC(6,1),
    hardness_text VARCHAR(20),
    free_cl NUMERIC(5,2),
    total_cl NUMERIC(5,2),
    status VARCHAR(10) NOT NULL,
    fails TEXT,
    tester_name VARCHAR(100),
    tester_hash VARCHAR(64),
    source VARCHAR(20) DEFAULT 'line_group',
    raw_text TEXT,
    note TEXT,
    photos JSONB DEFAULT '[]'::jsonb,
    drive JSONB DEFAULT '[]'::jsonb,
    created_at TIMESTAMP DEFAULT NOW()
)
"""
CREATE_INDEX_SQL = "CREATE INDEX IF NOT EXISTS ix_hd_water_daily_date ON hd_water_daily(check_date DESC)"
CREATE_INBOX_SQL = """
CREATE TABLE IF NOT EXISTS hd_water_inbox (
    id SERIAL PRIMARY KEY,
    user_hash VARCHAR(64),
    msg_id VARCHAR(40) UNIQUE,
    gcs_path TEXT NOT NULL,
    received_at TIMESTAMP DEFAULT NOW(),
    record_id INTEGER REFERENCES hd_water_daily(id)
)
"""
ALTER_SQL = [
    "ALTER TABLE hd_water_daily ADD COLUMN IF NOT EXISTS removed_photos JSONB DEFAULT '[]'::jsonb",
    "ALTER TABLE hd_water_daily ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP",
    "ALTER TABLE hd_water_daily ADD COLUMN IF NOT EXISTS deleted_by VARCHAR(100)",
]
CREATE_SQL = CREATE_SQL + ";\n" + CREATE_INDEX_SQL + ";\n" + CREATE_INBOX_SQL + ";\n" + ";\n".join(ALTER_SQL) + ";"


def _thai_now():
    return datetime.now(timezone.utc) + timedelta(hours=7)


def _num(s):
    s = str(s).replace(",", ".").strip()
    gt = s.startswith((">", "มากกว่า"))
    m = re.search(r"\d+(?:\.\d+)?", s)
    if not m:
        return None, False
    return float(m.group(0)), gt


_PREFIX = re.compile(r"^\s*(?:@?\s*(?:รักษ์ไต|raktai)\s*[,:：]?\s*)?(?:ตรวจ)?\s*(?:น้ำ\s*ro|water\s*ro|water|ro\s*water|น้ำอาร์โอ)\b", re.I)
_KEYS = {
    "hardness": r"(?:h|hard(?:ness)?|ความกระด้าง|กระด้าง)",
    "free": r"(?:f|fcl|free(?:\s*cl|\s*chlorine)?|คลอรีนอิสระ|อิสระ)",
    "total": r"(?:t|tcl|total(?:\s*cl|\s*chlorine)?|คลอรีนรวม|รวม)",
}
_VAL = r"\s*[:=]?\s*((?:>|มากกว่า)?\s*\d+(?:[.,]\d+)?)"


def parse_command(text):
    """'น้ำRO เช้า H 0 F 0.1 T 0' → dict or None when the text is not a water command.
    Returns {"hardness", "hardness_gt", "free", "total", "shift", "point", "date", "note"}."""
    if not text:
        return None
    m = _PREFIX.match(text)
    if not m:
        return None
    body = text[m.end():]
    out = {"hardness": None, "hardness_gt": False, "free": None, "total": None, "shift": None,
           "point": None, "date": None, "note": ""}
    rest = body
    for key, pat in _KEYS.items():
        mm = re.search(r"(?<![a-zA-Z])" + pat + _VAL, rest, re.I)
        if mm:
            v, gt = _num(mm.group(1))
            out[key] = v
            if key == "hardness":
                out["hardness_gt"] = gt
            rest = rest[:mm.start()] + " " + rest[mm.end():]
    low = rest.lower()
    for k, words in (("morning", ("เช้า", "morning")), ("afternoon", ("บ่าย", "afternoon")), ("evening", ("ค่ำ", "เย็น", "evening"))):
        if any(w in low for w in words):
            out["shift"] = k
            for w in words:
                rest = re.sub(w, " ", rest, flags=re.I)
            break
    pm = re.search(r"(หลัง\s*ro|หลัง\s*อาร์โอ|ก่อน\s*softener|หลัง\s*softener|หลัง\s*ถัง\s*คาร์บอน|หลังคาร์บอน|post\s*ro|post\s*carbon|pre\s*softener)", rest, re.I)
    if pm:
        out["point"] = pm.group(1).strip()
        rest = rest[:pm.start()] + " " + rest[pm.end():]
    dm = re.search(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", rest)
    if dm:
        d, mo, y = int(dm.group(1)), int(dm.group(2)), dm.group(3)
        yr = _thai_now().year
        if y:
            y = int(y)
            yr = y - 543 if y > 2400 else (y + 2000 if y < 100 else y)
            if yr > _thai_now().year + 1:
                yr -= 543
        try:
            out["date"] = date(yr, mo, d)
            rest = rest[:dm.start()] + " " + rest[dm.end():]
        except ValueError:
            pass
    out["note"] = re.sub(r"\s+", " ", rest).strip(" -:,")[:200]
    if out["hardness"] is None and out["free"] is None and out["total"] is None:
        return {"help": True}
    return out


def limits_from(get_setting):
    lim = {}
    for k, dv in DEFAULTS.items():
        v = get_setting(k, "")
        if v in ("", None):
            lim[k] = dv
        elif isinstance(dv, str):
            lim[k] = str(v)
        else:
            try:
                lim[k] = float(v)
            except ValueError:
                lim[k] = dv
    return lim


def evaluate(vals, lim=None):
    """→ {"status": pass|fail|partial, "fails": [..], "lines": [..]}"""
    lim = dict(DEFAULTS, **(lim or {}))
    fails, lines, missing = [], [], []
    h, hgt = vals.get("hardness"), vals.get("hardness_gt")
    if h is None:
        missing.append("Hardness")
    else:
        ok = (not hgt) and h <= float(lim["water_hardness_max"])
        lines.append(f"Hardness {'>' if hgt else ''}{h:g} ppm {'✅' if ok else '❌'} (เกณฑ์ ≤ {float(lim['water_hardness_max']):g})")
        if not ok:
            fails.append(f"Hardness {'>' if hgt else ''}{h:g} > {float(lim['water_hardness_max']):g} ppm — softener ไม่ทำงาน/เกลือหมด")
    f = vals.get("free")
    if f is None:
        missing.append("Free Cl")
    else:
        ok = f < float(lim["water_free_cl_max"])
        lines.append(f"Free Cl {f:g} {'✅' if ok else '❌'} (< {float(lim['water_free_cl_max']):g})")
        if not ok:
            fails.append(f"Free chlorine {f:g} ≥ {float(lim['water_free_cl_max']):g} ppm")
    t = vals.get("total")
    if t is None:
        missing.append("Total Cl")
    else:
        ok = t < float(lim["water_total_cl_max"])
        lines.append(f"Total Cl {t:g} {'✅' if ok else '❌'} (< {float(lim['water_total_cl_max']):g})")
        if not ok:
            fails.append(f"Total chlorine (คลอรามีน) {t:g} ≥ {float(lim['water_total_cl_max']):g} ppm — ถังคาร์บอนอิ่มตัว")
    if f is not None and t is not None and t + 0.001 < f:
        lines.append("⚠️ Total < Free — ตรวจสอบว่าป้ายขวดสลับกันหรือเก็บคนละจุด")
    status = "fail" if fails else ("partial" if missing else "pass")
    return {"status": status, "fails": fails, "lines": lines, "missing": missing}


def inbox_path(day, user_hash, msg_id, ext="jpg"):
    return f"{GCS_ROOT}/_inbox/{day.isoformat()}/{(user_hash or 'x')[:10]}_{msg_id}.{ext}"


def record_dir(day):
    return f"{GCS_ROOT}/{day.year}/{day.strftime('%Y-%m')}/{day.isoformat()}"


def drive_dir(day):
    return f"{DRIVE_ROOT}/{day.year}/{day.strftime('%Y-%m')}"


def add_inbox(conn, user_hash, msg_id, gcs_path):
    with conn.cursor() as cur:
        cur.execute(CREATE_SQL)
        cur.execute("""INSERT INTO hd_water_inbox (user_hash, msg_id, gcs_path) VALUES (%s,%s,%s)
                       ON CONFLICT (msg_id) DO NOTHING""", (user_hash, msg_id, gcs_path))
    conn.commit()


def save_record(conn, vals, ev, *, tester_name, tester_hash, raw_text, source="line_group",
                sample_point=None, check_date=None, shift=None, window_min=20):
    """Insert the row and claim the sender's recent inbox photos. Returns record dict."""
    now = _thai_now()
    day = check_date or vals.get("date") or now.date()
    sh = shift or vals.get("shift") or ("morning" if now.hour < 13 else ("afternoon" if now.hour < 17 else "evening"))
    with conn.cursor() as cur:
        cur.execute(CREATE_SQL)
        cur.execute("""INSERT INTO hd_water_daily (check_date, shift, sample_point, hardness_ppm, hardness_text, free_cl,
                       total_cl, status, fails, tester_name, tester_hash, source, raw_text, note)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (day, sh, sample_point or vals.get("point") or DEFAULTS["water_sample_point"],
                     vals.get("hardness"), (">" if vals.get("hardness_gt") else "") + (f"{vals['hardness']:g}" if vals.get("hardness") is not None else ""),
                     vals.get("free"), vals.get("total"), ev["status"], "; ".join(ev["fails"]) or None,
                     tester_name, tester_hash, source, raw_text, vals.get("note") or None))
        rid = cur.fetchone()[0]
        photos = []
        if tester_hash:
            cur.execute("""UPDATE hd_water_inbox SET record_id = %s
                           WHERE user_hash = %s AND record_id IS NULL AND received_at >= NOW() - (%s || ' minutes')::interval
                           RETURNING gcs_path""", (rid, tester_hash, str(window_min)))
            photos = [r[0] for r in cur.fetchall()]
    conn.commit()
    return {"id": rid, "date": day, "shift": sh, "photos": photos,
            "sample_point": sample_point or vals.get("point") or DEFAULTS["water_sample_point"]}


def archive(conn, storage, rec, vals, ev, drive_upload=None, tester_name="", extra_photos=None):
    """Copy inbox photos (+ photos uploaded directly from the web form) and a JSON
    sheet into the dated folder (GCS) and Drive. Best-effort."""
    bucket = getattr(storage, "_bucket", None)
    day, stored, drive_out = rec["date"], [], []
    stamp = _thai_now().strftime("%H%M")
    base = f"{day.isoformat()}_{SHIFT_TH.get(rec['shift'], rec['shift'])}_{stamp}"
    sheet = {"date": day.isoformat(), "shift": rec["shift"], "sample_point": rec["sample_point"],
             "hardness_ppm": vals.get("hardness"), "free_cl": vals.get("free"), "total_cl": vals.get("total"),
             "status": ev["status"], "fails": ev["fails"], "tester": tester_name, "note": vals.get("note"),
             "recorded_at": _thai_now().isoformat(timespec="minutes")}
    files = [(f"{base}.json", json.dumps(sheet, ensure_ascii=False, indent=1).encode("utf-8"), "application/json")]
    if bucket is not None:
        for i, p in enumerate(rec["photos"], 1):
            try:
                data = bucket.blob(p).download_as_bytes()
                files.append((f"{base}_{i}.jpg", data, "image/jpeg"))
            except Exception as e:
                logger.warning("water photo read %s: %s", p, e)
    for j, data in enumerate(extra_photos or [], len(rec["photos"]) + 1):
        if data:
            files.append((f"{base}_{j}.jpg", data, "image/jpeg"))
    for name, data, ctype in files:
        gpath = f"{record_dir(day)}/{name}"
        if bucket is not None:
            try:
                bucket.blob(gpath).upload_from_string(data, content_type=ctype)
                stored.append(gpath)
            except Exception as e:
                logger.warning("water archive gcs %s: %s", gpath, e)
        if drive_upload:
            try:
                drive_upload(f"{drive_dir(day)}/{name}", data, ctype)
                drive_out.append(f"{drive_dir(day)}/{name}")
            except Exception as e:
                logger.warning("water archive drive %s: %s", name, str(e)[:80])
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE hd_water_daily SET photos = %s, drive = %s WHERE id = %s",
                        (json.dumps(stored), json.dumps(drive_out), rec["id"]))
        conn.commit()
    except Exception as e:
        logger.warning("water archive update: %s", e)
    return stored, drive_out


def add_photos(conn, storage, rid, photo_blobs, drive_upload=None):
    """Append uploaded photos to an existing record (GCS dated folder + Drive). Originals are never deleted."""
    bucket = getattr(storage, "_bucket", None)
    with conn.cursor() as cur:
        cur.execute("SELECT check_date, shift, photos, drive FROM hd_water_daily WHERE id = %s AND deleted_at IS NULL", (rid,))
        r = cur.fetchone()
        if not r:
            return None
        day, shift, photos, drive = r[0], r[1], list(r[2] or []), list(r[3] or [])
        n0 = sum(1 for p in photos if p.endswith(".jpg"))
        stamp = _thai_now().strftime("%H%M%S")
        for j, data in enumerate(photo_blobs, 1):
            name = f"{day.isoformat()}_{SHIFT_TH.get(shift, shift)}_add{stamp}_{n0 + j}.jpg"
            gpath = f"{record_dir(day)}/{name}"
            if bucket is not None:
                try:
                    bucket.blob(gpath).upload_from_string(data, content_type="image/jpeg")
                    photos.append(gpath)
                except Exception as e:
                    logger.warning("water add photo gcs: %s", e)
            if drive_upload:
                try:
                    drive_upload(f"{drive_dir(day)}/{name}", data, "image/jpeg")
                    drive.append(f"{drive_dir(day)}/{name}")
                except Exception as e:
                    logger.warning("water add photo drive: %s", str(e)[:80])
        cur.execute("UPDATE hd_water_daily SET photos = %s, drive = %s WHERE id = %s", (json.dumps(photos), json.dumps(drive), rid))
    conn.commit()
    return photos


def remove_photo(conn, rid, idx):
    """Unlink photo #idx from the record (kept in removed_photos; the file itself stays in GCS/Drive)."""
    with conn.cursor() as cur:
        cur.execute("SELECT photos, removed_photos FROM hd_water_daily WHERE id = %s AND deleted_at IS NULL", (rid,))
        r = cur.fetchone()
        if not r:
            return None
        photos, removed = list(r[0] or []), list(r[1] or [])
        jpgs = [p for p in photos if p.endswith(".jpg")]
        if idx < 0 or idx >= len(jpgs):
            return False
        gone = jpgs[idx]
        photos.remove(gone)
        removed.append(gone)
        cur.execute("UPDATE hd_water_daily SET photos = %s, removed_photos = %s WHERE id = %s",
                    (json.dumps(photos), json.dumps(removed), rid))
    conn.commit()
    return True


def reply_text(rec, ev, tester_name="", n_photos=0, drive_ok=False):
    d = rec["date"]
    head = "✅ ผ่านทุกค่า" if ev["status"] == "pass" else ("❌ ไม่ผ่าน" if ev["status"] == "fail" else "⚠️ บันทึกบางค่า")
    L = [f"💧 ตรวจน้ำ RO {d.day}/{d.month}/{d.year + 543} เวร{SHIFT_TH.get(rec['shift'], rec['shift'])} — จุดเก็บ: {rec['sample_point']}"]
    L += ev["lines"]
    L.append(head)
    for f in ev["fails"]:
        L.append(f"  • {f}")
    if ev["status"] == "fail":
        L.append("ตามเกณฑ์: ตรวจซ้ำ/แก้ไขระบบน้ำก่อนเริ่มฟอก — แจ้งแพทย์แล้ว")
    if ev.get("missing"):
        L.append(f"ยังไม่ได้ระบุ: {', '.join(ev['missing'])} (พิมพ์เพิ่มได้ เช่น น้ำRO T 0)")
    tail = []
    if tester_name:
        tail.append(f"ผู้ตรวจ {tester_name}")
    tail.append(f"แนบรูป {n_photos} รูป" if n_photos else "ไม่มีรูปแนบ — ส่งรูปก่อนแล้วพิมพ์ค่าภายใน 20 นาที")
    if drive_ok:
        tail.append("เก็บ Google Drive แล้ว")
    L.append(" · ".join(tail))
    return "\n".join(L)


HELP = ("💧 บันทึกตรวจน้ำ RO: ส่งรูปขวดทดสอบในกลุ่มก่อน แล้วพิมพ์\n"
        "น้ำRO H 0 F 0.1 T 0\n"
        "H = Hardness (ppm จากแถบสี 0/10/25/50, >50 พิมพ์ H >50) · F = Free Cl · T = Total Cl\n"
        "เพิ่มได้: เวร (เช้า/บ่าย/ค่ำ), จุดเก็บ (หลังRO / ก่อนsoftener), วันที่ 12/11, หมายเหตุท้ายข้อความ")


def latest(cur, n=1):
    cur.execute("""SELECT check_date, shift, sample_point, hardness_text, free_cl, total_cl, status, fails, tester_name,
                          jsonb_array_length(COALESCE(photos,'[]'::jsonb)) FROM hd_water_daily
                   WHERE deleted_at IS NULL ORDER BY check_date DESC, id DESC LIMIT %s""", (n,))
    return cur.fetchall()


def status_line(cur, interval_days=7, today=None):
    """One line for Morning Brief / assistant: last check and whether it is due."""
    today = today or _thai_now().date()
    try:
        rows = latest(cur, 1)
    except Exception:
        return None
    if not rows:
        return "💧 ตรวจน้ำ RO (Hardness/Cl): ยังไม่เคยบันทึกในระบบ"
    d, sh, pt, h, f, t, st, fails, who, nph = rows[0]
    age = (today - d).days
    ico = "✅" if st == "pass" else ("❌" if st == "fail" else "⚠️")
    s = f"💧 ตรวจน้ำ RO ล่าสุด {d.day}/{d.month} ({age} วันก่อน) {ico} H {h or '-'} · F {f if f is not None else '-'} · T {t if t is not None else '-'}"
    if st == "fail":
        s += f"\n  ❌ {fails}"
    if age >= int(interval_days):
        s += f"\n  ⏰ ครบกำหนดตรวจแล้ว (ทุก {int(interval_days)} วัน)"
    return s
