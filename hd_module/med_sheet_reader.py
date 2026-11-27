"""💊📄 Med Sheet Reader — อ่านใบยา 2 ฟอร์มแบบรู้โครง (ขั้น 1–2, 28 พ.ย. 2569 แพทย์อนุมัติ)

ฟอร์ม A  ใบตรวจรักษาผู้ป่วย รพ.ระนอง (พิมพ์): HN รพ. 8 หลัก, ชื่อ, วันที่, ประวัติการแพ้ยา, Diag,
          รายการยา "ชื่อ ความแรง #จำนวน" + รหัสวิธีใช้ "11pt (1 เม็ด*1pc)" · "#0 off" = หยุดยา
ฟอร์ม B  Medication Record หน่วยไตเทียมระนอง รักษ์ไต (ตาราง): Medication/dose/Unit/Frequency a×b/Route/Regimen
          + ลายมือ ("hold วันฟอก", "อังคาร ศุกร์")

หลักเดียวกับ Lab Sheet Reader: อ่าน 2 รอบอิสระ → เทียบรายการต่อรายการ → ตรงกันจึงมั่นใจ ·
พจนานุกรมยาของหน่วย normalize ชื่อ/แก้สะกด/ทักหน่วยผิด · ถอดรหัส pt/at/ht เป็นวิธีกินภาษาไทย ·
ผลลัพธ์ = "ร่าง" (ai_draft) ให้แพทย์ยืนยันในกล่องรอยืนยัน — ไม่มีรายการยาเปลี่ยนเอง
"""
from __future__ import annotations

import difflib
import json
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

VISION_TIMEOUT_S = 45

# ── พจนานุกรมยาของหน่วย: canonical → (aliases, class, unit ที่ถูก, ช่วงขนาดต่อครั้งที่สมเหตุสมผล) ──
FORMULARY = {
    "epoetin alfa": (("epiao", "epo", "eprex", "hema-plus", "hemaplus", "hemax", "erythropoietin"), "esa", "unit", (1000, 12000)),
    "iron sucrose": (("venofer", "iv iron", "iron iv"), "iron_iv", "mg", (50, 200)),
    "ferrous fumarate": (("ferrous", "fbc", "ferrous fumarate 200"), "iron_oral", "mg", (100, 600)),
    "calcitriol": (("vitamin d calcitriol", "calcitriol (d3)", "d3", "rocaltrol"), "active_vitd", "mcg", (0.25, 2)),
    "ergocalciferol": (("calciferol", "calciferol (d2)", "d2", "vitamin d2"), "vitd", "unit", (20000, 60000)),
    "calcium carbonate": (("caco3", "calcium carbonate 1500", "calcium mgso4", "calcium"), "binder", "mg", (500, 4500)),
    "sodium bicarbonate": (("nahco3", "sodamint", "sodium bicarb"), "alkali", "mg", (300, 1800)),
    "folic acid": (("folic", "folate"), "vitamin", "mg", (1, 15)),
    "atorvastatin": (("atrovastatin", "lipitor", "atorvastatin 40"), "statin", "mg", (10, 80)),
    "simvastatin": (("zocor",), "statin", "mg", (10, 40)),
    "aspirin": (("aspirin 81", "asa", "aspirin"), "antiplatelet", "mg", (81, 325)),
    "clopidogrel": (("plavix", "clopidogrel (gpo)"), "antiplatelet", "mg", (75, 75)),
    "omeprazole": (("losec", "omeprazole 20"), "ppi", "mg", (20, 40)),
    "losartan": (("cozaar", "100 losartan"), "arb", "mg", (25, 100)),
    "manidipine": (("madiplot",), "ccb", "mg", (10, 40)),
    "amlodipine": (("norvasc",), "ccb", "mg", (5, 10)),
    "hydralazine": (("hydralazine", "apresoline"), "vasodilator", "mg", (25, 100)),
    "carvedilol": (("dilatrend",), "beta_blocker", "mg", (6.25, 25)),
    "metoprolol": (("betaloc",), "beta_blocker", "mg", (25, 200)),
    "furosemide": (("lasix",), "loop", "mg", (40, 1000)),
    "isosorbide dinitrate": (("isordil", "sl isosorbide", "isosorbide sublingual"), "nitrate", "mg", (5, 20)),
    "isosorbide mononitrate": (("imdur", "isosorbide-5 mononitrate", "ismo"), "nitrate", "mg", (20, 60)),
    "lorazepam": (("ativan",), "benzo", "mg", (0.5, 2)),
    "gabapentin": (("neurontin",), "neuropathic", "mg", (100, 900)),
    "senna": (("senokot", "senna (ใบมะขามแขก)", "senna leaf"), "laxative", "tab", (1, 4)),
    "lactulose": (("duphalac",), "laxative", "ml", (10, 60)),
    "urea cream": (("urea 10%", "urea cream 10 %"), "topical", "%", (10, 20)),
    "nss": (("0.9% nss", "normal saline", "sodium chloride 0.9%"), "diluent", "ml", (100, 1000)),
    "cinacalcet": (("sensipar", "mimpara"), "calcimimetic", "mg", (25, 90)),
    "sevelamer": (("renvela", "renagel"), "binder", "mg", (800, 2400)),
    "lanthanum": (("fosrenol",), "binder", "mg", (500, 1000)),
    "heparin": (("heparin",), "anticoag", "unit", (1000, 10000)),
    "insulin": (("mixtard", "novomix", "humulin", "insulin"), "insulin", "unit", (2, 60)),
}
_ALIAS_INDEX = {}
for _canon, (_aliases, _cls, _unit, _rng) in FORMULARY.items():
    _ALIAS_INDEX[_canon] = _canon
    for _a in _aliases:
        _ALIAS_INDEX[_a.lower()] = _canon

