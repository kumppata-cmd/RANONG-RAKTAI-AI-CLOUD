"""📷 Magic Camera 2.0 — 25 พ.ย. 2569: อ่านจอเครื่องแบบรู้รุ่น + confidence + ตัวอย่างเฉลย.

ค่าทดสอบมาจากรูปจริงของหน่วย (โฟลเดอร์ "รูปหน้าจอHD"):
  4008B: UF Volume 2334, UF Rate 0750, UF Goal 3000, Time Left 0-53, BFR 300, heparin 02.8 · AP/VP/TMP เป็นแท่ง LED
  DBB-27: VEN 185, TMP -9, UF VOLUME 0.99 L, UF GOAL 1.70 L, UF RATE 0.44 L/h, DIALY FLOW 500, B.FLW 300, 1:38
  EXA: ART -191, VEN 180, TMP 184, UF volume 2.38 L, UF rate 1.13 L/h, goal 4.50, COND 14.0, TEMP 36.4, BF 300
"""
import json
import os
import types

import pytest

from hd_module import machine_ocr as mo

SESS = os.path.join(os.path.dirname(__file__), "..", "hd_module", "templates", "hd_session.html")
SHEET = os.path.join(os.path.dirname(__file__), "..", "hd_module", "templates", "hd_sheet.html")
MC = os.path.join(os.path.dirname(__file__), "..", "hd_module", "templates", "_magic_camera.html")


class TestProfiles:
    def test_seven_models_from_unit_photos(self):
        assert set(mo.MACHINE_PROFILES) == {"fresenius_4008b", "fresenius_4008s", "fresenius_5008s",
                                            "nikkiso_dbb07", "nikkiso_dbb27", "nikkiso_dbb27c", "nikkiso_exa"}
        for mid, p in mo.MACHINE_PROFILES.items():
            assert p["label"] and p["hint"] and "uf_removed" in p["numeric"], mid
            assert not (set(p["numeric"]) & set(p["bargraph"])), mid
        # 4008 รุ่นเก่า: ความดันเป็นแท่ง → ห้ามอ่านเป็นตัวเลข
        assert set(mo.MACHINE_PROFILES["fresenius_4008b"]["bargraph"]) == {"ap", "vp", "tmp", "conductivity"}
        assert mo.MACHINE_PROFILES["fresenius_5008s"]["bargraph"] == []

    def test_guess_from_equipment_registry(self):
        assert mo.guess_model("Fresenius", "4008 B") == "fresenius_4008b"
        assert mo.guess_model("Fresenius", "4008S") == "fresenius_4008s"
        assert mo.guess_model("Fresenius Medical Care", "5008S") == "fresenius_5008s"
        assert mo.guess_model("NIKKISO", "DBB-27C") == "nikkiso_dbb27c"
        assert mo.guess_model("NIKKISO", "DBB-27") == "nikkiso_dbb27"
        assert mo.guess_model("Nikkiso", "DBB 07") == "nikkiso_dbb07"
        assert mo.guess_model("NIKKISO", "DBB-EXA") == "nikkiso_exa"
        assert mo.guess_model("Toray", "TR-8000") is None and mo.guess_model(None, None) is None

    def test_prompt_mentions_model_hint_and_bargraph(self):
        p = mo.build_prompt("fresenius_4008b")
        assert "4008B" in p and "แท่งไฟ" in p and "ap, vp, tmp, conductivity" in p
        assert "ห้ามอ่านหรือตอบชื่อคน" in p and '"model_seen"' in p
        g = mo.build_prompt(None)
        assert "ถ้าไม่รู้รุ่น" in g


