"""Smart Bed Scheduler — planning + isolation rules (no DB)."""
import unittest
from datetime import date

from hd_module.bed_scheduler import check_bed, line_text, parse_beds, plan_day, plan_week


def P(i, name, days, shift="morning", bed=None, hbv=False, hcv=False):
    return {"id": i, "hn": str(100 + i), "name": name, "days": days, "access": "AVF", "per_week": 3,
            "usual_shift": shift, "usual_bed": bed, "hbv": hbv, "hcv": hcv}


def CTX(**kw):
    c = {"total_beds": 6, "hbv_beds": [5, 6], "hcv_beds": [], "bad_beds": {}, "patients": []}
    c.update(kw); return c


class SchedulerTests(unittest.TestCase):
    def test_parse_beds(self):
        self.assertEqual(parse_beds("24, 25"), [24, 25]); self.assertEqual(parse_beds("20-22,25"), [20, 21, 22, 25]); self.assertEqual(parse_beds(""), [])

    def test_hbv_isolation(self):
        ctx = CTX(patients=[P(1, "ก", ["จ"], bed=1), P(2, "ข", ["จ"], bed=5), P(3, "ค", ["จ"], hbv=True, bed=2)])
        d = plan_day(ctx, date(2026, 11, 16))   # Monday
        m = {a["name"]: a for a in d["shifts"]["morning"]}
        self.assertIn(m["ค"]["bed"], (5, 6))              # HBV goes to the zone even though usual bed was 2
        self.assertNotIn(m["ข"]["bed"], (5, 6))            # non-HBV pushed out of the HBV zone
        self.assertTrue(m["ข"]["moved"]); self.assertEqual(m["ก"]["bed"], 1)
        self.assertEqual(d["unassigned"], [])

    def test_bad_machine_and_overflow(self):
        ctx = CTX(total_beds=3, hbv_beds=[], bad_beds={2: "เครื่องซ่อม"},
                  patients=[P(i, f"p{i}", ["อ"], bed=i) for i in range(1, 5)])
        d = plan_day(ctx, date(2026, 11, 17))   # Tuesday
        beds = [a["bed"] for a in d["shifts"]["morning"]]
        self.assertNotIn(2, beds); self.assertEqual(len(beds), 2); self.assertEqual(len(d["unassigned"]), 2)

    def test_check_bed_warnings(self):
        ctx = CTX()
        self.assertTrue(any(w.startswith("⛔") for w in check_bed(ctx, P(1, "x", [], hbv=True), 1)))
        self.assertTrue(any(w.startswith("⛔") for w in check_bed(ctx, P(1, "x", []), 5)))
        self.assertEqual(check_bed(ctx, P(1, "x", []), 1), [])

    def test_week_and_text(self):
        ctx = CTX(patients=[P(1, "ก ข", ["จ", "พ", "ศ"], bed=1, hbv=True)])
        w = plan_week(ctx, date(2026, 11, 16))
        self.assertEqual(len(w["days"]), 7); self.assertEqual(w["hbv_patients"], 1)
        self.assertEqual(sum(len(dd["shifts"]["morning"]) for dd in w["days"]), 3)
        t = line_text(w["days"][0], "https://x")
        self.assertIn("5:ก🅱", t); self.assertIn("/hd/scheduler", t)


if __name__ == "__main__":
    unittest.main()
