"""Import the clinic's annual patient roster (Excel) into the HD module.

Source: "รายชื่อผู้ป่วยประจำปี" spreadsheet kept by the nursing team. Since
14 Nov 2026 the columns are read **by header name** (the 2569 edition added
Hematocrit, จำนวนการฉีด, วันฟอก, รอบการฟอก, iPTH/Ferritin per month), with the
2568 fixed-position layout as fallback.

Recognised headers (any order; others ignored — the ID/เลขบัตร column is
NEVER read or stored):
    HN clinic · HN รพ · ชื่อ-สกุล · ยาฉีด · จำนวนการฉีด · Hematocrit · วันฟอก ·
    รอบการฟอก · จำนวน HD/wk · Vascular Access · BFR/min · iPTH <เดือน><ปี> ·
    Ferritin <เดือน><ปี> · Hb <เดือน><ปี> (any lab name in lab_import.COLUMN_MAP
    followed by a Thai month + 2-digit พ.ศ.)

What gets written (only when the file has a value that differs):
    hd_patient_mapping  vascular_access, bfr, hd_per_week, schedule_days,
                        esa_drug, esa_dose, esa_per_week, default_shift
    hd_prescriptions    active prescription: blood_flow_rate, frequency_per_week,
                        access_type — created if the patient has none (D3)
    hd_lab_results      Hct (date given by caller, default = today) and the
                        month-coded labs (1st of that month, notes say "เดือน …")
Patient names are NOT overwritten — the master registry owns identity.

Two-phase: dry_run (preview) → confirm (write).
"""
import io
import logging
import re
from datetime import date

logger = logging.getLogger(__name__)

VA_MAP = {"AVF": "AVF", "AVG": "AVG", "PC": "Perm Cath", "PERM": "Perm Cath", "PERMCATH": "Perm Cath",
          "DLC": "DLC", "CVC": "CVC"}
ESA_PREFIX = {"E": "Epiao", "H": "Hema-plus", "HM": "Hemax", "HYPERCRIT": "Hypercrit", "EPOSIS": "Eposis"}
SHIFT_MAP = {"เช้า": "morning", "1": "morning", "บ่าย": "afternoon", "2": "afternoon",
             "ค่ำ": "evening", "เย็น": "evening", "3": "evening", "ดึก": "evening"}
TH_MONTH = {"มค": 1, "ม.ค": 1, "กพ": 2, "ก.พ": 2, "มีค": 3, "มี.ค": 3, "เมย": 4, "เม.ย": 4, "พค": 5, "พ.ค": 5,
            "มิย": 6, "มิ.ย": 6, "กค": 7, "ก.ค": 7, "สค": 8, "ส.ค": 8, "กย": 9, "ก.ย": 9, "ตค": 10, "ต.ค": 10,
            "พย": 11, "พ.ย": 11, "ธค": 12, "ธ.ค": 12}
