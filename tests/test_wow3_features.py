"""28 พ.ย. 2569 — หมอเงา · ถามชาร์ต · ตรวจกำยา · ฟังเสียงเส้น · Time Machine (pure-logic tests, no DB)."""
import json
import unittest
from datetime import date, timedelta

from hd_module import ai_text, ask_chart, bruit_check, pill_check, shadow_doc, time_machine


class _Resp:
    def __init__(self, text, code=200):
        self._t = text
        self.status_code = code
        self.text = text

    def json(self):
        return {"content": [{"type": "text", "text": self._t}]}


# ───────────── ai_text ─────────────
class AiTextTest(unittest.TestCase):
    def test_ask_uses_post_fn_and_extracts(self):
        seen = {}

        def post(b):
            seen["body"] = json.loads(b)
            return _Resp('```json\n{"a": 1}\n```')
        txt, err = ai_text.ask("hi", model="m", post_fn=post)
        self.assertIsNone(err)
        self.assertEqual(ai_text.extract_json(txt), {"a": 1})
        self.assertEqual(seen["body"]["model"], "m")

    def test_api_error_codes(self):
        self.assertEqual(ai_text.ask("x", post_fn=lambda b: _Resp("credit balance too low", 400))[1], "no_credit")
        self.assertEqual(ai_text.ask("x", post_fn=lambda b: _Resp("", 500), retries=0)[1], "api:500")


# ───────────── หมอเงา ─────────────
def _pt(**kw):
    p = {"id": 1, "hn": "100", "name": "นายทดสอบ ใจดี", "dry_weight": 60.0, "esa_dose": "4000 u", "is_active": True}
    p.update(kw)
    return p


class ShadowRulesTest(unittest.TestCase):
    today = date(2026, 11, 28)

    def _d(self, **kw):
        d = {"patient": _pt(), "labs": [], "meds": [], "sessions": [], "esa_doses": [], "idh_last5": None, "va": None,
             "alerts_no_order": 0, "cancelled_14d": 0, "antihbs": None}
        d.update(kw)
        return d

    def test_k_rising_with_raas(self):
        labs = [{"lab_date": self.today - timedelta(days=5), "k": 5.8}, {"lab_date": self.today - timedelta(days=35), "k": 5.5},
                {"lab_date": self.today - timedelta(days=65), "k": 5.1}]
        fs = shadow_doc.rules(self._d(labs=labs, meds=[{"drug": "Enalapril 5 mg"}]), self.today)
        rules = {f["rule"] for f in fs}
        self.assertIn("k_rising", rules)
        f = next(x for x in fs if x["rule"] == "k_rising")
        self.assertIn("Enalapril", f["text"])
        self.assertIn("5.1→5.5→5.8", f["text"])

    def test_hb_high_esa_unchanged(self):
        labs = [{"lab_date": self.today - timedelta(days=3), "hb": 13.1}]
        esa = [(self.today - timedelta(days=i * 3), "4000") for i in range(12)]
        fs = shadow_doc.rules(self._d(labs=labs, esa_doses=esa), self.today)
        f = next(x for x in fs if x["rule"] == "hb_high_esa")
        self.assertEqual(f["severity"], "high")
        self.assertIn("ขนาดเดิม", f["text"])

    def test_hb_low_iron_low(self):
        labs = [{"lab_date": self.today - timedelta(days=3), "hb": 8.2, "tsat": 14, "ferritin": 120}]
        fs = shadow_doc.rules(self._d(labs=labs), self.today)
        f = next(x for x in fs if x["rule"] == "hb_low")
        self.assertTrue(f["evidence"]["iron_low"])
        self.assertIn("เหล็ก", f["text"])

    def test_phos_binder_and_caxp(self):
        labs = [{"lab_date": self.today - timedelta(days=3), "phos": 7.4, "ca": 9.8}, {"lab_date": self.today - timedelta(days=33), "phos": 6.0}]
        fs = shadow_doc.rules(self._d(labs=labs, meds=[{"drug": "Sevelamer 800"}]), self.today)
        rules = {f["rule"]: f for f in fs}
        self.assertEqual(rules["phos_high"]["severity"], "high")
        self.assertIn("Sevelamer", rules["phos_high"]["text"])
        self.assertIn("caxp", rules)

    def test_ktv_with_access_signal(self):
        labs = [{"lab_date": self.today - timedelta(days=10), "ktv": 1.05}]
        va = {"level": "watch", "score": 30, "signals": [{"text": "VP มีแนวโน้มสูงขึ้น 18%"}]}
        fs = shadow_doc.rules(self._d(labs=labs, va=va), self.today)
        self.assertEqual(fs[0]["rule"], "ktv_access")
        self.assertEqual(fs[0]["severity"], "high")

    def test_idwg_dw_idh_missed(self):
        ses = [{"date": self.today - timedelta(days=i * 2), "pre": 63.0, "post": 60.8, "status": "completed"} for i in range(6)]
        fs = shadow_doc.rules(self._d(sessions=ses, idh_last5=3, cancelled_14d=2, labs=[{"lab_date": self.today, "hb": 11}]), self.today)
        rules = {f["rule"] for f in fs}
        self.assertTrue({"idwg_high", "idh_cluster", "dw_not_reached", "missed"} <= rules, rules)

    def test_labs_overdue_and_antihbs(self):
        labs = [{"lab_date": self.today - timedelta(days=80), "hb": 11}]
        fs = shadow_doc.rules(self._d(labs=labs, antihbs="<10"), self.today)
        rules = {f["rule"]: f for f in fs}
        self.assertEqual(rules["labs_overdue"]["severity"], "med")
        self.assertIn("antihbs_low", rules)

    def test_quiet_patient_has_nothing(self):
        labs = [{"lab_date": self.today - timedelta(days=10), "hb": 11.0, "k": 4.5, "phos": 4.8, "ca": 9.0, "albumin": 4.0, "hco3": 23, "ktv": 1.5}]
        ses = [{"date": self.today - timedelta(days=i * 2), "pre": 61.5, "post": 60.0, "status": "completed"} for i in range(6)]
        self.assertEqual(shadow_doc.rules(self._d(labs=labs, sessions=ses, idh_last5=0), self.today), [])

    def test_message_format(self):
        items = [{"id": 1, "patient_id": 1, "hn": "100", "name": "นายทดสอบ ใจดี", "rule": "k_high", "severity": "high", "text": "K 6.2"},
                 {"id": 2, "patient_id": 1, "hn": "100", "name": "นายทดสอบ ใจดี", "rule": "phos_high", "severity": "low", "text": "Phos 5.9"},
                 {"id": 3, "patient_id": 2, "hn": "200", "name": "นางสอง", "rule": "hb_low", "severity": "med", "text": "Hb 8.5"}]
        msg = shadow_doc.build_message(items, 30, 2, self.today, "https://x")
        self.assertIn("🕵️ หมอเงา", msg)
        self.assertIn("🔴 ทดสอบ (HN 100) — K 6.2", msg)
        self.assertIn("└ Phos 5.9", msg)
        self.assertIn("https://x/hd/shadow", msg)
        self.assertIsNone(shadow_doc.build_message([], 30, 0, self.today))


