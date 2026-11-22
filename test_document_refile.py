"""ย้ายเอกสารที่เข้าแฟ้มผิดคน

ช่องว่างเชิงปฏิบัติ: workflow จริงของคลินิกคือเจ้าหน้าที่เลือกผู้ป่วย
ก่อนส่งเอกสาร ถ้าลืมเปลี่ยนผู้ป่วย เอกสารชุดถัดไปจะเข้าแฟ้มคนก่อน
ทั้งชุด และก่อนหน้านี้ "ไม่มีวิธีแก้เลย" ต้องส่งเอกสารใหม่อย่างเดียว

ข้อมูลทั้งหมดเป็นค่าสังเคราะห์

import server แบบ lazy เพื่อไม่รบกวนลำดับ import ของโมดูลทดสอบอื่น
"""
import unittest


def _server():
    import server as _s
    return _s


class TestMoveCommandParsing(unittest.TestCase):

    def setUp(self):
        self.parse = _server()._parse_move_document

    def test_accepts_thai_and_english_forms(self):
        for text in ("ย้ายเอกสาร a1b2c3d4 57042",
                     "ย้าย a1b2c3d4 57042",
                     "move a1b2c3d4 57042",
                     "MOVE A1B2C3D4 57042"):
            with self.subTest(text=text):
                got = self.parse(text)
                self.assertIsNotNone(got, f"ต้องรับ: {text!r}")
                self.assertEqual(got[1], "57042")

    def test_tolerates_extra_whitespace(self):
        self.assertEqual(
            self.parse("  ย้ายเอกสาร   a1b2c3d4   57042  "),
            ("a1b2c3d4", "57042", None),
        )

    def test_target_category_suffix(self):
        """22 พ.ย. 2569: ระบุหมวดปลายทางเพื่อย้ายเอกสาร 'review' พร้อมจัดหมวด"""
        for text, cat in (("ย้ายเอกสาร a1b2c3d4 57042 แล็บ", "lab"),
                          ("ย้ายเอกสาร a1b2c3d4 57042 แลบ", "lab"),
                          ("ย้ายเอกสาร a1b2c3d4 57042 ยา", "rx"),
                          ("ย้าย a1b2c3d4 57042 ฟอก", "dialysis"),
                          ("move a1b2c3d4 57042 lab", "lab"),
                          ("ย้ายเอกสาร a1b2c3d4 57042", None)):
            with self.subTest(text=text):
                got = self.parse(text)
                self.assertIsNotNone(got, f"ต้องรับ: {text!r}")
                self.assertEqual(got[2], cat)

    def test_unknown_category_suffix_rejected(self):
        self.assertIsNone(self.parse("ย้ายเอกสาร a1b2c3d4 57042 บิล"))


class TestMoveBatchParsing(unittest.TestCase):
    """22 พ.ย. 2569: หลายคำสั่งย้ายใน 1 ข้อความ — กันข้อความหลุดไปเข้า FAQ"""

    def setUp(self):
        self.batch = _server()._parse_move_batch

    def test_three_commands_one_message(self):
        got = self.batch("ย้ายเอกสาร 8dfe9dbd 67367 ยา\n"
                         "ย้ายเอกสาร d4f7c738 67379 ยา\n"
                         "ย้ายเอกสาร e34eb1b3 67379 ยา")
        self.assertEqual(len(got), 3)
        self.assertEqual(got[0], ("8dfe9dbd", "67367", "rx"))
        self.assertEqual(got[2], ("e34eb1b3", "67379", "rx"))

    def test_mixed_lines_reported_not_dropped(self):
        got = self.batch("ย้ายเอกสาร 8dfe9dbd 67367 แล็บ\nสวัสดีครับ")
        self.assertEqual(got[0], ("8dfe9dbd", "67367", "lab"))
        self.assertEqual(got[1], (None, "สวัสดีครับ", None))

    def test_plain_text_returns_none(self):
        for text in ("ยาคืออะไร", "สอบถามเรื่องยา\nกินยังไง", "", "  \n "):
            with self.subTest(text=text):
                self.assertIsNone(self.batch(text))

    def test_single_line_still_works(self):
        got = self.batch("ย้ายเอกสาร a1b2c3d4 57042")
        self.assertEqual(got, [("a1b2c3d4", "57042", None)])

    def test_rejects_incomplete_commands(self):
        for text in ("ย้ายเอกสาร a1b2c3d4",     # ไม่มี HN
                     "ย้ายเอกสาร",               # ไม่มีอะไรเลย
                     "ย้ายเอกสาร zzzz 57042",    # รหัสไม่ใช่ hex
                     "ย้ายเอกสาร abc 57042",     # รหัสสั้นเกินไป
                     ""):
            with self.subTest(text=text):
                self.assertIsNone(self.parse(text))

    def test_does_not_collide_with_other_commands(self):
        """ต้องไม่จับคำสั่งอื่นผิด — อยู่ใน elif chain เดียวกัน"""
        for text in ("ผู้ป่วย 57042", "รับทราบ a1b2c3d4",
                     "แจ้งเตือนค้าง", "ล้างผู้ป่วย",
                     "ค้นหา เบาหวาน", "ทะเบียนผู้ป่วย"):
            with self.subTest(text=text):
                self.assertIsNone(self.parse(text))


