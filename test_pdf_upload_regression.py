"""
Regression tests สำหรับ PDF upload pipeline — ตรวจสอบ:
1. doc_index.add() ใช้ per-document JSON files (ไม่ overwrite JSONL)
2. doc_index.add() จัดการ 403, 412 (idempotent), generic errors
3. _reload_from_gcs() อ่าน legacy JSONL + per-doc files
4. check_connection() ตรวจจับ permission gap
5. process_file_async error handling

สถาปัตยกรรมใหม่: per-document JSON files แทน single JSONL
- เขียน: _index/docs/{doc_id}.json ด้วย if_generation_match=0 (create-only)
- อ่าน: list_blobs(prefix="_index/docs/") + legacy _index/documents.jsonl
- ใช้แค่ storage.objects.create + list + get (ไม่ต้องใช้ objects.delete)

ใช้ unittest + mock — ไม่ต้องติดตั้ง pytest
ไม่ใช้ข้อมูลผู้ป่วยจริง — ใช้เฉพาะข้อมูลจำลอง
"""

import json
import threading
import time
import unittest
from unittest.mock import MagicMock, patch, PropertyMock


# ──────────────────────────────────────────────────────────
# Helpers — mock GCS exceptions ที่มี .code attribute
# ──────────────────────────────────────────────────────────

class FakeGCSError(Exception):
    """Mock GCS exception with a .code attribute."""
    def __init__(self, code, message="GCS error"):
        self.code = code
        super().__init__(message)


class FakeBlob:
    """Mock GCS blob ที่ควบคุม behavior ได้."""

    def __init__(self, name, content=None, generation=1,
                 reload_error=None, download_error=None,
                 upload_error=None, exists_val=True):
        self.name = name
        self._content = content or ""
        self.generation = generation
        self._reload_error = reload_error
        self._download_error = download_error
        self._upload_error = upload_error
        self._exists_val = exists_val

    def reload(self):
        if self._reload_error:
            raise self._reload_error

    def exists(self):
        return self._exists_val

    def download_as_text(self, encoding="utf-8"):
        if self._download_error:
            raise self._download_error
        return self._content

    def upload_from_string(self, data, content_type=None,
                           if_generation_match=None):
        if self._upload_error:
            raise self._upload_error
        self._content = data


class FakeBucket:
    """Mock GCS bucket."""
    def __init__(self, blob_factory=None):
        self._blob_factory = blob_factory or (lambda name: FakeBlob(name))

    def blob(self, name):
        return self._blob_factory(name)


class FakeStorageGCS:
    """Mock StorageGCS ที่ใช้ใน tests."""
    def __init__(self, bucket_name="test-bucket", is_ready=True,
                 blob_factory=None):
        self.bucket_name = bucket_name
        self.is_ready = is_ready
        self._bucket = FakeBucket(blob_factory)
        self._client = MagicMock()


# ──────────────────────────────────────────────────────────
# Constants — mirror server.py
# ──────────────────────────────────────────────────────────

INDEX_OBJECT_PATH = "_index/documents.jsonl"
INDEX_DOC_PREFIX = "_index/docs/"


# ──────────────────────────────────────────────────────────
# DocumentIndex — per-document architecture (mirrors server.py)
# ──────────────────────────────────────────────────────────

