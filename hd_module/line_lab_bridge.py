"""📄➡️📱 Line Lab Bridge — ขั้นที่ 3 ของ Lab Sheet Reader (26 พ.ย. 2569)

เชื่อม "ตัวอ่านใบแล็บ รพ.ระนอง" (lab_sheet_reader: Claude Vision 2 รอบ + ตรวจ 6 ชั้น) เข้ากับ
ทางเดินรับรูปของ LINE OA ใน server.py — เป็นฟังก์ชันล้วนให้ server.py เรียก 4 จุดเล็ก ๆ:

  1. _multipage_flush  → plan(): อ่านทุกหน้า → จัดกลุ่ม "ผู้ป่วย + ใบต่อเนื่อง" ก่อนรวม PDF
                          (แก้ปัญหา 25 พ.ย.: แล็บ 3 คน 3 รูป ถูกรวมเป็น PDF ใบเดียวของคนแรก)
  2. Lab extraction    → lab_report(): ใช้ค่าจากตัวอ่าน (แทน Tesseract+regex) เมื่อมีผลของ doc นั้น
  3. สรุปชุด           → value_summary() + ปุ่ม quick reply "✅ ยืนยันแล็บ" 1 แตะ
  4. คำสั่ง "ยืนยันแล็บ <รหัส>" → confirm(): ติ๊ก approved ใน JSON + verified ใน hd_lab_docs

เปิดใช้ด้วย ⚙️ ตั้งค่า `lab_sheet_ai` = on เท่านั้น (ค่าเริ่มต้น off = ทางเดินเดิมเป๊ะ)
ล้มเมื่อไหร่ → คืน None/False ให้ server.py ใช้ทางเดิม — ไม่มีทางทำให้รูปหาย
"""
import logging
import threading
import time

from . import lab_sheet_reader as lsr

logger = logging.getLogger(__name__)

SETTING_KEY = "lab_sheet_ai"
CONFIRM_RE_SRC = r"^\s*(?:ยืนยันแล็บ|ยืนยันแลป|labok|lab\s*ok)\s+([0-9a-fA-F]{6,12})\s*$"

# key ของตัวอ่าน → ชื่อ analyte มาตรฐานของระบบเอกสาร (lab_extractor / lab_sync.ANALYTE_COL)
CANON = {
    "hb": "Hemoglobin", "hct": "Hematocrit", "k": "Potassium", "na": "Sodium", "ca": "Calcium",
    "phos": "Phosphate", "albumin": "Albumin", "pth": "PTH", "ferritin": "Ferritin",
    "creatinine": "Creatinine", "bun": "BUN", "glucose": "Glucose", "plt": "Platelet",
    "hco3": "Bicarbonate", "wbc": "WBC", "iron": "Iron", "tibc": "TIBC", "vitd": "Vitamin D",
    "cl": "Chloride", "chol": "Total Cholesterol", "tg": "Triglycerides", "ldl": "LDL", "hdl": "HDL",
    "gfr": "eGFR", "uric": "Uric Acid", "mg": "Magnesium", "tp": "Total Protein", "globulin": "Globulin",
    "alp": "ALP", "ast": "AST", "alt": "ALT", "tbili": "Total Bilirubin", "dbili": "Direct Bilirubin",
    "rbc": "RBC", "mcv": "MCV", "mch": "MCH", "mchc": "MCHC", "rdw": "RDW",
    "neut": "Neutrophil", "lymph": "Lymphocyte", "mono": "Monocyte", "eos": "Eosinophil", "baso": "Basophil",
    "hbsag": "HBsAg", "antihcv": "Anti-HCV", "antihbs": "Anti-HBs", "antihbc": "Anti-HBc", "antihiv": "Anti-HIV",
}
# ค่าที่โชว์ในสรุป LINE (เรียงตามที่แพทย์สนใจ) — Anti HIV ไม่โชว์ (lsr.NEVER_PUSH)
SUMMARY_ORDER = ("hb", "k", "ferritin", "tsat", "pth", "iron", "tibc", "vitd", "phos", "ca", "albumin",
                 "creatinine", "bun", "hbsag", "antihbs", "antihbc", "antihcv")