class FakeBlob:
    def __init__(self, name, bucket):
        self.name = name
        self._bucket = bucket

    def exists(self):
        return self.name in self._bucket.objects

    def upload_from_string(self, data, content_type=None):
        self._bucket.objects[self.name] = data
        return True


class FakeBucket:
    def __init__(self):
        self.objects = {}
        self.copies = []

    def blob(self, name):
        return FakeBlob(name, self)

    def copy_blob(self, src, bucket, new_name):
        self.copies.append((src.name, new_name))
        self.objects[new_name] = self.objects.get(src.name, b"data")
        return FakeBlob(new_name, self)


class FakePatient:
    def __init__(self, folder_name, hn_display, patient_name):
        self.folder_name = folder_name
        self.hn_display = hn_display
        self.patient_name = patient_name


class MoveFixture(unittest.TestCase):
    """ประกอบสภาพแวดล้อมจำลองรอบ _move_document_to_patient"""

    DOC_ID = "aabbccdd11223344"
    OLD_PATH = "05_DIALYSIS_RECORDS/2026-Q4/2026-10/57099_Wrong/f.pdf"

    def setUp(self):
        self.server = _server()
        s = self.server

        self._backup = {
            "bucket": getattr(s.storage, "_bucket", None),
            "docs": list(s.doc_index._docs),
            "match_hn": s.master_registry.match_hn,
            "ensure": s.doc_index._ensure_fresh,
            "update_cat": s.doc_index.update_category,
            "is_ready": type(s.storage).is_ready,
        }

        self.bucket = FakeBucket()
        self.bucket.objects[self.OLD_PATH] = b"pdf-bytes"
        s.storage._bucket = self.bucket
        type(s.storage).is_ready = property(lambda self: True)

        self.doc = {
            "document_id": self.DOC_ID,
            "category": "dialysis",
            "gcs_object_path": f"gs://test-bucket/{self.OLD_PATH}",
            "upload_date": "2026-10-20",
        }
        s.doc_index._docs = [self.doc]
        s.doc_index._ensure_fresh = lambda: None

        self.updates = []
        s.doc_index.update_category = (
            lambda d, c, p: self.updates.append((d, c, p)) or True
        )

        self.patient = FakePatient(
            "57042_Correct_Patient", "57042", "Correct Patient",
        )
        s.master_registry.match_hn = (
            lambda hn: self.patient if hn == "57042" else None
        )

    def tearDown(self):
        s = self.server
        s.storage._bucket = self._backup["bucket"]
        s.doc_index._docs = self._backup["docs"]
        s.master_registry.match_hn = self._backup["match_hn"]
        s.doc_index._ensure_fresh = self._backup["ensure"]
        s.doc_index.update_category = self._backup["update_cat"]
        type(s.storage).is_ready = self._backup["is_ready"]


