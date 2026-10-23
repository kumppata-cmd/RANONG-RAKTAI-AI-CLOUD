"""
E2E Lab Flow Test — Root Cause Debug for TEST-CKD-001.pdf
ทดสอบครบวงจร: Upload → OCR → Classification → Lab Extraction → Index → Retrieval

สร้างจาก Root Cause Debug Items 4-6:
- ตรวจว่าระบบค้นหาเอกสารฉบับใดเมื่อสั่ง 'ผลแล็บล่าสุด'
- ตรวจว่าข้อมูล Lab ถูก persist และ retrieve ด้วย doc_id/owner_hash ที่ถูกต้อง
- Synthetic PDF E2E test (BUN 45, Cr 5.2, K 4.5)

ห้ามใช้ข้อมูลผู้ป่วยจริง — ใช้เฉพาะ synthetic test data
"""

import hashlib
import json
import os
import sys
import threading
import time
import unittest

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["APP_ENV"] = "testing"

import logging
logging.disable(logging.WARNING)


class TestE2ELabFlow(unittest.TestCase):
    """End-to-end test of the lab flow using TEST-CKD-001.pdf."""

    @classmethod
    def setUpClass(cls):
        from server import (
            extract_pdf_text,
            classify_with_keywords,
            DocumentIndex,
            StorageLocal,
            LAB_RESULTS_PREFIX,
            hash_user_id,
        )
        from lab_extractor import extract_lab_results_from_text
        from line_bot_enhancer import (
            parse_command,
            CommandType,
            find_latest_lab_doc,
            find_latest_doc_by_category,
        )

        cls.extract_pdf_text = staticmethod(extract_pdf_text)
        cls.classify_with_keywords = staticmethod(classify_with_keywords)
        cls.DocumentIndex = DocumentIndex
        cls.StorageLocal = StorageLocal
        cls.LAB_RESULTS_PREFIX = LAB_RESULTS_PREFIX
        cls.hash_user_id = staticmethod(hash_user_id)
        cls.extract_lab_results_from_text = staticmethod(extract_lab_results_from_text)
        cls.parse_command = staticmethod(parse_command)
        cls.CommandType = CommandType
        cls.find_latest_lab_doc = staticmethod(find_latest_lab_doc)
        cls.find_latest_doc_by_category = staticmethod(find_latest_doc_by_category)

        cls.TEST_PDF = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "test_data",
            "TEST-CKD-001.pdf",
        )

    # ────────────────────────────────────────────
    # Item 1: PDF มีค่าผลแล็บจริงหรือไม่
    # ────────────────────────────────────────────

    def test_01_pdf_text_extraction(self):
        """extract_pdf_text ต้อง return >= 50 chars จาก TEST-CKD-001.pdf"""
        text = self.extract_pdf_text(self.TEST_PDF)
        self.assertGreaterEqual(len(text.strip()), 50,
            "extracted text too short — extraction may have failed")

    def test_02_pdf_has_lab_keywords(self):
        """PDF text ต้องมี keyword สำหรับ lab classification"""
        text = self.extract_pdf_text(self.TEST_PDF)
        keywords = ["Creatinine", "BUN", "eGFR", "Potassium", "Sodium"]
        found = [k for k in keywords if k.lower() in text.lower()]
        self.assertGreaterEqual(len(found), 3,
            f"PDF should contain lab keywords, found: {found}")

    # ────────────────────────────────────────────
    # Item 2: Classification
    # ────────────────────────────────────────────

    def test_03_classification_is_lab(self):
        """classify_with_keywords ต้อง return category='lab'"""
        result = self.classify_with_keywords(self.TEST_PDF)
        self.assertEqual(result.get("category"), "lab",
            f"Expected 'lab', got '{result.get('category')}' — reason: {result.get('reason')}")

    # ────────────────────────────────────────────
    # Item 3: Lab Extraction
    # ────────────────────────────────────────────

    def test_04_lab_extraction_produces_results(self):
        """extract_lab_results_from_text ต้อง return >= 3 results"""
        text = self.extract_pdf_text(self.TEST_PDF)
        report = self.extract_lab_results_from_text(text, "test-ckd-001", "regex_pdf_text")
        self.assertGreaterEqual(len(report.results), 3,
            f"Expected >= 3 lab results, got {len(report.results)}")

    def test_05_key_analytes_extracted(self):
        """BUN=45, Creatinine=5.2, Potassium=4.5 ต้องถูก extract ถูกต้อง"""
        text = self.extract_pdf_text(self.TEST_PDF)
        report = self.extract_lab_results_from_text(text, "test-ckd-001", "regex_pdf_text")

        by_name = {}
        for r in report.results:
            name_lower = r.analyte.lower()
            by_name[name_lower] = r

        # BUN
        bun = by_name.get("bun") or by_name.get("blood urea nitrogen")
        self.assertIsNotNone(bun, f"BUN not found in results. Available: {list(by_name.keys())}")
        self.assertAlmostEqual(bun.value, 45.0, places=1,
            msg=f"BUN should be 45, got {bun.value}")

        # Creatinine
        cr = by_name.get("creatinine") or by_name.get("cr")
        self.assertIsNotNone(cr, f"Creatinine not found. Available: {list(by_name.keys())}")
        self.assertAlmostEqual(cr.value, 5.2, places=1,
            msg=f"Creatinine should be 5.2, got {cr.value}")

        # Potassium
        k = by_name.get("potassium") or by_name.get("k")
        self.assertIsNotNone(k, f"Potassium not found. Available: {list(by_name.keys())}")
        self.assertAlmostEqual(k.value, 4.5, places=1,
            msg=f"Potassium should be 4.5, got {k.value}")

    def test_06_lab_json_serialization(self):
        """Lab report ต้อง serialize เป็น valid JSON"""
        text = self.extract_pdf_text(self.TEST_PDF)
        report = self.extract_lab_results_from_text(text, "test-ckd-001", "regex_pdf_text")
        lab_json = report.to_json()
        parsed = json.loads(lab_json)
        self.assertIn("results", parsed)
        self.assertEqual(len(parsed["results"]), len(report.results))

    # ────────────────────────────────────────────
    # Item 4: Command Parsing
    # ────────────────────────────────────────────

    def test_07_command_parsing_latest(self):
        """'ผลแล็บล่าสุด' ต้อง parse เป็น LAB_RESULT with no args"""
        cmd = self.parse_command("ผลแล็บล่าสุด")
        self.assertEqual(cmd.command_type, self.CommandType.LAB_RESULT)
        # args is '' (empty string) — falsy, triggers latest search
        self.assertFalse(cmd.args,
            f"args should be falsy for latest search, got: {repr(cmd.args)}")

    def test_08_command_parsing_with_doc_id(self):
        """'ผลแล็บ abc123' ต้อง parse เป็น LAB_RESULT with args='abc123'"""
        cmd = self.parse_command("ผลแล็บ abc123")
        self.assertEqual(cmd.command_type, self.CommandType.LAB_RESULT)
        self.assertEqual(cmd.args, "abc123")

    # ────────────────────────────────────────────
    # Item 5: Document Index & Retrieval
    # ────────────────────────────────────────────

    def test_09_list_by_owner_filters_correctly(self):
        """list_by_owner ต้อง return เฉพาะ docs ที่มี owner_hash ตรงกัน"""
        storage = self.StorageLocal()
        idx = self.DocumentIndex(storage)

        hash_a = "aaa_hash_aaa"
        hash_b = "bbb_hash_bbb"

        doc_a = {"document_id": "doc-a", "category": "lab", "owner_hash": hash_a,
                 "upload_date": "2026-01-15"}
        doc_b = {"document_id": "doc-b", "category": "lab", "owner_hash": hash_b,
                 "upload_date": "2026-01-16"}

        with idx._lock:
            idx._docs.extend([doc_a, doc_b])
            idx._doc_map["doc-a"] = doc_a
            idx._doc_map["doc-b"] = doc_b

        results_a = idx.list_by_owner(hash_a, limit=100)
        results_b = idx.list_by_owner(hash_b, limit=100)

        self.assertEqual(len(results_a), 1)
        self.assertEqual(results_a[0]["document_id"], "doc-a")
        self.assertEqual(len(results_b), 1)
        self.assertEqual(results_b[0]["document_id"], "doc-b")

    def test_10_find_latest_lab_doc_filters_category(self):
        """find_latest_lab_doc ต้อง return เฉพาะ category=lab"""
        hash_x = "xxx_hash_xxx"
        docs = [
            {"document_id": "lab-1", "category": "lab", "owner_hash": hash_x,
             "upload_date": "2026-01-10"},
            {"document_id": "review-1", "category": "review", "owner_hash": hash_x,
             "upload_date": "2026-01-20"},  # newer but not lab
        ]
        latest = self.find_latest_lab_doc(docs, hash_x)
        self.assertIsNotNone(latest)
        self.assertEqual(latest["document_id"], "lab-1")

    def test_11_find_latest_lab_doc_picks_newest(self):
        """find_latest_lab_doc ต้อง return doc ที่มี upload_date ใหม่ที่สุด"""
        hash_x = "xxx_hash_xxx"
        docs = [
            {"document_id": "old-lab", "category": "lab", "owner_hash": hash_x,
             "upload_date": "2026-01-01"},
            {"document_id": "new-lab", "category": "lab", "owner_hash": hash_x,
             "upload_date": "2026-01-15"},
        ]
        latest = self.find_latest_lab_doc(docs, hash_x)
        self.assertIsNotNone(latest)
        self.assertEqual(latest["document_id"], "new-lab")

    def test_12_get_with_owner_check(self):
        """get_with_owner_check ต้อง return (doc, True) เมื่อ owner ตรง"""
        storage = self.StorageLocal()
        idx = self.DocumentIndex(storage)
        hash_x = "xxx_hash_xxx"
        hash_y = "yyy_hash_yyy"

        doc = {"document_id": "doc-x", "category": "lab", "owner_hash": hash_x}
        with idx._lock:
            idx._docs.append(doc)
            idx._doc_map["doc-x"] = doc

        found, auth = idx.get_with_owner_check("doc-x", hash_x)
        self.assertIsNotNone(found)
        self.assertTrue(auth)

        found2, auth2 = idx.get_with_owner_check("doc-x", hash_y)
        self.assertIsNotNone(found2)
        self.assertFalse(auth2)

    # ────────────────────────────────────────────
    # Item 6: Full E2E Chain (in-memory simulation)
    # ────────────────────────────────────────────

    def test_13_full_e2e_chain(self):
        """
        ทดสอบครบวงจร:
        PDF → text → classify → index.add → extract_lab → storage.put
        → list_by_owner → find_latest → get_with_owner_check → storage.get
        """
        storage = self.StorageLocal()
        idx = self.DocumentIndex(storage)

        user_hash = self.hash_user_id("U_E2E_TEST_USER")
        doc_id = "e2e-test-001"

        # 1. Extract
        text = self.extract_pdf_text(self.TEST_PDF)
        self.assertGreaterEqual(len(text.strip()), 50)

        # 2. Classify
        result = self.classify_with_keywords(self.TEST_PDF)
        category = result.get("category")
        self.assertEqual(category, "lab")

        # 3. Index (manual — StorageLocal.is_ready is False)
        doc_meta = {
            "document_id": doc_id,
            "original_filename": "TEST-CKD-001.pdf",
            "category": category,
            "upload_date": "2026-01-15",
            "ocr_status": "native",
            "extracted_text": text,
            "owner_hash": user_hash,
        }
        with idx._lock:
            idx._docs.append(doc_meta)
            idx._doc_map[doc_id] = doc_meta

        # 4. Lab extraction
        report = self.extract_lab_results_from_text(text, doc_id, "regex_pdf_text")
        self.assertGreaterEqual(len(report.results), 3)

        # 5. Store lab JSON
        lab_json = report.to_json()
        lab_path = f"{self.LAB_RESULTS_PREFIX}{doc_id}.json"
        storage.put(lab_path, lab_json.encode("utf-8"), content_type="application/json")

        # 6. Retrieve
        all_docs = idx.list_by_owner(user_hash, limit=100)
        self.assertGreaterEqual(len(all_docs), 1, "list_by_owner returned empty")

        latest = self.find_latest_lab_doc(all_docs, user_hash)
        self.assertIsNotNone(latest, "find_latest_lab_doc returned None")
        self.assertEqual(latest["document_id"], doc_id)

        doc, authorized = idx.get_with_owner_check(doc_id, user_hash)
        self.assertIsNotNone(doc)
        self.assertTrue(authorized)

        # 7. Read lab data
        lab_data = storage.get(lab_path)
        self.assertIsNotNone(lab_data, f"Lab JSON not found at {lab_path}")

        parsed = json.loads(lab_data)
        self.assertGreaterEqual(len(parsed.get("results", [])), 3)

    def test_14_on_demand_reextraction_scenario(self):
        """
        ทดสอบ scenario: lab JSON ไม่อยู่ใน storage
        แต่ index entry มี extracted_text → on-demand re-extraction ทำงาน
        """
        text = self.extract_pdf_text(self.TEST_PDF)

        # Simulate: index has the doc with extracted_text
        doc = {
            "document_id": "reextract-001",
            "category": "lab",
            "extracted_text": text,
            "owner_hash": "test_hash",
        }

        # Verify conditions for on-demand re-extraction
        self.assertEqual(doc["category"], "lab")
        self.assertIsNotNone(doc["extracted_text"])
        self.assertGreaterEqual(len(doc["extracted_text"].strip()), 50)

        # Re-extraction works
        report = self.extract_lab_results_from_text(
            doc["extracted_text"], doc["document_id"], "regex_pdf_text"
        )
        self.assertGreaterEqual(len(report.results), 3,
            "On-demand re-extraction should produce results")

    def test_15_empty_owner_hash_scenario(self):
        """
        ทดสอบ scenario: owner_hash ว่าง → list_by_owner ไม่ return doc
        (RC5: owner_hash mismatch)
        """
        storage = self.StorageLocal()
        idx = self.DocumentIndex(storage)

        # Doc with empty owner_hash
        doc = {
            "document_id": "empty-owner",
            "category": "lab",
            "owner_hash": "",
            "upload_date": "2026-01-15",
        }
        with idx._lock:
            idx._docs.append(doc)
            idx._doc_map["empty-owner"] = doc

        # Query with real hash should NOT find this doc
        real_hash = self.hash_user_id("U_REAL_USER")
        results = idx.list_by_owner(real_hash, limit=100)
        ids = [d["document_id"] for d in results]
        self.assertNotIn("empty-owner", ids,
            "Doc with empty owner_hash should NOT be returned for a real user")


if __name__ == "__main__":
    unittest.main(verbosity=2)
