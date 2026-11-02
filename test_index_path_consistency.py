"""ดัชนีต้องเก็บ path ของ object เป็นรูปเดียว (ไม่มี gs://) และงาน
"จัดแฟ้มย้อนหลัง" ต้องไม่แตะเอกสารที่อยู่ในแฟ้มผู้ป่วยถูกแล้ว

เหตุการณ์จริง 2026-11-02 17:34: หลัง worker เกิดใหม่ (OOM) งานหลังบูตรัน
"จัดแฟ้มย้อนหลัง" → copy ใบที่อยู่ในแฟ้ม 66338 ถูกแล้วไปอีก path และเขียน
gcs_object_path = "gs://bucket/05_…" ลงดัชนี → ลิงก์เปิดไฟล์/LIFF 404
(ใบที่ไม่ถูกแตะเปิดได้ปกติ)

ข้อมูลทั้งหมดเป็นค่าสังเคราะห์ · import server แบบ lazy
"""
import json
import unittest
from datetime import datetime, timezone


def _server():
    import server as _s
    return _s


class _Blob:
    def __init__(self, name, bucket, data=b""):
        self.name, self.b, self._data = name, bucket, data
        self.time_created = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
        self.metadata = None

    def exists(self):
        return self.name in self.b.objects

    def download_as_text(self, encoding="utf-8"):
        return self.b.objects[self.name].decode()

    def upload_from_string(self, data, content_type=None, if_generation_match=None):
        if if_generation_match == 0 and self.name in self.b.objects:
            raise RuntimeError("412")
        self.b.objects[self.name] = data if isinstance(data, bytes) else data.encode()

    def generate_signed_url(self, **kw):
        return f"https://signed/{self.name}"


class _Bucket:
    def __init__(self):
        self.objects = {}
        self.copies = []

    def blob(self, name):
        return _Blob(name, self)

    def list_blobs(self, prefix="", max_results=None):
        return [_Blob(n, self) for n in sorted(self.objects) if n.startswith(prefix)]

    def copy_blob(self, src, bucket, new_name):
        self.copies.append((src.name, new_name))
        self.objects[new_name] = self.objects[src.name]
        return _Blob(new_name, self)


class _Client:
    def __init__(self, b):
        self.b = b

    def list_blobs(self, bucket_name, prefix="", max_results=None):
        return self.b.list_blobs(prefix)


class _Storage:
    is_ready = True
    bucket_name = "bkt"

    def __init__(self, b):
        self._bucket = b
        self._client = _Client(b)


class TestStripGsUri(unittest.TestCase):

    def test_strip(self):
        s = _server()
        self.assertEqual(s._strip_gs_uri("gs://bkt/05_X/2026-Q4/2026-11/a.jpg"),
                         "05_X/2026-Q4/2026-11/a.jpg")
        self.assertEqual(s._strip_gs_uri("05_X/a.jpg"), "05_X/a.jpg")
        self.assertEqual(s._strip_gs_uri(""), "")


class TestIndexLoadNormalizesPath(unittest.TestCase):

    def test_gs_prefixed_path_in_index_file_is_normalized_on_load(self):
        s = _server()
        b = _Bucket()
        path = "05_DIALYSIS_RECORDS/2026-Q4/2026-11/66338_สมมติ/b6de.jpg"
        b.objects[path] = b"jpg"
        b.objects[f"{s.INDEX_DOC_PREFIX}b6de.json"] = json.dumps({
            "document_id": "b6de", "category": "dialysis",
            "gcs_object_path": "gs://bkt/" + path,        # รูปที่ทำให้ 404
            "patient_hn_folder": "66338_สมมติ", "owner_hash": "o",
        }).encode()
        idx = s.DocumentIndex(_Storage(b))
        idx._blob_cache = {}
        self.assertTrue(idx._reload_from_gcs())
        self.assertEqual(idx.get("b6de")["gcs_object_path"], path)

    def test_update_fields_strips_gs_prefix(self):
        s = _server()
        b = _Bucket()
        b.objects[f"{s.INDEX_DOC_PREFIX}d1.json"] = json.dumps({
            "document_id": "d1", "category": "lab", "gcs_object_path": "04_LAB/a.pdf",
        }).encode()
        idx = s.DocumentIndex(_Storage(b))
        idx._blob_cache = {}
        idx._reload_from_gcs()
        self.assertTrue(idx.update_fields("d1", {"gcs_object_path": "gs://bkt/04_LAB/b.pdf"}))
        self.assertEqual(idx.get("d1")["gcs_object_path"], "04_LAB/b.pdf")


