"""🧭 Doc Classifier AI — ใบที่ตัวจัดประเภทไม่แน่ใจ (ใบยา/ใบฟอก) → Claude Vision 1 ครั้ง (26 พ.ย. 2569)"""
import io
import json
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hd_module import doc_classifier_ai as dca  # noqa: E402


def _png_bytes():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 60), (255, 255, 255)).save(buf, "PNG")
    return buf.getvalue()


class _Resp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._p = payload

    def json(self):
        return self._p


def _ai_resp(category, conf, reason="เนื้อหาหลักเป็นรายการยา"):
    return _Resp({"content": [{"type": "text", "text": json.dumps(
        {"category": category, "confidence": conf, "reason": reason}, ensure_ascii=False)}], "stop_reason": "end_turn"})


AMBIG = {"category": "dialysis", "ambiguous_with": "rx", "confidence": 0.9,
         "reason": "HD work sheet pre-check (39 marks)", "_ocr_text": "x"}


class TestNormalizeDecide:
    def test_normalize_clamps_and_maps_unknown(self):
        assert dca.normalize({"category": "RX", "confidence": "0.93", "reason": "x"}) == {"category": "rx", "confidence": 0.93, "reason": "x"}
        assert dca.normalize({"category": "bills", "confidence": 2})["category"] == "other"
        assert dca.normalize({"confidence": "bad"})["confidence"] == 0.0

    def test_decide_changes_when_confident(self):
        res, changed, note = dca.decide(AMBIG, {"category": "rx", "confidence": 0.92, "reason": "รายการยา"})
        assert changed and note == "changed" and res["category"] == "rx" and "ambiguous_with" not in res
        assert res["ai_classify"]["used"] is True and AMBIG["category"] == "dialysis"   # ไม่แก้ dict เดิม

    def test_decide_confirms_without_change(self):
        res, changed, note = dca.decide(AMBIG, {"category": "dialysis", "confidence": 0.85, "reason": ""})
        assert not changed and note == "confirmed" and res["category"] == "dialysis" and "ambiguous_with" not in res

    def test_decide_keeps_flag_when_low_conf_or_other(self):
        for ai in ({"category": "rx", "confidence": 0.6}, {"category": "other", "confidence": 0.95}, None):
            res, changed, note = dca.decide(AMBIG, ai)
            assert not changed and res["category"] == "dialysis" and res["ambiguous_with"] == "rx"
        assert dca.decide(AMBIG, None)[2] == "ai_failed"

    def test_prompt_lists_categories_and_privacy(self):
        p = dca.build_prompt("dialysis", "rx")
        assert "(rx)" in p and "(dialysis)" in p and "(lab)" in p and "ห้ามอ่าน" in p and "JSON" in p


class TestResolveSafety:
    def test_untouched_when_not_ambiguous_or_disabled(self, tmp_path):
        clear = {"category": "dialysis", "confidence": 0.9}
        assert dca.resolve(str(tmp_path / "a.png"), clear) is clear
        with mock.patch.object(dca, "is_enabled", return_value=False):
            assert dca.resolve(str(tmp_path / "a.png"), AMBIG) is AMBIG

    def test_resolve_changes_category_via_vision(self, tmp_path):
        p = tmp_path / "doc.png"
        p.write_bytes(_png_bytes())
        calls = []

        def post(body):
            calls.append(json.loads(body))
            return _ai_resp("rx", 0.9)
        with mock.patch.object(dca, "is_enabled", return_value=True), \
             mock.patch.object(dca, "_api", return_value=("k", "claude-x")):
            res = dca.resolve(str(p), AMBIG, post_fn=post)
        assert res["category"] == "rx" and "ambiguous_with" not in res and res["ai_classify"]["used"]
        assert len(calls) == 1 and calls[0]["model"] == "claude-x"
        assert calls[0]["messages"][0]["content"][0]["source"]["media_type"] == "image/jpeg"
        assert AMBIG["category"] == "dialysis"
        assert dca.stats()["changed"] >= 1

    def test_resolve_keeps_flag_on_low_conf_and_failure(self, tmp_path):
        p = tmp_path / "doc.png"
        p.write_bytes(_png_bytes())
        with mock.patch.object(dca, "is_enabled", return_value=True), \
             mock.patch.object(dca, "_api", return_value=("k", "m")):
            r1 = dca.resolve(str(p), AMBIG, post_fn=lambda b: _ai_resp("rx", 0.5))
            r2 = dca.resolve(str(p), AMBIG, post_fn=lambda b: _Resp({}, status=500))
            r3 = dca.resolve(str(p), AMBIG, post_fn=lambda b: (_ for _ in ()).throw(TimeoutError("slow")))
        for r in (r1, r2, r3):
            assert r["category"] == "dialysis" and r["ambiguous_with"] == "rx"
        assert r1["ai_classify"]["used"] is False

    def test_resolve_busy_and_missing_key(self, tmp_path):
        p = tmp_path / "doc.png"
        p.write_bytes(_png_bytes())
        from hd_module.machine_ocr import VISION_SLOTS
        with mock.patch.object(dca, "is_enabled", return_value=True), \
             mock.patch.object(dca, "_api", return_value=("", "")):
            assert dca.resolve(str(p), AMBIG) is AMBIG
        # คิว Vision เต็ม → คืนผลเดิมทันที ไม่ล้ม
        with mock.patch.object(dca, "is_enabled", return_value=True), \
             mock.patch.object(dca, "_api", return_value=("k", "m")):
            VISION_SLOTS.acquire(); VISION_SLOTS.acquire()
            try:
                assert dca.resolve(str(p), AMBIG, post_fn=lambda b: _ai_resp("rx", 0.9)) is AMBIG
            finally:
                VISION_SLOTS.release(); VISION_SLOTS.release()

    def test_image_for_rejects_unknown_ext(self, tmp_path):
        p = tmp_path / "x.txt"
        p.write_text("hi")
        assert dca.image_for(str(p)) is None


class TestServerHookWiring:
    def test_server_calls_resolve_only_when_ambiguous(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server.py"), encoding="utf-8").read()
        i = src.index('if result.get("ambiguous_with"):\n                try:\n                    from hd_module import doc_classifier_ai as _dca')
        assert "result = _dca.resolve(filepath, result)" in src[i:i + 400]
        assert 'app.config["DOC_KEYWORD_CLASSIFY_FN"] = classify_with_keywords' in src
        assert "EXTERNAL_AI_CLASSIFICATION_ALLOWED = False" in src   # ล็อกเดิมไม่ถูกแตะ


class TestSettingsAndTemplate:
    def test_setting_key_allowed_and_card_present(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        bp = open(os.path.join(root, "hd_module", "blueprint.py"), encoding="utf-8").read()
        assert '"doc_classify_ai",' in bp and '/doc-classify/read' in bp and '/doc-classify-test' in bp
        tpl = open(os.path.join(root, "hd_module", "templates", "hd_settings.html"), encoding="utf-8").read()
        assert 'id="setting_doc_classify_ai"' in tpl and "saveSetting('doc_classify_ai')" in tpl
        assert os.path.exists(os.path.join(root, "hd_module", "templates", "hd_doc_classify_test.html"))
