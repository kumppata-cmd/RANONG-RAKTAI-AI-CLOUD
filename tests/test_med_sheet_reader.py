"""💊📄 Med Sheet Reader ขั้น 1–2 (28 พ.ย. 2569) — sig codes, formulary, analyze/merge, drafts, samples, wiring"""
import json
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hd_module import med_sheet_reader as m  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HOSP = {"form": "hospital", "header": {"hn": "00342226", "name": "นาง ก", "date": "27 พฤศจิกายน 2569", "allergies": ["CLINDAMYCIN", "MEROPENAM"]},
        "items": [{"no": 3, "name": "ATORvastatin", "strength": "40 mg", "qty": "#90", "sig_code": "11ht", "route": "Oral", "conf": 0.9},
                  {"no": 5, "name": "FERROUS FUMARATE", "strength": "200 MG", "qty": "#270", "sig_code": "31ht", "route": "Oral", "conf": 0.9},
                  {"no": 8, "name": "SODIUM BICARBONATE", "strength": "300 MG", "qty": "#540", "sig_code": "32pt", "route": "Oral", "conf": 0.9},
                  {"no": 10, "name": "losartan", "strength": "50 MG", "qty": "#0", "sig_text": "off", "route": "Oral", "conf": 0.9},
                  {"no": 11, "name": "VENOFER", "strength": "100 mg/5ml", "qty": "#6", "sig_text": "100 mg iv ทุกวันที่ 15 และ 30", "route": "IV", "conf": 0.85},
                  {"no": 9, "name": "Calciferol (D2)", "strength": "20,000 u.", "qty": "#12", "sig_text": "รับประทานครั้งละ 1 เม็ด ทุกวันจันทร์", "route": "Oral", "conf": 0.85}]}
CLINIC = {"form": "clinic", "header": {"hn": "65259", "name": "ร้อฟีย๊ะ"},
          "items": [{"no": 1, "name": "Hema-plus", "dose": "4000", "unit": "Unit", "freq_a": "1", "freq_b": "2", "route": "IV", "regimen": "q 1week.", "conf": 0.9},
                    {"no": 4, "name": "MANIDIPINE", "dose": "20", "unit": "mg", "freq_a": "1", "freq_b": "1", "route": "Oral", "regimen": "p.c.", "handwritten": "hold วันฟอก", "conf": 0.9},
                    {"no": 7, "name": "FERROUS FUMARATE", "dose": "200", "unit": "mg", "freq_a": "3", "freq_b": "1", "route": "Oral", "regimen": "hs.", "conf": 0.9},
                    {"no": 12, "name": "CALCIUM CARBONATE", "dose": "1500", "unit": "mg", "freq_a": "2", "freq_b": "4", "route": "Oral", "regimen": "with meal", "conf": 0.85}]}


class TestParsing:
    def test_sig_codes(self):
        assert m.parse_sig_code("11pt")["text"] == "1 เม็ด วันละ 1 ครั้ง หลังอาหาร"
        assert m.parse_sig_code("31ht") == {"tabs": 3, "times": 1, "timing": "ก่อนนอน", "text": "3 เม็ด วันละ 1 ครั้ง ก่อนนอน"}
        assert m.parse_sig_code("12at")["timing"] == "ก่อนอาหาร" and m.parse_sig_code("33pt")["times"] == 3
        assert m.parse_sig_code("00pt") is None and m.parse_sig_code("abc") is None and m.parse_sig_code(None) is None

    def test_formulary_aliases_and_misspelling(self):
        assert m.canonical_drug("Epiao (Vial)") == ("epoetin alfa", False) and m.canonical_drug("Hema-plus")[0] == "epoetin alfa"
        assert m.canonical_drug("VENOFER 100 mg/5ml")[0] == "iron sucrose"
        assert m.canonical_drug("atrovastatin") == ("atorvastatin", False) or m.canonical_drug("atrovastatin")[0] == "atorvastatin"
        assert m.canonical_drug("calcium mgSO4")[0] == "calcium carbonate"
        assert m.canonical_drug("SL ISOSORBIDE SUBLINGUAL 5 MG")[0] == "isosorbide dinitrate"
        assert m.canonical_drug("isosorbide-5 MONOnitrate 20 MG")[0] == "isosorbide mononitrate"
        assert m.canonical_drug("zzz-unknown") == (None, False)
        assert m._strength_from_name("Calciferol (D2) 20,000 u.") == (20000.0, "unit")
        assert m._strength_from_name("0.9% NSS 100 ML") == (100.0, "ml")


