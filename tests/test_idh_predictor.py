"""ว้าว #1: IDH predictor scoring is transparent and bounded."""
import unittest

from hd_module.idh_predictor import score


class IdhScoreTests(unittest.TestCase):
    def test_low_risk_baseline(self):
        r = score({"uf_goal": 1500, "dry_weight": 60, "duration_hours": 4, "pre_weight": 61.2,
                   "sbp_series": [140, 138, 136], "idh_history": 0, "hb": 11, "albumin": 3.8, "age": 55})
        self.assertEqual(r["level"], "low"); self.assertEqual(r["factors"], [])

    def test_high_risk_stack(self):
        r = score({"uf_goal": 3400, "dry_weight": 60, "duration_hours": 4,      # 14.2 mL/kg/h → +25
                   "pre_weight": 63.0,                                           # 5% → +15
                   "sbp_series": [150, 135, 120],                                # fall ≥10 twice → +20
                   "idh_history": 3, "hb": 9.2, "albumin": 3.1, "age": 74})      # +15 +5 +5 +5
        self.assertEqual(r["level"], "high"); self.assertEqual(r["score"], 90)
        texts = " ".join(f["text"] for f in r["factors"])
        self.assertIn("UF 14.2", texts); self.assertIn("IDWG 3.0", texts); self.assertIn("150→135→120", texts)
        self.assertIn("ทุก 15 นาที", r["advice"])

    def test_moderate_and_cap(self):
        r = score({"uf_goal": 2600, "dry_weight": 60, "duration_hours": 4, "sbp_series": [105], "idh_history": 1})
        self.assertEqual((r["score"], r["level"]), (30, "low"))        # 12 + 10 + 8
        r = score({"uf_goal": 2600, "dry_weight": 60, "duration_hours": 4, "sbp_series": [98], "idh_history": 3})
        self.assertEqual((r["score"], r["level"]), (42, "moderate"))   # 12 + 15 + 15
        r2 = score({"uf_goal": 4000, "dry_weight": 50, "duration_hours": 3, "pre_weight": 55, "sbp_series": [130, 110, 95],
                    "idh_history": 5, "hb": 7, "albumin": 2.5, "age": 80, "pre_sbp": 170})
        self.assertEqual(r2["score"], 100)

    def test_garbage_input(self):
        r = score({"uf_goal": "x", "dry_weight": None, "sbp_series": ["140", None, "120"]})
        self.assertIn(r["level"], ("low", "moderate")); self.assertIsInstance(r["score"], int)


if __name__ == "__main__":
    unittest.main()