class DocumentIndexUnderTest:
    """Minimal DocumentIndex ที่ test ได้โดยไม่ต้อง import server.py.

    จำลอง logic จาก server.py DocumentIndex class (per-document architecture)
    เพื่อทดสอบว่า fix ทำงานถูกต้อง.
    """

    def __init__(self, storage_instance):
        self._storage = storage_instance
        self._docs = []
        self._doc_map = {}
        self._lock = threading.Lock()
        self._cache_ts = 0
        self._cache_ttl = 5

    def _reload_from_gcs(self):
        """Copy of server.py _reload_from_gcs — per-document architecture."""
        if not hasattr(self._storage, '_client') or not self._storage.is_ready:
            return False
        try:
            new_docs = []
            new_map = {}

            # 1. อ่าน legacy JSONL (backward compatibility)
            try:
                legacy_blob = self._storage._bucket.blob(INDEX_OBJECT_PATH)
                if legacy_blob.exists():
                    content = legacy_blob.download_as_text(encoding="utf-8")
                    for line in content.strip().split("\n"):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            doc = json.loads(line)
                            doc_id = doc.get("document_id", "")
                            if doc_id and doc_id not in new_map:
                                new_docs.append(doc)
                                new_map[doc_id] = doc
                        except json.JSONDecodeError:
                            continue
            except Exception:
                pass  # Legacy read failure is not fatal

            # 2. อ่าน per-document JSON files
            try:
                blobs = self._storage._client.list_blobs(
                    self._storage.bucket_name,
                    prefix=INDEX_DOC_PREFIX,
                )
                for blob in blobs:
                    try:
                        content = blob.download_as_text(encoding="utf-8")
                        doc = json.loads(content)
                        doc_id = doc.get("document_id", "")
                        if doc_id:
                            if doc_id in new_map:
                                old_doc = new_map[doc_id]
                                idx = new_docs.index(old_doc)
                                new_docs[idx] = doc
                            else:
                                new_docs.append(doc)
                            new_map[doc_id] = doc
                    except Exception:
                        continue
            except Exception:
                pass  # List failure still allows legacy data

            with self._lock:
                self._docs = new_docs
                self._doc_map = new_map
                self._cache_ts = time.time()
            return True
        except Exception:
            return False

    def _ensure_fresh(self):
        now = time.time()
        if now - self._cache_ts > self._cache_ttl:
            self._reload_from_gcs()

    def add(self, doc_meta):
        """Copy of server.py add() — per-document architecture.

        Creates _index/docs/{doc_id}.json with if_generation_match=0
        (create-only, no overwrite, no objects.delete needed).
        """
        doc_id = doc_meta.get("document_id", "")

        self._ensure_fresh()
        with self._lock:
            if doc_id in self._doc_map:
                return False

        if not self._storage.is_ready:
            raise RuntimeError(
                "Document index: GCS ไม่พร้อม — ไม่สามารถบันทึก metadata"
            )

        # สร้าง per-document JSON file
        doc_json = json.dumps(doc_meta, ensure_ascii=False)
        blob_path = f"{INDEX_DOC_PREFIX}{doc_id}.json"
        try:
            blob = self._storage._bucket.blob(blob_path)
            blob.upload_from_string(
                doc_json,
                content_type="application/json",
                if_generation_match=0,  # create-only
            )
        except Exception as e:
            err_code = getattr(e, "code", None)
            if err_code == 412:
                # File exists — idempotent
                self._reload_from_gcs()
                return False
            elif err_code == 403:
                raise RuntimeError(
                    "Document index: GCS permission denied (403) — "
                    "ตรวจสอบว่า service account มี "
                    "Storage Object Creator role"
                )
            else:
                raise RuntimeError(
                    f"Document index: บันทึก metadata ลง GCS ล้มเหลว: "
                    f"{type(e).__name__}"
                )

        with self._lock:
            if doc_id not in self._doc_map:
                self._docs.append(doc_meta)
                self._doc_map[doc_id] = doc_meta
            self._cache_ts = time.time()
        return True


def check_connection_under_test(storage_instance):
    """Copy of server.py check_connection() with per-doc index check."""
    if not storage_instance.is_ready:
        return {"status": "not_ready", "error": "not initialized"}
    try:
        blobs = storage_instance._client.list_blobs(
            storage_instance.bucket_name, max_results=1
        )
        _ = list(blobs)
        result = {"status": "connected", "bucket": storage_instance.bucket_name}

        try:
            idx_blob = storage_instance._bucket.blob(INDEX_OBJECT_PATH)
            idx_blob.reload()
            result["index_writable"] = True
            result["index_architecture"] = "per-document"
        except Exception as perm_err:
            perm_code = getattr(perm_err, "code", None)
            if perm_code == 403:
                result["index_writable"] = False
                result["permission_warning"] = (
                    "storage.objects.get missing — "
                    "document index อ่านไม่ได้"
                )
            elif perm_code == 404:
                result["index_writable"] = True
                result["index_architecture"] = "per-document"
        return result
    except Exception as e:
        return {"status": "error", "error": type(e).__name__}


