"""💬 ถามชาร์ตได้ (Ask-the-Chart) — 28 พ.ย. 2569.

The physician types a plain-Thai question in LINE ("ใครบ้าง Hb ต่ำกว่า 9 ที่ยังไม่ปรับ ESA",
"เดือนนี้ IDH เกิดกับใครบ่อยสุด", "เครื่อง 5 TMP สูงกี่ครั้ง") → AI writes ONE
read-only SELECT against an allow-listed schema → we validate it, run it in a
READ ONLY transaction with a statement timeout → AI (or a plain table) turns
the rows into a short Thai answer.

Safety
- Allow-list of tables/columns (anti-HIV is NOT in it — never readable here).
- Rejects anything that is not a single SELECT/WITH; forbidden keywords;
  unknown tables; forces LIMIT.
- `SET TRANSACTION READ ONLY` + `statement_timeout` — even a bad SQL cannot
  write or hang the DB.
- Switch ⚙️ ask_chart (default off). Physician/admin only (LINE 1:1 or the web
  page). Nurse group only when ask_chart_group is also on.
- Every question/SQL/outcome logged in hd_ask_chart_log.
"""
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from . import ai_text

logger = logging.getLogger(__name__)

SETTING_KEY = "ask_chart"
GROUP_KEY = "ask_chart_group"
MODEL_KEY = "ask_chart_model"
MAX_ROWS = 100
TIMEOUT_MS = 8000

SCHEMA = {
    "hd_patient_mapping": ("ผู้ป่วย 1 แถว/คน", ["id", "hn", "patient_name", "dry_weight", "vascular_access", "schedule_days",
                                                "esa_drug", "esa_dose", "is_active", "date_of_birth", "gender", "allergies",
                                                "hd_per_week", "default_shift", "hospital_hn"]),
    "hd_labs": ("ผลแล็บ (view รวมทุกแหล่ง) 1 แถว/ใบ · join patient_id = hd_patient_mapping.id",
                ["patient_id", "lab_date", "hb", "hct", "k", "na", "ca", "phos", "albumin", "pth", "ferritin", "tsat",
                 "ktv", "urr", "bun", "creatinine", "hco3", "plt", "wbc", "glucose", "hbsag", "antihcv", "antihbs",
                 "verified_status", "source"]),
    "hd_sessions": ("รอบฟอก 1 แถว/รอบ (status: scheduled|in_progress|completed|cancelled)",
                    ["id", "patient_id", "bed_number", "session_date", "shift", "status", "pre_weight", "post_weight",
                     "uf_goal", "actual_uf", "kt_v", "urr", "hb_value", "esa_drug_given", "esa_dose_given",
                     "blood_flow_rate", "dialyzer_type", "started_at", "ended_at", "equipment_id", "access_type"]),
    "hd_flowsheet": ("ค่าระหว่างฟอกทุก 30–60 นาที (จากพยาบาล/Magic Camera) · join session_id",
                     ["session_id", "slot_time", "recorded_at", "sbp", "dbp", "pr", "bfr", "vp", "ap", "tmp", "ufr", "total_uf", "conductivity"]),
    "hd_vital_signs": ("vital signs pre/intra/post · join session_id", ["session_id", "phase", "recorded_at", "sbp", "dbp", "heart_rate", "spo2", "symptoms"]),
    "hd_medication_orders": ("รายการยา (is_active=TRUE = ยาปัจจุบัน; verify_status ai_draft|pending|confirmed)",
                             ["id", "patient_id", "drug_name", "dose", "route", "frequency", "is_active", "ordered_at", "verify_status", "source", "drug_category"]),
    "hd_clinical_alerts": ("แจ้งเตือนทางคลินิก (severity critical|warning|info)",
                           ["id", "session_id", "patient_id", "alert_type", "severity", "message", "created_at", "acknowledged", "physician_order"]),
    "hd_idh_twin_state": ("คะแนนเสี่ยง IDH ต่อรอบ + ผลจริง (outcome idh|no_idh|unknown)",
                          ["session_id", "patient_id", "last_score", "last_level", "peak_score", "outcome", "updated_at"]),
    "hd_esa_proposals": ("ข้อเสนอปรับ ESA (status pending|approved|rejected)", ["patient_id", "hn", "hb", "hb_date", "action", "current_dose", "proposed_dose", "status", "created_at"]),
    "hd_shadow_findings": ("ประเด็นจากหมอเงา", ["patient_id", "hn", "rule", "severity", "text", "status", "first_seen", "last_seen"]),
    "hd_fluid_assessments": ("ผล BCM", ["patient_id", "hn", "assessed_at", "oh_pre_l", "rel_oh_pct", "lti", "fti"]),
    "hd_water_daily": ("ตรวจน้ำ RO รายวัน", ["check_date", "shift", "hardness_ppm", "free_cl", "total_cl", "status"]),
    "hd_equipment": ("เครื่องไตเทียม/อุปกรณ์", ["id", "equipment_type", "brand", "model", "serial_number", "bed_number", "status", "last_pm_date", "next_pm_date"]),
}
ALLOWED_TABLES = set(SCHEMA)
FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|grant|revoke|truncate|copy|vacuum|analyze|lock|"
                       r"pg_sleep|pg_read|pg_ls|information_schema|pg_catalog|dblink|antihiv|hiv|current_setting|set_config|"
                       r"lo_import|lo_export|execute|prepare|do)\b", re.I)