CLASS_TH = {"esa": "ยากระตุ้นเม็ดเลือด", "iron_iv": "เหล็ก IV", "iron_oral": "เหล็กกิน", "active_vitd": "วิตามินดีแอคทีฟ",
            "vitd": "วิตามินดี", "binder": "จับฟอสเฟต", "alkali": "โซเดียมไบคาร์บอเนต", "statin": "ลดไขมัน",
            "antiplatelet": "ต้านเกล็ดเลือด", "ppi": "ลดกรด", "arb": "ลดความดัน (ARB)", "ccb": "ลดความดัน (CCB)",
            "vasodilator": "ขยายหลอดเลือด", "beta_blocker": "ลดความดัน (beta-blocker)", "loop": "ขับปัสสาวะ",
            "nitrate": "ไนเตรต", "benzo": "ยานอนหลับ", "neuropathic": "ปลายประสาท", "laxative": "ระบาย",
            "topical": "ยาทา", "diluent": "สารน้ำผสมยา", "calcimimetic": "ลด PTH", "anticoag": "กันเลือดแข็ง", "insulin": "อินซูลิน"}

BP_CLASSES = ("arb", "ccb", "vasodilator", "beta_blocker", "nitrate")

# รหัสวิธีใช้ของ รพ.ระนอง: หลักแรก = เม็ด/ครั้ง, หลักสอง = ครั้ง/วัน, ตัวอักษร = เวลา
_SIG_RE = re.compile(r"^\s*(\d)(\d)\s*(pt|at|ht|hs|pc|ac|hj|h)?\s*$", re.I)
SIG_TIMING = {"pt": "หลังอาหาร", "pc": "หลังอาหาร", "at": "ก่อนอาหาร", "ac": "ก่อนอาหาร", "ht": "ก่อนนอน", "hs": "ก่อนนอน", "hj": "ก่อนนอน", "h": ""}
FREQ_TH = {1: "วันละ 1 ครั้ง", 2: "วันละ 2 ครั้ง", 3: "วันละ 3 ครั้ง", 4: "วันละ 4 ครั้ง"}