class TestSanitize:
    def test_4008b_real_values_and_bargraph_dropped(self):
        raw = {"model_seen": "Fresenius 4008 B",
               "values": {"uf_removed": 2334, "uf_rate": 750, "uf_goal": 3000, "time_left_min": 53, "bfr": 300,
                          "heparin_rate": 2.8, "vp": 150, "ap": 40, "tmp": 100},
               "confidence": {"uf_removed": 0.95, "uf_rate": 0.9, "uf_goal": 0.95, "time_left_min": 0.85, "bfr": 0.9,
                              "heparin_rate": 0.8, "vp": 0.4, "ap": 0.4, "tmp": 0.4}}
        r = mo.sanitize(raw, "fresenius_4008b")
        assert r["values"] == {"uf_removed": 2334, "uf_rate": 750, "uf_goal": 3000, "time_left_min": 53, "bfr": 300, "heparin_rate": 2.8}
        assert set(r["dropped"]) == {"vp", "ap", "tmp"} and r["uncertain"] == []
        assert r["model_mismatch"] is False

    def test_nikkiso_liters_converted_and_negative_tmp_kept(self):
        raw = {"model_seen": "NIKKISO DBB-27",
               "values": {"vp": 185, "tmp": -9, "uf_removed": 0.99, "uf_goal": 1.70, "uf_rate": 0.44, "dfr": 500, "bfr": 300,
                          "time_left_min": 98, "conductivity": 14.1, "dialysate_temp": 36.7, "heparin_rate": 2.0},
               "confidence": {k: 0.9 for k in ["vp", "tmp", "uf_removed", "uf_goal", "uf_rate", "dfr", "bfr", "time_left_min",
                                               "conductivity", "dialysate_temp", "heparin_rate"]}}
        r = mo.sanitize(raw, "nikkiso_dbb27")
        v = r["values"]
        assert (v["uf_removed"], v["uf_goal"], v["uf_rate"]) == (990, 1700, 440)
        assert v["tmp"] == -9 and v["vp"] == 185 and v["dfr"] == 500 and v["conductivity"] == 14.1
        assert r["dropped"] == [] and r["model_mismatch"] is False

    def test_low_confidence_goes_to_uncertain_not_into_fields(self):
        raw = {"values": {"uf_removed": 2380, "bfr": 300, "vp": 180}, "confidence": {"uf_removed": 0.95, "bfr": 0.55, "vp": 0.69}}
        r = mo.sanitize(raw, "nikkiso_exa")
        assert r["values"] == {"uf_removed": 2380}
        assert [u["field"] for u in r["uncertain"]] == ["bfr", "vp"]

    def test_out_of_range_dropped_and_mismatch_flagged(self):
        raw = {"model_seen": "NIKKISO DBB-EXA", "values": {"vp": 1400, "sbp": 130, "bfr": "abc"}, "confidence": {"vp": 0.9, "sbp": 0.9}}
        r = mo.sanitize(raw, "fresenius_5008s")
        assert r["values"] == {"sbp": 130} and set(r["dropped"]) == {"vp", "bfr"}
        assert r["model_mismatch"] is True

    def test_time_hmm_decimal_form(self):
        r = mo.sanitize({"values": {"time_left_min": 1.47}, "confidence": {"time_left_min": 0.9}}, None)
        assert r["values"]["time_left_min"] == 107


class TestReadWithClaude:
    def _resp(self, payload, status=200):
        return types.SimpleNamespace(status_code=status, json=lambda: {"content": [{"type": "text", "text": json.dumps(payload)}]})

    def test_parses_and_sanitizes(self):
        payload = {"model_seen": "DBB-EXA", "values": {"ap": -191, "vp": 180, "tmp": 184, "uf_removed": 2.38, "uf_rate": 1.13, "uf_goal": 4.5,
                                                       "conductivity": 14.0, "dialysate_temp": 36.4, "bfr": 300, "time_left_min": 112},
                   "confidence": {k: 0.92 for k in ["ap", "vp", "tmp", "uf_removed", "uf_rate", "uf_goal", "conductivity", "dialysate_temp", "bfr", "time_left_min"]}}
        r = mo.read_with_claude(b"\xff\xd8not-a-real-jpeg", "nikkiso_exa", "key", "claude-x", post_fn=lambda b: self._resp(payload))
        assert r["values"]["uf_removed"] == 2380 and r["values"]["uf_rate"] == 1130 and r["values"]["uf_goal"] == 4500
        assert r["values"]["ap"] == -191 and r["model_mismatch"] is False

    def test_no_key_returns_none_and_http_error_none(self):
        assert mo.read_with_claude(b"x", None, "", "m") is None
        assert mo.read_with_claude(b"x", None, "key", "m", post_fn=lambda b: self._resp({}, status=500)) is None

    def test_busy_when_two_slots_taken(self):
        mo.VISION_SLOTS.acquire(); mo.VISION_SLOTS.acquire()
        try:
            with pytest.raises(RuntimeError, match="busy"):
                mo.read_with_claude(b"x", None, "key", "m", post_fn=lambda b: self._resp({}))
        finally:
            mo.VISION_SLOTS.release(); mo.VISION_SLOTS.release()
        # slots released → works again
        assert mo.read_with_claude(b"x", None, "key", "m", post_fn=lambda b: self._resp({"values": {}, "confidence": {}})) is not None

    def test_legacy_flat_json_goes_to_uncertain(self):
        # รูปแบบเก่า (ไม่มี confidence) → ถือว่า 0.5 → ไม่ใส่ช่องเอง แต่โชว์ให้พยาบาลเลือก
        r = mo.read_with_claude(b"x", "fresenius_4008s", "key", "m", post_fn=lambda b: self._resp({"uf_removed": 2108, "uf_rate": 750}))
        assert r["values"] == {}
        assert {u["field"]: u["value"] for u in r["uncertain"]} == {"uf_removed": 2108, "uf_rate": 750}


