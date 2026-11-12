"""VA Watch — pure assessment logic (ว้าว #2). No DB."""
import unittest

from hd_module.va_watch import assess, brief_lines, summarize_session


def _sess(vps, bfr=300, ap=-150):
    return [{"date": f"2026-10-{i+1:02d}", "vp": v, "bfr": bfr, "ap": ap} for i, v in enumerate(vps)]


class SummarizeTests(unittest.TestCase):
    def test_medians(self):
        s = summarize_session([(300, 120, -150), (300, 140, -160), (280, 130, None)])
        self.assertEqual(s["bfr"], 300)
        self.assertEqual(s["vp"], 130)
        self.assertEqual(s["ap"], -155)
        self.assertEqual(s["n"], 3)


class AssessTests(unittest.TestCase):
    def test_stable_ok(self):
        a = assess(_sess([120, 125, 118, 122, 130, 124]), prescribed_bfr=300, ktv=1.4)
        self.assertEqual(a["level"], "ok")
        self.assertEqual(a["signals"], [])

    def test_vp_high_at_low_bfr(self):
        a = assess(_sess([210], bfr=280))
        self.assertEqual(a["score"], 25)
        self.assertIn("outflow", a["signals"][0]["text"])

    def test_catheter_ignores_vp_rule(self):
        a = assess(_sess([210], bfr=280), access_type="Perm cath")
        self.assertEqual(a["score"], 0)

    def test_rising_trend(self):
        a = assess(_sess([120, 125, 120, 140, 150, 160, 165]))
        texts = " ".join(s["text"] for s in a["signals"])
        self.assertIn("baseline", texts)
        self.assertGreaterEqual(a["score"], 25)

    def test_ap_very_negative_and_bfr_short(self):
        a = assess(_sess([130, 130, 130], bfr=230, ap=-260), prescribed_bfr=300)
        self.assertEqual(a["score"], 40)
        self.assertEqual(a["level"], "watch")

    def test_alert_combination(self):
        a = assess(_sess([120, 125, 120, 190, 205, 215, 230]), prescribed_bfr=350, ktv=1.1)
        self.assertEqual(a["level"], "alert")
        self.assertTrue(a["advice"])

    def test_no_data(self):
        a = assess([])
        self.assertEqual(a["level"], "ok")


class BriefTests(unittest.TestCase):
    def test_lines(self):
        items = [{"level": "alert", "name": "ก", "hn": "1", "signals": [{"text": "VP 230 — x"}]},
                 {"level": "ok", "name": "ข", "hn": "2", "signals": []}]
        L = brief_lines(items)
        self.assertEqual(len(L), 2)
        self.assertIn("1 คน", L[0])
        self.assertEqual(brief_lines([items[1]]), [])


if __name__ == "__main__":
    unittest.main()