def parse_sig_code(code: str | None) -> dict | None:
    """'11pt' → {"tabs": 1, "times": 1, "timing": "หลังอาหาร", "text": "1 เม็ด วันละ 1 ครั้ง หลังอาหาร"}"""
    if not code:
        return None
    m = _SIG_RE.match(str(code))
    if not m:
        return None
    tabs, times, t = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if tabs == 0 or times == 0:
        return None
    timing = SIG_TIMING.get(t, "")
    text = f"{tabs} เม็ด {FREQ_TH.get(times, f'วันละ {times} ครั้ง')}" + (f" {timing}" if timing else "")
    return {"tabs": tabs, "times": times, "timing": timing, "text": text}


def _f(v):
    try:
        return float(str(v).replace(",", "")) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def canonical_drug(name: str | None) -> tuple[str | None, bool]:
    """→ (canonical, spelled_wrong) · ไม่รู้จัก → (None, False)"""
    if not name:
        return None, False
    n = re.sub(r"\s+", " ", str(name).lower()).strip()
    n = re.sub(r"\(.*?\)", lambda m: m.group(0) if "d2" in m.group(0) or "d3" in m.group(0) or "gpo" in m.group(0) else " ", n).strip()
    n_nodose = re.sub(r"\b\d+(\.\d+)?\s*(mg|mcg|µg|ug|u|unit|iu|ml|%)\b.*$", "", n).strip()
    for cand in (n, n_nodose):
        if cand in _ALIAS_INDEX:
            return _ALIAS_INDEX[cand], False
    # เริ่มต้นด้วย alias
    for alias, canon in sorted(_ALIAS_INDEX.items(), key=lambda x: -len(x[0])):
        if n_nodose.startswith(alias) or n.startswith(alias):
            return canon, False
    # fuzzy (สะกดผิด เช่น atrovastatin)
    best = difflib.get_close_matches(n_nodose.split(" ")[0], list(_ALIAS_INDEX.keys()), n=1, cutoff=0.82)
    if best:
        return _ALIAS_INDEX[best[0]], True
    return None, False


