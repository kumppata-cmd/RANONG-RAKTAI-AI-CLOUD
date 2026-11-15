"""Tests for hd_module.patient_card_pdf (printable card + QR as PDF)."""
import unittest

from hd_module.patient_card_pdf import card_pdf


class CardPdfTests(unittest.TestCase):
    def test_full_card(self):
        pdf = card_pdf({"name": "นายทดสอบ ระบบ", "hn": "99999", "hospital_hn": "1234567",
                        "schedule_days": "จ/พ/ศ", "default_shift": "morning",
                        "vascular_access": "AVF", "dry_weight": 74},
                       "https://example.test/hd/my/99999.sig")
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertGreater(len(pdf), 5000)

    def test_missing_fields_and_long_name(self):
        pdf = card_pdf({"name": "นาง" + "ก" * 80, "hn": "1"}, "https://example.test/x")
        self.assertTrue(pdf.startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