class TestSignedUrlAcceptsBothForms(unittest.TestCase):

    def test_generate_signed_url_strips_prefix(self):
        s = _server()
        b = _Bucket()
        orig = s.storage
        try:
            s.storage = _Storage(b)
            url = s.generate_signed_url("gs://bkt/05_X/2026-Q4/2026-11/f/a.jpg")
            self.assertEqual(url, "https://signed/05_X/2026-Q4/2026-11/f/a.jpg")
        finally:
            s.storage = orig


class TestRetroactiveLeavesFiledDocsAlone(unittest.TestCase):
    """เอกสารที่อยู่ในแฟ้มผู้ป่วยที่ตรงกับทะเบียนแล้ว ต้องไม่ถูก copy/ย้าย
    แม้เดือน/ไตรมาสที่คำนวณจากวันที่จะต่างจาก path เดิม"""

    def setUp(self):
        self.s = _server()
        s = self.s
        self.b = _Bucket()
        self._orig = (s.storage, s.doc_index, s.master_registry)
        s.storage = _Storage(self.b)

        class Reg:
            def count(self):
                return 1

            def get_folder_for_hn(self, hn):
                from patient_master_registry import _normalize_hn
                return "66338_สมมติ" if _normalize_hn(hn) == "66338" else ""
        s.master_registry = Reg()
        # ใบที่อยู่ในแฟ้มถูกแล้ว แต่ path เดือน 2026-10 ขณะ submitted_at เป็น 11
        self.path_ok = "05_DIALYSIS_RECORDS/2026-Q4/2026-10/66338_สมมติ/p1.jpg"
        self.b.objects[self.path_ok] = b"jpg"
        self.b.objects[f"{s.INDEX_DOC_PREFIX}p1.json"] = json.dumps({
            "document_id": "p1", "category": "dialysis",
            "gcs_object_path": self.path_ok, "patient_hn_folder": "66338_สมมติ",
            "extracted_text": "Hemodialysis HN 66338 HD No. 213",
            "submitted_at": "2026-11-02T10:28:00+00:00", "upload_date": "2026-11-02",
        }).encode()
        # ใบเก่าที่ยังไม่อยู่ในแฟ้ม → ต้องถูกย้าย และดัชนีได้ path เปล่า
        self.path_old = "05_DIALYSIS_RECORDS/2026-11/old.jpg"
        self.b.objects[self.path_old] = b"jpg"
        self.b.objects[f"{s.INDEX_DOC_PREFIX}old.json"] = json.dumps({
            "document_id": "old", "category": "dialysis",
            "gcs_object_path": self.path_old,
            "extracted_text": "HN 66338", "upload_date": "2026-11-02",
        }).encode()
        s.doc_index = s.DocumentIndex(s.storage)
        s.doc_index._blob_cache = {}

    def tearDown(self):
        s = self.s
        (s.storage, s.doc_index, s.master_registry) = self._orig

    def test_filed_doc_untouched_old_doc_moved_with_plain_path(self):
        r = self.s._retroactive_route_run(dry_run=False)
        self.assertEqual(r.get("error"), None)
        self.assertEqual(r["already_in_folder"], 1)
        self.assertEqual(r["routed_to_patient_folder"], 1)
        # ใบที่ถูกอยู่แล้ว: ไม่ copy และดัชนีไม่เปลี่ยน
        self.assertNotIn(self.path_ok, [src for src, _ in self.b.copies])
        self.assertEqual(self.s.doc_index.get("p1")["gcs_object_path"], self.path_ok)
        # ใบเก่า: copy เข้าแฟ้ม และดัชนีเป็น path เปล่า ชี้ object ที่มีจริง
        new_path = self.s.doc_index.get("old")["gcs_object_path"]
        self.assertFalse(new_path.startswith("gs://"))
        self.assertIn(new_path, self.b.objects)
        self.assertIn("/66338_สมมติ/", new_path)


class TestBootMaintenanceScope(unittest.TestCase):

    def test_boot_worker_only_reconciles(self):
        import inspect
        s = _server()
        src = inspect.getsource(s._reconcile_on_boot_worker)
        self.assertIn("_reconcile_patient_folder_index", src)
        self.assertNotIn("_retroactive_route_run(", src)
        self.assertNotIn("_BOOT_BILL_MOVES", src)
        self.assertFalse(hasattr(s, "_BOOT_BILL_MOVES"))

    def test_process_concurrency_default_one(self):
        s = _server()
        self.assertEqual(s._PROCESS_MAX_CONCURRENT, 1)


if __name__ == "__main__":
    unittest.main()