def _strength_from_name(name: str) -> tuple[float | None, str | None]:
    """'Calciferol (D2) 20,000 u.' → (20000, 'unit') · เลือก mg/mcg/unit/ml ก่อน % (เช่น '0.9% NSS 100 ML' → 100 ml)"""
    txt = str(name or "")
    m = re.search(r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(mg|mcg|µg|ug|unit|u|iu|ml)\b", txt, re.I) or \
        re.search(r"(\d+(?:\.\d+)?)\s*(%)", txt)
    if not m:
        return None, None
    unit = m.group(2).lower().replace("µg", "mcg").replace("ug", "mcg").replace("iu", "unit")
    unit = "unit" if unit == "u" else unit
    return float(m.group(1).replace(",", "")), unit


# ── prompt ──
def build_prompt(variant: int = 0) -> str:
    order = "อ่านตารางจากแถวล่างสุดขึ้นบน แล้วค่อยเรียงกลับเป็นลำดับปกติในคำตอบ" if variant == 1 else "อ่านตารางจากบนลงล่างทีละแถว"
    return (
        "คุณเป็นเภสัชกรผู้ช่วยอ่านใบยาของผู้ป่วยฟอกไต รูปนี้เป็น 1 ใน 2 ฟอร์ม:\n"
        "A) 'ใบตรวจรักษาผู้ป่วย โรงพยาบาลระนอง' — หัวกระดาษมี HN (8 หลัก), ชื่อ, วันที่, 'ประวัติการแพ้ยา', Diag Text; "
        "รายการสั่งยาเป็นข้อ ๆ: 'ชื่อยา ความแรง #จำนวน' และบรรทัดถัดไปเป็นรหัสวิธีใช้ เช่น '11pt (1 เม็ด*1pc)' '31ht' '12at' "
        "— '#0' หรือคำว่า 'off' แปลว่าหยุดยา; ยา IV จะมีข้อความตารางเช่น '100 mg iv ทุกวันที่ 15 และ 30'\n"
        "B) 'Medication Record หน่วยไตเทียมระนอง รักษ์ไต' — ตาราง ลำดับ | Medication | dose | Unit | Frequency (a x b) | Route | Regimen "
        "และอาจมีลายมือเขียนทับ เช่น 'hold วันฟอก' หรือวันในสัปดาห์\n\n"
        f"{order} ดึงทุกรายการที่เห็น แม้จางหรือเอียง อ่านไม่ออกบางช่องให้เว้น \"\" ห้ามเดาชื่อยาที่ไม่มีในรูป\n"
        "ตอบเป็น JSON เท่านั้น:\n"
        '{"form":"hospital|clinic|other","header":{"hn":"","name":"","date":"ตามที่พิมพ์","allergies":["..."],"diagnosis":""},'
        '"items":[{"no":1,"name":"ชื่อยาตามที่พิมพ์","strength":"เช่น 40 mg","qty":"เช่น #90 (ฟอร์ม A)","sig_code":"เช่น 11pt (ฟอร์ม A)",'
        '"sig_text":"ข้อความวิธีใช้ที่พิมพ์/เขียน","dose":"ตัวเลข (ฟอร์ม B)","unit":"mg|mcg|Unit|ml (ฟอร์ม B)","freq_a":"a","freq_b":"b",'
        '"route":"Oral|IV|SC|SL|ทา","regimen":"เช่น p.c./hs./ทุก HD/q 1week","handwritten":"ลายมือที่เขียนทับ ถ้ามี","status":"active|off|hold","conf":0.0-1.0}],'
        '"notes":"ปัญหาของรูป เช่น เบลอ/ตัดขอบ/ลายมืออ่านยาก"}\n'
        "conf = ความมั่นใจว่าอ่านชื่อยา+ขนาด+วิธีใช้ถูก · ห้ามใส่เลขบัตรประชาชน ที่อยู่ เบอร์โทร"
    )


# ── analyze 1 pass ──
def analyze(raw: dict) -> dict:
    items_out, flags = [], []
    header = dict((raw or {}).get("header") or {})
    hn_digits = re.sub(r"\D", "", str(header.get("hn") or ""))
    header["hn_norm"] = hn_digits.lstrip("0") if hn_digits else ""
    header["allergies"] = [str(a).strip() for a in (header.get("allergies") or []) if str(a).strip() and str(a).strip().lower() not in ("ไม่ทราบ", "unknown", "-", "none", "ไม่มี")]
    form = str((raw or {}).get("form") or "other").lower()
    for i, it in enumerate((raw or {}).get("items") or []):
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        if not name:
            continue
        canon, misspelled = canonical_drug(name)
        s_val, s_unit = _strength_from_name(f"{name} {it.get('strength') or ''}")
        dose_v = _f(it.get("dose"))
        unit = (str(it.get("unit") or s_unit or "")).strip().lower().replace("units", "unit").replace("u", "unit") if (it.get("unit") or s_unit) else ""
        unit = {"unitnit": "unit", "iunit": "unit"}.get(unit, unit)
        sig = parse_sig_code(it.get("sig_code"))
        status = str(it.get("status") or "active").lower()
        qty = str(it.get("qty") or "")
        hand = str(it.get("handwritten") or "").strip()
        if re.search(r"#\s*0\b", qty) or re.search(r"\boff\b", str(it.get("sig_text") or ""), re.I) or re.search(r"\boff\b", hand, re.I):
            status = "off"
        if re.search(r"hold", hand, re.I) or re.search(r"\bhold\b", str(it.get("regimen") or ""), re.I):
            status = "hold"
        if status not in ("active", "off", "hold"):
            status = "active"
        # ขนาดต่อครั้ง / ความถี่ (ฟอร์ม B: freq_a × freq_b ; ฟอร์ม A: sig code)
        fa, fb = _f(it.get("freq_a")), _f(it.get("freq_b"))
        per_dose = dose_v if dose_v is not None else s_val
        if sig and s_val:
            per_dose = s_val * sig["tabs"]
        times = int(fb) if fb else (sig["times"] if sig else None)
        if fa and dose_v is not None and fa != 1 and form == "clinic":
            per_dose = dose_v * fa      # ฟอร์ม B: dose เป็นต่อเม็ด, a = จำนวนต่อครั้ง
        route = str(it.get("route") or "").strip()
        route = {"oral": "PO", "po": "PO", "iv": "IV", "sc": "SC", "sl": "SL", "ทา": "Topical", "topical": "Topical"}.get(route.lower(), route or "PO")
        regimen = str(it.get("regimen") or "").strip()
        if sig:
            freq_text = sig["text"]
        elif route in ("IV", "SC") and (fa or fb):
            # ฟอร์ม B ยาฉีด: "1 × 2" + regimen (ทุก HD / q 1week) — ไม่แปลเป็น "วันละ"
            freq_text = " ".join(x for x in (f"{fa:g} × {fb:g}" if (fa and fb) else "", regimen) if x).strip()
        else:
            tablet_like = unit in ("mg", "mcg", "tab", "")
            freq_text = " ".join(x for x in (
                (f"{fa:g} เม็ด" if tablet_like else f"ครั้งละ {fa:g} {unit}") if fa else "",
                FREQ_TH.get(int(fb), f"วันละ {fb:g} ครั้ง") if fb else "", regimen) if x).strip() or str(it.get("sig_text") or "").strip()
        item_flags = []
        if canon is None:
            item_flags.append("unknown_drug")
        if misspelled:
            item_flags.append("misspelled")
        if canon:
            _, cls, ok_unit, rng = FORMULARY[canon]
            if unit and ok_unit and unit != ok_unit and ok_unit not in ("tab", "%"):
                item_flags.append(f"unit_mismatch:{unit}→{ok_unit}")
                unit = ok_unit          # ใช้หน่วยที่ถูกในร่าง (บันทึกที่ผิดไว้ใน flag/โน้ต)
            if per_dose is not None and rng and ok_unit not in ("tab", "%") and (per_dose < rng[0] * 0.5 or per_dose > rng[1] * 1.5):
                item_flags.append(f"dose_out_of_range:{per_dose:g}{unit}")
        if per_dose in (0, 0.0):
            item_flags.append("zero_dose")
        try:
            conf = max(0.0, min(float(it.get("conf") or 0.0), 1.0))
        except (TypeError, ValueError):
            conf = 0.0
        if item_flags and conf > 0.7:
            conf = 0.7
        items_out.append({
            "no": it.get("no") or (i + 1), "name_raw": name, "drug": canon or name.strip(), "canonical": canon,
            "class": FORMULARY[canon][1] if canon else None, "strength": s_val, "unit": unit or (FORMULARY[canon][2] if canon else ""),
            "per_dose": per_dose, "times_per_day": times, "frequency": freq_text, "route": route,
            "regimen": regimen, "sig_code": str(it.get("sig_code") or "").strip(),
            "sig_text": str(it.get("sig_text") or "").strip(), "handwritten": hand, "status": status, "qty": qty,
            "conf": conf, "flags": item_flags,
        })
    seen = {}
    for it in items_out:
        k = it["canonical"] or it["drug"].lower()
        if k in seen:
            it["flags"].append("duplicate")
        seen[k] = True
    if not items_out:
        flags.append("no_items")
    if form == "other":
        flags.append("unknown_form")
    return {"form": form, "header": header, "items": items_out, "flags": flags, "notes": str((raw or {}).get("notes") or "")}


# ── merge 2 passes ──
def _key(it):
    return it["canonical"] or re.sub(r"\s+", " ", it["drug"].lower())


def merge_passes(p1: dict, p2: dict | None) -> dict:
    if not p2:
        res = dict(p1)
        res["passes"] = 1
        res["uncertain"] = [it for it in res["items"] if it["conf"] < 0.7]
        res["items"] = [it for it in res["items"] if it["conf"] >= 0.7]
        return res
    b = {_key(it): it for it in p2["items"]}
    agreed, uncertain = [], []
    for it in p1["items"]:
        o = b.pop(_key(it), None)
        if not o:
            it = dict(it, flags=it["flags"] + ["only_in_pass1"], conf=min(it["conf"], 0.5))
            uncertain.append(it)
            continue
        same_dose = (it["per_dose"] is None or o["per_dose"] is None or abs((it["per_dose"] or 0) - (o["per_dose"] or 0)) < 1e-6)
        same_freq = (it["times_per_day"] is None or o["times_per_day"] is None or it["times_per_day"] == o["times_per_day"])
        same_status = it["status"] == o["status"]
        if same_dose and same_freq and same_status:
            it = dict(it, conf=max(it["conf"], o["conf"]), handwritten=it["handwritten"] or o["handwritten"])
            if it["conf"] < 0.7 and not it["flags"]:
                it["flags"] = it["flags"] + ["agree_low_conf"]
            agreed.append(it)
        else:
            why = []
            if not same_dose:
                why.append(f"ขนาดไม่ตรง ({it['per_dose']} vs {o['per_dose']})")
            if not same_freq:
                why.append(f"ความถี่ไม่ตรง ({it['times_per_day']} vs {o['times_per_day']})")
            if not same_status:
                why.append(f"สถานะไม่ตรง ({it['status']} vs {o['status']})")
            uncertain.append(dict(it, flags=it["flags"] + ["pass_conflict:" + "; ".join(why)], conf=min(it["conf"], 0.5)))
    for o in b.values():
        uncertain.append(dict(o, flags=o["flags"] + ["only_in_pass2"], conf=min(o["conf"], 0.5)))
    h1, h2 = p1["header"], p2["header"]
    header = dict(h1)
    hflags = []
    if h1.get("hn_norm") and h2.get("hn_norm") and h1["hn_norm"] != h2["hn_norm"]:
        hflags.append("hn_conflict")
        header["hn_norm"] = ""
    if not h1.get("allergies") and h2.get("allergies"):
        header["allergies"] = h2["allergies"]
    form = p1["form"] if p1["form"] == p2["form"] else "other"
    return {"form": form, "header": header, "items": agreed, "uncertain": uncertain, "passes": 2,
            "flags": sorted(set(p1["flags"]) | set(p2["flags"]) | set(hflags)), "notes": p1.get("notes") or p2.get("notes") or ""}


# ── vision ──
def read_once(img_jpeg: bytes, api_key: str, ai_model_id: str, variant: int = 0, post_fn=None) -> dict | None:
    import base64
    from . import machine_ocr as mo
    if not api_key:
        return None
    body = json.dumps({"model": ai_model_id, "max_tokens": 6000,
                       "messages": [{"role": "user", "content": [
                           {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(img_jpeg).decode()}},
                           {"type": "text", "text": build_prompt(variant)}]}]}).encode("utf-8")
    if not mo.VISION_SLOTS.acquire(blocking=False):
        raise RuntimeError("busy")
    try:
        if post_fn is None:
            import requests as _rq
            post_fn = lambda b: _rq.post("https://api.anthropic.com/v1/messages",
                                         headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                                         data=b, timeout=VISION_TIMEOUT_S)
        resp = post_fn(body)
    finally:
        mo.VISION_SLOTS.release()
    if getattr(resp, "status_code", 0) != 200:
        logger.info("med-sheet claude HTTP %s", getattr(resp, "status_code", "?"))
        return None
    rj = resp.json()
    txt = "".join(x.get("text", "") for x in (rj.get("content") or []) if x.get("type") == "text").strip()
    raw = mo.extract_json(txt)
    if not isinstance(raw, dict):
        logger.warning("med-sheet claude: no JSON (stop=%s, len=%s)", rj.get("stop_reason"), len(txt))
        return None
    return analyze(raw)