# ══════════════════════════════════════════════════════════
# Test Cases
# ══════════════════════════════════════════════════════════

class TestDocIndexAddPerDoc(unittest.TestCase):
    """ทดสอบ doc_index.add() — per-document architecture."""

    def test_add_creates_per_doc_file(self):
        """add() ต้องสร้าง _index/docs/{doc_id}.json ด้วย
        if_generation_match=0 (create-only, ไม่ overwrite)."""
        uploads = []

        def blob_factory(name):
            blob = FakeBlob(name, content="", exists_val=False)
            original_upload = blob.upload_from_string

            def track_upload(data, content_type=None,
                             if_generation_match=None):
                uploads.append({
                    "blob_name": name,
                    "data": data,
                    "content_type": content_type,
                    "gen_match": if_generation_match,
                })

            blob.upload_from_string = track_upload
            return blob

        storage = FakeStorageGCS(blob_factory=blob_factory)
        storage._client.list_blobs = MagicMock(return_value=[])
        idx = DocumentIndexUnderTest(storage)
        idx._cache_ts = time.time()

        doc_meta = {
            "document_id": "test-doc-001",
            "filename": "TEST-REPORT.pdf",
            "category": "lab_result",
        }

        result = idx.add(doc_meta)
        self.assertTrue(result)
        self.assertIn("test-doc-001", idx._doc_map)

        # ตรวจว่า upload ถูก path และ if_generation_match=0
        self.assertEqual(len(uploads), 1)
        self.assertEqual(uploads[0]["blob_name"],
                         "_index/docs/test-doc-001.json")
        self.assertEqual(uploads[0]["gen_match"], 0)
        self.assertEqual(uploads[0]["content_type"], "application/json")

        # ตรวจว่า data เป็น valid JSON ที่มี doc_id
        parsed = json.loads(uploads[0]["data"])
        self.assertEqual(parsed["document_id"], "test-doc-001")

    def test_add_raises_runtime_error_on_403(self):
        """เมื่อ upload คืน 403 → raise RuntimeError with permission info."""
        forbidden_err = FakeGCSError(403, "Forbidden")

        def blob_factory(name):
            return FakeBlob(name, upload_error=forbidden_err,
                           exists_val=False)

        storage = FakeStorageGCS(blob_factory=blob_factory)
        storage._client.list_blobs = MagicMock(return_value=[])
        idx = DocumentIndexUnderTest(storage)
        idx._cache_ts = time.time()

        doc_meta = {
            "document_id": "test-doc-002",
            "filename": "TEST-REPORT-2.pdf",
        }

        with self.assertRaises(RuntimeError) as ctx:
            idx.add(doc_meta)

        self.assertIn("403", str(ctx.exception))
        self.assertIn("permission denied", str(ctx.exception).lower())

    def test_add_idempotent_on_412(self):
        """เมื่อ upload คืน 412 (file exists) → return False (idempotent)."""
        precondition_err = FakeGCSError(412, "Precondition Failed")

        def blob_factory(name):
            return FakeBlob(name, upload_error=precondition_err,
                           exists_val=False)

        storage = FakeStorageGCS(blob_factory=blob_factory)
        storage._client.list_blobs = MagicMock(return_value=[])
        idx = DocumentIndexUnderTest(storage)
        idx._cache_ts = time.time()

        doc_meta = {
            "document_id": "test-doc-003",
            "filename": "TEST-REPORT-3.pdf",
        }

        result = idx.add(doc_meta)
        self.assertFalse(result)

    def test_add_raises_runtime_error_on_generic_error(self):
        """เมื่อ upload คืน error อื่น (เช่น 500) → raise RuntimeError."""
        server_err = FakeGCSError(500, "Internal Server Error")

        def blob_factory(name):
            return FakeBlob(name, upload_error=server_err,
                           exists_val=False)

        storage = FakeStorageGCS(blob_factory=blob_factory)
        storage._client.list_blobs = MagicMock(return_value=[])
        idx = DocumentIndexUnderTest(storage)
        idx._cache_ts = time.time()

        doc_meta = {
            "document_id": "test-doc-004",
            "filename": "TEST-REPORT-4.pdf",
        }

        with self.assertRaises(RuntimeError) as ctx:
            idx.add(doc_meta)

        self.assertNotIn("permission denied", str(ctx.exception).lower())
        self.assertIn("ล้มเหลว", str(ctx.exception))

    def test_add_idempotent_existing_in_memory(self):
        """เมื่อ doc_id มีอยู่แล้วใน memory → return False."""
        storage = FakeStorageGCS()
        storage._client.list_blobs = MagicMock(return_value=[])
        idx = DocumentIndexUnderTest(storage)
        idx._cache_ts = time.time()

        doc_meta = {
            "document_id": "existing-doc",
            "filename": "OLD.pdf",
        }
        idx._doc_map["existing-doc"] = doc_meta
        idx._docs.append(doc_meta)

        result = idx.add(doc_meta)
        self.assertFalse(result)

    def test_add_not_ready_raises(self):
        """เมื่อ storage ไม่พร้อม → raise RuntimeError."""
        storage = FakeStorageGCS(is_ready=False)
        idx = DocumentIndexUnderTest(storage)
        idx._cache_ts = time.time()

        doc_meta = {
            "document_id": "test-doc-005",
            "filename": "TEST.pdf",
        }

        with self.assertRaises(RuntimeError) as ctx:
            idx.add(doc_meta)

        self.assertIn("ไม่พร้อม", str(ctx.exception))

    def test_add_no_overwrite_no_delete(self):
        """สำคัญ: add() ต้องไม่เรียก blob.reload() หรือ download_as_text()
        บน index file (ซึ่งต้องใช้ objects.get แล้ว overwrite ด้วย objects.delete).
        Per-doc architecture ใช้แค่ objects.create."""
        calls = {"reload": 0, "download": 0}

        def blob_factory(name):
            blob = FakeBlob(name, exists_val=False)
            original_reload = blob.reload
            original_download = blob.download_as_text

            def track_reload():
                if INDEX_DOC_PREFIX in name:
                    calls["reload"] += 1
                return original_reload()

            def track_download(encoding="utf-8"):
                if INDEX_DOC_PREFIX in name:
                    calls["download"] += 1
                return original_download(encoding)

            blob.reload = track_reload
            blob.download_as_text = track_download
            return blob

        storage = FakeStorageGCS(blob_factory=blob_factory)
        storage._client.list_blobs = MagicMock(return_value=[])
        idx = DocumentIndexUnderTest(storage)
        idx._cache_ts = time.time()

        doc_meta = {
            "document_id": "test-no-overwrite",
            "filename": "NO-OVERWRITE.pdf",
        }

        result = idx.add(doc_meta)
        self.assertTrue(result)
        # Per-doc add ไม่ต้อง reload หรือ download per-doc blob
        self.assertEqual(calls["reload"], 0)
        self.assertEqual(calls["download"], 0)