# ผลอ่านต่อ doc_id (รอ pipeline มาหยิบ) — TTL 30 นาที
_READS: dict = {}
_READS_LOCK = threading.Lock()
_READ_TTL = 1800


def is_enabled(get_setting):
    try:
        return str(get_setting(SETTING_KEY, "") or "").strip().lower() in ("on", "1", "true", "yes")
    except Exception:
        return False


def remember(doc_id, res):
    with _READS_LOCK:
        now = time.time()
        for k in [k for k, (ts, _) in _READS.items() if now - ts > _READ_TTL]:
            _READS.pop(k, None)
        _READS[doc_id] = (now, res)


def recall(doc_id):
    with _READS_LOCK:
        item = _READS.get(doc_id)
    return item[1] if item else None


def usable(res):
    """ผลอ่านใช้จัดกลุ่ม/แทน OCR ได้ไหม: ต้องมีค่าอย่างน้อย 1 และไม่ใช่ '2 คนในหน้าเดียว'"""
    if not res or not res.get("values"):
        return False
    if any("2 คน" in p for p in res.get("problems") or []):
        return False
    return True


def read_page(img_bytes, api_key, model_id, double=True):
    """อ่าน 1 รูปจาก LINE → ผล lsr หรือ None (ล้ม/คิวเต็ม/ไม่ใช่ใบแล็บ)"""
    try:
        res = lsr.read_sheet(img_bytes, api_key, model_id, double=double)
    except RuntimeError:          # busy
        return None
    except Exception as e:
        logger.info("line-lab read: %s", type(e).__name__)
        return None
    return res if usable(res) else None


def page_text(res, page_no=0, page_total=0):
    """ข้อความแทน OCR ให้ระบบเอกสารเดิมใช้ (จัดประเภท = แล็บ, หา HN/ชื่อ, index ค้นหา)"""
    h = res.get("header") or {}
    lines = ["ใบรายงานผลการตรวจทางห้องปฏิบัติการ โรงพยาบาลระนอง (อ่านด้วย Lab Sheet Reader)"]
    if page_total:
        lines[0] += f" [หน้า {page_no}/{page_total}]"
    if h.get("request_form"):
        lines.append(f"Request Form {h['request_form']}")
    if h.get("hn"):
        lines.append(f"HN {h['hn']}")
    if h.get("an"):
        lines.append(f"AN {h['an']}")
    if h.get("name"):
        lines.append(f"ชื่อผู้ป่วย {h['name']}")
    if h.get("request_date"):
        lines.append(f"Request Date {h['request_date']} {h.get('request_time', '')}".rstrip())
    if h.get("receive_date"):
        lines.append(f"Receive Date {h['receive_date']}")
    lines.append("No. Parameter Result Unit")
    rows = sorted(((e.get("no") or 0), k, e) for k, e in (res.get("values") or {}).items())
    for no, k, e in rows:
        v = e["value"]
        if isinstance(v, str):
            v = v.capitalize()
        lines.append(f"{no or ''} {e.get('param') or lsr.A[k]['label']} {v} {e.get('unit', '')}".strip())
    for u in res.get("uncertain") or []:
        lines.append(f"{u.get('no') or ''} {u.get('param') or u.get('label')} ? (ไม่แน่ใจ: {u.get('raw') or u.get('value')})".strip())
    if (res.get("computed") or {}).get("tsat") is not None:
        lines.append(f"TSAT (คำนวณ) {res['computed']['tsat']} %")
    return "\n".join(lines)


def _is_continuation(res):
    """หน้าต่อ (ไม่มีหัวใบ / เริ่มแถว >1) — ต้องติดไปกับใบก่อนหน้า"""
    h = res.get("header") or {}
    rows = res.get("rows_seen") or []
    return (not h.get("hn_norm")) and bool(rows) and rows[0] > 1


