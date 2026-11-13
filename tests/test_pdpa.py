"""PDPA consent registry — pure parts + PDF."""
import unittest

from hd_module import pdpa


class FakeCur:
    def __init__(self): self.rows = []; self.inserted = []
    def execute(self, sql, params=()):
        if sql.startswith("SELECT id FROM hd_patient_mapping"):
            self.rows = [(7,)]
        elif sql.startswith("INSERT INTO hd_consents"):
            self.inserted.append(params); self.rows = [(len(self.inserted),)]
        elif "DISTINCT ON (consent_type)" in sql:
            self.rows = [("line_messaging", "withdrawn", None, "line", "2026-11-13"), ("data_processing", "given", None, "paper", "2026-11-13")]
        else:
            self.rows = []
    def fetchone(self): return self.rows[0] if self.rows else None
    def fetchall(self): return self.rows


class PdpaTests(unittest.TestCase):
    def test_record_and_status(self):
        c = FakeCur()
        rid = pdpa.record(c, "100", "line_messaging", "given", "paper", "nurse")
        self.assertEqual(rid, 1); self.assertEqual(c.inserted[0][0], 7); self.assertEqual(c.inserted[0][1], "100")
        with self.assertRaises(ValueError):
            pdpa.record(c, "100", "nope", "given", "paper", "nurse")
        st = pdpa.status_for(c, "100")
        self.assertEqual(st["line_messaging"]["status"], "withdrawn")
        self.assertTrue(pdpa.has_consent(c, "100", "data_processing"))
        self.assertFalse(pdpa.has_consent(c, "100", "line_messaging"))

    def test_notice_and_pdf(self):
        t = pdpa.notice_text("คลินิก X", "077-1")
        self.assertIn("ถอนความยินยอม", t); self.assertIn("077-1", t); self.assertIn("ไม่ส่งข้อมูลให้ระบบปัญญาประดิษฐ์ภายนอก", t)
        pdf = pdpa.consent_form_pdf("คลินิก X", "นาย ก", "100")
        self.assertTrue(pdf.startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