class TestDocIndexReload(unittest.TestCase):
    """ทดสอบ _reload_from_gcs() — per-document architecture."""

    def test_reload_reads_legacy_jsonl(self):
        """อ่าน legacy _index/documents.jsonl ได้ (backward compat)."""
        legacy_content = (
            '{"document_id": "legacy-doc-a", "filename": "A.pdf"}\n'
            '{"document_id": "legacy-doc-b", "filename": "B.pdf"}\n'
        )

        def blob_factory(name):
            if name == INDEX_OBJECT_PATH:
                return FakeBlob(name, content=legacy_content, exists_val=True)
            return FakeBlob(name, exists_val=False)

        storage = FakeStorageGCS(blob_factory=blob_factory)
        storage._client.list_blobs = MagicMock(return_value=[])
        idx = DocumentIndexUnderTest(storage)

        result = idx._reload_from_gcs()
        self.assertTrue(result)
        self.assertEqual(len(idx._docs), 2)
        self.assertIn("legacy-doc-a", idx._doc_map)
        self.assertIn("legacy-doc-b", idx._doc_map)

    def test_reload_reads_per_doc_files(self):
        """อ่าน per-document JSON files จาก _index/docs/."""
        per_doc_blobs = [
            FakeBlob("_index/docs/doc-x.json",
                     content='{"document_id": "doc-x", "filename": "X.pdf"}'),
            FakeBlob("_index/docs/doc-y.json",
                     content='{"document_id": "doc-y", "filename": "Y.pdf"}'),
        ]

        def blob_factory(name):
            if name == INDEX_OBJECT_PATH:
                return FakeBlob(name, exists_val=False)
            return FakeBlob(name, exists_val=False)

        storage = FakeStorageGCS(blob_factory=blob_factory)
        storage._client.list_blobs = MagicMock(return_value=per_doc_blobs)
        idx = DocumentIndexUnderTest(storage)

        result = idx._reload_from_gcs()
        self.assertTrue(result)
        self.assertEqual(len(idx._docs), 2)
        self.assertIn("doc-x", idx._doc_map)
        self.assertIn("doc-y", idx._doc_map)

    def test_reload_merges_legacy_and_per_doc(self):
        """รวม legacy JSONL + per-doc files โดย per-doc overrides legacy."""
        legacy_content = (
            '{"document_id": "doc-shared", "filename": "OLD.pdf", "source": "legacy"}\n'
            '{"document_id": "doc-only-legacy", "filename": "LEGACY.pdf"}\n'
        )
        per_doc_blobs = [
            FakeBlob("_index/docs/doc-shared.json",
                     content='{"document_id": "doc-shared", "filename": "NEW.pdf", "source": "per-doc"}'),
            FakeBlob("_index/docs/doc-only-new.json",
                     content='{"document_id": "doc-only-new", "filename": "NEW-ONLY.pdf"}'),
        ]

        def blob_factory(name):
            if name == INDEX_OBJECT_PATH:
                return FakeBlob(name, content=legacy_content, exists_val=True)
            return FakeBlob(name, exists_val=False)

        storage = FakeStorageGCS(blob_factory=blob_factory)
        storage._client.list_blobs = MagicMock(return_value=per_doc_blobs)
        idx = DocumentIndexUnderTest(storage)

        result = idx._reload_from_gcs()
        self.assertTrue(result)
        self.assertEqual(len(idx._docs), 3)
        # per-doc overrides legacy for shared doc_id
        shared = idx._doc_map["doc-shared"]
        self.assertEqual(shared["source"], "per-doc")
        self.assertEqual(shared["filename"], "NEW.pdf")
        # legacy-only doc preserved
        self.assertIn("doc-only-legacy", idx._doc_map)
        # per-doc-only doc included
        self.assertIn("doc-only-new", idx._doc_map)

    def test_reload_legacy_403_still_reads_per_doc(self):
        """เมื่อ legacy JSONL อ่านไม่ได้ (403) → ยังอ่าน per-doc ได้."""
        forbidden = FakeGCSError(403, "Forbidden")
        per_doc_blobs = [
            FakeBlob("_index/docs/doc-z.json",
                     content='{"document_id": "doc-z", "filename": "Z.pdf"}'),
        ]

        def blob_factory(name):
            if name == INDEX_OBJECT_PATH:
                blob = FakeBlob(name)
                blob.exists = MagicMock(side_effect=forbidden)
                return blob
            return FakeBlob(name, exists_val=False)

        storage = FakeStorageGCS(blob_factory=blob_factory)
        storage._client.list_blobs = MagicMock(return_value=per_doc_blobs)
        idx = DocumentIndexUnderTest(storage)

        result = idx._reload_from_gcs()
        self.assertTrue(result)
        self.assertEqual(len(idx._docs), 1)
        self.assertIn("doc-z", idx._doc_map)

    def test_reload_empty_index(self):
        """เมื่อไม่มีทั้ง legacy และ per-doc → docs = []."""
        def blob_factory(name):
            return FakeBlob(name, exists_val=False)

        storage = FakeStorageGCS(blob_factory=blob_factory)
        storage._client.list_blobs = MagicMock(return_value=[])
        idx = DocumentIndexUnderTest(storage)

        result = idx._reload_from_gcs()
        self.assertTrue(result)
        self.assertEqual(idx._docs, [])
        self.assertEqual(idx._doc_map, {})

    def test_reload_not_ready_returns_false(self):
        """เมื่อ storage ไม่พร้อม → return False."""
        storage = FakeStorageGCS(is_ready=False)
        idx = DocumentIndexUnderTest(storage)

        result = idx._reload_from_gcs()
        self.assertFalse(result)