def plan_groups(results):
    """results: list[res|None] เรียงตามลำดับรูป → list[group]
    group = {"pages": [idx...], "hn": hn_norm|"", "hn_sheet": str, "name": str, "res": res|None, "lab": bool}
    กฎ: หน้าที่อ่านไม่ได้/ไม่ใช่แล็บ → กลุ่มของตัวเอง (ทางเดิม) ·
        หน้าแล็บที่มี HN → รวมกับหน้าแล็บ HN เดียวกัน ·
        หน้าต่อ (ไม่มี HN, เริ่มแถว >1) → ติดกลุ่มแล็บก่อนหน้า ถ้าไม่มีก็กลุ่มเดี่ยว"""
    groups = []
    by_hn = {}
    last_lab = None
    for idx, res in enumerate(results):
        if not res:
            groups.append({"pages": [idx], "hn": "", "hn_sheet": "", "name": "", "res": None, "lab": False})
            last_lab = None
            continue
        h = res.get("header") or {}
        hn = h.get("hn_norm") or ""
        if hn:
            g = by_hn.get(hn)
            if g is None:
                g = {"pages": [], "hn": hn, "hn_sheet": h.get("hn") or hn, "name": h.get("name") or "",
                     "res": res, "lab": True}
                by_hn[hn] = g
                groups.append(g)
            else:
                g["res"] = merge_results(g["res"], res)
            g["pages"].append(idx)
            last_lab = g
        elif _is_continuation(res) and last_lab is not None:
            last_lab["pages"].append(idx)
            last_lab["res"] = merge_results(last_lab["res"], res)
        else:
            g = {"pages": [idx], "hn": "", "hn_sheet": "", "name": h.get("name") or "", "res": res, "lab": True}
            groups.append(g)
            last_lab = g
    return groups


def merge_results(a, b):
    """รวมค่าจาก 2 หน้าของใบเดียวกัน (หน้าต่อ) — ค่าซ้ำให้หน้าแรกชนะ"""
    out = dict(a)
    vals = dict(a.get("values") or {})
    for k, e in (b.get("values") or {}).items():
        vals.setdefault(k, e)
    out["values"] = vals
    out["uncertain"] = list(a.get("uncertain") or []) + [u for u in (b.get("uncertain") or []) if u["key"] not in vals]
    out["problems"] = [p for p in list(a.get("problems") or []) + list(b.get("problems") or [])
                       if not p.startswith("เริ่มที่แถว")]
    out["rows_seen"] = sorted(set(a.get("rows_seen") or []) | set(b.get("rows_seen") or []))
    comp = dict(a.get("computed") or {})
    if comp.get("tsat") is None and "iron" in vals and "tibc" in vals and vals["tibc"]["value"]:
        comp["tsat"] = round(vals["iron"]["value"] / vals["tibc"]["value"] * 100, 2)
    out["computed"] = comp
    return out


def lab_report(res, doc_id, extraction_method="regex_ocr"):
    """ผลตัวอ่าน → LabReport ของระบบเอกสาร (ชื่อ analyte มาตรฐาน) · None ถ้าไม่มีค่า"""
    from lab_models import LabReport, LabResult, ReviewStatus
    from datetime import datetime, timezone
    results = []
    for key, e in (res.get("values") or {}).items():
        canon = CANON.get(key)
        if not canon:
            continue
        v = e["value"]
        if isinstance(v, str):
            num = 1.0 if v == "positive" else 0.0
            results.append(LabResult(analyte=canon, value=num, unit="", reference_range="negative",
                                     confidence=0.98, extraction_method="lab_sheet_ai", flags=[],
                                     raw_text=f"{e.get('param', canon)} {v.capitalize()}"[:120]))
        else:
            results.append(LabResult(analyte=canon, value=float(v), unit=e.get("unit", ""), reference_range="",
                                     confidence=float(e.get("conf") or 0.95), extraction_method="lab_sheet_ai",
                                     flags=[], raw_text=f"{e.get('param', canon)} {e.get('raw', v)}"[:120]))
    comp = res.get("computed") or {}
    if comp.get("tsat") is not None:
        results.append(LabResult(analyte="TSAT", value=float(comp["tsat"]), unit="%", reference_range="",
                                 confidence=0.98, extraction_method="lab_sheet_ai", flags=[],
                                 raw_text="TSAT = Iron/TIBC x100 (คำนวณ)"))
    if not results:
        return None
    h = res.get("header") or {}
    rep = LabReport(source_document_id=doc_id, specimen_date=h.get("lab_date") or "unknown",
                    extraction_method=extraction_method, results=results,
                    status=ReviewStatus.PENDING_REVIEW.value,
                    extracted_at=datetime.now(timezone.utc).isoformat())
    warn = [f"ไม่แน่ใจ: {u.get('label')} ({u.get('raw') or u.get('value')})" for u in (res.get("uncertain") or [])]
    warn += [f"ตรวจสอบ: {p}" for p in (res.get("problems") or [])]
    try:
        rep.warnings = warn
    except Exception:
        pass
    return rep


