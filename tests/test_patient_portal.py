"""Patient Portal — unit tests."""
import unittest
from datetime import date
from hd_module.patient_portal import _next_dialysis, _interpret_hb, _interpret_ktv, _interpret_ferritin


class NextDialysisTests(unittest.TestCase):
    def test_schedule_days(self):
        r = _next_dialysis("จ,พ,ศ")
        self.assertIsNotNone(r)
        self.assertIn("date", r)
        self.assertIn("in_days", r)
        self.assertIn(r["in_days"], range(7))

    def test_empty(self):
        self.assertIsNone(_next_dialysis(""))
        self.assertIsNone(_next_dialysis(None))


class InterpretTests(unittest.TestCase):
    def test_hb(self):
        self.assertEqual(_interpret_hb(7.5)["status"], "critical")
        self.assertEqual(_interpret_hb(9.0)["status"], "low")
        self.assertEqual(_interpret_hb(11.0)["status"], "ok")
        self.assertEqual(_interpret_hb(13.5)["status"], "high")
        self.assertIn("🟢", _interpret_hb(11.0)["emoji"])

    def test_ktv(self):
        self.assertEqual(_interpret_ktv(1.5)["status"], "ok")
        self.assertEqual(_interpret_ktv(1.0)["status"], "low")
        self.assertEqual(_interpret_ktv(0.8)["status"], "critical")

    def test_ferritin(self):
        self.assertEqual(_interpret_ferritin(150)["status"], "low")
        self.assertEqual(_interpret_ferritin(350)["status"], "ok")
        self.assertEqual(_interpret_ferritin(600)["status"], "high")


if __name__ == "__main__":
    unittest.main()


class PortalTokenTests(unittest.TestCase):
    """Signed portal link: bare HN must no longer open the portal."""

    def setUp(self):
        import hd_module.blueprint as bp
        self.bp = bp

    def test_sign_roundtrip(self):
        tok = self.bp.portal_token("56001")
        self.assertTrue(tok.startswith("56001."))
        self.assertEqual(self.bp._portal_hn_from_token(tok), "56001")

    def test_bare_hn_rejected(self):
        self.assertIsNone(self.bp._portal_hn_from_token("56001"))
        self.assertIsNone(self.bp._portal_hn_from_token(""))
        self.assertIsNone(self.bp._portal_hn_from_token(None))

    def test_tampered_sig_rejected(self):
        tok = self.bp.portal_token("56001")
        self.assertIsNone(self.bp._portal_hn_from_token(tok[:-1] + ("0" if tok[-1] != "0" else "1")))
        # signature of another HN must not unlock this HN
        other = self.bp.portal_token("57051").split(".")[1]
        self.assertIsNone(self.bp._portal_hn_from_token("56001." + other))


class IdwgTests(unittest.TestCase):
    def test_compute(self):
        from hd_module.patient_portal import compute_idwg
        sessions = [  # newest first
            {"date": "2026-11-14", "pre_weight": 76.0, "post_weight": 73.5},
            {"date": "2026-11-12", "pre_weight": 75.5, "post_weight": 73.0},
            {"date": "2026-11-10", "pre_weight": 75.0, "post_weight": 72.9},
        ]
        out = compute_idwg(sessions, 73.0)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0], {"date": "2026-11-12", "kg": 2.6, "pct": 3.6})
        self.assertEqual(out[1], {"date": "2026-11-14", "kg": 3.0, "pct": 4.1})

    def test_skips_gaps_and_errors(self):
        from hd_module.patient_portal import compute_idwg
        sessions = [
            {"date": "2026-11-14", "pre_weight": 90.0, "post_weight": 73.5},  # +17 kg → data error, skipped
            {"date": "2026-11-12", "pre_weight": None, "post_weight": 73.0},
            {"date": "2026-11-10", "pre_weight": 75.0, "post_weight": None},
        ]
        self.assertEqual(compute_idwg(sessions, 73.0), [])
        self.assertEqual(compute_idwg([], 73.0), [])