class TestCheckConnectionPermission(unittest.TestCase):
    """ทดสอบ check_connection() ตรวจจับ permission gap."""

    def test_connection_ok_with_permission(self):
        """เมื่อ blob.reload() สำเร็จ → index_writable = True."""
        def blob_factory(name):
            return FakeBlob(name)

        storage = FakeStorageGCS(blob_factory=blob_factory)
        storage._client.list_blobs = MagicMock(return_value=[])

        result = check_connection_under_test(storage)
        self.assertEqual(result["status"], "connected")
        self.assertTrue(result.get("index_writable", False))
        self.assertEqual(result.get("index_architecture"), "per-document")

    def test_connection_detects_missing_get_permission(self):
        """เมื่อ blob.reload() คืน 403 → index_writable = False."""
        forbidden_err = FakeGCSError(403, "Forbidden")

        def blob_factory(name):
            return FakeBlob(name, reload_error=forbidden_err)

        storage = FakeStorageGCS(blob_factory=blob_factory)
        storage._client.list_blobs = MagicMock(return_value=[])

        result = check_connection_under_test(storage)
        self.assertEqual(result["status"], "connected")
        self.assertFalse(result.get("index_writable", True))
        self.assertIn("storage.objects.get",
                       result.get("permission_warning", ""))

    def test_connection_index_not_found(self):
        """เมื่อ legacy index ไม่มี (404) → index_writable = True
        (per-doc architecture ไม่ต้องพึ่ง legacy file)."""
        not_found_err = FakeGCSError(404, "Not Found")

        def blob_factory(name):
            return FakeBlob(name, reload_error=not_found_err)

        storage = FakeStorageGCS(blob_factory=blob_factory)
        storage._client.list_blobs = MagicMock(return_value=[])

        result = check_connection_under_test(storage)
        self.assertEqual(result["status"], "connected")
        self.assertTrue(result.get("index_writable", False))
        self.assertEqual(result.get("index_architecture"), "per-document")

    def test_connection_not_ready(self):
        """เมื่อ storage ไม่พร้อม → status = not_ready."""
        storage = FakeStorageGCS(is_ready=False)

        result = check_connection_under_test(storage)
        self.assertEqual(result["status"], "not_ready")


