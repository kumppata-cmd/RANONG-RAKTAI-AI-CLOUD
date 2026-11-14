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


class BcmSignalTests(unittest.TestCase):
    def test_set_when_no_dw(self):
        from hd_module.dw_autopilot import bcm_signal
        b = {"date": "2026-09-16", "days_ago": 59, "weight": 69.0, "oh": 3.0, "rel_oh": 19.0, "dw_bcm": 66.0}
        p = bcm_signal(b, None)
        self.assertEqual((p["direction"], p["proposed_dw"]), ("set", 66.0))
        self.assertIn("ยังไม่มีน้ำหนักแห้ง", p["rationale"])

    def test_down_is_bounded_by_step(self):
        from hd_module.dw_autopilot import bcm_signal
        b = {"date": "2026-09-16", "days_ago": 59, "weight": 69.0, "oh": 3.0, "rel_oh": 19.0, "dw_bcm": 66.0}
        p = bcm_signal(b, 68.0)
        self.assertEqual((p["direction"], p["proposed_dw"]), ("down", 67.5))
        self.assertEqual(p["evidence"]["dw_diff"], -2.0)

    def test_up_and_threshold(self):
        from hd_module.dw_autopilot import bcm_signal
        b = {"date": "2026-09-16", "days_ago": 10, "weight": 57.5, "oh": -1.5, "rel_oh": -10.0, "dw_bcm": 59.0}
        p = bcm_signal(b, 58.3)
        self.assertEqual((p["direction"], p["proposed_dw"]), ("up", 58.8))   # 0.7 kg gap → one 0.5 step
        self.assertIsNone(bcm_signal(b, 58.7))                               # 0.3 kg → inside threshold
        self.assertIsNone(bcm_signal(None, 60))


class GenerateWithBcmTests(unittest.TestCase):
    """End-to-end generate() against a fake DB shaped like production on 14 Nov 2026:
    patients have BCM results but no dry weight yet → one 'set' proposal each."""

    class Cur:
        def __init__(self):
            self.sql = ""
            self.inserts = []
            self._one = None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            self.sql, self.params = sql, params
            if "FROM hd_dw_proposals" in sql:
                self._one = None
            elif "FROM hd_fluid_assessments" in sql:
                from datetime import date
                pid = params[0]
                self._one = (date(2026, 9, 8), 73.7, 7.5, 34.2, 14.9) if pid == 1 else ((date(2026, 9, 16), 69.0, -0.3, -2.3, 6.8) if pid == 2 else None)
            elif sql.lstrip().startswith("INSERT INTO hd_dw_proposals"):
                self.inserts.append(params)
                self._one = (len(self.inserts),)

        def fetchone(self):
            return self._one

        def fetchall(self):
            if "FROM hd_patient_mapping" in self.sql:
                return [(1, "67401", "พระพงศกร", None, 0), (2, "67361", "สะรีฝ้า", None, 0), (3, "99999", "ไม่มี BCM", None, 0)]
            return []

    class Conn:
        def __init__(self):
            self.c = GenerateWithBcmTests.Cur()
            self.committed = False

        def cursor(self):
            return self.c

        def commit(self):
            self.committed = True

    def test_generate_sets_dw_from_bcm(self):
        from hd_module.dw_autopilot import generate
        conn = self.Conn()
        stats = {}
        created = generate(conn, stats=stats)
        self.assertEqual(len(created), 2)                  # both BCM patients get a 'set' proposal; the third has no BCM
        self.assertEqual([c["direction"] for c in created], ["set", "set"])
        self.assertEqual(created[0]["proposed_dw"], 66.2)  # 73.7 − 7.5
        self.assertEqual(created[1]["proposed_dw"], 69.3)  # 69.0 − (−0.3)
        self.assertIsNone(created[0]["current_dw"])
        self.assertEqual((stats["patients"], stats["with_dw"], stats["with_bcm"], stats["from_bcm"], stats["no_signal"]), (3, 0, 2, 2, 1))
        self.assertTrue(conn.committed)
        self.assertEqual(conn.c.inserts[0][4], "set")


if __name__ == "__main__":
    unittest.main()