# ───────────── ถามชาร์ต ─────────────
class AskChartTest(unittest.TestCase):
    def test_validate_accepts_select_and_forces_limit(self):
        sql, why = ask_chart.validate_sql("SELECT p.hn, p.patient_name FROM hd_patient_mapping p WHERE p.is_active")
        self.assertIsNone(why)
        self.assertIn("LIMIT 100", sql)

    def test_validate_rejects(self):
        self.assertIsNotNone(ask_chart.validate_sql("DELETE FROM hd_sessions")[1])
        self.assertIsNotNone(ask_chart.validate_sql("SELECT 1; DROP TABLE x")[1])
        self.assertIsNotNone(ask_chart.validate_sql("SELECT * FROM hd_users")[1])
        self.assertIsNotNone(ask_chart.validate_sql("SELECT antihiv FROM hd_lab_docs")[1])
        self.assertIsNotNone(ask_chart.validate_sql("SELECT * FROM hd_labs -- x")[1])
        self.assertIsNotNone(ask_chart.validate_sql("SELECT pg_sleep(10)")[1])

    def test_validate_cte_and_limit_cap(self):
        sql, why = ask_chart.validate_sql("WITH last AS (SELECT DISTINCT ON (patient_id) * FROM hd_labs ORDER BY patient_id, lab_date DESC) "
                                          "SELECT p.hn, l.hb FROM last l JOIN hd_patient_mapping p ON p.id = l.patient_id LIMIT 5000")
        self.assertIsNone(why)
        self.assertIn("LIMIT 100", sql)
        self.assertNotIn("5000", sql)

    def test_schema_excludes_hiv(self):
        self.assertNotIn("hiv", ask_chart.schema_card().lower())

    def test_answer_pipeline_with_fake_db(self):
        class Cur:
            description = [("hn",), ("hb",)]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=None):
                self.last = sql

            def fetchmany(self, n):
                return [("100", 8.4), ("200", 8.9)]

            def fetchall(self):
                return []

        class Conn:
            def cursor(self):
                return Cur()

            def commit(self):
                pass

            def rollback(self):
                pass
        calls = []

        def post(b):
            body = json.loads(b)
            calls.append(body)
            if "คำถาม:" in body["messages"][0]["content"] and "ข้อมูล(JSON)" not in body["messages"][0]["content"]:
                return _Resp(json.dumps({"sql": "SELECT p.hn, l.hb FROM hd_labs l JOIN hd_patient_mapping p ON p.id = l.patient_id WHERE l.hb < 9", "explain": "Hb < 9"}))
            return _Resp("พบ 2 คน Hb ต่ำกว่า 9: HN 100 (8.4), HN 200 (8.9)")
        res = ask_chart.answer(Conn(), "ใครบ้าง Hb ต่ำกว่า 9", who="t", channel="web", post_fn=post, model="m")
        self.assertIsNone(res["error"])
        self.assertIn("พบ 2 คน", res["text"])
        self.assertEqual(res["rows"], [["100", "8.4"], ["200", "8.9"]])
        self.assertEqual(len(calls), 2)

    def test_answer_rejects_bad_sql_from_ai(self):
        class Conn:
            def cursor(self):
                raise AssertionError("must not run")

            def commit(self):
                pass

            def rollback(self):
                pass
        res = ask_chart.answer(Conn(), "ลบข้อมูลทั้งหมด", post_fn=lambda b: _Resp(json.dumps({"sql": "DELETE FROM hd_sessions", "explain": ""})), model="m")
        self.assertEqual(res["error"], "reject")