TABLE_RE = re.compile(r"\b(?:from|join)\s+([a-zA-Z_][\w.]*)", re.I)


def _thai_today():
    return (datetime.now(timezone.utc) + timedelta(hours=7)).date()


def is_enabled(get_setting=None):
    try:
        if get_setting is None:
            from .blueprint import get_setting_value as get_setting
        return str(get_setting(SETTING_KEY, "off") or "off").strip().lower() in ("on", "1", "true", "yes")
    except Exception:
        return False


def schema_card():
    return "\n".join(f"- {t} ({d}): {', '.join(c)}" for t, (d, c) in SCHEMA.items())


def build_sql_prompt(question, today=None):
    today = today or _thai_today()
    return (
        "คุณคือผู้ช่วยเขียน SQL (PostgreSQL) สำหรับหน่วยไตเทียม ตอบเป็น JSON เท่านั้น: "
        '{"sql": "...", "explain": "อธิบายสั้น ๆ ภาษาไทยว่าค้นอะไร", "note": "ข้อจำกัด/สมมติฐาน (ถ้ามี)"}\n'
        "กติกา: (1) SELECT เดียว (ใช้ WITH ได้) ห้าม DML/DDL ห้าม ; (2) ใช้เฉพาะตาราง/คอลัมน์ในรายการนี้เท่านั้น "
        "(3) ใส่ LIMIT ไม่เกิน 100 เสมอ (4) ชื่อผู้ป่วยใช้ ILIKE '%ชื่อ%' (5) join ผู้ป่วยด้วย patient_id = hd_patient_mapping.id "
        "และแสดง hn, patient_name เสมอเมื่อคำตอบเป็นรายชื่อ (6) 'ล่าสุด' = DISTINCT ON (patient_id) ORDER BY patient_id, lab_date DESC "
        "(7) ผู้ป่วยปัจจุบัน = is_active = TRUE (8) 'เดือนนี้/สัปดาห์นี้' อิงวันนี้ (9) ถ้าคำถามตอบจากตารางเหล่านี้ไม่ได้ "
        'ให้ตอบ {"sql": null, "explain": "...เหตุผล..."}\n'
        f"วันนี้: {today.isoformat()} (เวลาไทย)\n\nตาราง:\n{schema_card()}\n\nคำถาม: {question}"
    )


def validate_sql(sql):
    """→ (clean_sql, None) หรือ (None, reason)."""
    if not sql or not isinstance(sql, str):
        return None, "ไม่มี SQL"
    s = sql.strip().rstrip(";").strip()
    if ";" in s:
        return None, "มีหลายคำสั่ง"
    if "--" in s or "/*" in s:
        return None, "มี comment"
    head = s.lstrip("( \n\t").lower()
    if not (head.startswith("select") or head.startswith("with")):
        return None, "ไม่ใช่ SELECT"
    if FORBIDDEN.search(s):
        return None, "มีคำสั่งต้องห้าม"
    ctes = set(m.group(1).lower() for m in re.finditer(r"(?:with|,)\s*([a-zA-Z_]\w*)\s+as\s*\(", s, re.I))
    for m in TABLE_RE.finditer(s):
        t = m.group(1).lower().split(".")[-1]
        if t in ("(", "lateral", "generate_series", "unnest"):
            continue
        if t not in ALLOWED_TABLES and t not in ctes:
            return None, f"ตาราง {t} ไม่อยู่ในรายการที่อนุญาต"
    m = re.search(r"\blimit\s+(\d+)", s, re.I)
    if not m:
        s = f"{s}\nLIMIT {MAX_ROWS}"
    elif int(m.group(1)) > MAX_ROWS:
        s = re.sub(r"\blimit\s+\d+", f"LIMIT {MAX_ROWS}", s, flags=re.I)
    return s, None