def value_summary(res, limit=7):
    """สรุปค่าแบบสั้นสำหรับ LINE: 'Hb 8.3 · K 4.17 · Ferritin 226 · TSAT 27%' (ไม่มี Anti HIV)"""
    vals = res.get("values") or {}
    comp = res.get("computed") or {}
    parts = []
    short = {"hb": "Hb", "k": "K", "ferritin": "Ferritin", "pth": "PTH", "iron": "Iron", "tibc": "TIBC",
             "vitd": "VitD", "phos": "PO4", "ca": "Ca", "albumin": "Alb", "creatinine": "Cr", "bun": "BUN",
             "hbsag": "HBsAg", "antihbs": "HBsAb", "antihbc": "HBcAb", "antihcv": "HCV"}

    def _fmt(v):
        if isinstance(v, str):
            return "+" if v == "positive" else "−"
        return f"{v:g}"
    for k in SUMMARY_ORDER:
        if k in lsr.NEVER_PUSH:
            continue
        if k == "tsat":
            if comp.get("tsat") is not None:
                parts.append(f"TSAT {comp['tsat']:g}%")
            continue
        if k in vals:
            parts.append(f"{short.get(k, k)} {_fmt(vals[k]['value'])}")
        if len(parts) >= limit:
            break
    n_total = len(vals) + (1 if comp.get("tsat") is not None else 0)
    more = n_total - len(parts)
    s = " · ".join(parts)
    if more > 0:
        s += f" (+{more})"
    n_unc = len(res.get("uncertain") or [])
    if n_unc:
        s += f" · ❓{n_unc}"
    return s


def approve_report_json(raw_json, who):
    """JSON ของ _lab_results → ติ๊ก approved (ยืนยันจาก LINE) → JSON ใหม่ · None ถ้า parse ไม่ได้"""
    import json
    from datetime import datetime, timezone
    try:
        rep = json.loads(raw_json if isinstance(raw_json, str) else raw_json.decode("utf-8"))
    except Exception:
        return None
    rep["status"] = "approved"
    rep["reviewed_by"] = f"line:{who}"[:80]
    rep["reviewed_at"] = datetime.now(timezone.utc).isoformat()
    rep["verified_via"] = "line_quick_confirm"
    return json.dumps(rep, ensure_ascii=False)


def confirm_in_hd(cur, doc_id, who):
    """ถ้า lab_sync เอาแถวเข้า hd_lab_docs แล้ว → ติ๊ก verified (ถ้ายัง → lab_sync รอบถัดไปจะเห็น status approved เอง)"""
    cur.execute("""UPDATE hd_lab_docs SET verified_status = 'verified', verified_by = %s, verified_at = NOW()
                   WHERE doc_id = %s AND COALESCE(verified_status, 'pending') <> 'verified'""",
                (f"line:{who}"[:80], doc_id))
    return cur.rowcount


def confirm_quick_items(items, max_items=13):
    """ปุ่ม quick reply '✅ ยืนยันแล็บ ①' สำหรับใบที่มีค่าจากตัวอ่าน (items ของ batch collector)"""
    out = []
    for i in sorted(items, key=lambda x: x.get("n", 0)):
        if i.get("status") != "ok" or not i.get("labvals") or not i.get("doc8"):
            continue
        n = i.get("n")
        out.append({"type": "action", "action": {"type": "message",
                                                  "label": f"✅ ยืนยันแล็บ {_circ(n)}"[:20],
                                                  "text": f"ยืนยันแล็บ {i['doc8']}"}})
        if len(out) >= max_items:
            break
    return out


def _circ(n):
    s = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    return s[n - 1] if 1 <= n <= len(s) else f"({n})"
