"""เปลี่ยนประเภทเอกสารที่ถูกจัดหมวดไว้ผิด

ช่องว่างที่พบจริง: ตรวจ production 26 ต.ค. 2026 พบเอกสาร 2 ใบถูกจัด
เป็นประเภท lab ทั้งที่ไม่พบชื่อรายการตรวจแม้แต่ชนิดเดียวในเอกสาร
(analyte_names_found = 0) แปลว่าไม่ใช่ใบแล็บตั้งแต่แรก

ของเดิมแก้ประเภทได้เฉพาะ "ตอนเพิ่งส่งเข้ามา" ผ่านการตอบเลข 1-4 ตามที่
ระบบถาม (pending classification) พอเลยช่วงนั้นไปแล้วก็ไม่มีวิธีแก้อีก
เลย เอกสารที่จัดผิดจึงค้างอยู่ในหมวดผิดตลอดไป

หลักที่เทสต์ชุดนี้ล็อกไว้
    ไม่ลบไฟล์ต้นฉบับ — copy แล้วชี้ index ไป path ใหม่
    ไม่เดาประเภทเอง — คนระบุเท่านั้น
    เฉพาะแพทย์หรือผู้ดูแลระบบ
    เอกสารทางคลินิกต้องเข้าแฟ้มรายผู้ป่วย ไม่ลงที่รากหมวด
    เตือนเมื่อย้ายออกจาก lab ที่มีผลแล็บอ่านไว้แล้ว

ข้อมูลทั้งหมดเป็นค่าสังเคราะห์ ไม่ใช่ของผู้ป่วยจริง
"""
import unittest


def _server():
    import server as _s
    return _s


# ─────────────────────────────────────────────────────────────
# การแยกคำสั่ง
# ─────────────────────────────────────────────────────────────

class TestChangeCategoryParsing(unittest.TestCase):

    def setUp(self):
        self.parse = _server()._parse_change_category

    def test_รับคำไทยและอังกฤษ(self):
        cases = {
            "เปลี่ยนประเภท a1b2c3d4 ใบยา": "rx",
            "เปลี่ยนประเภท a1b2c3d4 rx": "rx",
            "เปลี่ยนหมวด a1b2c3d4 ฟอกเลือด": "dialysis",
            "recategorize a1b2c3d4 dialysis": "dialysis",
            "recat a1b2c3d4 bills": "bills",
            "เปลี่ยนประเภท a1b2c3d4 ใบเสร็จ": "bills",
            "เปลี่ยนประเภท a1b2c3d4 แล็บ": "lab",
            "เปลี่ยนประเภท a1b2c3d4 รอตรวจ": "review",
        }
        for text, cat in cases.items():
            with self.subTest(text=text):
                got = self.parse(text)
                self.assertIsNotNone(got, f"ต้องรับ: {text!r}")
                self.assertEqual(got[0], "a1b2c3d4")
                self.assertEqual(got[1], cat)
                self.assertEqual(got[2], "")

    def test_รับ_hn_ต่อท้ายได้(self):
        got = self.parse("เปลี่ยนประเภท a1b2c3d4 ใบยา 57042")
        self.assertEqual(got, ("a1b2c3d4", "rx", "57042"))

    def test_ปฏิเสธคำสั่งที่ไม่ครบหรือประเภทไม่รู้จัก(self):
        for text in ("เปลี่ยนประเภท a1b2c3d4",
                     "เปลี่ยนประเภท a1b2c3d4 ไม่รู้จัก",
                     "เปลี่ยนประเภท abc ใบยา",      # รหัสสั้นเกินไป
                     "เปลี่ยนประเภท zzzzzz ใบยา",   # ไม่ใช่ hex
                     "เปลี่ยนประเภท",
                     ""):
            with self.subTest(text=text):
                self.assertIsNone(self.parse(text))

    def test_ไม่ชนกับคำสั่งอื่น(self):
        """อยู่ใน elif chain เดียวกับคำสั่งอื่น ต้องไม่จับผิดตัว"""
        for text in ("ย้ายเอกสาร a1b2c3d4 57042", "ผู้ป่วย 57042",
                     "รับทราบ a1b2c3d4", "แจ้งเตือนค้าง",
                     "ค้นหา เบาหวาน", "ทะเบียนผู้ป่วย"):
            with self.subTest(text=text):
                self.assertIsNone(self.parse(text))


