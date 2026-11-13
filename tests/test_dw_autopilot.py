"""Dry Weight Autopilot + UF Planner — pure logic."""
import unittest

from hd_module.dw_autopilot import plan_uf, review_dry_weight


class UfPlannerTests(unittest.TestCase):
    def test_ok(self):
        p = plan_uf(52.0, 50.0, 4)
        self.assertEqual((p["uf_ml"], p["rate"], p["level"]), (2000, 10.0, "ok"))
        self.assertEqual(p["options"], [])

    def test_high_offers_time_and_cap(self):
        p = plan_uf(53.2, 50.0, 4)          # 3.2 L → 16 mL/kg/h
        self.assertEqual(p["level"], "high")
        types = [o["type"] for o in p["options"]]
        self.assertEqual(types, ["time", "cap"])
        self.assertEqual(p["options"][0]["hours"], 5.0)
        self.assertEqual(p["options"][1]["uf_ml"], 2600)
        self.assertTrue(any("จำกัดน้ำ" in a for a in p["advice"]))

    def test_caution(self):
        self.assertEqual(plan_uf(52.4, 50.0, 4)["level"], "caution")

    def test_missing(self):
        self.assertEqual(plan_uf(None, 50)["level"], "unknown")


def _s(post, idh=False, cramp=False, post_sbp=130, edema=False, uf_goal=2000, actual_uf=2000):
    return {"post_weight": post, "idh": idh, "cramp": cramp, "post_sbp": post_sbp, "edema": edema,
            "uf_goal": uf_goal, "actual_uf": actual_uf}


class DwReviewTests(unittest.TestCase):
    def test_too_low(self):
        ss = [_s(50.0, idh=True), _s(49.9, cramp=True), _s(50.1), _s(50.0, idh=True), _s(49.8, idh=True, post_sbp=95), _s(50.0)]
        r = review_dry_weight(ss, 50.0)
        self.assertEqual(r["direction"], "up"); self.assertEqual(r["proposed_dw"], 50.5)

    def test_too_high(self):
        ss = [_s(50.6, post_sbp=165, actual_uf=1500), _s(50.5, post_sbp=170, actual_uf=1400), _s(50.8, edema=True),
              _s(50.4, post_sbp=162), _s(50.7, actual_uf=1600), _s(50.3)]
        r = review_dry_weight(ss, 50.0)
        self.assertEqual(r["direction"], "down"); self.assertEqual(r["proposed_dw"], 49.5)

    def test_stable_below(self):
        ss = [_s(49.3), _s(49.4), _s(49.2), _s(49.5), _s(49.3), _s(49.4)]
        r = review_dry_weight(ss, 50.0)
        self.assertEqual(r["direction"], "down"); self.assertAlmostEqual(r["proposed_dw"], 49.35, delta=0.06)

    def test_keep(self):
        ss = [_s(50.0), _s(50.1), _s(49.9), _s(50.0), _s(50.2), _s(50.0)]
        self.assertIsNone(review_dry_weight(ss, 50.0))
        self.assertIsNone(review_dry_weight(ss[:3], 50.0))


if __name__ == "__main__":
    unittest.main()
