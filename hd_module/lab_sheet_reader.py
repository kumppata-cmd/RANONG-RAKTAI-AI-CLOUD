"""📄 Lab Sheet Reader — อ่าน "ใบรายงานผลการตรวจทางห้องปฏิบัติการ โรงพยาบาลระนอง" (26 พ.ย. 2569)

ขั้นที่ 1 ของแผน (hd_module ล้วน · ไม่แตะ lab_extractor.py / lab_sync.py / ทางเดิน Tesseract เดิม):
  Claude Vision + profile ของฟอร์ม รพ.ระนอง → ค่ารายแถว + หัวใบ (HN/AN/ชื่อ/วันที่)
  → ตรวจความสอดคล้อง 6 ชั้น (อ่าน 2 รอบ, TSAT คำนวณ, ช่วงค่า, แถวต่อเนื่อง, HN+ชื่อตรงทะเบียน, คุณภาพรูป)
  → ผลเป็น "ค่าแนะนำ + ธง" ให้คนยืนยัน — โมดูลนี้ไม่บันทึกอะไรเอง

ลักษณะฟอร์มที่เห็นจากตัวอย่างจริง 10 ใบ (6 รูปถ่าย + 4 สแกน PDF):
  • หัวใบ: Request Form (Chemistry/Hematology/Routine Lab) · Request By · Request Date เวลา ·
    Request Number · Visit Type · HN (มีศูนย์นำ เช่น 00038358) · AN · ชื่อผู้ป่วย · Receive Date/Time
  • ตาราง: No. | Parameter | Result | Unit | Reference — เลขแถวต่อเนื่อง 1..N
  • 1 หน้าสแกนอาจมี 2 บล็อก (ครึ่งบน/ครึ่งล่าง) — บล็อกที่สองมักเป็น "หน้าต่อ" (แถว 21–25) ไม่มีหัวใบ
  • Reference ขวาสุดถูกตัดขอบบ่อย ("0.55-1.18", "3.50-5.1(") → ไม่ใช้ reference จากใบ
  • TSAT เขียนด้วยมือใต้ตาราง → คำนวณเองจาก Iron/TIBC แล้วเทียบ
  • มีขีด/วงกลมด้วยปากกาบนค่า (เช่น HCT) → ต้องอ่านตัวเลขที่พิมพ์ ไม่ใช่รอยปากกา
"""
import json
import logging
import re
import threading

logger = logging.getLogger(__name__)

PROFILE_ID = "ranong_hospital_v1"

# ── รายการค่าที่รู้จัก: key → label, aliases (regex, lower), kind, unit ที่คาด, ช่วงที่เป็นไปได้ทางคลินิก ─────
# ช่วงกว้างพอสำหรับผู้ป่วยไตวาย (เช่น PTH 2681, Cr 13.6, ALT 1450 ในตัวอย่างจริง) แต่ตัดเลขอ่านผิดชัด ๆ
A = {}


def _a(key, label, aliases, kind="num", unit="", lo=None, hi=None, group=""):
    A[key] = {"label": label, "aliases": [re.compile(p, re.I) for p in aliases], "kind": kind,
              "unit": unit, "lo": lo, "hi": hi, "group": group}