def run_sql(conn, sql):
    """READ ONLY + timeout. → (columns, rows, ms) ; raises on SQL error (caller catches)."""
    t0 = time.time()
    try:
        conn.rollback()          # ปิด transaction ค้าง (ถ้ามี) ก่อนเปิดอันใหม่แบบ READ ONLY
    except Exception:
        pass
    with conn.cursor() as cur:
        cur.execute("BEGIN")
        try:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(f"SET LOCAL statement_timeout = {int(TIMEOUT_MS)}")
            cur.execute(sql)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(MAX_ROWS)
        finally:
            cur.execute("ROLLBACK")
    return cols, [list(r) for r in rows], int((time.time() - t0) * 1000)


def _cell(v):
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.2f}".rstrip("0").rstrip(".")
    if hasattr(v, "isoformat"):
        try:
            return v.strftime("%d/%m/%y") if not hasattr(v, "hour") else v.strftime("%d/%m %H:%M")
        except Exception:
            return str(v)
    return str(v)


def rows_as_text(cols, rows, max_rows=40):
    if not rows:
        return "(ไม่มีแถว)"
    lines = []
    for r in rows[:max_rows]:
        lines.append(" · ".join(f"{c}={_cell(v)}" if len(cols) > 2 else _cell(v) for c, v in zip(cols, r)))
    if len(rows) > max_rows:
        lines.append(f"…อีก {len(rows) - max_rows} แถว")
    return "\n".join(lines)


def build_answer_prompt(question, explain, cols, rows):
    data = {"columns": cols, "rows": [[_cell(v) for v in r] for r in rows[:60]], "total_rows": len(rows)}
    return ("คุณคือผู้ช่วยแพทย์โรคไต สรุปผลค้นหาจากฐานข้อมูลหน่วยไตเทียมเป็นภาษาไทยสำหรับอ่านใน LINE: "
            "บรรทัดแรก = คำตอบตรง ๆ 1 ประโยค (จำนวน/ใคร/เท่าไร) ตามด้วยรายการสำคัญไม่เกิน 15 บรรทัด (ชื่อ HN ค่า วันที่) "
            "ห้ามแต่งข้อมูลที่ไม่มีในแถว ห้ามแนะนำการรักษา ถ้าไม่มีแถวให้บอกว่าไม่พบ ความยาวรวม ≤ 1200 ตัวอักษร ไม่ใช้ markdown\n\n"
            f"คำถาม: {question}\nสิ่งที่ค้น: {explain}\nข้อมูล(JSON): {json.dumps(data, ensure_ascii=False)}")


LOG_SQL = """CREATE TABLE IF NOT EXISTS hd_ask_chart_log (
    id SERIAL PRIMARY KEY, who VARCHAR(80), channel VARCHAR(20), question TEXT, sql TEXT, explain TEXT,
    n_rows INTEGER, ms INTEGER, ok BOOLEAN, error TEXT, created_at TIMESTAMPTZ DEFAULT NOW())"""


