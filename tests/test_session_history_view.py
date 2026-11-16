"""ดูรอบฟอกย้อนหลัง (16 พ.ย. 2569): Pre-HD ต้องถูกบันทึกตอนเริ่มรอบ และ GET รอบต้องคืนข้อมูลครบ"""
import unittest
from datetime import datetime
from unittest import mock

from flask import Flask, g

from hd_module import blueprint as bp


class FakeCur:
    def __init__(self, answers=None, fail_on=()):
        self.sql, self.args, self.answers, self.fail_on = [], [], list(answers or []), fail_on
        self._last = None

    def execute(self, sql, args=None):
        if any(k in sql for k in self.fail_on) and "SAVEPOINT" not in sql:
            raise RuntimeError("boom")
        self.sql.append(sql)
        self.args.append(args)
        self._last = sql

    def fetchone(self):
        return self.answers.pop(0) if self.answers else None

    def fetchall(self):
        r = self.answers.pop(0) if self.answers else []
        return r


class SavePreHdTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask("t")

    def _run(self, data, **kw):
        cur = FakeCur(**kw)
        with self.app.test_request_context():
            g.hd_user_id = 7
            bp._save_pre_hd(cur, 86, data)
        return cur

    def test_pre_vitals_and_session_fields_saved(self):
        cur = self._run({"pre_sbp": 130, "pre_dbp": 80, "pre_hr": 82, "pre_temp": 36.6, "pre_spo2": 98,
                         "pre_symptoms": ["บวม", "เหนื่อย"], "dialyzer_type": "FX80", "dialyzer_reuse": 3,
                         "vascular_access": "AVF", "hep_drug": "standard", "hep_bolus": 2000, "hep_maintenance": 500})
        ins = [a for s, a in zip(cur.sql, cur.args) if "INSERT INTO hd_vital_signs" in s]
        self.assertEqual(len(ins), 1)
        self.assertEqual(ins[0][:6], (86, 130, 80, 82, 36.6, 98))
        self.assertEqual(ins[0][6], "บวม, เหนื่อย")
        upd = [a for s, a in zip(cur.sql, cur.args) if "UPDATE hd_sessions SET pre_symptoms" in s][0]
        self.assertEqual(upd[:4], ("บวม, เหนื่อย", "FX80", 3, "AVF"))
        self.assertEqual(upd[4], "standard 2000+500/h")

    def test_no_vitals_no_insert_and_out_of_range_dropped(self):
        cur = self._run({"pre_sbp": None, "pre_hr": 999})
        self.assertFalse(any("INSERT INTO hd_vital_signs" in s for s in cur.sql))

    def test_failure_is_contained(self):
        cur = self._run({"pre_sbp": 120}, fail_on=("INSERT INTO hd_vital_signs", "UPDATE hd_sessions"))
        self.assertIn("ROLLBACK TO SAVEPOINT sp_pre_vital", cur.sql)
        self.assertIn("ROLLBACK TO SAVEPOINT sp_pre_sess", cur.sql)


class ExtrasTests(unittest.TestCase):
    def test_extras_collects_everything(self):
        t = datetime(2026, 11, 16, 2, 8)
        cur = FakeCur(answers=[
            ("morning", "FX80", 3, "AVF", "std 2000", "บวม", "ตะคริว", True, "Darbepoetin", "8000", "IV", None,
             10.5, 31.5, 1.3, 70.0),
            [("pre", 130, 80, 82, 36.6, 98, None, t)],
            [("post", 118, 70, 78, None, None, None, t)],
            ("standard", 2000, 500),
            ("FX80", 3, 20),
            ("แขนซ้าย",),
            ("พยาบาล ก",),
        ])
        out = bp._session_record_extras(cur, 86, 5)
        self.assertEqual(out["pre_vitals"]["sbp"], 130)
        self.assertEqual(out["post_vitals"]["sbp"], 118)
        self.assertEqual((out["pre_symptoms"], out["post_symptoms"]), ("บวม", "ตะคริว"))
        self.assertEqual(out["heparin"], {"protocol": "standard", "bolus": 2000, "maintenance": 500})
        self.assertEqual((out["dialyzer_type"], out["dialyzer_reuse"], out["dialyzer_max"]), ("FX80", 3, 20))
        self.assertEqual((out["va_location"], out["nurse_name"], out["hb_value"]), ("แขนซ้าย", "พยาบาล ก", 10.5))

    def test_missing_columns_do_not_break(self):
        cur = FakeCur(fail_on=("pre_symptoms",))
        out = bp._session_record_extras(cur, 86, 5)
        self.assertIsInstance(out, dict)
        self.assertNotIn("pre_symptoms", out)


if __name__ == "__main__":
    unittest.main()


class DialyzerReuseTests(unittest.TestCase):
    def test_counts_on_from_last_session(self):
        self.assertEqual(bp._next_dialyzer_reuse(None, "HF80", (1, "HF80", 2, 20)), 3)

    def test_nurse_value_wins(self):
        self.assertEqual(bp._next_dialyzer_reuse(5, "HF80", (1, "HF80", 2, 20)), 5)

    def test_new_type_or_max_starts_at_one(self):
        self.assertEqual(bp._next_dialyzer_reuse(None, "FX80", (1, "HF80", 2, 20)), 1)
        self.assertEqual(bp._next_dialyzer_reuse(0, "HF80", (1, "HF80", 20, 20)), 1)
        self.assertEqual(bp._next_dialyzer_reuse(None, "HF80", None), 1)