# iron / bone / vitamin
_a("ferritin", "Ferritin", [r"^ferritin"], unit="ng/ml", lo=1, hi=20000, group="iron")
_a("pth", "PTH intact", [r"^pth", r"parathyroid", r"^i-?pth"], unit="pg/ml", lo=1, hi=6000, group="bone")
_a("iron", "Iron", [r"^iron$", r"^iron\b(?!\s*binding)", r"^serum iron", r"^fe$"], unit="ug/dl", lo=3, hi=600, group="iron")
_a("tibc", "TIBC", [r"^tibc", r"iron binding"], unit="ug/dl", lo=40, hi=800, group="iron")
_a("vitd", "Vitamin D total", [r"vitamin\s*d", r"^25-?oh", r"^vit\.?\s*d"], unit="ng/ml", lo=1, hi=250, group="vitamin")
# serology (qualitative)
_a("hbsag", "HBs Ag", [r"^hbs\s*-?\s*ag", r"^hbsag"], kind="qual", group="sero")
_a("antihbs", "HBs Ab", [r"^hbs\s*-?\s*ab", r"^anti\s*-?\s*hbs", r"^hbsab"], kind="qual", group="sero")
_a("antihbc", "HBc Ab", [r"^hbc\s*-?\s*ab", r"^anti\s*-?\s*hbc", r"^hbcab"], kind="qual", group="sero")
_a("antihcv", "HCV Ab", [r"^hcv\s*-?\s*ab", r"^anti\s*-?\s*hcv", r"^hcvab"], kind="qual", group="sero")
_a("antihiv", "Anti HIV", [r"^anti\s*-?\s*hiv", r"^hiv"], kind="qual", group="sero")
# CBC
_a("wbc", "WBC", [r"^wbc"], unit="10^3/mm^3", lo=0.1, hi=200, group="cbc")
_a("rbc", "RBC", [r"^rbc"], unit="10^6/mm^3", lo=0.5, hi=9, group="cbc")
_a("hb", "Hb", [r"^hgb", r"^hb\b", r"^hemoglobin", r"^haemoglobin"], unit="g/dl", lo=2, hi=25, group="cbc")
_a("hct", "Hct", [r"^hct", r"^hematocrit"], unit="%", lo=6, hi=75, group="cbc")
_a("mcv", "MCV", [r"^mcv"], unit="fl", lo=40, hi=150, group="cbc")
_a("mch", "MCH", [r"^mch\b(?!c)", r"^mch\.?$"], unit="pg", lo=10, hi=50, group="cbc")
_a("mchc", "MCHC", [r"^mchc"], unit="g/dl", lo=20, hi=45, group="cbc")
_a("plt", "Platelet", [r"^plt", r"^platelet"], unit="10^3/mm^3", lo=1, hi=2000, group="cbc")
_a("rdw", "RDW", [r"^rdw"], unit="%", lo=8, hi=40, group="cbc")
_a("neut", "Neutrophil", [r"^neut"], unit="%", lo=0, hi=100, group="cbc")
_a("lymph", "Lymphocyte", [r"^lymph"], unit="%", lo=0, hi=100, group="cbc")
_a("mono", "Monocyte", [r"^mono"], unit="%", lo=0, hi=100, group="cbc")
_a("eos", "Eosinophil", [r"^eos"], unit="%", lo=0, hi=100, group="cbc")
_a("baso", "Basophil", [r"^baso"], unit="%", lo=0, hi=100, group="cbc")
# renal / chem
_a("gfr", "GFR", [r"^e?gfr"], unit="mL/min", lo=0.5, hi=200, group="renal")
_a("creatinine", "Creatinine", [r"^creat", r"^cr\.?$"], unit="mg/dl", lo=0.1, hi=40, group="renal")
_a("bun", "BUN", [r"^bun", r"^urea"], unit="mg/dl", lo=1, hi=300, group="renal")
_a("uric", "Uric acid", [r"^uric"], unit="mg/dl", lo=0.5, hi=25, group="renal")
_a("glucose", "Glucose/FBS", [r"^fbs", r"^glucose", r"^fasting"], unit="mg/dl", lo=15, hi=1200, group="chem")
_a("chol", "Cholesterol", [r"^chol"], unit="mg/dl", lo=40, hi=800, group="lipid")
_a("tg", "Triglyceride", [r"^trig", r"^tg$"], unit="mg/dl", lo=10, hi=3000, group="lipid")
_a("ldl", "LDL", [r"^ldl"], unit="mg/dl", lo=5, hi=500, group="lipid")
_a("hdl", "HDL", [r"^hdl"], unit="mg/dl", lo=5, hi=200, group="lipid")
_a("na", "Sodium", [r"^sodium", r"^na\b"], unit="mmol/L", lo=100, hi=180, group="lyte")
_a("k", "Potassium", [r"^potassium", r"^k\b"], unit="mmol/L", lo=1.5, hi=10, group="lyte")
_a("cl", "Chloride", [r"^chloride", r"^cl\b"], unit="mmol/L", lo=60, hi=140, group="lyte")
_a("hco3", "Bicarbonate", [r"^bicarb", r"^hco3", r"^co2"], unit="mmol/L", lo=5, hi=50, group="lyte")
_a("ca", "Calcium", [r"^calcium", r"^ca\b"], unit="mg/dl", lo=4, hi=18, group="lyte")
_a("phos", "Phosphorus", [r"^phos", r"^po4"], unit="mg/dl", lo=0.5, hi=20, group="lyte")
_a("mg", "Magnesium", [r"^magnesium", r"^mg\b"], unit="mg/dl", lo=0.5, hi=8, group="lyte")
# liver / protein
_a("tp", "Total Protein", [r"^total protein"], unit="g/dl", lo=2, hi=12, group="liver")
_a("albumin", "Albumin", [r"^albumin", r"^alb\b"], unit="g/dl", lo=0.8, hi=6, group="liver")
_a("globulin", "Globulin", [r"^globulin"], unit="gm/dl", lo=0.5, hi=8, group="liver")
_a("alp", "ALP", [r"^alkaline", r"^alp"], unit="U/L", lo=5, hi=3000, group="liver")
_a("ast", "AST", [r"^sgot", r"^ast"], unit="U/L", lo=2, hi=10000, group="liver")
_a("alt", "ALT", [r"^sgpt", r"^alt"], unit="U/L", lo=2, hi=10000, group="liver")
_a("tbili", "Total Bilirubin", [r"^total bili", r"^t\.?\s*bili"], unit="mg/dl", lo=0.05, hi=60, group="liver")
_a("dbili", "Direct Bilirubin", [r"^direct bili", r"^d\.?\s*bili"], unit="mg/dl", lo=0.0, hi=50, group="liver")

# ค่าที่หมอขอเป็นพิเศษ (26 พ.ย. 2569) — ขึ้นก่อนในตาราง/การ์ด
PRIORITY = ("ferritin", "pth", "iron", "tibc", "tsat", "vitd", "hbsag", "antihbs", "antihbc", "antihcv", "antihiv")
# ⚠️ ไม่ออก LINE/การ์ดผู้ป่วย/กลุ่ม ไม่ว่ากรณีใด (กติกาเดิมของระบบ)
NEVER_PUSH = ("antihiv",)

_NEG = re.compile(r"non[\s-]?reactive|negative|\bneg\b|ไม่พบ|ผลลบ|^ลบ$", re.I)
_POS = re.compile(r"reactive|positive|\bpos\b|ผลบวก|^บวก$|^พบ$", re.I)
_NUM = re.compile(r"[-+]?\d+(?:[.,]\d+)?")


def canonical(parameter_text):
    """ชื่อพารามิเตอร์บนใบ → key (None = ไม่รู้จัก เก็บไว้เป็น unknown_rows)"""
    t = (parameter_text or "").strip().lower()
    t = re.sub(r"\s*\((?:elisa|poct|calc\.?)\)\s*", " ", t).strip()
    t = t.replace(";", " ").replace(":", " ").replace(",", " ")
    t = re.sub(r"\s+", " ", t).strip(" .")
    if not t:
        return None
    for key, spec in A.items():
        for rx in spec["aliases"]:
            if rx.search(t):
                return key
    return None


