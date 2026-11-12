"""Lab results from the document system → hd_lab_results → bed cards (B6).

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
}
NUMERIC_COLS = ["hb", "hct", "k", "na", "ca", "phos", "albumin", "pth", "ferritin", "tsat",
                "ktv", "urr", "creatinine", "bun", "glucose"]
TEXT_COLS = ["hbsag", "antihcv", "antihbs"]

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS hd_lab_results (
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
    synced_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_hd_lab_hn_date ON hd_lab_results(hn, specimen_date DESC);
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


def sync_labs(conn, storage, doc_index, days=180, max_docs=400):
    """Copy lab JSONs of recent lab documents into hd_lab_results.

    Returns {"scanned", "inserted", "skipped", "no_hn", "no_json"}.
    """
    stats = {"scanned": 0, "inserted": 0, "skipped": 0, "no_hn": 0, "no_json": 0, "errors": 0}
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
        cur.execute("SELECT doc_id FROM hd_lab_results")
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
                cols = ["patient_id", "hn", "doc_id", "specimen_date", "doc_date", "status"] + NUMERIC_COLS + TEXT_COLS
                vals = [pid_by_hn[hn], hn, doc_id, _parse_date(rep.get("specimen_date")) or _parse_date(ddate),
                        _parse_date(ddate), rep.get("status", "")] + [row[c] for c in NUMERIC_COLS + TEXT_COLS]
                cur.execute("SAVEPOINT lab_ins")
                cur.execute(f"INSERT INTO hd_lab_results ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
                            f"ON CONFLICT (doc_id) DO NOTHING", vals)
                cur.execute("RELEASE SAVEPOINT lab_ins")
                stats["inserted"] += 1
                have.add(doc_id)
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
    """{patient_id: {date, status, values…, flags[]}} for the newest lab row each."""
    if not patient_ids:
        return {}
    cur.execute(f"""
        SELECT DISTINCT ON (patient_id) patient_id, COALESCE(specimen_date, doc_date) AS d, status, doc_id,
               {', '.join(NUMERIC_COLS + TEXT_COLS)}
        FROM hd_lab_results WHERE patient_id = ANY(%s)
        ORDER BY patient_id, COALESCE(specimen_date, doc_date) DESC NULLS LAST, id DESC
    """, (list(patient_ids),))
    out = {}
    for r in cur.fetchall():
        row = dict(zip(NUMERIC_COLS + TEXT_COLS, r[4:]))
        for c in NUMERIC_COLS:
            if row[c] is not None:
                row[c] = float(row[c])
        row.update({"date": r[1].isoformat() if r[1] else None, "status": r[2], "doc_id": r[3],
                    "age_days": (_thai_today() - r[1]).days if r[1] else None})
        row["flags"] = flags_for(row)
        out[r[0]] = row
    return out
