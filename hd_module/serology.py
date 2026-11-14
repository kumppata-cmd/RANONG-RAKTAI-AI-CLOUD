"""Hepatitis serology (HBsAg / Anti-HCV) — one source of truth for every screen. (14 Nov 2026)

Results live in hd_lab_results (hepatitis_b / hepatitis_c, 'positive' | 'negative') and,
for results that came in through the LINE lab sync, in hd_lab_docs (hbsag / antihcv).
`latest_for()` merges both, newest first per field, so the patient list, bed cards, TV,
Patient 360, session page, weekly grid and the bed scheduler all read the same status.

`parse_import()` + `apply()` load a CSV/XLSX of HN · name · HBsAg · Anti-HCV · date. Rows
are matched by clinic HN first, then the name must agree (first-name token) — a mismatch is
reported and never written. Writing never alters earlier rows (append-only lab history);
a row whose values equal the patient's latest status is skipped as a duplicate.

Display convention (user request 14 Nov 2026): positives in **bold red** everywhere.
"""
import io
import re
from datetime import date, datetime

POSITIVE = ("pos", "react", "+", "บวก")
NEGATIVE = ("neg", "non", "-", "ลบ")
EXACT = {"p": "positive", "n": "negative", "0": "negative", "1": "positive", "nr": "negative", "r": "positive"}
LABEL = {"hbsag": "HBsAg", "antihcv": "Anti-HCV"}


def normalize(v):
    """'Positive' / 'reactive' / '+' / 'บวก' → 'positive'; 'Neg' / 'non-reactive' / '-' / 'ลบ' → 'negative'; else None."""
    s = str(v or "").strip().lower()
    if not s:
        return None
    if s in EXACT:
        return EXACT[s]
    if s.startswith(POSITIVE):
        return "positive"
    if s.startswith(NEGATIVE):
        return "negative"
    return None


def is_pos(v):
    return normalize(v) == "positive"


def latest_for(cur, patient_ids):
    """→ {patient_id: {"hbsag": 'positive'|'negative'|None, "antihcv": ..., "hbsag_date": iso|None,
    "antihcv_date": iso|None, "any_positive": bool}} for the given patients (missing → not in dict)."""
    ids = [int(i) for i in patient_ids if i is not None]
    out = {}
    if not ids:
        return out

    def take(pid, field, val, d):
        v = normalize(val)
        if v is None:
            return
        rec = out.setdefault(pid, {"hbsag": None, "antihcv": None, "hbsag_date": None, "antihcv_date": None})
        cur_d = rec[field + "_date"]
        d_iso = d.isoformat() if hasattr(d, "isoformat") else (str(d) if d else None)
        if rec[field] is None or (d_iso or "") > (cur_d or ""):
            rec[field], rec[field + "_date"] = v, d_iso

    queries = (
        ("""SELECT patient_id, hepatitis_b, hepatitis_c, lab_date FROM hd_lab_results
            WHERE patient_id = ANY(%s) AND (hepatitis_b IS NOT NULL OR hepatitis_c IS NOT NULL)
            ORDER BY lab_date DESC, id DESC""", "sero1"),
        ("""SELECT patient_id, hbsag, antihcv, COALESCE(specimen_date, doc_date) FROM hd_lab_docs
            WHERE patient_id = ANY(%s) AND (hbsag IS NOT NULL OR antihcv IS NOT NULL)
            ORDER BY COALESCE(specimen_date, doc_date) DESC NULLS LAST, id DESC""", "sero2"),
    )
    for sql, sp in queries:
        try:
            cur.execute(f"SAVEPOINT {sp}")
            cur.execute(sql, (ids,))
            for pid, b, c, d in cur.fetchall():
                take(pid, "hbsag", b, d)
                take(pid, "antihcv", c, d)
            cur.execute(f"RELEASE SAVEPOINT {sp}")
        except Exception:
            try:
                cur.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            except Exception:
                pass
    for rec in out.values():
        rec["any_positive"] = rec["hbsag"] == "positive" or rec["antihcv"] == "positive"
    return out


def text(rec):
    """Short Thai/English status line: 'HBsAg+ · Anti-HCV−' (positives first)."""
    if not rec:
        return ""
    parts = []
    for f in ("hbsag", "antihcv"):
        v = rec.get(f)
        if v == "positive":
            parts.append(LABEL[f] + "+")
    for f in ("hbsag", "antihcv"):
        if rec.get(f) == "negative":
            parts.append(LABEL[f] + "−")
    return " · ".join(parts)


# ─────────────────────────────── import ───────────────────────────────
HEADERS = {"hn": "hn", "hnclinic": "hn", "hnคลินิก": "hn", "ชื่อ-สกุล": "name", "ชื่อ": "name", "name": "name",
           "hbsag": "hbsag", "hbs": "hbsag", "hepatitisb": "hbsag", "ไวรัสตับอักเสบบี": "hbsag",
           "antihcv": "antihcv", "anti-hcv": "antihcv", "hcv": "antihcv", "hepatitisc": "antihcv", "ไวรัสตับอักเสบซี": "antihcv",
           "date": "date", "วันที่": "date", "วันที่ตรวจ": "date", "labdate": "date"}


