"""Import the clinic's annual patient roster (Excel) into hd_patient_mapping.

Source: "รายชื่อผู้ป่วยประจำปี" spreadsheet kept by the nursing team.
Columns (row 3 = header, data from row 4):
    A ลำดับ | B คำนำหน้า | C ชื่อ-สกุล | D HN รพ | E HN clinic | F ID (เลขบัตร — NEVER stored)
    G ยาฉีด | H รอบการฟอก | I ที่อยู่ | J จำนวน HD/wk | K Vascular Access | L BFR/min

Only clinical scheduling fields are written (vascular_access, bfr, hd_per_week,
schedule_days, esa_drug, esa_dose). Patient names are NOT overwritten — the
master registry stays the source of truth for identity.

Two-phase: dry_run (preview, default) → confirm (write). Matching is by clinic
HN first, then hospital HN (zero-padding ignored).
"""
import io
import logging
import re

logger = logging.getLogger(__name__)

# Spreadsheet shorthand → the values the HD forms already use
VA_MAP = {
    "AVF": "AVF",
    "AVG": "AVG",
    "PC": "Perm Cath",
    "PERM": "Perm Cath",
    "PERMCATH": "Perm Cath",
    "DLC": "DLC",
    "CVC": "CVC",
}

# ยาฉีด codes used on the roster → ESA drug name + dose
ESA_PREFIX = {
    "E": "Epiao",
    "H": "Hema-plus",
    "HM": "Hemax",
    "HYPERCRIT": "Hypercrit",
    "EPOSIS": "Eposis",
}


def _clean(v):
    if v is None:
        return ""
    return re.sub(r"\s+", " ", str(v)).strip()


def _norm_hn(v):
    """'00007567' / 7567 / '56034.0' → '7567' (leading zeros and .0 dropped)."""
    s = _clean(v)
    if not s:
        return ""
    s = re.sub(r"\.0$", "", s)
    s = re.sub(r"\D", "", s)
    return s.lstrip("0") or ("0" if s else "")


def normalize_va(raw):
    """'PC' → 'Perm Cath', 'PC+AVF' → 'Perm Cath+AVF', unknown → raw upper."""
    s = _clean(raw).upper().replace(" ", "")
    if not s:
        return ""
    parts = [p for p in re.split(r"[+/,]", s) if p]
    out = [VA_MAP.get(p, p) for p in parts]
    return "+".join(out)


def normalize_esa(raw):
    """'E4' → ('Epiao', '4000 IU'); 'H4+H10' → ('Hema-plus', '4000+10000 IU');
    'hypercrit' → ('Hypercrit', ''); legend rows → ('', '')."""
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
        name = ESA_PREFIX.get(prefix) or ESA_PREFIX.get(prefix[:2]) or ESA_PREFIX.get(prefix[0])
        if not name:
            name = tok
        if name not in drugs:
            drugs.append(name)
        if num:
            doses.append(str(int(num) * 1000) if int(num) < 100 else num)
    return "+".join(drugs), ("+".join(doses) + " IU") if doses else ""


def normalize_bfr(raw):
    """'300' → 300, '250-300' → 250 (conservative lower bound), '' → None."""
    s = _clean(raw)
    m = re.search(r"\d+", s)
    return int(m.group(0)) if m else None


def parse_roster(file_bytes):
    """Return (rows, skipped) where rows are dicts ready for matching."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
    ws = wb.worksheets[0]
    rows, skipped = [], []
    for idx, r in enumerate(ws.iter_rows(min_row=1, values_only=True), start=1):
        r = list(r) + [None] * 12
        name = _clean(r[2]) or _clean(r[1])
        hn_clinic = _norm_hn(r[4])
        hn_hosp = _norm_hn(r[3])
        if idx <= 3 or not name or (not hn_clinic and not hn_hosp):
            if idx > 3 and name:
                skipped.append({"row": idx, "name": name[:40], "reason": "ไม่มี HN"})
            continue
        esa_drug, esa_dose = normalize_esa(r[6])
        hdw = _clean(r[9])
        rows.append({
            "row": idx,
            "name": f"{_clean(r[1])} {_clean(r[2])}".strip()[:60],
            "hn_clinic": hn_clinic,
            "hn_hospital": hn_hosp,
            "vascular_access": normalize_va(r[10]),
            "bfr": normalize_bfr(r[11]),
            "hd_per_week": int(float(hdw)) if re.match(r"^\d+(\.0)?$", hdw) else None,
            "schedule_days": _clean(r[7]),
            "esa_drug": esa_drug,
            "esa_dose": esa_dose,
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
    updated_at      = NOW()
WHERE id = %(id)s
"""