def parse_value(key, result_text):
    """ข้อความช่อง Result → (value, note). ตัวเลข: float · เชิงคุณภาพ: 'positive'/'negative' · อ่านไม่ออก: (None, เหตุผล)"""
    spec = A.get(key)
    s = (result_text or "").strip()
    if not spec or not s:
        return None, "ว่าง"
    if spec["kind"] == "qual":
        if _NEG.search(s):
            return "negative", None
        if _POS.search(s):
            return "positive", None
        return None, f"ไม่ใช่ Negative/Positive: '{s[:20]}'"
    m = _NUM.search(s.replace(" ", ""))
    if not m:
        return None, f"ไม่ใช่ตัวเลข: '{s[:20]}'"
    try:
        v = float(m.group(0).replace(",", "."))
    except ValueError:
        return None, f"ไม่ใช่ตัวเลข: '{s[:20]}'"
    lo, hi = spec["lo"], spec["hi"]
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        return None, f"นอกช่วงที่เป็นไปได้ ({v} ไม่อยู่ใน {lo}–{hi}) — ตรวจกับใบจริง"
    return v, None


def normalize_hn(hn):
    """'00038358' → '38358' · ตัดอักขระที่ไม่ใช่ตัวเลข"""
    d = re.sub(r"\D", "", str(hn or ""))
    return d.lstrip("0") or ("0" if d else "")


def parse_sheet_date(s):
    """'19/11/2026' (ค.ศ. ตามฟอร์ม รพ.ระนอง) หรือ '19/11/2569' (พ.ศ.) → date · None ถ้าอ่านไม่ออก"""
    from datetime import date as _date
    m = re.search(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})", str(s or ""))
    if not m:
        return None
    dd, mm, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if yy < 100:
        yy += 2000
    if yy > 2400:
        yy -= 543
    try:
        return _date(yy, mm, dd)
    except ValueError:
        return None


_TITLES = re.compile(r"^(นาย|นางสาว|นาง|น\.ส\.|ด\.ช\.|ด\.ญ\.|เด็กชาย|เด็กหญิง|mr\.?|mrs\.?|ms\.?|miss)\s*", re.I)


def name_tokens(name):
    """ชื่อบนใบ/ทะเบียน → ชุดคำ (ตัดคำนำหน้า, วงเล็บ, สัญชาติ)"""
    s = re.sub(r"\(.*?\)", " ", str(name or ""))
    s = re.sub(r"\b(เมียนมา|พม่า|ลาว|กัมพูชา|myanmar|thai)\b", " ", s, flags=re.I)
    s = _TITLES.sub("", s.strip())
    toks = [t for t in re.split(r"[\s/,-]+", s.lower()) if len(t) >= 2]
    return set(toks)


def names_match(sheet_name, registry_name):
    """ชื่อบนใบตรงกับชื่อในทะเบียน HN นั้นไหม — ต้องมีคำร่วมอย่างน้อย 1 คำ (ชื่อหรือนามสกุล)"""
    a, b = name_tokens(sheet_name), name_tokens(registry_name)
    if not a or not b:
        return None          # ตัดสินไม่ได้
    if a & b:
        return True
    # ชื่อภาษาอังกฤษสะกดต่างเล็กน้อย: เทียบ 4 ตัวแรก
    return any(x[:4] == y[:4] for x in a for y in b if len(x) >= 4 and len(y) >= 4)


# ── prompt ──────────────────────────────────────────────────────────────────────────────────────
def build_prompt(variant=0):
    """prompt อ่านใบ — variant 1 ใช้สำหรับรอบที่สอง (คนละมุม) เพื่อตรวจความตรงกัน"""
    base = (
        "นี่คือ 'ใบรายงานผลการตรวจทางห้องปฏิบัติการ โรงพยาบาลระนอง' (ภาพถ่ายหรือสแกน) "
        "หน้าหนึ่งอาจมี 2 บล็อก (ครึ่งบน/ครึ่งล่าง) และบล็อกล่างอาจไม่มีหัวใบ (เป็นหน้าต่อ แถว 21-25).\n"
        "ให้ถอดข้อมูลเป็น JSON เท่านั้น ไม่มีคำอธิบาย รูปแบบ:\n"
        '{"blocks":[{"header":{"hn":"","an":"","name":"","request_form":"","request_by":"","request_date":"","request_time":"",'
        '"request_number":"","visit_type":"","receive_date":"","receive_time":""} หรือ null ถ้าบล็อกนี้ไม่มีหัวใบ,'
        '"rows":[{"no":1,"parameter":"Ferritin","result":"226.1","unit":"ng/ml","reference":"","conf":0.98}]}],'
        '"handwritten":["TSAT=27.27%"],"quality":{"blur":false,"cut":false,"note":""}}\n'
        "กติกา:\n"
        "- ลอกตัวพิมพ์ตามที่เห็น ห้ามเดา ห้ามแก้หน่วย ห้ามคำนวณ\n"
        "- conf = ความมั่นใจว่าตัวเลขในช่อง Result อ่านถูกทุกหลักรวมจุดทศนิยม (0-1) เท่านั้น — "
        "คอลัมน์ Reference ขวาสุดมักถูกตัดขอบ ไม่ต้องสนใจ ไม่มีผลต่อ conf · ถ้า Result ชัดให้ conf ≥ 0.9 · ถ้าอ่านไม่ชัดใส่ conf < 0.7 และใส่ค่าที่เห็นดีที่สุด\n"
        "- result ของค่าเชิงคุณภาพคือคำว่า Negative/Positive ตามใบ\n"
        "- เลข no ต้องเป็นเลขลำดับที่พิมพ์ในคอลัมน์ No. ของแถวนั้นจริง ๆ\n"
        "- hn ลอกทุกหลักรวมศูนย์นำ · วันที่ลอกตามใบ (dd/mm/yyyy)\n"
        "- ขีดเส้น/วงกลมด้วยปากกาไม่ใช่ข้อมูล ให้อ่านตัวเลขที่พิมพ์ · ข้อความเขียนมือ (เช่น TSAT=...) ใส่ใน handwritten\n"
        "- quality.cut=true ถ้าหัวใบหรือแถวสุดท้ายถูกตัดออกจากภาพ · blur=true ถ้าตัวเลขเบลอจนไม่แน่ใจหลายแถว\n"
        "- ห้ามใส่ข้อมูลอื่นนอกจาก JSON"
    )
    if variant == 1:
        base += ("\nรอบนี้ให้อ่านทีละแถวจากล่างขึ้นบน แล้วเรียง no จากน้อยไปมากก่อนตอบ "
                 "และตรวจซ้ำว่าจุดทศนิยมของทุกค่าอยู่ตำแหน่งถูกต้อง")
    return base