def _log(conn, who, channel, question, sql, explain, n_rows, ms, ok, error):
    try:
        with conn.cursor() as cur:
            cur.execute(LOG_SQL)
            cur.execute("INSERT INTO hd_ask_chart_log (who, channel, question, sql, explain, n_rows, ms, ok, error) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        ((who or "")[:80], channel, (question or "")[:1000], sql, (explain or "")[:500], n_rows, ms, ok, (error or "")[:300]))
        conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.info("ask_chart log: %s", type(e).__name__)
        try:
            conn.rollback()
        except Exception:
            pass


def answer(conn, question, who="", channel="line", post_fn=None, model=None, today=None, base_url=""):
    """Full pipeline → dict {text, sql, columns, rows, error}. Never raises."""
    q = (question or "").strip()
    if len(q) < 3:
        return {"text": "พิมพ์คำถามต่อท้ายได้เลย เช่น @รักษ์ไต ใครบ้าง Hb ต่ำกว่า 9 ที่ยังไม่ปรับ ESA", "error": "short"}
    if len(q) > 400:
        q = q[:400]
    model = model or ai_text.model_id(MODEL_KEY)
    raw, err = ai_text.ask(build_sql_prompt(q, today), model=model, max_tokens=700, timeout=40, post_fn=post_fn, temperature=0)
    if err:
        _log(conn, who, channel, q, None, None, None, None, False, err)
        hint = {"no_api_key": "ยังไม่ได้ตั้งค่า AI (no_api_key)", "no_credit": "เครดิต AI หมด — แจ้งแอดมินเติมเครดิต",
                "api:404": f"ไม่รู้จักรุ่น AI '{model}' — ล้างช่อง ask_chart_model ใน ⚙️ ตั้งค่า แล้วลองใหม่",
                "api:401": "API key ไม่ถูกต้อง (401)", "api:429": "AI ถูกจำกัดอัตรา (429) รอ 1 นาทีแล้วถามใหม่"}.get(err)
        return {"text": hint or f"AI ไม่ตอบในรอบนี้ ({err}) — ลองถามใหม่อีกครั้ง หรือดูประวัติที่ {base_url or ''}/hd/ask", "error": err}
    js = ai_text.extract_json(raw) or {}
    sql, explain = js.get("sql"), js.get("explain") or ""
    if not sql:
        _log(conn, who, channel, q, None, explain, None, None, False, "no_sql")
        return {"text": "ตอบจากข้อมูลในระบบไม่ได้: " + (explain or "คำถามอยู่นอกตารางที่มี"), "error": "no_sql"}
    clean, why = validate_sql(sql)
    if not clean:
        _log(conn, who, channel, q, sql, explain, None, None, False, "reject:" + why)
        return {"text": f"ระบบไม่ให้รันคำสั่งนี้ ({why}) — ลองถามอีกแบบ", "error": "reject", "sql": sql}
    try:
        cols, rows, ms = run_sql(conn, clean)
    except Exception as e:  # noqa: BLE001
        emsg = str(e).split("\n")[0][:160]
        try:
            conn.rollback()
        except Exception:
            pass
        _log(conn, who, channel, q, clean, explain, None, None, False, "sql:" + emsg)
        return {"text": "ค้นไม่สำเร็จ (SQL ผิด) — ลองถามอีกแบบ หรือระบุชื่อตาราง/ค่าให้ชัดขึ้น", "error": "sql", "sql": clean, "detail": emsg}
    table = rows_as_text(cols, rows)
    text = None
    if rows:
        s2, e2 = ai_text.ask(build_answer_prompt(q, explain, cols, rows), model=model, max_tokens=900, timeout=40, post_fn=post_fn, temperature=0)
        if s2 and not e2:
            text = s2.strip()
    if not text:
        text = (f"🔎 {explain}\n{len(rows)} แถว\n" + table) if rows else f"🔎 {explain}\nไม่พบรายการที่ตรงเงื่อนไข"
    foot = f"\n\n({len(rows)} แถว · {ms} ms · ข้อมูลในระบบ ไม่ใช่คำสั่งรักษา)"
    if base_url and channel == "line":
        foot += f"\nดู SQL/ประวัติ: {base_url}/hd/ask"
    _log(conn, who, channel, q, clean, explain, len(rows), ms, True, None)
    return {"text": (text + foot)[:4800], "sql": clean, "explain": explain, "columns": cols,
            "rows": [[_cell(v) for v in r] for r in rows], "ms": ms, "error": None}


def line_entry(question, who="physician", channel="line", base_url=""):
    """Open own DB connection (used from server.py LINE hook). Returns reply text."""
    import os
    if not is_enabled():
        return None
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return "ฐานข้อมูลไม่พร้อม"
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
    except Exception as e:  # noqa: BLE001
        return f"เชื่อมต่อฐานข้อมูลไม่ได้ ({type(e).__name__})"
    try:
        return answer(conn, question, who=who, channel=channel, base_url=base_url).get("text")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def history(conn, limit=50):
    with conn.cursor() as cur:
        cur.execute(LOG_SQL)
        conn.commit()
        cur.execute("SELECT id, who, channel, question, sql, explain, n_rows, ms, ok, error, created_at FROM hd_ask_chart_log ORDER BY id DESC LIMIT %s", (limit,))
        return [{"id": r[0], "who": r[1], "channel": r[2], "question": r[3], "sql": r[4], "explain": r[5], "n_rows": r[6],
                 "ms": r[7], "ok": r[8], "error": r[9], "at": r[10].isoformat() if r[10] else None} for r in cur.fetchall()]