def read_sheet(img_bytes: bytes, api_key: str, ai_model_id: str, double: bool = True, post_fn=None) -> dict | None:
    from .lab_sheet_reader import _jpeg
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
    res = merge_passes(p1, p2)
    res["read_id"] = new_read_id()
    return res


def new_read_id() -> str:
    return secrets.token_hex(8)


# ── drafts for the existing reconcile flow ──
def to_drafts(res: dict, include_uncertain: bool = False) -> list[dict]:
    """รายการสำหรับ INSERT hd_medication_orders (ai_draft) — ใช้ reconcile/apply เดิมในกล่องรอยืนยัน"""
    out = []
    pool = list(res.get("items") or []) + (list(res.get("uncertain") or []) if include_uncertain else [])
    for it in pool:
        if it["status"] == "off" and not it.get("canonical"):
            continue
        name = it["canonical"].title() if it.get("canonical") else it["name_raw"]
        strength = f"{it['strength']:g} {it['unit']}" if it.get("strength") else ""
        dose = (f"{it['per_dose']:g} {it['unit']}".strip() if it.get("per_dose") is not None else "")
        notes = []
        if it["status"] == "off":
            notes.append("ใบยาระบุหยุด (off/#0)")
        if it["status"] == "hold":
            notes.append("ลายมือ: " + (it["handwritten"] or "hold"))
        elif it.get("handwritten"):
            notes.append("ลายมือ: " + it["handwritten"])
        if it.get("regimen"):
            notes.append(it["regimen"])
        for f in it.get("flags") or []:
            if f.startswith("unit_mismatch"):
                notes.append("⚠️ หน่วยบนใบน่าจะผิด " + f.split(":", 1)[1])
            elif f.startswith("dose_out_of_range"):
                notes.append("⚠️ ขนาดผิดปกติ โปรดตรวจ")
            elif f == "misspelled":
                notes.append(f"สะกดบนใบ: {it['name_raw']}")
            elif f == "unknown_drug":
                notes.append("ยานอกพจนานุกรมหน่วย — ตรวจชื่อ")
        out.append({"drug": f"{name} {strength}".strip(), "dose": dose, "frequency": it.get("frequency") or it.get("sig_text") or "",
                    "route": it.get("route") or "PO", "status": it["status"], "notes": "; ".join(notes)[:300],
                    "class": it.get("class"), "conf": it["conf"], "canonical": it.get("canonical")})
    return out