# ── วิเคราะห์ผล JSON ดิบ → โครงสร้างพร้อมธง ─────────────────────────────────────────────────────
def analyze(raw):
    """raw = JSON จาก Claude (1 รอบ) → dict:
    header, values{key:{value,unit,no,conf,param}}, uncertain[], unknown_rows[], problems[], rows_seen[],
    missing_rows[], computed{}, handwritten{}, quality{}"""
    out = {"profile": PROFILE_ID, "header": None, "values": {}, "uncertain": [], "unknown_rows": [],
           "problems": [], "rows_seen": [], "missing_rows": [], "computed": {}, "handwritten": {},
           "quality": {}, "blocks": 0}
    if not isinstance(raw, dict):
        out["problems"].append("AI ไม่ได้ตอบเป็น JSON")
        return out
    blocks = raw.get("blocks") or []
    out["blocks"] = len(blocks)
    out["quality"] = raw.get("quality") if isinstance(raw.get("quality"), dict) else {}
    # หัวใบ: ใช้บล็อกแรกที่มี header
    headers = [b.get("header") for b in blocks if isinstance(b, dict) and isinstance(b.get("header"), dict)]
    if headers:
        h = {k: str(v or "").strip() for k, v in headers[0].items()}
        h["hn_norm"] = normalize_hn(h.get("hn"))
        out["header"] = h
        d = parse_sheet_date(h.get("request_date") or h.get("receive_date"))
        if d:
            h["lab_date"] = d.isoformat()
            from datetime import date as _date, timedelta as _td
            today = _date.today()
            if d > today + _td(days=1):
                out["problems"].append(f"วันที่บนใบ {d.strftime('%d/%m/%Y')} อยู่ในอนาคต — อ่านปีผิด?")
            elif d < today - _td(days=730):
                out["problems"].append(f"วันที่บนใบ {d.strftime('%d/%m/%Y')} เก่ากว่า 2 ปี — อ่านปีผิด? (ตรวจกับใบ)")
        elif h.get("request_date"):
            out["problems"].append(f"อ่านวันที่ไม่ออก ('{h.get('request_date')[:12]}')")
        # 2 หัวใบใน 1 หน้าที่ HN ต่างกัน = คนละคน → ห้ามรวม
        other = [normalize_hn(x.get("hn")) for x in headers[1:] if normalize_hn(x.get("hn"))]
        if any(o != h["hn_norm"] for o in other):
            out["problems"].append("หน้านี้มีใบของผู้ป่วย 2 คน (HN ต่างกัน) — แยกถ่ายทีละใบ")
    else:
        out["problems"].append("ไม่พบหัวใบ (HN/ชื่อ) — ถ่ายให้เห็นส่วนบนของใบ")
    # แถว
    seen = {}
    for b in blocks:
        for r in (b.get("rows") or []) if isinstance(b, dict) else []:
            if not isinstance(r, dict):
                continue
            no = r.get("no")
            try:
                no = int(no)
            except (TypeError, ValueError):
                no = None
            param = str(r.get("parameter") or "").strip()
            key = canonical(param)
            conf = r.get("conf")
            try:
                conf = float(conf) if conf is not None else 0.9
            except (TypeError, ValueError):
                conf = 0.9
            if no is not None:
                if no in seen and seen[no] != key:
                    out["problems"].append(f"เลขแถว {no} ซ้ำ ({seen[no] or '?'} / {key or param[:12]})")
                seen[no] = key
            if not key:
                out["unknown_rows"].append({"no": no, "parameter": param[:40], "result": str(r.get("result") or "")[:20]})
                continue
            val, note = parse_value(key, str(r.get("result") or ""))
            # หน่วย: ใช้หน่วยมาตรฐานของค่านั้น (ใบจริงบางใบพิมพ์หน่วยเหลื่อมแถว เช่น GFR/Creatinine ใบ 444)
            entry = {"value": val, "unit": A[key]["unit"] or str(r.get("unit") or "").strip(),
                     "unit_sheet": str(r.get("unit") or "").strip()[:12], "no": no, "conf": conf,
                     "param": param[:40], "raw": str(r.get("result") or "")[:20]}
            if val is None:
                out["uncertain"].append({"key": key, "label": A[key]["label"], **entry, "why": note})
            elif conf < 0.7:
                out["uncertain"].append({"key": key, "label": A[key]["label"], **entry, "why": f"AI ไม่มั่นใจ ({int(conf*100)}%)"})
            else:
                if key in out["values"]:
                    out["problems"].append(f"{A[key]['label']} ปรากฏ 2 ครั้ง ({out['values'][key]['value']} / {val})")
                out["values"][key] = entry
    nos = sorted(n for n in seen if n is not None)
    out["rows_seen"] = nos
    if nos:
        full = set(range(nos[0], nos[-1] + 1))
        out["missing_rows"] = sorted(full - set(nos))
        if out["missing_rows"]:
            out["problems"].append("แถวหาย: " + ", ".join(str(x) for x in out["missing_rows"][:8]))
        if nos[0] > 1:
            out["problems"].append(f"เริ่มที่แถว {nos[0]} — แถว 1–{nos[0]-1} อยู่อีกหน้า (ถ่ายหน้าก่อนด้วย)")
    else:
        out["problems"].append("ไม่พบแถวผลตรวจ")
    # เขียนมือ
    for hw in raw.get("handwritten") or []:
        s = str(hw)
        m = re.search(r"tsat\s*[=:]?\s*([\d.]+)", s, re.I)
        if m:
            try:
                out["handwritten"]["tsat"] = float(m.group(1))
            except ValueError:
                pass
    # คำนวณ TSAT และเทียบ
    v = out["values"]
    if "iron" in v and "tibc" in v and v["tibc"]["value"]:
        tsat = round(v["iron"]["value"] / v["tibc"]["value"] * 100, 2)
        out["computed"]["tsat"] = tsat
        hw = out["handwritten"].get("tsat")
        if hw is not None:
            if abs(hw - tsat) <= 0.5:
                out["computed"]["tsat_check"] = "ตรงกับที่เขียนมือ"
            else:
                out["computed"]["tsat_check"] = f"ไม่ตรงกับที่เขียนมือ ({hw}) — ตรวจ Iron/TIBC"
                out["problems"].append(f"TSAT คำนวณ {tsat}% ≠ เขียนมือ {hw}% → Iron หรือ TIBC อาจอ่านผิด")
    # ความสอดคล้องภายในใบ
    if "tbili" in v and "dbili" in v and v["dbili"]["value"] > v["tbili"]["value"] + 0.05:
        out["problems"].append("Direct Bilirubin > Total Bilirubin — อ่านผิดแถวหรือจุดทศนิยม")
    if "hb" in v and "hct" in v:
        ratio = v["hct"]["value"] / v["hb"]["value"] if v["hb"]["value"] else 0
        if not (2.3 <= ratio <= 3.8):
            out["problems"].append(f"Hct/Hb = {ratio:.1f} ผิดปกติ (ควร ~3) — Hb หรือ Hct อาจอ่านผิด")
    if "mchc" in v and "hb" in v and "hct" in v and v["hct"]["value"]:
        calc = v["hb"]["value"] / v["hct"]["value"] * 100
        if abs(calc - v["mchc"]["value"]) > 2.5:
            out["problems"].append(f"MCHC คำนวณ {calc:.1f} ≠ ใบ {v['mchc']['value']} — Hb/Hct/MCHC ตัวใดตัวหนึ่งอ่านผิด")
    q = out["quality"]
    if q.get("blur"):
        out["problems"].append("รูปเบลอ — ถ่ายใหม่ให้ชัด")
    if q.get("cut"):
        out["problems"].append("ใบถูกตัดขอบ — ถ่ายให้เห็นทั้งใบ")
    return out


