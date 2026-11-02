"""ส่งหลายรูปพร้อมกัน (ชุดเดียว) ของผู้ป่วยคนเดียว ต้องเข้าแฟ้มเดียวกัน

เหตุการณ์จริง 2026-11-02 10:57: ใบฟอกเลือด 2 หน้าของ HN 63203
หน้า 1 → 05_DIALYSIS_RECORDS/…/63203_<ชื่อ>  (ถูก)
หน้า 2 → 05_DIALYSIS_RECORDS/…/HN162          (OCR อ่าน HN เพี้ยน
                                                "162" ไม่มีในทะเบียน)
→ LIFF "ดูผลแล็บ" ของผู้ป่วยเห็นใบเดียว

กฎใหม่ (เฉพาะชุดที่ส่งพร้อมกัน):
  หน้าที่ OCR อ่าน HN ไม่ได้ หรืออ่านได้แต่ไม่มีในทะเบียน
  → ใช้แฟ้มที่หน้าอื่นในชุดจับคู่ทะเบียนได้ "รายเดียว"
  ถ้าชุดนั้นชี้ผู้ป่วยหลายราย → ไม่เดา จัดตามเดิม

ข้อมูลทั้งหมดเป็นค่าสังเคราะห์ · import server แบบ lazy
"""
import unittest


def _server():
    import server as _s
    return _s


class _FakeRegistry:
    """ทะเบียนผู้ป่วยจำลอง — มี 2 ราย"""
    _folders = {
        "63203": "63203_สมมติ_หนึ่ง",
        "57042": "57042_สมมติ_สอง",
    }

    def get_folder_for_hn(self, hn):
        from patient_master_registry import _normalize_hn
        return self._folders.get(_normalize_hn(hn), "")

    def count(self):
        return len(self._folders)


class TestBatchPatientFolder(unittest.TestCase):

    def setUp(self):
        self.s = _server()
        self._orig = self.s.master_registry
        self.s.master_registry = _FakeRegistry()

    def tearDown(self):
        self.s.master_registry = self._orig

    def test_second_page_misread_hn_follows_first_page(self):
        pages = [
            "Hemodialysis record HN 63203 วันที่ 2/11/2569",
            "Hemodialysis record HN 162 (OCR เพี้ยน)",   # ไม่มีในทะเบียน
        ]
        self.assertEqual(
            self.s._batch_patient_folder(pages), "63203_สมมติ_หนึ่ง"
        )

    def test_page_without_any_hn_follows_sibling(self):
        pages = ["HN 63203 ฟอกเลือด", "(หน้าหลัง ไม่มี HN)"]
        self.assertEqual(
            self.s._batch_patient_folder(pages), "63203_สมมติ_หนึ่ง"
        )

    def test_two_registered_patients_in_one_batch_no_guess(self):
        pages = ["HN 63203", "HN 57042"]
        self.assertEqual(self.s._batch_patient_folder(pages), "")

    def test_no_registered_match_anywhere(self):
        pages = ["HN 162", "HN 999"]
        self.assertEqual(self.s._batch_patient_folder(pages), "")

    def test_registry_unavailable(self):
        self.s.master_registry = None
        self.assertEqual(self.s._batch_patient_folder(["HN 63203"]), "")


class TestRoutingBlockUsesBatchFolder(unittest.TestCase):
    """จำลองเงื่อนไขในบล็อก Auto HN route ของ process_file_async
    (โค้ดจริงอยู่ในฟังก์ชันปิด — ทดสอบตรรกะเดียวกันผ่านลำดับการตัดสิน)"""

    def _route(self, found_hns, batch_folder, registry):
        metadata = {}
        matched = False
        for hn in found_hns:
            f = registry.get_folder_for_hn(hn)
            if f:
                metadata["patient_hn_folder"] = f
                matched = True
                break
        if not matched and batch_folder:
            metadata["patient_hn_folder"] = batch_folder
            metadata["hn_route_source"] = "batch_sibling"
            matched = True
        if not matched and found_hns:
            metadata["patient_hn_folder"] = f"HN{found_hns[0]}"
        return metadata

    def test_registered_hn_on_page_wins_over_batch(self):
        m = self._route(["57042"], "63203_สมมติ_หนึ่ง", _FakeRegistry())
        self.assertEqual(m["patient_hn_folder"], "57042_สมมติ_สอง")

    def test_unregistered_hn_uses_batch_folder(self):
        m = self._route(["162"], "63203_สมมติ_หนึ่ง", _FakeRegistry())
        self.assertEqual(m["patient_hn_folder"], "63203_สมมติ_หนึ่ง")
        self.assertEqual(m["hn_route_source"], "batch_sibling")

    def test_unregistered_hn_without_batch_keeps_old_fallback(self):
        m = self._route(["162"], "", _FakeRegistry())
        self.assertEqual(m["patient_hn_folder"], "HN162")


if __name__ == "__main__":
    unittest.main()
