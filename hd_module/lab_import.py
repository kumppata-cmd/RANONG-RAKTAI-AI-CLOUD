"""Bulk lab import from the clinic's Excel/CSV reports (14 Nov 2026).

First use case: the monthly "รายงานผลตรวจ Hematocrit" export (ลำดับ / ชื่อ-สกุล /
HN / Hct(%) / HB(g/dL) / วันที่ตรวจ) for all ~140 patients. The parser finds
the header row by itself, maps Thai/English column names to hd_lab_results
columns, and returns plain rows; the API matches them to patients and shows
a preview — nothing is written until a nurse/physician confirms.

Never modifies or deletes existing results: duplicates (same patient, same
date, same values) are skipped; a different value on the same date is
inserted as a new row (source = import:<file>) so history is kept.
"""
import io
import re
from datetime import date, datetime

# header text (lower, spaces removed) → hd_lab_results column
COLUMN_MAP = [
    (("hn", "เลขhn", "รหัสผู้ป่วย"), "hn"),
    (("ชื่อ-สกุล", "ชื่อ", "name", "patient"), "name"),
    (("วันที่ตรวจ", "วันที่", "date", "labdate"), "lab_date"),
    (("hb(g/dl)", "hb", "hemoglobin", "ฮีโมโกลบิน"), "hemoglobin"),
    (("hct(%)", "hct", "hematocrit", "ฮีมาโตคริต"), "hematocrit"),
    (("ferritin", "เฟอร์ริติน"), "ferritin"),
    (("tsat", "tsat(%)", "transferrinsaturation"), "tsat"),
    (("k", "k+", "potassium", "โพแทสเซียม"), "potassium"),
    (("na", "sodium"), "sodium"),
    (("ca", "calcium"), "calcium"),
    (("p", "po4", "phosphate", "phosphorus", "ฟอสเฟต"), "phosphate"),
    (("alb", "albumin", "อัลบูมิน"), "albumin"),
    (("ipth", "pth"), "ipth"),
    (("bun",), "bun"),
    (("cr", "creatinine"), "creatinine"),
    (("kt/v", "ktv", "spkt/v"), "kt_v"),
    (("urr", "urr(%)"), "urr"),
    (("hbsag", "hepatitisb"), "hepatitis_b"),
    (("anti-hcv", "antihcv", "hcv", "hepatitisc"), "hepatitis_c"),
]
NUMERIC = {"hemoglobin", "hematocrit", "ferritin", "tsat", "potassium", "sodium", "calcium", "phosphate",
           "albumin", "ipth", "bun", "creatinine", "kt_v", "urr"}
TEXT = {"hepatitis_b", "hepatitis_c"}
LAB_FIELDS = sorted(NUMERIC | TEXT)


def _norm(h):
    return re.sub(r"\s+", "", str(h or "")).lower()


def _map_header(cells):
    """→ {col_index: field} or None when the row is not a header (needs hn + ≥1 lab field)."""
    m = {}
    for i, c in enumerate(cells):
        n = _norm(c)
        if not n:
            continue
        for keys, field in COLUMN_MAP:
            if n in keys and field not in m.values():
                m[i] = field
                break
    if "hn" in m.values() and any(f in NUMERIC or f in TEXT for f in m.values()):
        return m
    return None


def parse_date(v, default=None):
    """Excel date / d/m/Y / Y-m-d / Thai Buddhist year → date (or default)."""
    if v is None or str(v).strip() == "":
        return default
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y"):
        try:
            d = datetime.strptime(s, fmt).date()
            if d.year > 2400:
                d = d.replace(year=d.year - 543)
            return d
        except ValueError:
            continue
    return default


def _num(v):
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s in ("", "-", "—", "N/A", "NA"):
        return None
    m = re.search(r"-?\d+(\.\d+)?", s)
    return float(m.group(0)) if m else None


def _title_date(rows):
    """Report title 'รายงานผลตรวจ … วันที่ 2 พฤศจิกายน พ.ศ. 2569 ถึง …' → date."""
    th = ["มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน", "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม"]
    for r in rows[:10]:
        for c in r:
            s = str(c or "")
            m = re.search(r"วันที่\s*(\d{1,2})\s+(\S+)\s+พ\.ศ\.\s*(\d{4})", s)
            if m and m.group(2) in th:
                y = int(m.group(3))
                return date(y - 543 if y > 2400 else y, th.index(m.group(2)) + 1, int(m.group(1)))
    return None