def merge_passes(p1, p2):
    """อ่าน 2 รอบอิสระ → รับเฉพาะค่าที่ตรงกัน; ไม่ตรง → ย้ายไป uncertain พร้อมทั้งสองค่า"""
    if not p2:
        p1["double_checked"] = False
        return p1
    out = dict(p1)
    out["double_checked"] = True
    def _same(key, a, b):
        if a is None or b is None:
            return False
        if A[key]["kind"] == "qual":
            return a == b
        try:
            return abs(float(a) - float(b)) <= 1e-9
        except (TypeError, ValueError):
            return False

    # ค่าที่รอบนั้น "อ่านได้แต่ไม่มั่นใจ" (parse ผ่าน, conf 0.5–0.7) — ใช้เทียบข้ามรอบได้
    def _low(p):
        return {u["key"]: u for u in p["uncertain"] if u.get("value") is not None and (u.get("conf") or 0) >= 0.5}
    low1, low2 = _low(p1), _low(p2)
    vals, unc = {}, []
    keys = list(dict.fromkeys(list(p1["values"]) + list(p2["values"]) + list(low1) + list(low2)))
    for key in keys:
        e, f = p1["values"].get(key), p2["values"].get(key)
        u1, u2 = low1.get(key), low2.get(key)
        if e and f:
            if _same(key, e["value"], f["value"]):
                vals[key] = e
            else:
                unc.append({"key": key, "label": A[key]["label"], **e, "alt": f["value"],
                            "why": f"2 รอบอ่านไม่ตรงกัน ({e['value']} / {f['value']})"})
        elif e and u2 and _same(key, e["value"], u2["value"]):
            # รอบ 2 เห็นค่าเดียวกันแต่รายงานไม่มั่นใจ (มักเพราะ reference ถูกตัดขอบ) → 2 รอบตรงกันถือว่าผ่าน
            vals[key] = dict(e, agree_low_conf=True)
        elif f and u1 and _same(key, f["value"], u1["value"]):
            vals[key] = dict(f, agree_low_conf=True)
        elif u1 and u2 and _same(key, u1["value"], u2["value"]):
            vals[key] = dict({k: v for k, v in u1.items() if k not in ("why", "label", "key")}, agree_low_conf=True)
        elif e or f:
            src = e or f
            unc.append({"key": key, "label": A[key]["label"], **src,
                        "why": "รอบที่ 2 อ่านไม่ได้/ไม่มั่นใจ" if e else "รอบแรกไม่เห็นค่านี้",
                        **({"alt": (u2 or u1)["value"]} if (u2 or u1) else {})})
        else:
            u = u1 or u2
            unc.append({"key": key, "label": A[key]["label"], **{k: v for k, v in u.items() if k not in ("why", "label", "key")},
                        "why": u.get("why", "AI ไม่มั่นใจ")})
    # ค่าที่ parse ไม่ผ่าน (นอกช่วง/ไม่ใช่ตัวเลข) ของรอบแรก คงไว้
    have = set(vals) | {u["key"] for u in unc}
    for u in p1["uncertain"]:
        if u["key"] not in have:
            unc.append(u)
            have.add(u["key"])
    out["values"] = vals
    out["uncertain"] = unc
    # หัวใบ: วันที่/ชื่อ 2 รอบต้องตรงกัน (ตัวอย่าง 26 พ.ย.: ปี 2026 ถูกอ่านเป็น 2025 1 รอบ)
    h1, h2 = p1.get("header") or {}, p2.get("header") or {}
    probs = list(p1["problems"])
    for fld, label in (("request_date", "วันที่ขอตรวจ"), ("receive_date", "วันที่รับ"), ("name", "ชื่อผู้ป่วย"), ("an", "AN")):
        a, b = (h1.get(fld) or "").strip(), (h2.get(fld) or "").strip()
        if a and b and a != b and not (fld == "name" and names_match(a, b)):
            probs.append(f"{label} 2 รอบอ่านไม่ตรง ({a} / {b}) — ตรวจกับใบ")
            if out.get("header"):
                out["header"] = dict(out["header"], **{fld + "_alt": b})
    out["problems"] = probs
    # HN ต้องตรงกัน
    if h1 and h2 and h1.get("hn_norm") and h2.get("hn_norm") and h1["hn_norm"] != h2["hn_norm"]:
        out["problems"] = out["problems"] + [f"HN อ่าน 2 รอบไม่ตรง ({h1.get('hn')} / {h2.get('hn')}) — ห้ามผูกคนไข้"]
        out["header"] = dict(out["header"] or h1, hn_conflict=True)
    # คำนวณ TSAT ใหม่จากค่าที่ผ่านทั้ง 2 รอบ
    out["computed"] = dict(p1.get("computed") or {})
    if ("iron" not in vals or "tibc" not in vals) and "tsat" in out["computed"]:
        out["computed"]["tsat"] = None
        out["computed"]["tsat_check"] = "Iron/TIBC ไม่ผ่านการตรวจ 2 รอบ"
    return out