def _norm_h(h):
    return re.sub(r"\s+", "", str(h or "")).lower()


def _parse_date(v, default):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v or "").strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            d = datetime.strptime(s, fmt).date()
            return d.replace(year=d.year - 543) if d.year > 2400 else d
        except ValueError:
            continue
    return default


def _rows_from_file(filename, data):
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        return [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
    import csv
    return [row for row in csv.reader(io.StringIO(data.decode("utf-8-sig", errors="replace")))]


def parse_import(filename, data, default_date=None):
    """→ (rows, errors). Row: {row, hn, name, hbsag, antihcv, date(iso)}. Needs HN + at least one result column."""
    raw = _rows_from_file(filename, data)
    hdr, start = None, 0
    for i, r in enumerate(raw[:15]):
        m = {ci: HEADERS[_norm_h(h)] for ci, h in enumerate(r) if _norm_h(h) in HEADERS}
        if "hn" in m.values() and ("hbsag" in m.values() or "antihcv" in m.values()):
            hdr, start = m, i + 1
            break
    if not hdr:
        return [], ["ไม่พบหัวตาราง (ต้องมีคอลัมน์ HN และ HBsAg หรือ Anti-HCV)"]
    default_date = default_date or date.today()
    out, errors = [], []
    for n, r in enumerate(raw[start:], start=start + 1):
        rec = {f: (r[ci] if ci < len(r) else None) for ci, f in hdr.items()}
        hn = str(rec.get("hn") or "").strip().upper()
        if not hn or not re.search(r"\d", hn):
            continue
        b, c = normalize(rec.get("hbsag")), normalize(rec.get("antihcv"))
        raw_b, raw_c = str(rec.get("hbsag") or "").strip(), str(rec.get("antihcv") or "").strip()
        if raw_b and b is None:
            errors.append(f"แถว {n} HN {hn}: อ่านค่า HBsAg '{raw_b}' ไม่ได้ (ใช้ positive/negative)")
        if raw_c and c is None:
            errors.append(f"แถว {n} HN {hn}: อ่านค่า Anti-HCV '{raw_c}' ไม่ได้")
        if b is None and c is None:
            continue
        out.append({"row": n, "hn": hn, "name": str(rec.get("name") or "").strip(), "hbsag": b, "antihcv": c,
                    "date": _parse_date(rec.get("date"), default_date).isoformat()})
    return out, errors


def _first_token(name):
    s = re.sub(r"^(นาย|นางสาว|นาง|น\.ส\.|ด\.ช\.|ด\.ญ\.|พระ|Mr\.?|Mrs\.?|Ms\.?)\s*", "", (name or "").strip(), flags=re.I)
    s = re.sub(r"[^\w฀-๿ ]", "", s)
    return (s.split() or [""])[0].lower()


def apply(cur, rows, confirm=False, source="import:serology"):
    """Match rows to patients (HN, then name check), compare with the latest status, insert when confirmed.
    → {"matched": [...], "unmatched": [...], "name_mismatch": [...], "written": n, "skipped_same": n,
       "pos_hbsag": n, "pos_antihcv": n}"""
    hns = sorted({r["hn"] for r in rows})
    cur.execute("SELECT id, UPPER(hn), patient_name FROM hd_patient_mapping WHERE UPPER(hn) = ANY(%s)", (hns,))
    pmap = {hn: (pid, nm) for pid, hn, nm in cur.fetchall()}
    latest = latest_for(cur, [v[0] for v in pmap.values()])
    matched, unmatched, mismatch = [], [], []
    for r in rows:
        p = pmap.get(r["hn"])
        if not p:
            unmatched.append(r)
            continue
        pid, reg_name = p
        if r["name"] and _first_token(r["name"]) and _first_token(r["name"]) != _first_token(reg_name):
            mismatch.append(dict(r, registered_name=reg_name))
            continue
        cur_s = latest.get(pid, {})
        same = all(r[f] is None or cur_s.get(f) == r[f] for f in ("hbsag", "antihcv"))
        change = {f: [cur_s.get(f), r[f]] for f in ("hbsag", "antihcv") if r[f] is not None and cur_s.get(f) != r[f]}
        matched.append(dict(r, patient_id=pid, registered_name=reg_name, duplicate=same, changes=change))
    written = skipped = 0
    if confirm:
        for m in matched:
            if m["duplicate"]:
                skipped += 1
                continue
            cur.execute("""INSERT INTO hd_lab_results (patient_id, lab_date, hepatitis_b, hepatitis_c, source, notes)
                           VALUES (%s, %s, %s, %s, %s, %s)""",
                        (m["patient_id"], m["date"], m["hbsag"], m["antihcv"], source[:50],
                         "ผล HBsAg/Anti-HCV จากทะเบียนหน่วย (นำเข้า)"))
            written += 1
    return {"matched": matched, "unmatched": unmatched, "name_mismatch": mismatch, "written": written,
            "skipped_same": skipped,
            "pos_hbsag": sum(1 for m in matched if m["hbsag"] == "positive"),
            "pos_antihcv": sum(1 for m in matched if m["antihcv"] == "positive"),
            "to_write": sum(1 for m in matched if not m["duplicate"])}
