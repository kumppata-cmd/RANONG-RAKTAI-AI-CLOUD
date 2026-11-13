"""Regression: Hemodialysis WORK SHEET must classify as dialysis, not rx (13 Nov 2026, HN 67360).
The sheet carries a 'Hemodialysis Prescription' block and 'Medication / Route' words,
which used to trip the HD-prescription → rx pre-check."""
import os
import unittest

os.environ.setdefault("APP_ENV", "testing")

WORKSHEET = """ระนอง รักษ์ไต Hemodialysis work sheet HD No. 420
Patient's name นางสาว อ. Age 37 Yrs HN. 67360 Dx CKD stage 5 Date 13/11/2026
Pre-dialysis assesment Anxiety OY ON Sleep disturbance Dyspnea Orthopnea Crepitation JVP engorged
Last wt 51.4 kg Pre BW Dry BW 50 kg Wt Gain Meal/drink Target UF Post BW Wt lose
Hemodialysis Prescription Machine No. Dialyzer SADL Surface area m2 Use No. Last TCV
Anticoagulant Heparin Loading 2500 start Maintenance 2500 In hr Flush Nss cc q min
Duration 4 hr Time start Time off O2 sat BT Hct= 31 % Hb= 10.3 g/dl
Vascular access Arm Leg Lt Rt AVF AVG DLC Perm Cath Thrill Bruit
Dialysate K+ 2 mEq/L Ca 2.5 Hco3 35 Dialysate Temp 36 Dialysate flow 500 cc/min Na/UF Variation Pro
Medication EPO Hemax 4000 Route IV SC Time
Nursing Diagnosis Nursing care plan Expective outcome
TIME BP mm/Hg PR /min RR /min BFR ml/min VP mm/Hg TMP mm/Hg UFR ml/hr Conduct mS/cm Total UF ml Remark
Pre-HD Start HD Nurse start Nurse Off Total UF Net UF balance
Nss 50%glucose Extra fluid meal and drink Total repletement
Complication Hypotenstion Hypertension Muscle cramp Technical complication Blood leak Clot dialyzer
Nursing managment Monitor V/S Health Edecation Nutrition Vascular access Exercise
Post HD note เหนื่อย เลือดออก BP ก่อนกลับ 152/91 mm/Hg Discharge by Home Admit เดินกลับ รถเข็น
Nephrologist นพ.ก. Nurse"""

HD_RX_ORDER = """Medication Order Form หน่วยไตเทียม ระนอง รักษ์ไต HN 56001
Order for one day / Order for continuation
Drug Dose Frequency Route Regimen
Losartan 50 mg 1x1 oral p.c. Amlodipine 10 mg 1x1 oral Atorvastatin 20 mg 1x1 h.s.
Calcium carbonate 1 tab 3x1 Folic acid 5 mg 1x1 Sodium bicarbonate 2 tab 3x1
Heparin loading 2000 unit maintenance 1000 unit/hr dialyzer reuse Dry weight 50 kg
แพทย์ผู้สั่ง นพ.ก. วันที่สั่ง 1/11/2026"""


class HDWorksheetClassificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from server import classify_with_keywords
        cls.classify = staticmethod(classify_with_keywords)

    def test_worksheet_is_dialysis(self):
        r = self.classify("x.jpg", override_text=WORKSHEET)
        self.assertEqual(r["category"], "dialysis", r.get("reason"))
        self.assertIn("work sheet", r.get("reason", ""))

    def test_worksheet_partial_ocr_is_dialysis(self):
        # OCR that only caught the lower half (flowsheet + complications)
        half = "\n".join(WORKSHEET.splitlines()[12:])
        r = self.classify("x.jpg", override_text=half)
        self.assertEqual(r["category"], "dialysis", r.get("reason"))

    def test_medication_order_form_still_rx(self):
        r = self.classify("x.jpg", override_text=HD_RX_ORDER)
        self.assertEqual(r["category"], "rx", r.get("reason"))


if __name__ == "__main__":
    unittest.main()