class TestMoveDocument(MoveFixture):

    def test_moves_to_correct_patient_folder(self):
        r = self.server._move_document_to_patient(
            self.DOC_ID[:8], "57042", "admin_hash",
        )
        self.assertEqual(r.get("status"), "moved", r)
        self.assertEqual(len(self.bucket.copies), 1)
        _src, dest = self.bucket.copies[0]
        self.assertIn("57042_Correct_Patient", dest)

    def test_keeps_original_quarter_and_month(self):
        """ต้องอยู่ช่วงเวลาเดิมของเอกสาร ไม่ย้ายไปไตรมาสปัจจุบัน

        เอกสารลงวันที่ 2026-10-20 → ต้องยังอยู่ 2026-Q4/2026-10
        """
        self.server._move_document_to_patient(
            self.DOC_ID[:8], "57042", "admin_hash",
        )
        _src, dest = self.bucket.copies[0]
        self.assertIn("2026-Q4/2026-10/", dest)

    def test_original_file_is_not_deleted(self):
        """ต้นฉบับต้องยังอยู่ — ระบบไม่มีสิทธิ์ลบ และห้ามลบอยู่แล้ว"""
        self.server._move_document_to_patient(
            self.DOC_ID[:8], "57042", "admin_hash",
        )
        self.assertIn(self.OLD_PATH, self.bucket.objects)

    def test_index_is_pointed_at_new_path(self):
        """ถ้าไม่อัปเดต index ระบบจะยังชี้ไปแฟ้มเดิม = ย้ายไม่สำเร็จจริง"""
        self.server._move_document_to_patient(
            self.DOC_ID[:8], "57042", "admin_hash",
        )
        self.assertEqual(len(self.updates), 1)
        doc_id, cat, new_path = self.updates[0]
        self.assertEqual(doc_id, self.DOC_ID)
        self.assertEqual(cat, "dialysis")
        self.assertIn("57042_Correct_Patient", new_path)

    def test_unknown_hn_is_rejected(self):
        r = self.server._move_document_to_patient(
            self.DOC_ID[:8], "99999", "admin_hash",
        )
        self.assertIn("error", r)
        self.assertEqual(self.bucket.copies, [])

    def test_unknown_document_is_rejected(self):
        r = self.server._move_document_to_patient(
            "ffffffff", "57042", "admin_hash",
        )
        self.assertIn("error", r)
        self.assertEqual(self.bucket.copies, [])

    def test_short_prefix_is_rejected(self):
        """ไม่เดารหัสสั้น — ปลายทางคือการย้ายเวชระเบียน"""
        r = self.server._move_document_to_patient(
            "aa", "57042", "admin_hash",
        )
        self.assertIn("error", r)
        self.assertEqual(self.bucket.copies, [])

    def test_ambiguous_prefix_refuses_to_guess(self):
        """คำนำหน้าตรงหลายเอกสาร ต้องไม่เดาว่าเป็นอันไหน"""
        self.server.doc_index._docs.append({
            "document_id": self.DOC_ID[:4] + "ffffffffffff",
            "category": "dialysis",
            "gcs_object_path": "gs://b/x.pdf",
            "upload_date": "2026-10-20",
        })
        r = self.server._move_document_to_patient(
            self.DOC_ID[:4] + "", "57042", "admin_hash",
        )
        self.assertIn("error", r)
        self.assertEqual(self.bucket.copies, [])

    def test_already_correct_is_a_no_op(self):
        """อยู่แฟ้มถูกอยู่แล้วต้องไม่ copy ซ้ำ"""
        correct = (
            "05_DIALYSIS_RECORDS/2026-Q4/2026-10/"
            "57042_Correct_Patient/f.pdf"
        )
        self.doc["gcs_object_path"] = f"gs://test-bucket/{correct}"
        self.bucket.objects[correct] = b"pdf-bytes"
        r = self.server._move_document_to_patient(
            self.DOC_ID[:8], "57042", "admin_hash",
        )
        self.assertEqual(r.get("status"), "unchanged")
        self.assertEqual(self.bucket.copies, [])

    def test_non_clinical_document_is_rejected(self):
        """บิลไม่ได้จัดเก็บรายผู้ป่วย จึงย้ายแบบนี้ไม่ได้"""
        self.doc["category"] = "bills"
        r = self.server._move_document_to_patient(
            self.DOC_ID[:8], "57042", "admin_hash",
        )
        self.assertIn("error", r)
        self.assertEqual(self.bucket.copies, [])

    def test_missing_source_file_is_reported(self):
        self.bucket.objects.pop(self.OLD_PATH)
        r = self.server._move_document_to_patient(
            self.DOC_ID[:8], "57042", "admin_hash",
        )
        self.assertIn("error", r)
        self.assertEqual(self.bucket.copies, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