# ── เรียก Claude Vision ──────────────────────────────────────────────────────────────────────────
VISION_TIMEOUT_S = 40


def _jpeg(img_bytes, max_side=2000):
    import io
    from PIL import Image, ImageOps
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(img_bytes))).convert("RGB")
    if max(im.size) > max_side:
        im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88)
    return buf.getvalue()


def pdf_pages_to_jpeg(pdf_bytes, max_pages=3, dpi=150):
    """PDF (สแกน/พิมพ์) → รูป JPEG ต่อหน้า (ใช้ PyMuPDF ที่มีอยู่แล้วใน requirements)"""
    out = []
    try:
        import fitz
    except ImportError:
        fitz = None
    if fitz is not None:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            pix = page.get_pixmap(dpi=dpi)
            out.append(_jpeg(pix.tobytes("png")))
        return out
    # สำรอง (เครื่องที่ไม่มี PyMuPDF เช่น CI): poppler pdftoppm
    import subprocess
    import tempfile
    import os
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "in.pdf")
        with open(src, "wb") as fh:
            fh.write(pdf_bytes)
        subprocess.run(["pdftoppm", "-r", str(dpi), "-l", str(max_pages), "-png", src, os.path.join(td, "p")],
                       check=True, capture_output=True, timeout=60)
        for name in sorted(os.listdir(td)):
            if name.startswith("p") and name.endswith(".png"):
                with open(os.path.join(td, name), "rb") as fh:
                    out.append(_jpeg(fh.read()))
    return out


def read_once(img_jpeg, api_key, ai_model_id, variant=0, post_fn=None):
    """เรียก Claude Vision 1 ครั้ง → analyze() หรือ None (ล้ม). ใช้ VISION_SLOTS ร่วมกับ Magic Camera (กัน 502)"""
    import base64
    from . import machine_ocr as mo
    if not api_key:
        return None
    body = json.dumps({
        "model": ai_model_id, "max_tokens": 6000,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.b64encode(img_jpeg).decode()}},
            {"type": "text", "text": build_prompt(variant)},
        ]}],
    }).encode("utf-8")
    if not mo.VISION_SLOTS.acquire(blocking=False):
        raise RuntimeError("busy")
    try:
        if post_fn is None:
            import requests as _rq
            post_fn = lambda b: _rq.post("https://api.anthropic.com/v1/messages",
                                         headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                                                  "content-type": "application/json"},
                                         data=b, timeout=VISION_TIMEOUT_S)
        resp = post_fn(body)
    finally:
        mo.VISION_SLOTS.release()
    if getattr(resp, "status_code", 0) != 200:
        logger.info("lab-sheet claude HTTP %s", getattr(resp, "status_code", "?"))
        return None
    rj = resp.json()
    txt = "".join(x.get("text", "") for x in (rj.get("content") or []) if x.get("type") == "text").strip()
    raw = mo.extract_json(txt)
    if raw is None:
        logger.warning("lab-sheet claude: no JSON (stop=%s, len=%s)", rj.get("stop_reason"), len(txt))
        return None
    return analyze(raw)