# ───────────── ตรวจกำยา ─────────────
class PillCheckTest(unittest.TestCase):
    def test_slots_for_freq(self):
        self.assertEqual(pill_check.slots_for_freq("11pt"), {"morning"})
        self.assertEqual(pill_check.slots_for_freq("13pt"), {"morning", "noon", "evening"})
        self.assertEqual(pill_check.slots_for_freq("1x2 pc"), {"morning", "evening"})
        self.assertEqual(pill_check.slots_for_freq("hs"), {"bedtime"})
        self.assertEqual(pill_check.slots_for_freq("1 เม็ด ก่อนนอน"), {"bedtime"})
        self.assertEqual(pill_check.slots_for_freq("tid"), {"morning", "noon", "evening"})
        self.assertEqual(pill_check.slots_for_freq("TIW หลังฟอก"), set())

    def test_slot_now(self):
        from datetime import datetime
        self.assertEqual(pill_check.slot_now(datetime(2026, 1, 1, 7)), "morning")
        self.assertEqual(pill_check.slot_now(datetime(2026, 1, 1, 12)), "noon")
        self.assertEqual(pill_check.slot_now(datetime(2026, 1, 1, 18)), "evening")
        self.assertEqual(pill_check.slot_now(datetime(2026, 1, 1, 21)), "bedtime")

    def test_analyze_parses_and_downgrades_low_confidence(self):
        exp = [{"drug": "Sevelamer", "dose": "800 mg", "freq": "13pt", "tabs": 1}]
        res = pill_check.analyze(b"\xff\xd8fake", exp, ref_img=b"\xff\xd8ref", slot="morning", api_key="k", model_id="m",
                                 post_fn=lambda b: _Resp(json.dumps({"pills_seen": 3, "verdict": "ok", "confidence": 0.3, "items": []})))
        self.assertEqual(res["verdict"], "unsure")
        res = pill_check.analyze(b"\xff\xd8fake", exp, slot="morning", api_key="k", model_id="m",
                                 post_fn=lambda b: _Resp(json.dumps({"pills_seen": 2, "verdict": "missing", "missing": ["Sevelamer"], "confidence": 0.8})))
        self.assertEqual(res["verdict"], "missing")
        txt = pill_check.patient_text(res, "morning", exp, False)
        self.assertIn("อาจขาด: Sevelamer", txt)
        self.assertIn("แจ้งพยาบาล", txt)

    def test_prompt_mentions_reference(self):
        p = pill_check.build_prompt([{"drug": "A", "dose": "1", "freq": "11pt", "tabs": 1}], True, "evening")
        self.assertIn("รูปที่ 1", p)
        self.assertIn("มื้อเย็น", p)


# ───────────── ฟังเสียงเส้น ─────────────
def _feat(**kw):
    f = {"duration_s": 10, "rms": 0.05, "centroid_hz": 300, "peak_hz": 180, "flatness": 0.3, "pulse_bpm": 72, "pulsatility": 1.8,
         "clipping_pct": 0, "band_lo": 0.5, "band_mid": 0.4, "band_hi": 0.07, "band_vhi": 0.03}
    f.update(kw)
    return f


