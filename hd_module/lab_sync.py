"""Lab results from the document system → hd_lab_docs → bed cards (B6).

Source of truth stays the document pipeline (OCR → lab_extractor →
_lab_results/<doc_id>.json in GCS, status pending_review/approved). This
module only *copies* the clinically relevant analytes per patient into
PostgreSQL so the HD dashboard can show them next to the bed without
re-parsing documents, and flags values the physician wants to see before
a session starts. It never changes the documents or the lab JSON.

Rules (notify, never treat):
    K  ≥ 6.0  → critical  'โพแทสเซียมสูง'      (≥ 5.5 warning)
    Hb < 7.0  → critical  'ซีดมาก'  ; Hb < 9 or > 13 → warning 'ทบทวน ESA'
    P  ≥ 7.0  → warning   'ฟอสเฟตสูง'
    Ca ≥ 10.5 or < 8.0 → warning
    HBsAg / Anti-HCV positive → info flag on the card
"""
import logging
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# analyte name (lab_extractor canonical) → column
ANALYTE_COL = {
    "Hemoglobin": "hb", "Hematocrit": "hct", "Potassium": "k", "Sodium": "na",
    "Calcium": "ca", "Phosphate": "phos", "Albumin": "albumin", "PTH": "pth",
    "Ferritin": "ferritin", "TSAT": "tsat", "Kt/V": "ktv", "URR": "urr",
    "Creatinine": "creatinine", "BUN": "bun", "Glucose": "glucose", "FBS": "glucose",
    "HBsAg": "hbsag", "Anti-HCV": "antihcv", "Anti-HBs": "antihbs",
    # 2026-11-22 แพทย์สั่งเพิ่ม: PLT / HCO3 / WBC / Anti-HIV
    "Platelet": "plt", "Bicarbonate": "hco3", "WBC": "wbc", "Anti-HIV": "antihiv",
}
NUMERIC_COLS = ["hb", "hct", "k", "na", "ca", "phos", "albumin", "pth", "ferritin", "tsat",
                "ktv", "urr", "creatinine", "bun", "glucose", "plt", "hco3", "wbc"]