class TestSamples:
    def test_outcomes(self):
        assert mo.outcome_of("uf_removed", 2334, 2334) == "ok"
        assert mo.outcome_of("uf_removed", 2334, 2340) == "ok"          # ภายใน ±10
        assert mo.outcome_of("uf_removed", 2334, 2534) == "wrong"
        assert mo.outcome_of("vp", None, 150) == "blank"
        assert mo.outcome_of("vp", 150, None) == "unused"
        assert mo.outcome_of("vp", None, None) is None
        assert mo.outcome_of("conductivity", 14.0, 14.1) == "ok"

    def test_record_and_report(self):
        rows = []

        class Cur:
            def execute(self, sql, params=None):
                if sql.strip().startswith("INSERT"):
                    rows.append(params)
                self._sql = sql
            def fetchall(self):
                if "COUNT(DISTINCT read_id)" in self._sql:
                    return [("nikkiso_dbb27", 1)]
                agg = {}
                for r in rows:
                    agg[(r[1], r[2], r[6])] = agg.get((r[1], r[2], r[6]), 0) + 1
                return [(m, f, o, n) for (m, f, o), n in agg.items()]
        cur = Cur()
        n = mo.record_samples(cur, 42, "nikkiso_dbb27", {"uf_removed": 990, "vp": 185, "bfr": 300}, {"uf_removed": .9, "vp": .9, "bfr": .9},
                              {"uf_removed": 990, "vp": 200, "tmp": -9}, "abc123", nurse_id=8)
        assert n == 4   # uf ok, vp wrong, bfr unused, tmp blank
        rep = mo.accuracy_report(cur, days=30)
        f = rep["models"]["nikkiso_dbb27"]["fields"]
        assert f["uf_removed"]["ok"] == 1 and f["vp"]["wrong"] == 1 and f["bfr"]["unused"] == 1 and f["tmp"]["blank"] == 1
        assert f["uf_removed"]["precision"] == 1.0 and f["vp"]["precision"] == 0.0 and f["tmp"]["coverage"] == 0.0
        assert rep["models"]["nikkiso_dbb27"]["reads"] == 1


class TestWiring:
    def test_pages(self):
        s = open(SESS, encoding="utf-8").read()
        for n in ('{% include "_magic_camera.html" %}', "MC_MAP_IM", "MC_MAP_UF", 'id="imMcModel"', 'id="imShotReview"',
                  "mcConfirm(sessionData.session_id, MC_MAP_IM)", "mcConfirm(sessionData.session_id, MC_MAP_UF)"):
            assert n in s, n
        t = open(SHEET, encoding="utf-8").read()
        for n in ('{% include "_magic_camera.html" %}', "MC_MAP_E", 'id="eMcModel"', 'id="eShot"', "mcConfirm(SESSION_ID, MC_MAP_E)"):
            assert n in t, n
        m = open(MC, encoding="utf-8").read()
        assert "<script" not in m and "function mcRead(" in m and "function mcConfirm(" in m and "x.status === 429" in m

    def test_migration_and_routes(self):
        from hd_module import migrations, blueprint as bp
        import inspect
        assert "CREATE TABLE IF NOT EXISTS hd_ocr_samples" in migrations.MIGRATION_UP
        src = inspect.getsource(bp)
        for n in ('"/machine-screen/models"', '"/machine-screen/confirm"', '"/machine-screen/accuracy"', '"/ocr-accuracy"',
                  "mo.read_with_claude(", "429"):
            assert n in src, n


class TestExtractJson:
    def test_variants(self):
        assert mo.extract_json('{"values": {"bfr": 300}}') == {"values": {"bfr": 300}}
        assert mo.extract_json('```json\n{"values": {"bfr": 300}}\n```') == {"values": {"bfr": 300}}
        assert mo.extract_json('อ่านได้ดังนี้: {"values": {"bfr": 300}, "confidence": {"bfr": 0.9}} ครับ')["confidence"] == {"bfr": 0.9}
        assert mo.extract_json('{"values": {"bfr": 300, "vp": 1') is None      # ถูกตัดท้าย → None ไม่ใช่ค่ามั่ว
        assert mo.extract_json("") is None and mo.extract_json("ไม่มีตัวเลข") is None

    def test_prefixed_text_still_read(self):
        import types
        payload = 'ผลการอ่าน:\n```json\n{"model_seen":"4008 B","values":{"uf_removed":2334,"bfr":300},"confidence":{"uf_removed":0.95,"bfr":0.9}}\n```'
        resp = types.SimpleNamespace(status_code=200, json=lambda: {"content": [{"type": "text", "text": payload}], "stop_reason": "end_turn"})
        r = mo.read_with_claude(b"x", "fresenius_4008b", "key", "m", post_fn=lambda b: resp)
        assert r["values"] == {"uf_removed": 2334, "bfr": 300}

    def test_fallback_never_fills_fields(self):
        import inspect
        from hd_module import blueprint as bp
        src = inspect.getsource(bp.read_machine_screen)
        assert '"values": {}' in src and '"claude_failed": True' in src and '"confidence": 0.4' in src


class TestNotOnScreen:
    def test_field_absent_on_model_is_dropped(self):
        raw = {"values": {"ap": -1, "vp": 102, "uf_goal": 3.2, "sbp": 130}, "confidence": {"ap": .9, "vp": .9, "uf_goal": .9, "sbp": .9}}
        r = mo.sanitize(raw, "nikkiso_dbb07")     # DBB-07 ไม่มี AP และ UF goal บนจอ
        assert r["values"] == {"vp": 102, "sbp": 130} and set(r["dropped"]) == {"ap", "uf_goal"}