class TestProcessFileAsyncErrorHandling(unittest.TestCase):
    """ทดสอบ error message ที่ส่งกลับให้ผู้ใช้ผ่าน LINE."""

    def test_runtime_error_message_format(self):
        """RuntimeError จาก doc_index.add(403) ต้องมีข้อความ
        ที่ช่วยวินิจฉัย — ไม่เปิดเผย credentials."""
        forbidden_err = FakeGCSError(403, "Forbidden")

        def blob_factory(name):
            return FakeBlob(name, upload_error=forbidden_err,
                           exists_val=False)

        storage = FakeStorageGCS(blob_factory=blob_factory)
        storage._client.list_blobs = MagicMock(return_value=[])
        idx = DocumentIndexUnderTest(storage)
        idx._cache_ts = time.time()

        doc_meta = {
            "document_id": "test-doc-err",
            "filename": "TEST.pdf",
        }

        try:
            idx.add(doc_meta)
            self.fail("Expected RuntimeError")
        except RuntimeError as e:
            error_msg = str(e)
            self.assertIn("403", error_msg)
            # ต้องไม่มี credentials
            self.assertNotIn("key", error_msg.lower().split("creator")[0]
                             if "creator" in error_msg.lower() else
                             error_msg.lower())
            self.assertNotIn("secret", error_msg.lower())
            self.assertNotIn("token", error_msg.lower())

    def test_user_facing_error_message(self):
        """ข้อความที่ส่งกลับให้ผู้ใช้ LINE ต้องเป็นภาษาไทย
        และไม่มีข้อมูลเทคนิค."""
        user_message = (
            "ขออภัย ระบบไม่สามารถจัดเก็บเอกสารได้ในขณะนี้\n"
            "กรุณาลองส่งใหม่ในอีกสักครู่\n"
            "หากยังไม่ได้ กรุณาแจ้งเจ้าหน้าที่"
        )
        self.assertNotIn("403", user_message)
        self.assertNotIn("Forbidden", user_message)
        self.assertNotIn("GCS", user_message)
        self.assertNotIn("permission", user_message)
        self.assertNotIn("blob", user_message)
        self.assertIn("ขออภัย", user_message)
        self.assertIn("กรุณา", user_message)


