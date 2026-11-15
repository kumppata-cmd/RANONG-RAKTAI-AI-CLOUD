"""Regression: Ranong Hospital OPD visit sheet (ใบตรวจรักษาผู้ป่วย) is a ใบยา → rx.

15 Nov 2026 (clinic HN 00139): the sheet has a field label "ผลการตรวจทางห้องปฏิบัติการ"
and drugs "SODIUM BICARBONATE" / "Calcium Carbonate", so the lab pre-check counted
sodium + calcium as lab values and filed it under 04_LAB_RESULTS."""
import os
import unittest

os.environ.setdefault("APP_ENV", "testing")

OPD = """ใบตรวจรักษาผู้ป่วย โรงพยาบาลระนอง วันที่ 13 พฤศจิกายน 2569 เวลา 07:39:41
HN :00105761 สิทธิการรักษา : 77-UCS-สุขภาพถ้วนหน้า(ผู้สูงอายุ>60 ปี)
ชื่อ นายทดสอบ ระบบ อายุ 71 ปี สถานพยาบาลหลัก : โรงพยาบาลระนอง
ประวัติการแพ้ยา : ไม่พบ
อาการสำคัญ / ประวัติการเจ็บป่วยปัจจุบัน / ประวัติการเจ็บป่วยในอดีต
CC : รับยา HPI : PMH : MH/PE
การตรวจร่างกาย Visit Note PE text :
Diag Text : End stage renal disease, Hypertension
1 MANIDIPINE 20 MG 1x1 หลังอาหารเช้า  2 SODIUM BICARBONATE 300 MG วันละ 3 ครั้ง
3 LOSARTAN 50 MG ครั้งละ 1 เม็ด  4 FERROUS FUMARATE 200 MG  Calcium Carbonate 1000 mg
5 FOLIC ACID 5 MG  6 CALCITRIOL 0.25 MCG  7 CARVEDILOL 12.5 MG
ผลการตรวจทางห้องปฏิบัติการ/การวินิจฉัย
ผู้ตรวจรักษา นพ.ทดสอบ รวมค่าใช้จ่ายทั้งสิ้น 4,484.00 บาท"""

LAB_REPORT = """โรงพยาบาลระนอง ใบรายงานผลการตรวจทางห้องปฏิบัติการ HN 00105761
Request no 6911130042 Collection date 13/11/2026 Specimen: Blood
Test Result Unit Reference range
BUN 58 mg/dL 7-20  Creatinine 8.9 mg/dL 0.6-1.2  eGFR 5
Sodium 137 mmol/L  Potassium 5.6 mmol/L H  Calcium 8.4 mg/dL  Phosphorus 6.1 mg/dL
Hemoglobin 9.8 g/dL  Hematocrit 30 %  Albumin 3.6 g/dL"""


class OpdVisitSheetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from server import classify_with_keywords
        cls.classify = staticmethod(classify_with_keywords)

    def test_opd_visit_sheet_is_rx(self):
        r = self.classify("x.jpg", override_text=OPD)
        self.assertEqual(r["category"], "rx", r.get("reason"))

    def test_opd_partial_ocr_without_header_is_rx(self):
        t = OPD.replace("ใบตรวจรักษาผู้ป่วย", "").replace("Visit Note", "")
        r = self.classify("x.jpg", override_text=t)
        self.assertEqual(r["category"], "rx", r.get("reason"))

    def test_real_lab_report_stays_lab(self):
        r = self.classify("x.jpg", override_text=LAB_REPORT)
        self.assertEqual(r["category"], "lab", r.get("reason"))

    def test_drug_names_alone_do_not_make_lab(self):
        t = ("ห้องปฏิบัติการ Sodium bicarbonate 300 mg 1x3 Calcium carbonate 1000 mg 1x3 "
             "Losartan 50 mg Folic acid 5 mg tab หลังอาหาร")
        r = self.classify("x.jpg", override_text=t)
        self.assertEqual(r["category"], "rx", r.get("reason"))


if __name__ == "__main__":
    unittest.main()
