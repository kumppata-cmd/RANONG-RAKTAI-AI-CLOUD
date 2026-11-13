"""RO water check — command parser + limits (no DB)."""
import unittest
from datetime import date

from hd_module.water_check import DEFAULTS, evaluate, parse_command, reply_text


class ParseTests(unittest.TestCase):
    def test_basic(self):
        p = parse_command("น้ำRO H 0 F 0.1 T 0")
        self.assertEqual((p["hardness"], p["free"], p["total"]), (0.0, 0.1, 0.0))
        self.assertIsNone(p["shift"]); self.assertEqual(p["note"], "")

    def test_prefix_variants_and_words(self):
        p = parse_command("@รักษ์ไต น้ำ RO เช้า hardness 10 free 0.3 total 0.2 หลังถังคาร์บอน ขวดใหม่")
        self.assertEqual((p["hardness"], p["free"], p["total"], p["shift"]), (10.0, 0.3, 0.2, "morning"))
        self.assertIn("คาร์บอน", p["point"]); self.assertEqual(p["note"], "ขวดใหม่")
        p = parse_command("water H>50 F0.5 T0.1 12/11")
        self.assertEqual(p["hardness"], 50.0); self.assertTrue(p["hardness_gt"]); self.assertEqual(p["date"].day, 12)

    def test_not_a_command(self):
        self.assertIsNone(parse_command("@รักษ์ไต เตียง"))
        self.assertIsNone(parse_command("วันนี้น้ำท่วม"))
        self.assertEqual(parse_command("น้ำRO"), {"help": True})

    def test_thai_year(self):
        self.assertEqual(parse_command("น้ำRO T 0 5/11/2569")["date"], date(2026, 11, 5))


class EvaluateTests(unittest.TestCase):
    def test_pass(self):
        ev = evaluate({"hardness": 0, "free": 0.1, "total": 0.0})
        self.assertEqual(ev["status"], "pass"); self.assertEqual(ev["fails"], [])

    def test_fail_total_and_hardness(self):
        ev = evaluate({"hardness": 10, "hardness_gt": False, "free": 0.2, "total": 0.2})
        self.assertEqual(ev["status"], "fail"); self.assertEqual(len(ev["fails"]), 2)
        self.assertTrue(any("คลอรามีน" in f for f in ev["fails"]))

    def test_partial_and_swap_warning(self):
        ev = evaluate({"hardness": None, "free": 0.4, "total": 0.0})
        self.assertEqual(ev["status"], "partial"); self.assertIn("Hardness", ev["missing"])
        self.assertTrue(any("สลับ" in l for l in ev["lines"]))

    def test_custom_limits(self):
        ev = evaluate({"hardness": 10, "free": 0.1, "total": 0.05}, {"water_hardness_max": 17})
        self.assertEqual(ev["status"], "pass")

    def test_reply(self):
        ev = evaluate({"hardness": 0, "free": 0.1, "total": 0.3})
        txt = reply_text({"date": date(2026, 11, 13), "shift": "morning", "sample_point": "หลังถังคาร์บอน"}, ev, "พยาบาล ก", 2, True)
        self.assertIn("13/11/2569", txt); self.assertIn("❌ ไม่ผ่าน", txt); self.assertIn("แนบรูป 2 รูป", txt); self.assertIn("Drive", txt)


if __name__ == "__main__":
    unittest.main()