# ─────────────────────────────────────────────────────────────
# สภาพแวดล้อมจำลอง
# ─────────────────────────────────────────────────────────────

class FakeBlob:
    def __init__(self, name, bucket):
        self.name = name
        self._bucket = bucket

    def exists(self):
        return self.name in self._bucket.objects


class FakeBucket:
    def __init__(self):
        self.objects = {}
        self.copies = []
        self.deletes = []

    def blob(self, name):
        return FakeBlob(name, self)

    def copy_blob(self, src, bucket, new_name):
        self.copies.append((src.name, new_name))
        self.objects[new_name] = self.objects.get(src.name, b"data")
        return FakeBlob(new_name, self)

    def delete_blob(self, name):          # ต้องไม่ถูกเรียกเลย
        self.deletes.append(name)


class FakePatient:
    def __init__(self, folder_name, hn_display, patient_name):
        self.folder_name = folder_name
        self.hn_display = hn_display
        self.patient_name = patient_name


class RecatFixture(unittest.TestCase):

    DOC_ID = "10963875aabbccdd"
    OLD_PATH = "04_LAB_RESULTS/2026-Q4/2026-10/57042_Somchai/f.jpg"

    def setUp(self):
        self.server = s = _server()
        self._backup = {
            "bucket": getattr(s.storage, "_bucket", None),
            "docs": list(s.doc_index._docs),
            "match_hn": s.master_registry.match_hn,
            "ensure": s.doc_index._ensure_fresh,
            "update_cat": s.doc_index.update_category,
            "is_ready": type(s.storage).is_ready,
            "get": s.storage.get,
            "audit": s.audit_logger.log,
        }

        self.bucket = FakeBucket()
        self.bucket.objects[self.OLD_PATH] = b"image-bytes"
        s.storage._bucket = self.bucket
        type(s.storage).is_ready = property(lambda self: True)
        s.storage.get = lambda path: None      # ไม่มีผลแล็บอ่านไว้

        self.doc = {
            "document_id": self.DOC_ID,
            "category": "lab",
            "gcs_object_path": f"gs://test-bucket/{self.OLD_PATH}",
            "upload_date": "2026-10-24",
            "extracted_text": "HN 57042 ใบรับยา",
        }
        s.doc_index._docs = [self.doc]
        s.doc_index._ensure_fresh = lambda: None

        self.updates = []
        s.doc_index.update_category = (
            lambda d, c, p: self.updates.append((d, c, p)) or True
        )

        self.audits = []
        s.audit_logger.log = (
            lambda ev, actor, meta: self.audits.append((ev, meta))
        )

        self.patient = FakePatient("57042_Somchai", "57042", "Somchai")
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
        s.storage.get = self._backup["get"]
        s.audit_logger.log = self._backup["audit"]

    def _change(self, cat="rx", hn="57042"):
        return self.server._change_document_category(
            self.DOC_ID[:8], cat, hn, "actor-hash",
        )


# ─────────────────────────────────────────────────────────────
# พฤติกรรม
# ─────────────────────────────────────────────────────────────

