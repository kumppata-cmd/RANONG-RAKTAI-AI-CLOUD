"""Anemia Command Center — pure assessment logic."""
import unittest
from datetime import date

from hd_module.anemia import assess, hb_slope, parse_iu


class HelperTests(unittest.TestCase):
    def test_parse_iu(self):
        self.assertEqual(parse_iu("4000 IU"), 4000)
        self.assertEqual(parse_iu("4,000u"), 4000)
        self.assertEqual(parse_iu("40 mcg"), 8000)
        self.assertIsNone(parse_iu(""))

    def test_slope(self):
        pts = [(date(2026, 9, 1), 9.0), (date(2026, 9, 29), 9.5), (date(2026, 10, 27), 10.0)]
        self.assertAlmostEqual(hb_slope(pts), 0.5, places=1)
        self.assertIsNone(hb_slope(pts[:1]))
        self.assertIsNone(hb_slope([(date(2026, 9, 1), 9.0), (date(2026, 9, 3), 9.5)]))


class AssessTests(unittest.TestCase):
    def base(self, **kw):
        p = {"hb": 9.2, "ferritin": 350, "tsat": 28, "esa_weekly_iu": 12000, "weight": 55, "hb_points": []}
        p.update(kw); return p

    def test_iron_first(self):
        a = assess(self.base(ferritin=120, tsat=15))
        self.assertEqual(a["tag"], "iron_first"); self.assertEqual(a["priority"], 3); self.assertEqual(a["iron_status"], "deficient")

    def test_hyporesponse(self):
        a = assess(self.base(esa_weekly_iu=12000, weight=50, hb=9.0))   # ERI = 12000/50/9 = 26.7
        self.assertEqual(a["tag"], "hyporesponse"); self.assertGreaterEqual(a["eri"], 15)

    def test_esa_up(self):
        a = assess(self.base(esa_weekly_iu=3000, weight=60, hb=9.5))     # ERI 5.3
        self.assertEqual(a["tag"], "esa_up")

    def test_check_iron(self):
        self.assertEqual(assess(self.base(ferritin=None, tsat=None))["tag"], "check_iron")

    def test_hb_high_and_ok(self):
        self.assertEqual(assess(self.base(hb=12.8))["tag"], "hb_high")
        self.assertEqual(assess(self.base(hb=11.0, esa_weekly_iu=3000))["tag"], "ok")
        self.assertEqual(assess(self.base(hb=11.0, ferritin=150))["tag"], "iron_maint")

    def test_drifting(self):
        pts = [(date(2026, 8, 1), 12.2), (date(2026, 9, 1), 11.3), (date(2026, 10, 1), 10.4)]
        a = assess(self.base(hb=10.4, esa_weekly_iu=3000, hb_points=pts))
        self.assertEqual(a["tag"], "drifting"); self.assertLess(a["hb_4w"], 10)

    def test_overload(self):
        a = assess(self.base(hb=11.0, ferritin=1200, tsat=35, esa_weekly_iu=3000))
        self.assertIn("งดเหล็ก", " ".join(a["reasons"]))

    def test_no_hb(self):
        self.assertEqual(assess({"hb": None})["tag"], "no_hb")


if __name__ == "__main__":
    unittest.main()