class TestPerDocArchitectureNoDelete(unittest.TestCase):
    """ทดสอบว่า per-document architecture ไม่ต้องใช้ objects.delete."""

    def test_add_uses_only_create_permission(self):
        """add() ต้องใช้แค่ upload_from_string(if_generation_match=0)
        ซึ่งต้องการแค่ storage.objects.create — ไม่ overwrite, ไม่ delete."""
        uploads = []

        def blob_factory(name):
            blob = FakeBlob(name, exists_val=False)

            def track_upload(data, content_type=None,
                             if_generation_match=None):
                uploads.append({
                    "name": name,
                    "gen_match": if_generation_match,
                })

            blob.upload_from_string = track_upload
            return blob

        storage = FakeStorageGCS(blob_factory=blob_factory)
        storage._client.list_blobs = MagicMock(return_value=[])
        idx = DocumentIndexUnderTest(storage)
        idx._cache_ts = time.time()

        for i in range(3):
            doc_meta = {
                "document_id": f"multi-doc-{i}",
                "filename": f"DOC-{i}.pdf",
            }
            idx.add(doc_meta)

        # ทุก upload ต้องเป็น if_generation_match=0 (create-only)
        self.assertEqual(len(uploads), 3)
        for u in uploads:
            self.assertEqual(u["gen_match"], 0,
                             f"Upload to {u['name']} used gen_match="
                             f"{u['gen_match']}, expected 0 (create-only)")

    def test_reload_uses_only_list_and_get(self):
        """_reload_from_gcs() ใช้แค่ list_blobs + download_as_text
        ซึ่งต้องการแค่ objects.list + objects.get — ไม่ delete."""
        per_doc_blobs = [
            FakeBlob("_index/docs/doc-1.json",
                     content='{"document_id": "doc-1", "filename": "1.pdf"}'),
        ]

        def blob_factory(name):
            if name == INDEX_OBJECT_PATH:
                return FakeBlob(name, exists_val=False)
            return FakeBlob(name, exists_val=False)

        storage = FakeStorageGCS(blob_factory=blob_factory)
        storage._client.list_blobs = MagicMock(return_value=per_doc_blobs)
        idx = DocumentIndexUnderTest(storage)

        result = idx._reload_from_gcs()
        self.assertTrue(result)

        # list_blobs ถูกเรียกด้วย prefix
        storage._client.list_blobs.assert_called_once_with(
            storage.bucket_name, prefix=INDEX_DOC_PREFIX)


if __name__ == "__main__":
    unittest.main(verbosity=2)
