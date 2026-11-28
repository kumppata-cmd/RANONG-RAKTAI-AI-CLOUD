"""Shared text-only Claude call for hd_module features (28 พ.ย. 2569).

One tiny wrapper so หมอเงา / ถามชาร์ต / Time Machine don't each re-implement the
HTTP call. Same key, model selection and retry style as patient_engage /
blackbox. Never raises — returns (text, None) or (None, "err-code").

`post_fn(body_bytes) -> response-like` can be injected by tests.
"""
import json
import logging
import os
import time

logger = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"


def api_key():
    return (os.environ.get("ANTHROPIC_API_KEY", "") or "").strip()


def model_id(setting_key=None, default=None):
    """Model: env HD_TEXT_MODEL > ⚙️ <setting_key> > patient_engage.ai_model()."""
    m = (os.environ.get("HD_TEXT_MODEL", "") or "").strip()
    if m:
        return m
    if setting_key:
        try:
            from .blueprint import get_setting_value
            m = (get_setting_value(setting_key, "") or "").strip()
            if m:
                return m
        except Exception:
            pass
    if default:
        return default
    try:
        from .patient_engage import ai_model
        return ai_model()
    except Exception:
        return "claude-haiku-5-5"


def ask(prompt, system=None, model=None, max_tokens=1200, timeout=40, post_fn=None, retries=1, temperature=None):
    """Single-turn text call. Returns (text, err)."""
    key = api_key()
    if not key and post_fn is None:
        return None, "no_api_key"
    body = {"model": model or model_id(), "max_tokens": int(max_tokens),
            "messages": [{"role": "user", "content": prompt}]}
    if system:
        body["system"] = system
    if temperature is not None:
        body["temperature"] = temperature
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    if post_fn is None:
        import requests as _rq

        def post_fn(b):  # noqa: E306
            return _rq.post(API_URL, headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                              "content-type": "application/json"}, data=b, timeout=timeout)
    err = "empty"
    for attempt in range(retries + 1):
        try:
            resp = post_fn(raw)
        except Exception as e:  # noqa: BLE001
            err = f"net:{type(e).__name__}"
            logger.warning("ai_text net (%d): %s", attempt, err)
            if attempt < retries:
                time.sleep(1.2)
                continue
            return None, err
        code = getattr(resp, "status_code", 200)
        if code != 200:
            txt = getattr(resp, "text", "") or ""
            if code == 400 and "credit balance" in txt:
                return None, "no_credit"
            err = f"api:{code}"
            logger.warning("ai_text api %s model=%s: %s", code, body.get("model"), txt[:200].replace("\n", " "))
            if code == 400:
                # รุ่นใหม่บางรุ่นไม่รับ temperature → ถอดแล้วลองใหม่ 1 ครั้ง; ถ้ายัง 400 แนบข้อความจาก API ให้เห็นสาเหตุ
                if "temperature" in body and attempt == 0:
                    body.pop("temperature", None)
                    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
                    continue
                try:
                    detail = (json.loads(txt).get("error") or {}).get("message", "")
                except Exception:
                    detail = txt
                return None, f"api:400 {str(detail)[:140]}".strip()
            if attempt < retries and code in (429, 500, 502, 503, 529):
                time.sleep(1.5)
                continue
            return None, err
        try:
            rj = resp.json()
        except Exception:
            return None, "bad_json"
        text = "".join(b.get("text", "") for b in (rj.get("content") or []) if b.get("type") == "text").strip()
        if text:
            return text, None
        logger.warning("ai_text empty reply model=%s stop=%s", body.get("model"), rj.get("stop_reason"))
        if attempt < retries:
            time.sleep(1.0)
    return None, err


def extract_json(text):
    """Best-effort JSON object/array from a model reply (handles ```json fences)."""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = t.find(opener), t.rfind(closer)
        if i != -1 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except Exception:
                continue
    return None