class TestChangeCategory(RecatFixture):

    def test_ย้ายไปหมวดใหม่พร้อมแฟ้มผู้ป่วย(self):
        r = self._change("rx")
        self.assertEqual(r.get("status"), "changed", r)
        self.assertEqual(len(self.bucket.copies), 1)
        _, new_path = self.bucket.copies[0]
        self.assertTrue(new_path.startswith("03_PRESCRIPTIONS/"), new_path)
        self.assertIn("57042_Somchai", new_path)

    def test_ไม่ลบไฟล์ต้นฉบับ(self):
        self._change("rx")
        self.assertEqual(self.bucket.deletes, [])
        self.assertIn(self.OLD_PATH, self.bucket.objects)

    def test_index_ชี้ไป_path_ใหม่พร้อมหมวดใหม่(self):
        self._change("rx")
        self.assertEqual(len(self.updates), 1)
        doc_id, cat, path = self.updates[0]
        self.assertEqual(doc_id, self.DOC_ID)
        self.assertEqual(cat, "rx")
        self.assertTrue(path.startswith("03_PRESCRIPTIONS/"))

    def test_คงเดือนเดิมของเอกสารไม่ย้ายข้ามช่วงเวลา(self):
        self._change("rx")
        _, new_path = self.bucket.copies[0]
        self.assertIn("2026-10", new_path)

    def test_บันทึก_audit_พร้อมหมวดเดิมและหมวดใหม่(self):
        self._change("rx")
        events = [e for e, _ in self.audits]
        self.assertIn("document_category_changed", events)
        meta = dict(self.audits[0][1])
        self.assertEqual(meta["from_category"], "lab")
        self.assertEqual(meta["to_category"], "rx")

    def test_หมวดเดิมเท่าหมวดใหม่ไม่ทำอะไร(self):
        r = self._change("lab")
        self.assertEqual(r.get("status"), "unchanged")
        self.assertEqual(self.bucket.copies, [])
        self.assertEqual(self.updates, [])

    def test_เอกสารคลินิกที่หา_hn_ไม่ได้ต้องถูกปฏิเสธ(self):
        """ห้ามปล่อยให้ลงที่รากหมวดโดยไม่มีแฟ้มผู้ป่วย"""
        self.doc["extracted_text"] = "ไม่มีเลข HN ในเอกสารนี้"
        r = self.server._change_document_category(
            self.DOC_ID[:8], "dialysis", "", "actor-hash",
        )
        self.assertIn("error", r)
        self.assertIn("HN", r["error"])
        self.assertEqual(self.bucket.copies, [])

    def test_hn_ที่ไม่มีในทะเบียนถูกปฏิเสธ(self):
        r = self._change("rx", hn="99999")
        self.assertIn("error", r)
        self.assertEqual(self.bucket.copies, [])

    def test_ประเภทที่ไม่รู้จักถูกปฏิเสธ(self):
        r = self._change("ไม่มีหมวดนี้")
        self.assertIn("error", r)
        self.assertEqual(self.bucket.copies, [])

    def test_รหัสเอกสารกำกวมไม่เดา(self):
        s = self.server
        s.doc_index._docs = [
            dict(self.doc, document_id="10963875aaaa0001"),
            dict(self.doc, document_id="10963875bbbb0002"),
        ]
        r = self._change("rx")
        self.assertIn("error", r)
        self.assertEqual(self.bucket.copies, [])

    def test_ไม่พบไฟล์ต้นฉบับต้องไม่อัปเดต_index(self):
        self.bucket.objects.pop(self.OLD_PATH)
        r = self._change("rx")
        self.assertIn("error", r)
        self.assertEqual(self.updates, [])

    def test_บิลต้องเข้าแฟ้มบริษัทไม่ใช่แฟ้มผู้ป่วย(self):
        r = self._change("bills", hn="")
        self.assertEqual(r.get("status"), "changed", r)
        _, new_path = self.bucket.copies[0]
        self.assertTrue(new_path.startswith("02_BILLS_RECEIPTS/"), new_path)
        self.assertNotIn("57042_Somchai", new_path)

    def test_เตือนเมื่อย้ายออกจาก_lab_ที่มีผลแล็บอ่านไว้แล้ว(self):
        """ค่าที่อ่านไว้จะค้างอยู่ — ต้องบอกให้รู้ ไม่ลบให้เอง"""
        self.server.storage.get = lambda path: b'{"results": []}'
        r = self._change("rx")
        self.assertEqual(r.get("status"), "changed", r)
        self.assertIn("ผลแล็บ", r["message"])
        meta = dict(self.audits[0][1])
        self.assertTrue(meta["stale_lab_results"])

    def test_ไม่เตือนเมื่อไม่มีผลแล็บอ่านไว้(self):
        r = self._change("rx")
        meta = dict(self.audits[0][1])
        self.assertFalse(meta["stale_lab_results"])


class TestCommandIsGated(unittest.TestCase):
    """คำสั่งต้องจำกัดสิทธิ์ และต้องถูกต่อเข้า dispatcher จริง"""

    def test_dispatcher_ตรวจสิทธิ์ก่อนเรียก(self):
        import pathlib
        src = pathlib.Path("server.py").read_text(encoding="utf-8")
        i = src.index("_parse_change_category(text) is not None")
        block = src[i:i + 1400]
        self.assertIn("PHYSICIAN_USERS", block)
        self.assertIn("ADMIN_USERS", block)
        self.assertIn("document_category_change_denied", block)
        # การตรวจสิทธิ์ต้องมาก่อนการเรียกฟังก์ชันที่เขียนข้อมูล
        self.assertLess(
            block.index("PHYSICIAN_USERS"),
            block.index("_change_document_category("),
            "ตรวจสิทธิ์ต้องมาก่อนการย้ายเอกสาร",
        )


if __name__ == "__main__":
    unittest.main()