def parse_rows(raw_rows):
    """raw_rows: list of lists (any width). → {"rows": [...], "fields": [...], "default_date": iso|None, "errors": [...]}"""
    header, hdr_i = None, -1
    for i, r in enumerate(raw_rows[:40]):
        header = _map_header(r)
        if header:
            hdr_i = i
            break
    if not header:
        return {"rows": [], "fields": [], "default_date": None, "errors": ["ไม่พบแถวหัวตาราง (ต้องมีคอลัมน์ HN และผลแล็บอย่างน้อย 1 ค่า เช่น Hb/Hct)"]}
    default_date = _title_date(raw_rows)
    fields = [f for f in header.values() if f in NUMERIC or f in TEXT]
    out, errors = [], []
    for n, r in enumerate(raw_rows[hdr_i + 1:], start=hdr_i + 2):
        rec = {"row": n}
        for i, f in header.items():
            v = r[i] if i < len(r) else None
            if f == "hn":
                rec["hn"] = str(v or "").strip().upper()
            elif f == "name":
                rec["name"] = str(v or "").strip()
            elif f == "lab_date":
                rec["lab_date"] = parse_date(v, default_date)
            elif f in NUMERIC:
                rec[f] = _num(v)
            elif f in TEXT:
                rec[f] = (str(v).strip() or None) if v is not None else None
        if not rec.get("hn") or not re.search(r"\d", rec["hn"]):
            continue
        if not any(rec.get(f) is not None for f in fields):
            continue
        rec["lab_date"] = rec.get("lab_date") or default_date
        if not rec["lab_date"]:
            errors.append(f"แถว {n}: ไม่มีวันที่ตรวจ")
            continue
        rec["lab_date"] = rec["lab_date"].isoformat()
        out.append(rec)
    return {"rows": out, "fields": fields, "default_date": default_date.isoformat() if default_date else None, "errors": errors}


def read_file(filename, data):
    """xlsx / xls(x) via openpyxl, csv via csv module → raw rows."""
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        rows = []
        for ws in wb.worksheets:
            for r in ws.iter_rows(values_only=True):
                rows.append(list(r))
            if rows:
                break
        return rows
    if name.endswith(".csv") or name.endswith(".txt"):
        import csv
        txt = data.decode("utf-8-sig", errors="replace")
        return [row for row in csv.reader(io.StringIO(txt))]
    raise ValueError("รองรับเฉพาะ .xlsx หรือ .csv")


def match_patients(cur, rows):
    """Attach patient_id / registered name / duplicate flag. Returns (matched, unmatched)."""
    hns = sorted({r["hn"] for r in rows})
    if not hns:
        return [], []
    cur.execute("SELECT id, UPPER(hn), patient_name FROM hd_patient_mapping WHERE UPPER(hn) = ANY(%s)", (hns,))
    pmap = {hn: (pid, name) for pid, hn, name in cur.fetchall()}
    pids = [v[0] for v in pmap.values()]
    existing = {}
    if pids:
        dates = sorted({r["lab_date"] for r in rows})
        cur.execute("""SELECT patient_id, lab_date, hemoglobin, hematocrit, ferritin, tsat FROM hd_lab_results
                       WHERE patient_id = ANY(%s) AND lab_date = ANY(%s::date[])""", (pids, dates))
        for pid, d, hb, hct, fer, ts in cur.fetchall():
            existing.setdefault((pid, d.isoformat()), []).append({"hemoglobin": hb, "hematocrit": hct, "ferritin": fer, "tsat": ts})
    matched, unmatched = [], []
    for r in rows:
        p = pmap.get(r["hn"])
        if not p:
            unmatched.append(r)
            continue
        r = dict(r, patient_id=p[0], registered_name=p[1])
        dup = False
        for ex in existing.get((p[0], r["lab_date"]), []):
            if all((ex.get(f) is None and r.get(f) is None) or (ex.get(f) is not None and r.get(f) is not None and abs(float(ex[f]) - float(r[f])) < 0.05)
                   for f in ("hemoglobin", "hematocrit", "ferritin", "tsat") if f in r or ex.get(f) is not None):
                dup = True
                break
        r["duplicate"] = dup
        matched.append(r)
    return matched, unmatched


def insert_rows(cur, rows, source):
    """Insert confirmed rows (skips duplicate=True). → {"inserted": n, "skipped": n}"""
    ins = skip = 0
    for r in rows:
        if r.get("duplicate"):
            skip += 1
            continue
        cols = [f for f in LAB_FIELDS if r.get(f) is not None]
        if not cols:
            skip += 1
            continue
        vals = [r[f] for f in cols]
        cur.execute(f"""INSERT INTO hd_lab_results (patient_id, lab_date, {", ".join(cols)}, source)
                        VALUES (%s, %s, {", ".join(["%s"] * len(cols))}, %s)""",
                    [r["patient_id"], r["lab_date"]] + vals + [source[:50]])
        ins += 1
    return {"inserted": ins, "skipped": skip}


def summary(rows):
    """Quick anaemia distribution for the confirmation screen (Hb g/dL)."""
    hb = [r["hemoglobin"] for r in rows if r.get("hemoglobin") is not None]
    if not hb:
        return {}
    return {"n": len(hb), "mean": round(sum(hb) / len(hb), 2),
            "lt8": sum(1 for v in hb if v < 8), "lt10": sum(1 for v in hb if v < 10),
            "in_range": sum(1 for v in hb if 10 <= v <= 12), "gt12": sum(1 for v in hb if v > 12)}