class TestAnalyze:
    def test_hospital_form(self):
        a = m.analyze(HOSP)
        by = {it["canonical"]: it for it in a["items"]}
        assert a["form"] == "hospital" and a["header"]["hn_norm"] == "342226" and a["header"]["allergies"] == ["CLINDAMYCIN", "MEROPENAM"]
        assert by["atorvastatin"]["per_dose"] == 40 and by["atorvastatin"]["frequency"].startswith("1 เม็ด วันละ 1 ครั้ง")
        assert by["ferrous fumarate"]["per_dose"] == 600 and by["ferrous fumarate"]["times_per_day"] == 1
        assert by["sodium bicarbonate"]["per_dose"] == 900 and by["sodium bicarbonate"]["times_per_day"] == 2
        assert by["losartan"]["status"] == "off"
        assert by["iron sucrose"]["route"] == "IV" and "15" in by["iron sucrose"]["frequency"]
        assert by["ergocalciferol"]["per_dose"] == 20000 and by["ergocalciferol"]["unit"] == "unit"

    def test_clinic_form_flags(self):
        a = m.analyze(CLINIC)
        by = {it["canonical"]: it for it in a["items"]}
        assert by["epoetin alfa"]["unit"] == "unit" and by["epoetin alfa"]["frequency"] == "1 × 2 q 1week."
        assert by["manidipine"]["status"] == "hold" and "hold" in by["manidipine"]["handwritten"]
        assert by["ferrous fumarate"]["per_dose"] == 600 and by["calcium carbonate"]["per_dose"] == 3000
        bad = m.analyze({"form": "clinic", "header": {}, "items": [{"name": "Epiao", "dose": "4000", "unit": "mg", "route": "IV", "conf": 0.95},
                                                                     {"name": "senna", "dose": "0", "unit": "mg", "route": "Oral", "conf": 0.9}]})
        f0, f1 = bad["items"][0], bad["items"][1]
        assert "unit_mismatch:mg→unit" in f0["flags"] and f0["unit"] == "unit" and f0["conf"] <= 0.7
        assert "zero_dose" in f1["flags"]

    def test_merge_passes_agree_and_conflict(self):
        p1, p2 = m.analyze(HOSP), m.analyze(HOSP)
        r = m.merge_passes(p1, p2)
        assert r["passes"] == 2 and len(r["items"]) == 6 and r["uncertain"] == [] and r["header"]["allergies"]
        alt = json.loads(json.dumps(HOSP))
        alt["items"][0]["sig_code"] = "12ht"          # atorvastatin ความถี่ต่างกัน
        del alt["items"][-1]                          # calciferol หายไปในรอบ 2
        r2 = m.merge_passes(p1, m.analyze(alt))
        unc = {u["canonical"]: u for u in r2["uncertain"]}
        assert "atorvastatin" in unc and any(f.startswith("pass_conflict") for f in unc["atorvastatin"]["flags"])
        assert "ergocalciferol" in unc and "only_in_pass1" in unc["ergocalciferol"]["flags"]
        assert len(r2["items"]) == 4

    def test_drafts_and_summary(self):
        r = m.merge_passes(m.analyze(HOSP), None)
        d = m.to_drafts(r)
        names = [x["drug"] for x in d]
        assert "Atorvastatin 40 mg" in names and any(x["status"] == "off" and "หยุด" in x["notes"] for x in d)
        iron = [x for x in d if x["canonical"] == "iron sucrose"][0]
        assert iron["route"] == "IV" and iron["dose"] == "100 mg"
        lines = m.summary_lines(r, {"patient_name": "นาง ก", "hn": "6526", "status": "matched"})
        assert any("แพ้ยา" in x for x in lines) and any("⏹ losartan" in x for x in lines)
        d2 = m.to_drafts(m.merge_passes(m.analyze(CLINIC), None))
        hold = [x for x in d2 if x["canonical"] == "manidipine"][0]
        assert hold["status"] == "hold" and "ลายมือ" in hold["notes"]


class FakeCur:
    def __init__(self, answers=None):
        self.sql, self.answers, self.rowcount = [], list(answers or []), 2

    def execute(self, q, p=None):
        self.sql.append((" ".join(q.split()), p))

    def fetchone(self):
        return self.answers.pop(0) if self.answers else None

    def fetchall(self):
        return self.answers.pop(0) if self.answers else []


class TestSamples:
    def test_record_mark_report(self):
        cur = FakeCur()
        drafts = m.to_drafts(m.merge_passes(m.analyze(HOSP), None))
        m.record_samples(cur, "abc123", "hospital", "6526", drafts, list(range(1, len(drafts) + 1)))
        ins = [p for q, p in cur.sql if q.startswith("INSERT INTO hd_med_sheet_samples")]
        assert len(ins) == len(drafts) and ins[0][0] == "abc123" and ins[0][1] == "hospital"
        assert m.mark_outcome(cur, [1, 2], "accepted", "dr") == 2 and m.mark_outcome(cur, [], "rejected", "dr") == 0
        cur2 = FakeCur([[("hospital", "accepted", 40), ("clinic", "rejected", 1)], (12,), [("x", "1", "1", "rejected", "")]])
        rep = m.accuracy_report(cur2, 30)
        assert rep["sheets"] == 12 and rep["accuracy_pct"] == 97.6 and rep["gate"]["ok"] is False and len(rep["wrong"]) == 1

    def test_read_sheet_with_fake_vision(self):
        class R:
            status_code = 200

            def __init__(self, payload):
                self._p = payload

            def json(self):
                return {"content": [{"type": "text", "text": json.dumps(self._p, ensure_ascii=False)}], "stop_reason": "end_turn"}
        calls = []

        def post(body):
            calls.append(json.loads(body))
            return R(HOSP)
        from PIL import Image
        import io
        buf = io.BytesIO()
        Image.new("RGB", (60, 80), (255, 255, 255)).save(buf, "PNG")
        res = m.read_sheet(buf.getvalue(), "k", "claude-x", double=True, post_fn=post)
        assert res and res["passes"] == 2 and len(res["items"]) == 6 and len(calls) == 2 and res["read_id"]
        assert "ล่างสุดขึ้นบน" in calls[1]["messages"][0]["content"][1]["text"]


class TestWiring:
    def test_routes_and_templates(self):
        bp = open(os.path.join(ROOT, "hd_module", "blueprint.py"), encoding="utf-8").read()
        for needle in ('"/med-sheet/read", methods=["POST"]', '"/med-sheet/drafts", methods=["POST"]', '"/med-sheet/accuracy"',
                       '@hd_web.route("/med-sheet-test")', "mark_outcome(c2,", "'hd_sheet'"):
            assert needle in bp, needle
        base = open(os.path.join(ROOT, "hd_module", "templates", "hd_base.html"), encoding="utf-8").read()
        assert "/hd/med-sheet-test" in base
        assert os.path.exists(os.path.join(ROOT, "hd_module", "templates", "hd_med_sheet_test.html"))
