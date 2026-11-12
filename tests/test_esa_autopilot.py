"""ESA Autopilot — pure decision logic (ว้าว #3). No DB, no LINE."""
import unittest

from hd_module.esa_autopilot import DEFAULTS, line_summary, parse_dose, propose


class ParseDoseTests(unittest.TestCase):
    def test_iu(self):
        self.assertEqual(parse_dose("4000 IU"), (4000.0, "IU"))
        self.assertEqual(parse_dose("4,000 u"), (4000.0, "IU"))
        self.assertEqual(parse_dose("Eprex 10000"), (10000.0, "IU"))

    def test_mcg(self):
        self.assertEqual(parse_dose("40 mcg"), (40.0, "mcg"))
        self.assertEqual(parse_dose("60 µg q2w"), (60.0, "mcg"))

    def test_none(self):
        self.assertEqual(parse_dose(""), (None, ""))
        self.assertEqual(parse_dose("งดชั่วคราว"), (None, ""))


class ProposeTests(unittest.TestCase):
    def test_in_target_no_change(self):
        self.assertIsNone(propose(11.0, "4000 IU"))
        self.assertIsNone(propose(10.0, "4000 IU"))
        self.assertIsNone(propose(12.0, "4000 IU"))

    def test_low_increase_25pct_rounded(self):
        p = propose(9.2, "4000 IU")
        self.assertEqual(p["action"], "increase")
        self.assertEqual(p["proposed_dose"], "5000 IU")

    def test_high_decrease(self):
        p = propose(12.6, "8000 IU")
        self.assertEqual(p["action"], "decrease")
        self.assertEqual(p["proposed_dose"], "6000 IU")

    def test_hold_above_13(self):
        p = propose(13.4, "4000 IU")
        self.assertEqual(p["action"], "hold")

    def test_mcg_rounding(self):
        p = propose(9.0, "40 mcg")
        self.assertEqual(p["proposed_dose"], "50 mcg")

    def test_review_when_no_dose(self):
        self.assertEqual(propose(8.5, "")["action"], "review")
        self.assertEqual(propose(12.8, None)["action"], "review")

    def test_resume_after_hold(self):
        self.assertIsNone(propose(12.0, "8000 IU", held=True))
        p = propose(10.5, "8000 IU", held=True)
        self.assertEqual(p["action"], "resume")
        self.assertEqual(p["proposed_dose"], "6000 IU")

    def test_custom_protocol(self):
        proto = dict(DEFAULTS, esa_target_low=11.0, esa_step_pct=50)
        p = propose(10.5, "4000 IU", proto)
        self.assertEqual(p["action"], "increase")
        self.assertEqual(p["proposed_dose"], "6000 IU")

    def test_bad_hb(self):
        self.assertIsNone(propose("abc", "4000 IU"))


class SummaryTests(unittest.TestCase):
    def test_summary(self):
        created = [{"name": "ก", "hn": "1", "hb": 9.2, "action": "increase", "current_dose": "4000 IU", "proposed_dose": "5000 IU"}]
        msg = line_summary(created, "https://x")
        self.assertIn("ESA Autopilot", msg)
        self.assertIn("เพิ่ม 5000 IU", msg)
        self.assertIn("/hd/esa", msg)
        self.assertEqual(line_summary([], "x"), "")


if __name__ == "__main__":
    unittest.main()