def summary_lines(res: dict, patient: dict | None = None) -> list[str]:
    L = []
    form_th = {"hospital": "ใบตรวจรักษา รพ.ระนอง", "clinic": "Medication Record คลินิก"}.get(res.get("form"), "ใบยา (ไม่ทราบฟอร์ม)")
    h = res.get("header") or {}
    L.append(f"📄 {form_th}" + (f" · HN รพ. {h.get('hn_norm')}" if h.get("hn_norm") else "") + (f" · {h.get('date')}" if h.get("date") else ""))
    if patient:
        L.append(f"👤 {patient.get('patient_name', '')} (HN {patient.get('hn', '')}) — {patient.get('status', '')}")
    if h.get("allergies"):
        L.append("🚫 แพ้ยา: " + ", ".join(h["allergies"]))
    act = [i for i in res.get("items", []) if i["status"] == "active"]
    off = [i for i in res.get("items", []) if i["status"] == "off"]
    hold = [i for i in res.get("items", []) if i["status"] == "hold"]
    L.append(f"💊 อ่านได้ {len(act)} รายการ" + (f" · หยุด {len(off)}" if off else "") + (f" · hold {len(hold)}" if hold else "")
             + (f" · ไม่แน่ใจ {len(res.get('uncertain') or [])}" if res.get("uncertain") else ""))
    for i in (act + hold)[:14]:
        d = f"{i['per_dose']:g} {i['unit']}" if i.get("per_dose") is not None else ""
        L.append(f"• {i['drug']} {d} {i.get('frequency') or ''}".strip() + (f" ⏸ {i['handwritten']}" if i["status"] == "hold" else ""))
    for i in off:
        L.append(f"⏹ {i['drug']} — หยุด")
    return L