class BruitTest(unittest.TestCase):
    def test_quality_gate(self):
        self.assertEqual(bruit_check.quality_of(bruit_check.clean_features(_feat()))[0], "good")
        self.assertEqual(bruit_check.quality_of(bruit_check.clean_features(_feat(duration_s=3)))[0], "poor")
        self.assertEqual(bruit_check.quality_of(bruit_check.clean_features(_feat(pulse_bpm=None)))[0], "poor")
        self.assertEqual(bruit_check.quality_of(bruit_check.clean_features(_feat(clipping_pct=8)))[0], "poor")
        self.assertIsNone(bruit_check.clean_features({"junk": 1}))

    def test_baseline_and_score(self):
        base = bruit_check.baseline_from([_feat(), _feat(centroid_hz=320), _feat(centroid_hz=290)])
        self.assertAlmostEqual(base["centroid_hz"], 300)
        ok = bruit_check.score(_feat(centroid_hz=310), base)
        self.assertEqual(ok["level"], "ok")
        bad = bruit_check.score(_feat(centroid_hz=420, band_hi=0.2, band_vhi=0.08, pulsatility=2.8, rms=0.02), base, va_level="watch")
        self.assertEqual(bad["level"], "alert")
        self.assertTrue(any("แหลมขึ้น" in s["text"] for s in bad["signals"]))
        self.assertEqual(bruit_check.score(_feat(), None)["level"], "baseline")
        self.assertIsNone(bruit_check.baseline_from([_feat()]))

    def test_patient_text(self):
        self.assertIn("1/3", bruit_check.patient_text({"level": "baseline"}, True, 1))
        t = bruit_check.patient_text({"level": "alert", "score": 60, "signals": [{"text": "x"}]}, False, 3)
        self.assertIn("แจ้งพยาบาล", t)
        self.assertIn("ไม่ใช่การวินิจฉัย", t)


# ───────────── Time Machine ─────────────
class TimeMachineTest(unittest.TestCase):
    today = date(2026, 11, 28)

    def test_theil_sen_and_projection(self):
        pts = [(self.today - timedelta(days=150), 10.8), (self.today - timedelta(days=120), 10.5), (self.today - timedelta(days=90), 10.3),
               (self.today - timedelta(days=60), 10.0), (self.today - timedelta(days=30), 9.8), (self.today, 9.6)]
        f = time_machine.project("hb", pts, self.today)
        self.assertEqual(f["confidence"], "high")
        self.assertLess(f["slope_per_month"], 0)
        self.assertAlmostEqual(f["points"][0]["v"], 9.6, delta=0.2)
        self.assertTrue(f["points"][-1]["v"] < 9.6)
        self.assertTrue(all(p["lo"] <= p["v"] <= p["hi"] for p in f["points"]))
        self.assertIsNone(f["crossing"])  # already below 10 → no new crossing

    def test_crossing_detected(self):
        pts = [(self.today - timedelta(days=90), 4.2), (self.today - timedelta(days=60), 4.6), (self.today - timedelta(days=30), 5.0), (self.today, 5.3)]
        f = time_machine.project("k", pts, self.today)
        self.assertIsNotNone(f["crossing"])
        self.assertEqual(f["crossing"]["which"], "hi")
        self.assertLess(f["crossing"]["in_days"], 60)

    def test_insufficient_points(self):
        f = time_machine.project("hb", [(self.today, 11.0)], self.today)
        self.assertEqual(f["confidence"], "none")
        self.assertEqual(f["points"], [])

    def test_risk_index_and_whatif(self):
        fc = {"albumin": {"last": {"v": 2.8}}, "hb": {"last": {"v": 8.5}, "points": [{"d": "2026-11-28", "v": 8.5, "lo": 8, "hi": 9}, {"d": "2027-02-26", "v": 8.5, "lo": 7.5, "hi": 9.5}]},
              "k": {"last": {"v": 6.2}}, "phos": {"last": {"v": 5}}, "idwg_pct": {"last": {"v": 3}}, "ktv": {"last": {"v": 1.0}}}
        r = time_machine.risk_index({"age": 80}, fc, 3, 5, 2, {"level": "alert"})
        self.assertEqual(r["level"], "high")
        self.assertTrue(any("Albumin" in c["text"] for c in r["components"]))
        w = time_machine.what_if({"forecast": fc, "patient": {"dry_weight": 60}, "fingerprint": {}}, {"esa_pct": 25})
        self.assertIn("hb", w["adjusted"])
        self.assertGreater(w["adjusted"]["hb"][-1]["v"], 8.5)
        self.assertTrue(w["notes"])


if __name__ == "__main__":
    unittest.main()