def apply_roster(conn, rows, confirm=False):
    """Match rows to hd_patient_mapping and (if confirm) update them.

    Returns a summary dict with per-row results for the preview UI.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT id, hn, hospital_hn, patient_name, vascular_access, bfr, hd_per_week, "
                    "schedule_days, esa_drug FROM hd_patient_mapping")
        by_clinic, by_hosp = {}, {}
        for pid, hn, hhn, pname, va, bfr, hdw, sched, esa in cur.fetchall():
            rec = {"id": pid, "hn": hn, "name": pname, "va": va or "", "bfr": bfr,
                   "hdw": hdw, "sched": sched or "", "esa": esa or ""}
            if _norm_hn(hn):
                by_clinic[_norm_hn(hn)] = rec
            if _norm_hn(hhn):
                by_hosp[_norm_hn(hhn)] = rec

    matched, unmatched, unchanged = [], [], 0
    for r in rows:
        rec = by_clinic.get(r["hn_clinic"]) if r["hn_clinic"] else None
        how = "HN clinic"
        if rec is None and r["hn_hospital"]:
            rec = by_hosp.get(r["hn_hospital"])
            how = "HN รพ"
        if rec is None:
            unmatched.append({"row": r["row"], "name": r["name"],
                              "hn_clinic": r["hn_clinic"], "hn_hospital": r["hn_hospital"]})
            continue
        changes = {}
        if r["vascular_access"] and r["vascular_access"] != rec["va"]:
            changes["vascular_access"] = [rec["va"], r["vascular_access"]]
        if r["bfr"] is not None and r["bfr"] != (rec["bfr"] or 0):
            changes["bfr"] = [rec["bfr"], r["bfr"]]
        if r["hd_per_week"] is not None and r["hd_per_week"] != rec["hdw"]:
            changes["hd_per_week"] = [rec["hdw"], r["hd_per_week"]]
        if r["schedule_days"] and r["schedule_days"] != rec["sched"]:
            changes["schedule_days"] = [rec["sched"], r["schedule_days"]]
        if r["esa_drug"] and r["esa_drug"] != rec["esa"]:
            changes["esa_drug"] = [rec["esa"], r["esa_drug"]]
        if not changes:
            unchanged += 1
        matched.append({"row": r["row"], "name": r["name"], "db_name": rec["name"],
                        "hn": rec["hn"], "matched_by": how, "changes": changes,
                        "_id": rec["id"], "_vals": r})

    written = 0
    if confirm:
        with conn.cursor() as cur:
            for m in matched:
                if not m["changes"]:
                    continue
                v = dict(m["_vals"])
                v["id"] = m["_id"]
                cur.execute(UPDATE_SQL, v)
                written += 1
        conn.commit()

    for m in matched:
        m.pop("_id", None)
        m.pop("_vals", None)

    va_counts = {}
    for r in rows:
        k = r["vascular_access"] or "(ว่าง)"
        va_counts[k] = va_counts.get(k, 0) + 1

    return {
        "total_rows": len(rows),
        "matched": len(matched),
        "to_update": sum(1 for m in matched if m["changes"]),
        "unchanged": unchanged,
        "unmatched": unmatched,
        "written": written,
        "confirmed": confirm,
        "va_counts": va_counts,
        "details": matched,
    }