# ── accuracy samples ──
SAMPLES_SQL = """
CREATE TABLE IF NOT EXISTS hd_med_sheet_samples (
    id SERIAL PRIMARY KEY,
    read_id VARCHAR(20),
    form VARCHAR(12),
    hn VARCHAR(20),
    drug VARCHAR(120),
    ai_dose VARCHAR(60),
    ai_frequency VARCHAR(120),
    ai_status VARCHAR(10),
    confidence NUMERIC(4,2),
    flags TEXT,
    outcome VARCHAR(12),
    order_id INTEGER,
    decided_by VARCHAR(80),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    decided_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_med_sheet_samples_read ON hd_med_sheet_samples (read_id);
"""


def record_samples(cur, read_id: str, form: str, hn: str, drafts: list[dict], order_ids: list[int | None]):
    cur.execute(SAMPLES_SQL)
    for d, oid in zip(drafts, order_ids):
        cur.execute("""INSERT INTO hd_med_sheet_samples (read_id, form, hn, drug, ai_dose, ai_frequency, ai_status, confidence, flags, outcome, order_id)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s)""",
                    (read_id, (form or "")[:12], str(hn)[:20], d["drug"][:120], (d.get("dose") or "")[:60], (d.get("frequency") or "")[:120],
                     d.get("status", "active"), round(float(d.get("conf") or 0), 2), (d.get("notes") or "")[:300], oid))


