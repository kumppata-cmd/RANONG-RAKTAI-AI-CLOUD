"""🧭 Doc Classifier AI — ตัดสินใบที่ตัวจัดประเภทคำสำคัญ "ไม่แน่ใจ" (ใบยา ↔ ใบฟอกเลือด ↔ ใบแล็บ)

26 พ.ย. 2569 (แพทย์อนุมัติ): รอบทดสอบ 4 ใบ ใบ ② มีเครื่องหมายใบ HD work sheet 39 จุด
แต่มีหัวฟอร์มสั่งยา/ชื่อยาด้วย → ตัวจัดประเภทเดิมขึ้นธง "ไม่แน่ใจ" ให้คนยืนยัน
โมดูลนี้ถาม Claude Vision 1 ครั้ง **เฉพาะใบที่มีธงนั้น** แล้วใช้คำตอบเมื่อมั่นใจ ≥ ACCEPT_CONF

หลักการ (ห้ามกระทบทางเดิม):
- สวิตช์ ⚙️ `doc_classify_ai` ค่าเริ่มต้น off → ปิดอยู่ = โค้ดนี้ไม่ถูกเรียกเลย
- ใบที่ตัวจัดประเภทเดิมชัด (ไม่มี ambiguous_with) → ไม่แตะ
- AI ล้ม / ช้า / ตอบไม่เป็น JSON / มั่นใจต่ำ → คืนผลเดิมพร้อมธง "ไม่แน่ใจ" เหมือนก่อน
- ใช้คิว VISION_SLOTS เดียวกับ Magic Camera / ใบแล็บ → ไม่เพิ่มความเสี่ยง 502
- ไม่เก็บรูป · prompt ห้ามอ่านชื่อ/HN
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from datetime import date

logger = logging.getLogger(__name__)

SETTING_KEY = "doc_classify_ai"
ACCEPT_CONF = 0.8
TIMEOUT_S = 20
MAX_SIDE = 1568

CATEGORIES = {
    "rx": "ใบยา / ใบสั่งยา / Order for continuation / Medication record",
    "dialysis": "ใบฟอกเลือด / Hemodialysis work sheet / บันทึกการฟอกเลือดรายรอบ",
    "lab": "ใบรายงานผลตรวจทางห้องปฏิบัติการ (CBC, Electrolyte, BUN/Cr, Ferritin, PTH, HBV/HCV/HIV)",
}
ACCEPTABLE = tuple(CATEGORIES.keys())

_STATS_LOCK = threading.Lock()
_STATS = {"day": "", "calls": 0, "changed": 0, "kept": 0, "failed": 0, "busy": 0, "last": None}


# ───────────────────────── switch ─────────────────────────

def is_enabled() -> bool:
    try:
        from .blueprint import get_setting_value
        return str(get_setting_value(SETTING_KEY, "off") or "off").strip().lower() == "on"
    except Exception:
        return False


def _api():
    api_key = (os.environ.get("ANTHROPIC_API_KEY", "") or "").strip()
    try:
        from .patient_engage import ai_model
        model_id = ai_model()
    except Exception:
        model_id = ""
    return api_key, model_id


# ───────────────────────── image prep ─────────────────────────

def image_for(filepath: str) -> bytes | None:
    """รูป JPEG (ย่อ ≤1568 px) ของหน้าแรกของเอกสาร — jpg/png อ่านตรง, pdf แปลงหน้าแรก"""
    ext = os.path.splitext(filepath)[1].lower()
    with open(filepath, "rb") as fh:
        data = fh.read()
    if ext == ".pdf":
        from .lab_sheet_reader import pdf_pages_to_jpeg
        pages = pdf_pages_to_jpeg(data, max_pages=1, dpi=150)
        if not pages:
            return None
        data = pages[0]
    elif ext not in (".jpg", ".jpeg", ".png", ".webp"):
        return None
    return _shrink(data)


def _shrink(img_bytes: bytes) -> bytes:
    try:
        import io
        from PIL import Image, ImageOps
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(img_bytes))).convert("RGB")
        if max(im.size) > MAX_SIDE:
            im.thumbnail((MAX_SIDE, MAX_SIDE))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        return buf.getvalue()
    except Exception:
        return img_bytes


# ───────────────────────── prompt ─────────────────────────

def build_prompt(hint_from: str | None = None, hint_with: str | None = None) -> str:
    lines = [
        "คุณเป็นเจ้าหน้าที่เวชระเบียนของหน่วยไตเทียม ดูรูปเอกสาร 1 หน้า แล้วตอบว่าเป็นเอกสารประเภทใด",
        "ประเภทที่เป็นไปได้ (ตอบด้วยรหัสในวงเล็บเท่านั้น):",
    ]
    for k, v in CATEGORIES.items():
        lines.append(f"- ({k}) {v}")
    lines.append("- (other) ไม่ใช่ทั้งสามประเภท")
    lines += [
        "",
        "วิธีตัดสิน:",
        "- ใบฟอกเลือด: มีตารางบันทึกรายชั่วโมง (เวลา, BP, BFR, UF, TMP, VP), ช่อง dialyzer, น้ำหนักก่อน-หลัง, heparin — แม้จะมีรายการยาเล็ก ๆ อยู่ด้วยก็ยังเป็นใบฟอกเลือด",
        "- ใบยา: เนื้อหาหลักของหน้าคือรายการยา (ชื่อยา ขนาด ความถี่ วันเริ่ม/หยุด ลายเซ็นแพทย์) โดยไม่มีตารางบันทึกรายชั่วโมงของการฟอก — แม้หัวกระดาษจะเป็นฟอร์มของหน่วยไตเทียมก็ตาม",
        "- ใบแล็บ: ตารางชื่อการตรวจ / ผล / ค่าอ้างอิง / หน่วย",
        "- ดูที่ 'เนื้อหาส่วนใหญ่ของหน้า' ไม่ใช่หัวกระดาษ",
        "",
        "ห้ามอ่านหรือตอบชื่อผู้ป่วย HN หรือข้อมูลส่วนบุคคลใด ๆ",
        "ตอบเป็น JSON เท่านั้น ไม่มีข้อความอื่น:",
        '{"category": "rx|dialysis|lab|other", "confidence": 0.0-1.0, "reason": "สั้น ๆ ภาษาไทย ไม่เกิน 25 คำ ไม่ระบุชื่อคน"}',
    ]
    if hint_from and hint_with:
        lines.append(f"(ตัวจัดประเภทอัตโนมัติลังเลระหว่าง {hint_from} กับ {hint_with} — ตัดสินจากรูปโดยไม่ต้องเชื่อคำใบ้นี้)")
    return "\n".join(lines)


# ───────────────────────── vision call ─────────────────────────

def classify_image(img_bytes: bytes, api_key: str, model_id: str, post_fn=None,
                   hint_from: str | None = None, hint_with: str | None = None) -> dict | None:
    """ถาม Claude Vision 1 ครั้ง → {"category","confidence","reason"} หรือ None (ล้ม) / raise RuntimeError('busy')"""
    from .machine_ocr import VISION_SLOTS, extract_json
    if not api_key or not model_id:
        return None
    body = json.dumps({
        "model": model_id, "max_tokens": 600,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.b64encode(img_bytes).decode()}},
            {"type": "text", "text": build_prompt(hint_from, hint_with)},
        ]}],
    }).encode("utf-8")
    if not VISION_SLOTS.acquire(blocking=False):
        raise RuntimeError("busy")
    try:
        if post_fn is None:
            import requests as _rq
            post_fn = lambda b: _rq.post("https://api.anthropic.com/v1/messages",
                                         headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                                                  "content-type": "application/json"},
                                         data=b, timeout=TIMEOUT_S)
        resp = post_fn(body)
    finally:
        VISION_SLOTS.release()
    if getattr(resp, "status_code", 0) != 200:
        logger.info("doc-classify claude HTTP %s", getattr(resp, "status_code", "?"))
        return None
    rj = resp.json()
    txt = "".join(x.get("text", "") for x in (rj.get("content") or []) if x.get("type") == "text").strip()
    raw = extract_json(txt)
    if not isinstance(raw, dict):
        logger.warning("doc-classify claude: no JSON (stop=%s): %s", rj.get("stop_reason"), txt[:120].replace("\n", " "))
        return None
    return normalize(raw)


def normalize(raw: dict) -> dict:
    cat = str(raw.get("category") or "").strip().lower()
    if cat not in ACCEPTABLE and cat != "other":
        cat = "other"
    try:
        conf = float(raw.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(conf, 1.0))
    reason = str(raw.get("reason") or "").strip()[:160]
    return {"category": cat, "confidence": conf, "reason": reason}


# ───────────────────────── decision ─────────────────────────

def decide(keyword_result: dict, ai: dict | None) -> tuple[dict, bool, str]:
    """รวมผล: ใช้ AI เมื่อมั่นใจ ≥ ACCEPT_CONF และเป็น 1 ใน rx/dialysis/lab · คืน (result, changed, note)
    ไม่แก้ dict เดิม — คืนสำเนา"""
    res = dict(keyword_result)
    if not ai:
        return res, False, "ai_failed"
    cat, conf = ai.get("category"), float(ai.get("confidence") or 0)
    if cat not in ACCEPTABLE or conf < ACCEPT_CONF:
        res["ai_classify"] = {"category": cat, "confidence": conf, "reason": ai.get("reason", ""), "used": False}
        return res, False, "low_conf"
    old = res.get("category")
    res["ai_classify"] = {"category": cat, "confidence": conf, "reason": ai.get("reason", ""), "used": True}
    res["category"] = cat
    res["confidence"] = max(float(res.get("confidence") or 0), conf)
    res.pop("ambiguous_with", None)
    res["reason"] = f"{res.get('reason', '')} → AI vision: {cat} ({conf:.2f})".strip()
    return res, cat != old, ("changed" if cat != old else "confirmed")


def resolve(filepath: str, keyword_result: dict, post_fn=None) -> dict:
    """จุดเรียกจาก server.py — ปลอดภัยเสมอ: ไม่ raise, ไม่แก้ dict เดิม, คืนผลเดิมเมื่อทำไม่ได้"""
    try:
        if not isinstance(keyword_result, dict) or not keyword_result.get("ambiguous_with"):
            return keyword_result
        if not is_enabled():
            return keyword_result
        api_key, model_id = _api()
        if not api_key or not model_id:
            return keyword_result
        img = image_for(filepath)
        if not img:
            return keyword_result
        t0 = time.time()
        try:
            ai = classify_image(img, api_key, model_id, post_fn=post_fn,
                                hint_from=keyword_result.get("category"), hint_with=keyword_result.get("ambiguous_with"))
        except RuntimeError:
            _bump("busy")
            logger.info("doc-classify: vision busy — keep keyword result")
            return keyword_result
        res, changed, note = decide(keyword_result, ai)
        _bump("calls")
        _bump("changed" if changed else ("failed" if note == "ai_failed" else "kept"))
        with _STATS_LOCK:
            _STATS["last"] = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "from": keyword_result.get("category"),
                              "to": res.get("category"), "note": note, "ms": int((time.time() - t0) * 1000)}
        logger.info("doc-classify AI: %s/%s → %s (%s, %.1fs)", keyword_result.get("category"),
                    keyword_result.get("ambiguous_with"), res.get("category"), note, time.time() - t0)
        return res
    except Exception as e:  # noqa: BLE001 — ห้ามล้มทางเดิม
        logger.warning("doc-classify AI failed (keep keyword result): %s", e)
        _bump("failed")
        return keyword_result


def _bump(key: str):
    with _STATS_LOCK:
        today = date.today().isoformat()
        if _STATS["day"] != today:
            _STATS.update({"day": today, "calls": 0, "changed": 0, "kept": 0, "failed": 0, "busy": 0})
        _STATS[key] = _STATS.get(key, 0) + 1


def stats() -> dict:
    with _STATS_LOCK:
        return dict(_STATS)