TEXT_COLS = ["hbsag", "antihcv", "antihbs", "antihiv"]

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS hd_lab_docs (
    id SERIAL PRIMARY KEY,
    patient_id INTEGER REFERENCES hd_patient_mapping(id),
    hn VARCHAR(20) NOT NULL,
    doc_id VARCHAR(80) UNIQUE NOT NULL,
    specimen_date DATE,
    doc_date DATE,
    status VARCHAR(30),
    hb NUMERIC(5,1), hct NUMERIC(5,1), k NUMERIC(4,2), na NUMERIC(5,1), ca NUMERIC(5,2),
    phos NUMERIC(5,2), albumin NUMERIC(4,2), pth NUMERIC(8,1), ferritin NUMERIC(8,1),
    tsat NUMERIC(5,1), ktv NUMERIC(4,2), urr NUMERIC(5,1), creatinine NUMERIC(6,2),
    bun NUMERIC(6,1), glucose NUMERIC(6,1),
    hbsag VARCHAR(20), antihcv VARCHAR(20), antihbs VARCHAR(20),
    plt NUMERIC(7,1), hco3 NUMERIC(4,1), wbc NUMERIC(6,2), antihiv VARCHAR(20),
    synced_at TIMESTAMP DEFAULT NOW(),
    verified_status VARCHAR(20) DEFAULT 'pending',
    verified_by VARCHAR(80),
    verified_at TIMESTAMP,
    delta_flags TEXT
);
CREATE INDEX IF NOT EXISTS ix_hd_lab_docs_hn_date ON hd_lab_docs(hn, specimen_date DESC);
"""


def _thai_today():
    return (datetime.now(timezone.utc) + timedelta(hours=7)).date()


def _parse_date(s):
    if not s or s == "unknown":
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def _doc_hn(doc):
    """Clinic HN from patient_hn_folder '67398_ชื่อ' (or gcs path segment)."""
    folder = doc.get("patient_hn_folder") or ""
    if not folder:
        parts = (doc.get("gcs_object_path") or "").split("/")
        if len(parts) >= 5:
            folder = parts[3]
    return folder.split("_")[0].strip().upper() if folder else ""


def report_to_row(report: dict):
    """Flatten a _lab_results JSON into column values."""
    row = {c: None for c in NUMERIC_COLS + TEXT_COLS}
    for r in report.get("results", []):
        col = ANALYTE_COL.get(r.get("analyte"))
        if not col:
            continue
        v = r.get("value")
        if col in TEXT_COLS:
            raw = (r.get("raw_text") or "").lower()
            if v is None:
                row[col] = "positive" if "pos" in raw or "reactive" in raw else ("negative" if "neg" in raw else None)
            else:
                row[col] = "positive" if float(v) > 0 else "negative"
        else:
            try:
                row[col] = float(v)
            except (TypeError, ValueError):
                pass
    return row


def flags_for(row):
    """Clinical flags (list of dicts) for one lab row. Detect + explain only."""
    f = []
    k, hb, p, ca = row.get("k"), row.get("hb"), row.get("phos"), row.get("ca")
    if k is not None:
        k = float(k)
        if k >= 6.0:
            f.append({"sev": "critical", "code": "k_high", "text": f"K {k:.1f} ≥ 6.0 — โพแทสเซียมสูง ก่อนเริ่มฟอก"})
        elif k >= 5.5:
            f.append({"sev": "warning", "code": "k_high", "text": f"K {k:.1f} ≥ 5.5"})
    if hb is not None:
        hb = float(hb)
        if hb < 7.0:
            f.append({"sev": "critical", "code": "hb_low", "text": f"Hb {hb:.1f} < 7 — ซีดมาก"})
        elif hb < 9.0:
            f.append({"sev": "warning", "code": "hb_low", "text": f"Hb {hb:.1f} < 9 — ทบทวน ESA"})
        elif hb > 13.0:
            f.append({"sev": "warning", "code": "hb_high", "text": f"Hb {hb:.1f} > 13 — ทบทวน ESA"})
    if p is not None and float(p) >= 7.0:
        f.append({"sev": "warning", "code": "phos_high", "text": f"P {float(p):.1f} ≥ 7.0"})
    if ca is not None:
        ca = float(ca)
        if ca >= 10.5:
            f.append({"sev": "warning", "code": "ca_high", "text": f"Ca {ca:.1f} ≥ 10.5"})
        elif ca < 8.0:
            f.append({"sev": "warning", "code": "ca_low", "text": f"Ca {ca:.1f} < 8.0"})
    for col, name in (("hbsag", "HBsAg"), ("antihcv", "Anti-HCV")):
        if (row.get(col) or "") == "positive":
            f.append({"sev": "info", "code": col, "text": f"{name} positive"})
    return f


# ── Precision guards (OCR-suspicion, never auto-treat) ─────────────────
# 1) physiologic plausibility  2) Hb/Hct internal consistency (Hct ≈ 3×Hb)
# 3) delta vs. the patient's previous lab
# Any hit → stored in delta_flags and the row stays "pending" until the
# physician verifies against the source photo.
PLAUSIBLE_RANGE = {
    "hb": (3, 20), "hct": (9, 60), "k": (1.5, 9.0), "na": (110, 170),
    "ca": (4, 16), "phos": (0.5, 15), "albumin": (1, 6),
    "glucose": (20, 1000), "creatinine": (0.5, 30), "bun": (2, 200),
    "plt": (10, 1000), "hco3": (5, 45), "wbc": (1, 60),
}
DELTA_LIMIT = {
    "k": 1.5, "hb": 2.5, "hct": 8.0, "ca": 2.5, "phos": 4.0,
    "na": 10.0, "albumin": 1.5,
}
_LAB_TH = {"hb": "Hb", "hct": "Hct", "k": "K", "na": "Na", "ca": "Ca",
           "phos": "P", "albumin": "Alb", "glucose": "Glucose",
           "creatinine": "Cr", "bun": "BUN",
           "plt": "PLT", "hco3": "HCO3", "wbc": "WBC"}


def precision_flags(row, prev=None):
    """OCR-suspicion flags for one new lab row. Returns list of Thai strings.

    row/prev: dicts with numeric analyte columns (floats or None).
    Pure function — unit-tested in tests/test_lab_verify.py.
    """
    out = []
    for c, (lo, hi) in PLAUSIBLE_RANGE.items():
        v = row.get(c)
        if v is None:
            continue
        v = float(v)
        if v < lo or v > hi:
            out.append(f"{_LAB_TH[c]} {v:g} เกินช่วงที่เป็นไปได้ ({lo:g}–{hi:g}) — สงสัย OCR อ่านพลาด")
    hb, hct = row.get("hb"), row.get("hct")
    if hb is not None and hct is not None and abs(float(hct) - 3.0 * float(hb)) > 6.0:
        out.append(f"Hb {float(hb):g} / Hct {float(hct):g} ไม่สอดคล้องกัน (Hct ควร ≈ 3×Hb) — ตรวจรูปซ้ำ")
    if prev:
        for c, lim in DELTA_LIMIT.items():
            v, p = row.get(c), prev.get(c)
            if v is None or p is None:
                continue
            if abs(float(v) - float(p)) >= lim:
                out.append(f"{_LAB_TH[c]} เปลี่ยน {float(p):g} → {float(v):g} (ต่างเกิน {lim:g}) — ตรวจรูปซ้ำก่อนใช้ค่า")
    return out


def prev_lab_row(cur, patient_id, before_date):
    """Newest non-rejected lab row for the patient before before_date (dict or None)."""
    cols = list(DELTA_LIMIT.keys())
    try:
        cur.execute("SAVEPOINT prev_lab")
    except Exception:
        pass
    try:
        cur.execute(f"""
            SELECT {', '.join(cols)} FROM hd_labs
            WHERE patient_id = %s AND lab_date IS NOT NULL AND lab_date < %s
              AND (verified_status IS DISTINCT FROM 'rejected')
            ORDER BY lab_date DESC LIMIT 1
        """, (patient_id, before_date))
        r = cur.fetchone()
        try:
            cur.execute("RELEASE SAVEPOINT prev_lab")
        except Exception:
            pass
        if not r:
            return None
        return {c: (float(v) if v is not None else None) for c, v in zip(cols, r)}
    except Exception as e:
        try:
            cur.execute("ROLLBACK TO SAVEPOINT prev_lab")
        except Exception:
            pass
        logger.warning("prev_lab_row: %s", e)
        return None


def sync_labs(conn, storage, doc_index, days=180, max_docs=400):
    """Copy lab JSONs of recent lab documents into hd_lab_docs.

    Returns {"scanned", "inserted", "skipped", "no_hn", "no_json"}.
    """
    stats = {"scanned": 0, "inserted": 0, "skipped": 0, "no_hn": 0, "no_json": 0, "errors": 0,
             "alerts": []}  # ข้อความแจ้ง LINE (22 พ.ย. 2569): serology บวก / ค่าติดธง
    if doc_index is None or storage is None:
        return stats
    import json
    cutoff = (_thai_today() - timedelta(days=days)).isoformat()
    try:
        doc_index._ensure_fresh()
        docs = [d for d in list(doc_index._docs) if d.get("category") == "lab"]
    except Exception as e:
        logger.warning("lab_sync: doc index unavailable: %s", e)
        return stats
    docs.sort(key=lambda d: (d.get("submitted_at") or d.get("received_at") or d.get("upload_date") or ""), reverse=True)
    with conn.cursor() as cur:
        cur.execute(CREATE_SQL)
        cur.execute("SELECT doc_id FROM hd_lab_docs")
        have = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT id, hn FROM hd_patient_mapping")
        pid_by_hn = {str(r[1]).strip().upper(): r[0] for r in cur.fetchall()}
        for d in docs[:max_docs]:
            doc_id = d.get("document_id") or d.get("doc_id") or ""
            if not doc_id or doc_id in have:
                stats["skipped"] += 1
                continue
            ddate = (d.get("submitted_at") or d.get("received_at") or d.get("upload_date") or "")[:10]
            if ddate and ddate < cutoff:
                continue
            stats["scanned"] += 1
            hn = _doc_hn(d)
            if not hn or hn not in pid_by_hn:
                stats["no_hn"] += 1
                continue
            try:
                raw = storage.get(f"_lab_results/{doc_id}.json")
            except Exception:
                raw = None
            if not raw:
                stats["no_json"] += 1
                continue
            try:
                rep = json.loads(raw if isinstance(raw, (str, bytes)) else raw.decode("utf-8"))
                row = report_to_row(rep)
                if all(row[c] is None for c in NUMERIC_COLS + TEXT_COLS):
                    stats["no_json"] += 1
                    continue
                spec_date = _parse_date(rep.get("specimen_date")) or _parse_date(ddate)
                # ── Precision guards: compare with the patient's previous lab ──
                prev = prev_lab_row(cur, pid_by_hn[hn], spec_date or _thai_today())
                pflags = precision_flags(row, prev)
                # doc-system "approved" = physician already reviewed it there
                v_status = "verified" if rep.get("status") == "approved" and not pflags else "pending"
                v_by = "doc_system" if v_status == "verified" else None
                cols = (["patient_id", "hn", "doc_id", "specimen_date", "doc_date", "status"]
                        + NUMERIC_COLS + TEXT_COLS
                        + ["verified_status", "verified_by", "delta_flags"])
                vals = ([pid_by_hn[hn], hn, doc_id, spec_date,
                         _parse_date(ddate), rep.get("status", "")]
                        + [row[c] for c in NUMERIC_COLS + TEXT_COLS]
                        + [v_status, v_by, "; ".join(pflags) if pflags else None])
                cur.execute("SAVEPOINT lab_ins")
                cur.execute(f"INSERT INTO hd_lab_docs ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
                            f"ON CONFLICT (doc_id) DO NOTHING", vals)
                cur.execute("RELEASE SAVEPOINT lab_ins")
                stats["inserted"] += 1
                have.add(doc_id)
                # ── แจ้ง LINE (ผ่าน caller): ผลบวก/ค่าติดธงจากใบใหม่ ──
                if row.get("hbsag") == "positive":
                    stats["alerts"].append(f"🔴 HN {hn}: HBsAg+ จากใบแล็บใหม่ — ทบทวนการแยกโซน/เครื่อง")
                if row.get("antihcv") == "positive":
                    stats["alerts"].append(f"🟠 HN {hn}: Anti-HCV+ จากใบแล็บใหม่")
                if row.get("antihiv") == "positive":
                    # ความเป็นส่วนตัว: ไม่ระบุชนิดการตรวจบน LINE — ดูในระบบเท่านั้น
                    stats["alerts"].append(f"🛡 HN {hn}: มีผลซีโรโลยีสำคัญ รอดูในกล่องรอหมอ")
                if pflags:
                    stats["alerts"].append(f"🚨 HN {hn}: ค่าแล็บน่าสงสัย {len(pflags)} จุด — ตรวจรูปซ้ำในกล่องรอหมอ")
            except Exception as e:
                stats["errors"] += 1
                try:
                    cur.execute("ROLLBACK TO SAVEPOINT lab_ins")
                except Exception:
                    pass
                logger.warning("lab_sync: doc %s: %s", doc_id[:8], type(e).__name__)
    try:
        conn.commit()
    except Exception:
        pass
    return stats


def latest_for_patients(cur, patient_ids):
    """{patient_id: {date, status, values…, flags[]}} for the newest lab row each.

    Uses the unified hd_labs VIEW which merges hd_lab_results + hd_lab_docs
    with standardised column names (hb, hct, k, na, ca, phos, pth, ktv, …).
    """
    if not patient_ids:
        return {}
    out = {}
    try:
        cur.execute(f"""
            SELECT DISTINCT ON (patient_id) patient_id, lab_date, source,
                   {', '.join(NUMERIC_COLS + TEXT_COLS)},
                   verified_status, delta_flags, doc_row_id
            FROM hd_labs WHERE patient_id = ANY(%s)
              AND (verified_status IS DISTINCT FROM 'rejected')
            ORDER BY patient_id, lab_date DESC NULLS LAST, doc_row_id DESC NULLS LAST
        """, (list(patient_ids),))
        for r in cur.fetchall():
            pid, lab_date, src = r[0], r[1], r[2]
            row = dict(zip(NUMERIC_COLS + TEXT_COLS + ["verified_status", "delta_flags", "doc_row_id"], r[3:]))
            for c in NUMERIC_COLS:
                if row[c] is not None:
                    row[c] = float(row[c])
            d_iso = lab_date.isoformat() if lab_date else None
            age = (_thai_today() - lab_date).days if lab_date else None
            row.update({"date": d_iso, "status": src or "unknown", "doc_id": "",
                        "age_days": age, "source": src or "unknown"})
            row["flags"] = flags_for(row)
            out[pid] = row
    except Exception as e:
        logger.warning("latest_for_patients hd_labs: %s", e)

    return out