def mark_outcome(cur, order_ids: list[int], outcome: str, who: str):
    if not order_ids:
        return 0
    cur.execute(SAMPLES_SQL)
    cur.execute("""UPDATE hd_med_sheet_samples SET outcome = %s, decided_by = %s, decided_at = NOW()
                   WHERE order_id = ANY(%s) AND outcome = 'pending'""", (outcome[:12], (who or "")[:80], list(order_ids)))
    return cur.rowcount


def accuracy_report(cur, days: int = 30) -> dict:
    cur.execute(SAMPLES_SQL)
    cur.execute("""SELECT form, outcome, COUNT(*) FROM hd_med_sheet_samples
                   WHERE created_at >= NOW() - (%s || ' days')::interval GROUP BY 1, 2""", (str(int(days)),))
    by_form = {}
    for form, outcome, n in cur.fetchall():
        by_form.setdefault(form or "?", {})[outcome] = int(n)
    cur.execute("""SELECT COUNT(DISTINCT read_id) FROM hd_med_sheet_samples WHERE created_at >= NOW() - (%s || ' days')::interval""", (str(int(days)),))
    sheets = cur.fetchone()[0]
    tot = {k: sum(v.get(k, 0) for v in by_form.values()) for k in ("accepted", "changed", "rejected", "pending")}
    judged = tot["accepted"] + tot["changed"] + tot["rejected"]
    cur.execute("""SELECT drug, ai_dose, ai_frequency, outcome, flags FROM hd_med_sheet_samples
                   WHERE outcome IN ('rejected','changed') AND created_at >= NOW() - (%s || ' days')::interval
                   ORDER BY decided_at DESC LIMIT 30""", (str(int(days)),))
    wrong = [{"drug": r[0], "ai_dose": r[1], "ai_frequency": r[2], "outcome": r[3], "flags": r[4]} for r in cur.fetchall()]
    return {"days": days, "sheets": sheets, "by_form": by_form, "totals": tot, "judged": judged,
            "accuracy_pct": round(tot["accepted"] / judged * 100, 1) if judged else None,
            "gate": {"sheets_required": 50, "ok": sheets >= 50 and tot["rejected"] == 0 and tot["changed"] == 0}, "wrong": wrong}
