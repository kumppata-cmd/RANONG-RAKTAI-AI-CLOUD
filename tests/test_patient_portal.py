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