def read_sheet(img_bytes, api_key, ai_model_id, double=True, post_fn=None):
    """รูป 1 ใบ → ผลวิเคราะห์ (อ่าน 2 รอบถ้า double) · None = AI ล้มทั้งหมด · raise RuntimeError('busy') = คิวเต็ม"""
    try:
        jpg = _jpeg(img_bytes)
    except Exception:
        jpg = img_bytes
    p1 = read_once(jpg, api_key, ai_model_id, 0, post_fn)
    if p1 is None:
        return None
    p2 = None
    if double:
        try:
            p2 = read_once(jpg, api_key, ai_model_id, 1, post_fn)
        except RuntimeError:
            p2 = None
    return merge_passes(p1, p2)


# ── ผูกคนไข้ ────────────────────────────────────────────────────────────────────────────────────
def match_patient(cur, header):
    """HN บนใบ → ทะเบียน hd_patient_mapping · คืน {patient_id, hn, patient_name, name_match, status}
    status: matched / name_mismatch / not_found / no_hn"""
    hn = (header or {}).get("hn_norm") or ""
    name = (header or {}).get("name") or ""
    if not hn and not name:
        return {"status": "no_hn"}
    rows = []
    if hn:
        # HN บนใบคือ HN ของ รพ.ระนอง (hospital_hn) — ไม่ใช่ HN คลินิก (hn) · เทียบแบบตัดศูนย์นำทั้งสองฝั่ง
        cur.execute("""SELECT id, hn, patient_name, hospital_hn FROM hd_patient_mapping
                       WHERE ltrim(regexp_replace(COALESCE(hospital_hn,''), '\\D', '', 'g'), '0') = %s
                          OR ltrim(regexp_replace(hn, '\\D', '', 'g'), '0') = %s
                       ORDER BY (hospital_hn IS NOT NULL AND hospital_hn <> '') DESC LIMIT 3""", (hn, hn))
        rows = cur.fetchall() or []
    if rows:
        pid, reg_hn, reg_name, reg_hosp = rows[0]
        nm = names_match(name, reg_name)
        return {"status": "matched" if nm is not False else "name_mismatch", "patient_id": pid, "hn": reg_hn,
                "hospital_hn": reg_hosp, "patient_name": reg_name, "name_match": nm, "by": "hospital_hn" if reg_hosp else "hn"}
    # ไม่เจอ HN → ลองชื่อ (ทะเบียนคลินิกอาจยังไม่กรอก hospital_hn) — ต้องให้คนยืนยันก่อนผูก
    toks = [t for t in name_tokens(name) if len(t) >= 3]
    if toks:
        cur.execute("SELECT id, hn, patient_name, hospital_hn FROM hd_patient_mapping WHERE "
                    + " OR ".join(["patient_name ILIKE %s"] * len(toks)) + " LIMIT 5",
                    tuple("%" + t + "%" for t in toks))
        cands = [r for r in (cur.fetchall() or []) if names_match(name, r[2])]
        if len(cands) == 1:
            pid, reg_hn, reg_name, reg_hosp = cands[0]
            return {"status": "matched_by_name", "patient_id": pid, "hn": reg_hn, "hospital_hn": reg_hosp,
                    "patient_name": reg_name, "name_match": True, "by": "name", "sheet_hn": hn,
                    "hint": "HN รพ. บนใบยังไม่อยู่ในทะเบียน — ยืนยันแล้วระบบจะจำ hospital_hn ให้ (ขั้น 2)"}
        if len(cands) > 1:
            return {"status": "ambiguous_name", "hn": hn, "candidates": [{"hn": r[1], "patient_name": r[2]} for r in cands]}
    return {"status": "not_found", "hn": hn}


def summary_lines(res, patient=None):
    """ข้อความสรุปสั้นสำหรับการ์ด/ทดสอบ — ไม่รวม Anti HIV (NEVER_PUSH)"""
    lines = []
    h = res.get("header") or {}
    if h:
        lines.append(f"HN {h.get('hn_norm') or h.get('hn') or '?'} · {h.get('name','')[:30]} · {h.get('request_form','')} {h.get('request_date','')}")
    if patient:
        st = patient.get("status")
        lines.append({"matched": "✅ ตรงทะเบียน: " + str(patient.get("patient_name", "")) + f" (HN คลินิก {patient.get('hn','')})",
                      "matched_by_name": "🟡 พบจากชื่อ (HN รพ. ยังไม่อยู่ในทะเบียน): " + str(patient.get("patient_name", "")) + f" (HN คลินิก {patient.get('hn','')})",
                      "ambiguous_name": "⚠️ ชื่อซ้ำหลายคนในทะเบียน — ต้องเลือกเอง",
                      "name_mismatch": "⚠️ HN พบในทะเบียนแต่ชื่อไม่ตรง: " + str(patient.get("patient_name", "")),
                      "not_found": "❌ HN รพ. และชื่อ ไม่อยู่ในทะเบียนคลินิก", "no_hn": "❌ อ่าน HN ไม่ได้"}.get(st, st or ""))
    v = res.get("values") or {}
    order = [k for k in PRIORITY if k in v] + [k for k in v if k not in PRIORITY]
    for k in order:
        if k in NEVER_PUSH:
            continue
        e = v[k]
        lines.append(f"{A[k]['label']} {e['value']} {e.get('unit','')}".strip())
    if res.get("computed", {}).get("tsat") is not None:
        lines.append(f"TSAT (คำนวณ) {res['computed']['tsat']}% · {res['computed'].get('tsat_check','')}".strip(" ·"))
    for u in res.get("uncertain") or []:
        if u["key"] in NEVER_PUSH:
            continue
        lines.append(f"❓ {u['label']}: {u.get('raw') or u.get('value')} — {u.get('why','')}")
    for p in res.get("problems") or []:
        lines.append("⚠️ " + p)
    return lines


