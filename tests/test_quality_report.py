"""Adequacy estimate/options + monthly PDF rendering with synthetic data."""
import unittest

from hd_module.quality_report import adequacy_options, estimate_ktv, line_text, pdf_bytes


class AdequacyTests(unittest.TestCase):
    def test_estimate(self):
        # K=0.68*300=204 mL/min, V=0.55*60kg=33 L → 204*240/33000 = 1.48
        self.assertAlmostEqual(estimate_ktv(300, 4, 60), 1.48, places=2)
        self.assertIsNone(estimate_ktv(None, 4, 60))

    def test_options_stop_when_target_reached(self):
        o = adequacy_options(250, 3.5, 80)      # est 0.9 → both levers
        types = [x["type"] for x in o]
        self.assertIn("time", types); self.assertIn("bfr", types)
        self.assertTrue(all("≈" in x["text"] for x in o))


def _data():
    return {"year": 2026, "month": 10, "label": "ตุลาคม 2569", "active_patients": 141,
            "sessions": {"completed": 1650, "cancelled": 12, "patients": 139, "morning": 700, "afternoon": 600, "evening": 350},
            "idh_sessions": 95, "idh_rate": 5.8, "alerts": {"total": 210, "critical": 30, "with_order": 180, "pending": 4},
            "anemia": {"checked": 120, "in_target": 70, "below": 40, "above": 10, "mean": 10.4},
            "iron": {"checked": 60, "deficient": 18}, "adequacy": {"checked": 50, "adequate": 42, "mean_ktv": 1.45},
            "access": {"AVF": 102, "PC": 28, "AVG": 8, "DLC": 2}, "esa": {"given": 1400, "sessions": 1650}, "iron_doses": 40,
            "water_daily": {"checks": 20, "pass": 19, "fail": 1}, "water_lab": {"tests": 2, "pass": 2},
            "incidents": {"total": 3, "open": 1}, "esa_autopilot": {"proposed": 12, "approved": 9, "rejected": 2},
            "dw_autopilot": {"proposed": 5, "approved": 3}, "dialyzer": {"active": 130, "avg_reuse": 6.2},
            "adequacy_list": [{"hn": "100", "name": "นาย ก", "ktv": 1.05, "urr": None, "source": "measured", "est": 1.1,
                               "bfr_rx": 300, "bfr_actual": 260, "options": adequacy_options(260, 4, 70)}]}


class ReportTests(unittest.TestCase):
    def test_line_text(self):
        t = line_text(_data(), "https://x")
        self.assertIn("ตุลาคม 2569", t); self.assertIn("58%", t); self.assertIn("84%", t); self.assertIn("/hd/adequacy", t)

    def test_pdf(self):
        b = pdf_bytes(_data())
        self.assertTrue(b.startswith(b"%PDF")); self.assertGreater(len(b), 5000)


if __name__ == "__main__":
    unittest.main()
