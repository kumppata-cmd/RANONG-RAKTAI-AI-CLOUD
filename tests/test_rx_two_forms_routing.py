"""Regression (16 Nov 2026): ใบยา 2 แบบของผู้ป่วยรายเดียวกัน ต้องเข้าแฟ้มเดียวกัน แยกตามวันที่เอกสาร

1) ใบ "Medication Record หน่วยไตเทียม" ของคลินิก เคยถูกจัดเป็น dialysis (เพราะคำว่าหน่วยไตเทียม)
2) OCR อ่าน HN คลินิกผิด 1 หลัก → อาจตรงกับผู้ป่วยคนอื่น → ต้องให้ชื่อบนเอกสารเป็นตัวตัดสิน
3) HN หลายตัวชี้คนละคน และไม่มีชื่อยืนยัน → ไม่เดา
ข้อมูลในไฟล์นี้เป็นข้อมูลสมมติทั้งหมด"""
import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("APP_ENV", "testing")

CLINIC_MR = """Medication Record หน่วยไตเทียมระนอง รักษ์ไต
ชื่อ-สกุล นายทดสอบ ใบยา HN 67398 วันที่ 2 มีนาคม 2569
Medication Dose Frequency Route Regimen
Epoetin alfa 4000 unit 2 ครั้ง/สัปดาห์ SC
Iron sucrose 100 mg 1 ครั้ง/สัปดาห์ IV"""

HOSP_RX = """ใบตรวจรักษาผู้ป่วย โรงพยาบาลระนอง วันที่ 16 พฤศจิกายน 2569
HN :00097617 ชื่อ นายทดสอบ ใบยา อายุ 60 ปี
Diag Text : End stage renal disease  Visit Note PE text :
1 SODIUM BICARBONATE 300 MG  2 CALCIUM CARBONATE 1000 MG  3 FOLIC ACID 5 MG
ผู้ตรวจรักษา นพ.ทดสอบ"""


def _p(name, folder, chn="", hhn=""):
    return SimpleNamespace(patient_name=name, folder_name=folder, clinic_hn=chn, hospital_hn=hhn)


class FakeRegistry:
    def __init__(self, patients):
        self.patients = patients

    def match_hn(self, hn):
        hn = hn.strip().lstrip("0")
        for p in self.patients:
            if hn and hn in (p.clinic_hn.lstrip("0"), p.hospital_hn.lstrip("0")):
                return p
        return None

    def search_by_name(self, name):
        import server
        hits = [p for p in self.patients if server._names_agree(p.patient_name, name)]
        return hits[0] if len(hits) == 1 else None

    def get_folder_for_hn(self, hn):
        p = self.match_hn(hn)
        return p.folder_name if p else ""


TARGET = _p("นายทดสอบ ใบยา", "HN67398_ทดสอบ", "67398", "00097617")
OTHER = _p("นางสมศรี คนอื่น", "HN57398_สมศรี", "57398", "00011111")


class RxTwoFormsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server
        cls.s = server

    def setUp(self):
        self._orig = self.s.master_registry
        self.s.master_registry = FakeRegistry([TARGET, OTHER])

    def tearDown(self):
        self.s.master_registry = self._orig

    # ── classification ──
    def test_clinic_medication_record_is_rx(self):
        r = self.s.classify_with_keywords("x.jpg", override_text=CLINIC_MR)
        self.assertEqual(r["category"], "rx", r.get("reason"))

    def test_hospital_rx_is_rx(self):
        r = self.s.classify_with_keywords("x.jpg", override_text=HOSP_RX)
        self.assertEqual(r["category"], "rx", r.get("reason"))

    # ── document date → month folder ──
    def test_document_dates(self):
        from patient_master_registry import extract_document_date
        d1, d2 = extract_document_date(CLINIC_MR), extract_document_date(HOSP_RX)
        self.assertEqual((d1.year, d1.month, d1.day), (2026, 3, 2))
        self.assertEqual((d2.year, d2.month, d2.day), (2026, 11, 16))

    # ── HN + name cross-check ──
    def test_names_agree_tolerates_titles_and_ocr_noise(self):
        self.assertTrue(self.s._names_agree("นายทดสอบ ใบยา", "ทดสอบ ใบยา"))
        self.assertTrue(self.s._names_agree("นายทดสอบ ใบยา", "นายทดสบ ใบยา"))
        self.assertFalse(self.s._names_agree("นายทดสอบ ใบยา", "นางสมศรี คนอื่น"))

    def test_correct_hn_and_name(self):
        p, how, warn = self.s._pick_patient_for_doc(["67398"], "นายทดสอบ ใบยา")
        self.assertIs(p, TARGET)
        self.assertEqual((how, warn), ("hn+name", ""))

    def test_misread_hn_points_to_other_patient_name_wins(self):
        # OCR อ่าน 67398 เป็น 57398 (= ผู้ป่วยอีกคน) แต่ชื่อบนใบเป็นของ TARGET
        p, how, warn = self.s._pick_patient_for_doc(["57398"], "นายทดสอบ ใบยา")
        self.assertIs(p, TARGET)
        self.assertEqual((how, warn), ("ocr_name", "hn_name_mismatch"))

    def test_two_hns_one_agrees_with_name(self):
        p, how, _ = self.s._pick_patient_for_doc(["57398", "00097617"], "นายทดสอบ ใบยา")
        self.assertIs(p, TARGET)
        self.assertEqual(how, "hn+name")

    def test_two_hns_different_patients_no_name_is_not_guessed(self):
        p, how, warn = self.s._pick_patient_for_doc(["57398", "67398"], "")
        self.assertIsNone(p)
        self.assertEqual((how, warn), ("ambiguous", "multi_hn"))

    def test_hospital_hn_only(self):
        p, how, _ = self.s._pick_patient_for_doc(["00097617"], "")
        self.assertIs(p, TARGET)
        self.assertEqual(how, "hn")

    def test_batch_both_forms_same_patient_folder(self):
        clinic_misread = CLINIC_MR.replace("HN 67398", "HN 57398")
        self.assertEqual(self.s._batch_patient_folder([clinic_misread, HOSP_RX]), TARGET.folder_name)
        self.assertEqual(self.s._batch_patient_folder([CLINIC_MR, HOSP_RX]), TARGET.folder_name)


if __name__ == "__main__":
    unittest.main()


class OcrUprightTests(unittest.TestCase):
    """ภาพใบยาที่ถ่ายตะแคง (หมุน 270°) ต้องถูกหมุนกลับก่อน OCR"""

    def _run(self, osd):
        import server
        from PIL import Image
        img = Image.new("L", (300, 100))
        orig = server.pytesseract.image_to_osd
        server.pytesseract.image_to_osd = lambda *a, **k: osd
        try:
            return server._ocr_upright(img)
        finally:
            server.pytesseract.image_to_osd = orig

    def test_rotated_page_is_turned_upright(self):
        out = self._run("Rotate: 270\nOrientation confidence: 9.5\n")
        self.assertEqual(out.size, (100, 300))

    def test_low_confidence_left_alone(self):
        out = self._run("Rotate: 90\nOrientation confidence: 0.4\n")
        self.assertEqual(out.size, (300, 100))

    def test_osd_failure_left_alone(self):
        import server
        from PIL import Image
        img = Image.new("L", (300, 100))
        orig = server.pytesseract.image_to_osd

        def boom(*a, **k):
            raise RuntimeError("no osd")
        server.pytesseract.image_to_osd = boom
        try:
            self.assertEqual(server._ocr_upright(img).size, (300, 100))
        finally:
            server.pytesseract.image_to_osd = orig