# ── ขั้น 2: ฟอร์ม Lab ของ HD + ตัวอย่างเฉลย AI vs พยาบาล ──────────────────────────────────────
# key ของตัวอ่าน → ชื่อฟิลด์ที่ POST /patients/hn/<hn>/labs รับ (คอลัมน์ hd_lab_results)
FORM_FIELDS = {
    "bun": "bun", "creatinine": "creatinine", "k": "potassium", "na": "sodium", "ca": "calcium", "phos": "phosphate",
    "albumin": "albumin", "pth": "ipth", "hb": "hemoglobin", "hct": "hematocrit", "ferritin": "ferritin",
    "iron": "iron", "tibc": "tibc", "wbc": "wbc", "plt": "platelet", "uric": "uric_acid", "ast": "ast", "alt": "alt",
    "hbsag": "hepatitis_b", "antihcv": "hepatitis_c", "antihiv": "hiv",
    "vitd": "vitamin_d", "antihbs": "anti_hbs", "antihbc": "anti_hbc", "hco3": "hco3", "glucose": "glucose",
}
# ค่าที่ต้องตรงภายในเท่าไรจึงนับว่า "ตรง" (ฟอร์มปัดทศนิยม เช่น Ferritin step 1)
SAMPLE_TOL = {"ferritin": 0.6, "ipth": 0.6, "platelet": 0.6, "tsat": 0.06}


def new_read_id():
    import secrets
    return "LS" + secrets.token_hex(4)


def form_payload(res):
    """ผลวิเคราะห์ → {field: value} สำหรับเติมฟอร์ม (เฉพาะค่าที่ผ่าน) + tsat คำนวณ"""
    out = {}
    for key, e in (res.get("values") or {}).items():
        f = FORM_FIELDS.get(key)
        if f:
            out[f] = e["value"]
    if (res.get("computed") or {}).get("tsat") is not None:
        out["tsat"] = res["computed"]["tsat"]
    return out


def outcome_of(field, ai_value, nurse_value, uncertain=False):
    """match / mismatch / blank (พยาบาลเว้นว่าง) / manual (AI ไม่แน่ใจ พยาบาลกรอกเอง) / unused"""
    if nurse_value in (None, ""):
        return "blank" if ai_value not in (None, "") else "unused"
    if ai_value in (None, ""):
        return "manual" if uncertain else "unused"
    if isinstance(ai_value, str) and not _NUM.fullmatch(str(ai_value).strip() or "x"):
        return "match" if str(ai_value).strip().lower() == str(nurse_value).strip().lower() else "mismatch"
    try:
        a, n = float(ai_value), float(nurse_value)
    except (TypeError, ValueError):
        return "match" if str(ai_value) == str(nurse_value) else "mismatch"
    return "match" if abs(a - n) <= SAMPLE_TOL.get(field, 0.06) else "mismatch"


def record_samples(cur, read_id, ai_values, uncertain_fields, saved, nurse_id=None):
    """บันทึกตัวอย่างเฉลยต่อฟิลด์ (ไม่มี HN/ชื่อ) → สรุป {match, mismatch, blank, manual}"""
    stats = {"match": 0, "mismatch": 0, "blank": 0, "manual": 0}
    fields = set(ai_values or {}) | set(uncertain_fields or [])
    for field in sorted(fields):
        if field not in set(FORM_FIELDS.values()) | {"tsat"}:
            continue
        ai = (ai_values or {}).get(field)
        unc = field in (uncertain_fields or [])
        nv = saved.get(field)
        oc = outcome_of(field, ai, nv, unc)
        if oc == "unused":
            continue
        stats[oc] = stats.get(oc, 0) + 1
        cur.execute("""INSERT INTO hd_lab_sheet_samples (read_id, field, ai_value, ai_uncertain, nurse_value, outcome, nurse_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (str(read_id)[:40], field, None if ai is None else str(ai)[:30], unc,
                     None if nv in (None, "") else str(nv)[:30], oc, nurse_id))
    return stats


def accuracy_report(cur, days=30):
    cur.execute("""SELECT field, outcome, COUNT(*) FROM hd_lab_sheet_samples
                   WHERE created_at >= NOW() - (%s || ' days')::interval GROUP BY field, outcome""", (str(int(days)),))
    per = {}
    for field, oc, n in cur.fetchall() or []:
        per.setdefault(field, {"match": 0, "mismatch": 0, "blank": 0, "manual": 0})
        per[field][oc] = per[field].get(oc, 0) + int(n)
    cur.execute("SELECT COUNT(DISTINCT read_id) FROM hd_lab_sheet_samples WHERE created_at >= NOW() - (%s || ' days')::interval",
                (str(int(days)),))
    sheets = (cur.fetchone() or [0])[0]
    tot = {"match": 0, "mismatch": 0, "blank": 0, "manual": 0}
    for v in per.values():
        for k in tot:
            tot[k] += v.get(k, 0)
    return {"days": days, "sheets": sheets, "fields": per, "total": tot,
            "gate_ok": sheets >= 50 and tot["mismatch"] == 0,
            "gate": "ไปขั้น 3 (LINE OA) ได้เมื่อ ≥50 ใบ และผิด 0"}