TH_MONTH_NAME = ["", "ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]

HEADERS = {  # normalised header → field
    "hnclinic": "hn_clinic", "hn": "hn_clinic", "hnรพ": "hn_hospital", "hnโรงพยาบาล": "hn_hospital",
    "ชื่อ-สกุล": "name", "ชื่อสกุล": "name", "ชื่อ": "name",
    "ยาฉีด": "esa_code", "จำนวนการฉีด": "esa_regimen", "hematocrit": "hct", "hct": "hct", "hct(%)": "hct",
    "วันฟอก": "schedule_days", "รอบการฟอก": "shift", "รอบฟอก": "shift",
    "จำนวนhd/wk": "hd_per_week", "hd/wk": "hd_per_week", "vascularaccess": "vascular_access", "va": "vascular_access",
    "bfr/min": "bfr", "bfr": "bfr",
    "dryweight": "dry_weight", "dw": "dry_weight", "น้ำหนักแห้ง": "dry_weight",
}
LAB_WORDS = {"ipth": "ipth", "pth": "ipth", "ferritin": "ferritin", "feritin": "ferritin", "hb": "hemoglobin",
             "hct": "hematocrit", "tsat": "tsat", "alb": "albumin", "albumin": "albumin", "k": "potassium",
             "ca": "calcium", "p": "phosphate", "po4": "phosphate", "kt/v": "kt_v", "urr": "urr"}


def _clean(v):
    return "" if v is None else re.sub(r"\s+", " ", str(v)).strip()


def _norm_hn(v):
    s = _clean(v)
    if not s:
        return ""
    s = re.sub(r"\.0$", "", s)
    s = re.sub(r"\D", "", s)
    return s.lstrip("0") or ("0" if s else "")


def _norm_header(h):
    return re.sub(r"\s+", "", _clean(h)).lower()


def _month_of(txt):
    k = _clean(txt).replace(" ", "").replace("เดือน", "").rstrip(".")
    return TH_MONTH.get(k) or TH_MONTH.get(k.replace(".", ""))


def _year_of(txt):
    m = re.search(r"(\d{2,4})\s*$", _clean(txt))
    if not m:
        return None
    yr = int(m.group(1))
    if yr < 100:
        yr += 2500
    return yr - 543 if yr > 2400 else yr


def sheet_month(title):
    """Sheet/title text 'ตค69' / 'พ.ย.69' / 'รายชื่อ… ต.ค.69' → date(y, m, 1) or None."""
    s = _clean(title)
    m = re.search(r"([ก-๙.]{2,6})\s*(\d{2,4})\s*$", s)
    if not m:
        return None
    mon, yr = _month_of(m.group(1)), _year_of(m.group(2))
    return date(yr, mon, 1) if mon and yr else None


def lab_header(h, default_year=None):
    """'iPTH ม.ค.69' / 'Ferritin กค 69' / 'Hb ม.ค.2569' / 'Kt/v เดือนก.ค' (year from sheet) → (field, date) or None."""
    s = _clean(h)
    m = re.match(r"^([A-Za-z/]+)\s*(?:เดือน)?\s*([ก-๙.]+?)\s*\.?\s*(\d{2,4})?$", s)
    if not m:
        return None
    word = m.group(1).lower()
    field = LAB_WORDS.get(word)
    month = _month_of(m.group(2))
    yr = _year_of(m.group(3)) if m.group(3) else default_year
    if not field or not month or not yr:
        return None
    return field, date(yr, month, 1)


def normalize_va(raw):
    s = _clean(raw).upper().replace(" ", "")
    if not s:
        return ""
    parts = [p for p in re.split(r"[+/,]", s) if p]
    return "+".join(VA_MAP.get(p, p) for p in parts)


def normalize_esa(raw):
    """'E4' → ('Epiao', '4000 IU'); 'H4+H10' → ('Hema-plus', '4000+10000 IU'); 'hypercrit' → ('Hypercrit', '')."""
    s = _clean(raw)
    if not s or "…" in s or len(s) > 30:
        return "", ""
    drugs, doses = [], []
    for tok in re.split(r"[+/,]", s):
        tok = tok.strip()
        m = re.match(r"^([A-Za-z]+)\s*(\d+)?$", tok)
        if not m:
            continue
        prefix, num = m.group(1).upper(), m.group(2)
        name = ESA_PREFIX.get(prefix) or ESA_PREFIX.get(prefix[:2]) or ESA_PREFIX.get(prefix[0]) or tok
        if name not in drugs:
            drugs.append(name)
        if num:
            doses.append(str(int(num) * 1000) if int(num) < 100 else num)
    return "+".join(drugs), ("+".join(doses) + " IU") if doses else ""


def parse_regimen(raw):
    """'8000ū 2time/wk' → {"weekly_iu": 8000, "per_week": 2, "dose_iu": 4000, "text": '4000 IU x2/สัปดาห์'}
    '4000ū 1time/wk' → 4000/1/4000 · '10000ū q 2 wk' → 5000/0.5/10000 · 'hold' → {"hold": True}."""
    s = _clean(raw)
    if not s:
        return None
    if re.search(r"hold|งด|off", s, re.I):
        return {"hold": True, "text": "hold"}
    m = re.search(r"(\d[\d,]*)\s*[uūU]?", s)
    if not m:
        return None
    total = float(m.group(1).replace(",", ""))
    t = re.search(r"(\d+)\s*time", s, re.I)
    q = re.search(r"q\s*(\d+)\s*w", s, re.I)
    if t:
        per_week = float(t.group(1))
        dose = total / per_week if per_week else total
        weekly = total
    elif q:
        per_week = round(1.0 / float(q.group(1)), 2)
        dose = total
        weekly = total * per_week
    else:
        per_week, dose, weekly = 1.0, total, total
    txt = f"{dose:g} IU x{per_week:g}/สัปดาห์" if per_week >= 1 else f"{dose:g} IU ทุก {round(1 / per_week):g} สัปดาห์"
    return {"weekly_iu": round(weekly), "per_week": per_week, "dose_iu": round(dose), "text": txt}


def normalize_shift(raw):
    """'รอบเช้า' → morning · 'รอบบ่าย/รอบเช้า' → afternoon (first) · 'รอบ2' → afternoon · '' → ''."""
    s = _clean(raw)
    if not s:
        return ""
    first = re.split(r"[/,]", s)[0].replace("รอบ", "").strip()
    return SHIFT_MAP.get(first, "")


def normalize_schedule(raw):
    """'จ/พ/ศ' → 'จ,พ,ศ' (the format next_schedule_day / scheduler read)."""
    s = _clean(raw)
    return ",".join(p.strip() for p in re.split(r"[/,\s]+", s) if p.strip()) if s else ""


def normalize_bfr(raw):
    """'300' → 300, '250-300' → 300 (the prescribed target), '' / implausible (>600 or <100) → None."""
    nums = [int(n) for n in re.findall(r"\d+", _clean(raw))]
    nums = [n for n in nums if 100 <= n <= 600]
    return max(nums) if nums else None


def _num(v):
    s = _clean(v).replace(",", "")
    m = re.search(r"-?\d+(\.\d+)?", s)
    return float(m.group(0)) if m else None


def _find_header(ws_rows, default_year=None):
    for i, r in enumerate(ws_rows[:15]):
        cols = {}
        labs = {}
        for ci, h in enumerate(r):
            if h is None:
                continue
            key = _norm_header(h)
            if key in HEADERS:
                cols.setdefault(HEADERS[key], ci)
                continue
            lh = lab_header(h, default_year)
            if lh:
                labs[ci] = lh
        if "hn_clinic" in cols and ("vascular_access" in cols or "esa_code" in cols or "hd_per_week" in cols):
            return i, cols, labs
    return None, {}, {}


def parse_roster(file_bytes):
    """→ (rows, skipped). Reads EVERY sheet; a sheet named/titled with a month (ตค69, พ.ย.69) dates its
    Hematocrit / Dry weight to that month, and month-only lab headers ('Kt/v เดือนก.ค') take the sheet's year.
    Rows of the same patient across sheets are merged: scalar fields from the latest month, labs unioned."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
    merged, skipped = {}, []
    order = []
    for ws in wb.worksheets:
        all_rows = [list(r) for r in ws.iter_rows(min_row=1, values_only=True)]
        if not all_rows:
            continue
        sm = sheet_month(ws.title)
        if not sm:
            for r in all_rows[:4]:
                for c in r[:6]:
                    sm = sm or (sheet_month(c) if isinstance(c, str) else None)
        rows_s, skipped_s = _parse_sheet(all_rows, sm)
        skipped.extend(skipped_s)
        key_month = sm or date(1900, 1, 1)
        for r in rows_s:
            k = r["hn_clinic"] or ("H" + r["hn_hospital"])
            r["sheet_month"] = sm.isoformat() if sm else None
            if k in merged:
                prev = merged[k]
                newer = key_month >= prev["_month"]
                base, over = (prev, r) if newer else (r, prev)
                labs = prev["labs"] + [x for x in r["labs"] if x not in prev["labs"]]
                for f, v in over.items():
                    if f in ("labs", "row"):
                        continue
                    if v not in (None, ""):
                        base[f] = v
                base["labs"] = labs
                base["_month"] = max(key_month, prev["_month"])
                merged[k] = base
            else:
                r["_month"] = key_month
                merged[k] = r
                order.append(k)
    rows = [merged[k] for k in order]
    for r in rows:
        r.pop("_month", None)
    return rows, skipped


def _parse_sheet(all_rows, sm=None):
    """One sheet → (rows, skipped). sm = the sheet's month (date) when known."""
    hdr_i, cols, labs = _find_header(all_rows, sm.year if sm else None)
    legacy = hdr_i is None
    # 2568 layout called the schedule column "รอบการฟอก" (values จ/พ/ศ); 2569 uses it for the shift.
    if not legacy and "shift" in cols and "schedule_days" not in cols:
        vals = [_clean((r + [None] * 60)[cols["shift"]]) for r in all_rows[hdr_i + 1:hdr_i + 8]]
        if sum(1 for v in vals if v and re.fullmatch(r"[จอพฤศสา/,\s]+", v)) >= max(1, len([v for v in vals if v]) // 2):
            cols["schedule_days"] = cols.pop("shift")
    if legacy:   # 2568 layout: fixed positions
        hdr_i = 2
        cols = {"name": 2, "hn_hospital": 3, "hn_clinic": 4, "esa_code": 6, "schedule_days": 7, "hd_per_week": 9,
                "vascular_access": 10, "bfr": 11}
    # In the clinic file the "ชื่อ-สกุล" header sits over the title column (นาย/นาง) and the
    # actual name is in the next, header-less column. Detect that from the first data rows.
    TITLES = ("นาย", "นาง", "นางสาว", "น.ส.", "ด.ช.", "ด.ญ.", "นางสาง")
    name_col = cols.get("name", 2)
    title_col = None
    sample = [r for r in all_rows[hdr_i + 1:hdr_i + 6] if r]
    if sample and sum(1 for r in sample if _clean((r + [None] * 60)[name_col]) in TITLES) >= len(sample) / 2:
        title_col, name_col = name_col, name_col + 1
        cols["name"] = name_col
    elif name_col >= 1:
        title_col = name_col - 1
    rows, skipped = [], []
    for idx, r in enumerate(all_rows, start=1):
        if idx <= hdr_i + 1:
            continue
        r = r + [None] * 60

        def g(f):
            return r[cols[f]] if f in cols else None
        name = _clean(g("name"))
        title = _clean(r[title_col]) if title_col is not None else ""
        if title not in TITLES:
            name, title = (name or title), ""   # legend rows ("Hemax 4000……") sit in the title column
        hn_clinic, hn_hosp = _norm_hn(g("hn_clinic")), _norm_hn(g("hn_hospital"))
        if not name or (not hn_clinic and not hn_hosp):
            if name:
                skipped.append({"row": idx, "name": name[:40], "reason": "ไม่มี HN"})
            continue
        esa_drug, esa_dose = normalize_esa(g("esa_code"))
        reg = parse_regimen(g("esa_regimen")) if "esa_regimen" in cols else None
        esa_per_week = None
        if reg:
            if reg.get("hold"):
                esa_dose = "hold"
            else:
                esa_dose, esa_per_week = reg["text"], reg["per_week"]
        hdw = _clean(g("hd_per_week"))
        lab_vals = []
        for ci, (field, d) in labs.items():
            v = _num(r[ci])
            if v is not None:
                lab_vals.append({"field": field, "date": d.isoformat(), "value": v})
        hct = _num(g("hct")) if "hct" in cols else None
        if hct is not None and sm:
            # Hct of a month sheet → lab dated the 1st of that month (deduped against same-month values)
            lab_vals.append({"field": "hematocrit", "date": sm.isoformat(), "value": hct})
            hct = None
        dw = _num(g("dry_weight")) if "dry_weight" in cols else None
        if dw is not None and not (20 <= dw <= 200):
            dw = None
        rows.append({
            "row": idx, "name": f"{title} {name}".strip()[:60], "hn_clinic": hn_clinic, "hn_hospital": hn_hosp,
            "vascular_access": normalize_va(g("vascular_access")), "bfr": normalize_bfr(g("bfr")),
            "hd_per_week": int(float(hdw)) if re.match(r"^\d+(\.0)?$", hdw) else None,
            "schedule_days": normalize_schedule(g("schedule_days")), "shift": normalize_shift(g("shift")),
            "esa_drug": esa_drug, "esa_dose": esa_dose, "esa_per_week": esa_per_week,
            "dry_weight": dw, "hct": hct, "labs": lab_vals,
        })
    return rows, skipped


UPDATE_SQL = """
UPDATE hd_patient_mapping SET
    vascular_access = CASE WHEN %(vascular_access)s <> '' THEN %(vascular_access)s ELSE vascular_access END,
    bfr             = COALESCE(%(bfr)s, bfr),
    hd_per_week     = COALESCE(%(hd_per_week)s, hd_per_week),
    schedule_days   = CASE WHEN %(schedule_days)s <> '' THEN %(schedule_days)s ELSE schedule_days END,
    esa_drug        = CASE WHEN %(esa_drug)s <> '' THEN %(esa_drug)s ELSE esa_drug END,
    esa_dose        = CASE WHEN %(esa_dose)s <> '' THEN %(esa_dose)s ELSE esa_dose END,
    esa_per_week    = COALESCE(%(esa_per_week)s, esa_per_week),
    default_shift   = CASE WHEN %(shift)s <> '' THEN %(shift)s ELSE default_shift END,
    dry_weight      = COALESCE(%(dry_weight)s, dry_weight),
    updated_at      = NOW()
WHERE id = %(id)s
"""

FIELDS = ("vascular_access", "bfr", "hd_per_week", "schedule_days", "esa_drug", "esa_dose", "esa_per_week", "shift", "dry_weight")


def apply_roster(conn, rows, confirm=False, hct_date=None, source="import:roster"):
    """Match rows to hd_patient_mapping and (if confirm) write mapping, prescriptions and labs.
    hct_date: date for the Hematocrit column (default today)."""
    from . import lab_import as li
    hct_date = hct_date or date.today()
    with conn.cursor() as cur:
        cur.execute("""SELECT id, hn, hospital_hn, patient_name, vascular_access, bfr, hd_per_week,
                              schedule_days, esa_drug, esa_dose, esa_per_week, default_shift, dry_weight FROM hd_patient_mapping""")
        by_clinic, by_hosp = {}, {}
        for pid, hn, hhn, pname, va, bfr, hdw, sched, esa, dose, epw, shift, dwt in cur.fetchall():
            rec = {"id": pid, "hn": hn, "name": pname, "vascular_access": va or "", "bfr": bfr, "hd_per_week": hdw,
                   "schedule_days": sched or "", "esa_drug": esa or "", "esa_dose": dose or "",
                   "esa_per_week": float(epw) if epw is not None else None, "shift": shift or "",
                   "dry_weight": float(dwt) if dwt is not None else None}
            if _norm_hn(hn):
                by_clinic[_norm_hn(hn)] = rec
            if _norm_hn(hhn):
                by_hosp[_norm_hn(hhn)] = rec
        cur.execute("""SELECT DISTINCT ON (patient_id) patient_id, id, blood_flow_rate, frequency_per_week, access_type, dry_weight_target
                       FROM hd_prescriptions WHERE is_active = TRUE ORDER BY patient_id, effective_date DESC, id DESC""")
        rx = {pid: {"id": rid, "bfr": b, "freq": f, "access": a or "", "dw": float(d) if d is not None else None}
              for pid, rid, b, f, a, d in cur.fetchall()}

    matched, unmatched, unchanged = [], [], 0
    lab_rows = []
    for r in rows:
        rec = by_clinic.get(r["hn_clinic"]) if r["hn_clinic"] else None
        how = "HN clinic"
        if rec is None and r["hn_hospital"]:
            rec = by_hosp.get(r["hn_hospital"])
            how = "HN รพ"
        if rec is None:
            unmatched.append({"row": r["row"], "name": r["name"], "hn_clinic": r["hn_clinic"], "hn_hospital": r["hn_hospital"]})
            continue
        changes = {}
        for f in FIELDS:
            new = r.get(f)
            if new in (None, ""):
                continue
            old = rec.get(f)
            if f in ("bfr", "hd_per_week"):
                if new != (old or 0):
                    changes[f] = [old, new]
            elif f in ("esa_per_week", "dry_weight"):
                if old is None or abs(float(old) - float(new)) > 0.01:
                    changes[f] = [old, new]
            elif new != old:
                changes[f] = [old, new]
        # prescription (D3): BFR / frequency / access
        p = rx.get(rec["id"])
        rx_change = {}
        if r["bfr"] and (not p or (p["bfr"] or 0) != r["bfr"]):
            rx_change["blood_flow_rate"] = [p["bfr"] if p else None, r["bfr"]]
        if r["hd_per_week"] and (not p or p["freq"] != r["hd_per_week"]):
            rx_change["frequency_per_week"] = [p["freq"] if p else None, r["hd_per_week"]]
        if r["vascular_access"] and (not p or p["access"] != r["vascular_access"]):
            rx_change["access_type"] = [p["access"] if p else None, r["vascular_access"]]
        if r.get("dry_weight") and p and (p.get("dw") is None or abs(float(p["dw"]) - float(r["dry_weight"])) > 0.01):
            rx_change["dry_weight_target"] = [p.get("dw"), r["dry_weight"]]
        if rx_change:
            changes["prescription" if p else "prescription_new"] = rx_change
        # labs
        labs = list(r["labs"])
        if r.get("hct") is not None:
            labs.append({"field": "hematocrit", "date": hct_date.isoformat(), "value": r["hct"]})
        for lb in labs:
            lab_rows.append({"hn": rec["hn"], "patient_id": rec["id"], "lab_date": lb["date"], lb["field"]: lb["value"],
                             "_row": r["row"]})
        if not changes:
            unchanged += 1
        matched.append({"row": r["row"], "name": r["name"], "db_name": rec["name"], "hn": rec["hn"], "matched_by": how,
                        "changes": changes, "labs": len(labs), "_id": rec["id"], "_vals": r, "_rx": p})

    # merge lab rows per (patient, date) and mark duplicates against the DB
    merged = {}
    for lr in lab_rows:
        k = (lr["patient_id"], lr["lab_date"])
        merged.setdefault(k, {"hn": lr["hn"], "patient_id": lr["patient_id"], "lab_date": lr["lab_date"]}).update(
            {f: v for f, v in lr.items() if f in li.LAB_FIELDS})
    lab_list = list(merged.values())
    lab_new = lab_list
    if lab_list:
        with conn.cursor() as cur:
            m, _ = li.match_patients(cur, lab_list)
        lab_new = [x for x in m if not x["duplicate"]]

    written = rx_written = rx_created = labs_written = 0
    if confirm:
        with conn.cursor() as cur:
            for mt in matched:
                ch = mt["changes"]
                if any(f in ch for f in FIELDS):
                    v = dict(mt["_vals"])
                    v["id"] = mt["_id"]
                    cur.execute(UPDATE_SQL, v)
                    written += 1
                rxc = ch.get("prescription") or ch.get("prescription_new")
                if rxc:
                    vals = mt["_vals"]
                    if mt["_rx"]:
                        cur.execute("""UPDATE hd_prescriptions SET blood_flow_rate = COALESCE(%s, blood_flow_rate),
                                       frequency_per_week = COALESCE(%s, frequency_per_week),
                                       access_type = CASE WHEN %s <> '' THEN %s ELSE access_type END,
                                       dry_weight_target = COALESCE(%s, dry_weight_target), updated_at = NOW()
                                       WHERE id = %s""",
                                    (vals["bfr"], vals["hd_per_week"], vals["vascular_access"], vals["vascular_access"],
                                     vals.get("dry_weight"), mt["_rx"]["id"]))
                        rx_written += 1
                    else:
                        cur.execute("""INSERT INTO hd_prescriptions (patient_id, frequency_per_week, blood_flow_rate, access_type,
                                       dry_weight_target, effective_date, is_active, notes) VALUES (%s, %s, %s, %s, %s, CURRENT_DATE, TRUE, %s)""",
                                    (mt["_id"], vals["hd_per_week"] or 3, vals["bfr"], vals["vascular_access"] or None,
                                     vals.get("dry_weight"), "สร้างจากทะเบียนผู้ป่วยประจำปี (นำเข้า) — ตรวจสอบเวลา/dialyzer"))
                        rx_created += 1
            if lab_new:
                for x in lab_new:
                    d = date.fromisoformat(x["lab_date"])
                    if d.day == 1:
                        x["notes"] = f"เดือน {TH_MONTH_NAME[d.month]} {d.year + 543} (ไม่ระบุวัน) — ทะเบียนประจำปี"
                res = li.insert_rows(cur, lab_new, source)
                labs_written = res["inserted"]
        conn.commit()

    for mt in matched:
        mt.pop("_id", None)
        mt.pop("_vals", None)
        mt.pop("_rx", None)
    va_counts = {}
    for r in rows:
        k = r["vascular_access"] or "(ว่าง)"
        va_counts[k] = va_counts.get(k, 0) + 1
    return {
        "total_rows": len(rows), "matched": len(matched), "to_update": sum(1 for m in matched if m["changes"]),
        "unchanged": unchanged, "unmatched": unmatched, "written": written, "confirmed": confirm, "va_counts": va_counts,
        "rx_to_update": sum(1 for m in matched if "prescription" in m["changes"]),
        "rx_to_create": sum(1 for m in matched if "prescription_new" in m["changes"]),
        "rx_written": rx_written, "rx_created": rx_created,
        "labs_total": len(lab_list), "labs_new": len(lab_new), "labs_written": labs_written,
        "hct_date": hct_date.isoformat(), "details": matched,
    }
