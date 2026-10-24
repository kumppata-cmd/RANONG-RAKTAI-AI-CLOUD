#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests for OCR Review module (Feature 4).

ทดสอบ:
  - Structured data extraction จากบิล (regex)
  - Structured data extraction จากผลแล็บ (regex)
  - API endpoints (/admin/api/v2/ocr/*)
  - OCR Review UI page
  - Auth (ADMIN_SECRET)
"""

import json
import os
import sys
import unittest

# Add project root
sys.path.insert(0, os.path.dirname(__file__))


class TestBillStructuredExtraction(unittest.TestCase):
    """ทดสอบ extract_bill_structured_data."""

    def setUp(self):
        from ocr_review import extract_bill_structured_data
        self.extract = extract_bill_structured_data

    def test_empty_text(self):
        result = self.extract("")
        self.assertEqual(result["amounts"], [])
        self.assertIsNone(result["total"])

    def test_thai_amount(self):
        text = "จำนวนเงิน 15,750.00 บาท"
        result = self.extract(text)
        self.assertIn(15750.0, result["amounts"])
        self.assertEqual(result["total"], 15750.0)

    def test_thai_digits(self):
        text = "ยอดรวม ๑๒,๓๔๕.๖๗ บาท"
        result = self.extract(text)
        self.assertIn(12345.67, result["amounts"])

    def test_vat_extraction(self):
        text = "ยอดรวม 10,000.00 บาท\nภาษีมูลค่าเพิ่ม 700.00"
        result = self.extract(text)
        self.assertEqual(result["vat"], 700.0)

    def test_date_extraction(self):
        text = "วันที่ 15/08/2569 ยอดรวม 5,000.00 บาท"
        result = self.extract(text)
        self.assertTrue(len(result["dates"]) > 0)

    def test_invoice_number(self):
        text = "เลขที่ใบเสร็จ: INV-2569-0042 ยอดชำระ 8,500.00 บาท"
        result = self.extract(text)
        self.assertEqual(result["invoice_no"], "INV-2569-0042")

    def test_tax_id(self):
        text = "เลขประจำตัวผู้เสียภาษี: 0105551012345 ยอดรวม 3,200 บาท"
        result = self.extract(text)
        self.assertEqual(result["tax_id"], "0105551012345")

    def test_line_items(self):
        text = """1. น้ำยาฟอกไตขนาด 5 ลิตร 12,000.00
2. สายฟอกไต Blood Tubing Set 3,500.00
ยอดรวม 15,500.00 บาท"""
        result = self.extract(text)
        self.assertTrue(len(result["items"]) >= 2)

    def test_multiple_amounts(self):
        text = "รายการ 1: 5,000.00 บาท\nรายการ 2: 3,000.00 บาท\nรวมทั้งสิ้น 8,000.00 บาท"
        result = self.extract(text)
        self.assertTrue(len(result["amounts"]) >= 2)

    def test_extraction_method(self):
        result = self.extract("ยอดรวม 100 บาท")
        self.assertEqual(result["extraction_method"], "regex_local")


class TestLabStructuredExtraction(unittest.TestCase):
    """ทดสอบ extract_lab_structured_data."""

    def setUp(self):
        from ocr_review import extract_lab_structured_data
        self.extract = extract_lab_structured_data

    def test_empty_text(self):
        result = self.extract("")
        self.assertEqual(result["analytes"], [])

    def test_creatinine(self):
        text = "Creatinine: 1.5 mg/dL"
        result = self.extract(text)
        analytes = {a["analyte"]: a for a in result["analytes"]}
        self.assertIn("Creatinine", analytes)
        self.assertEqual(analytes["Creatinine"]["value"], 1.5)

    def test_multiple_analytes(self):
        text = """Creatinine: 2.1 mg/dL
BUN: 45 mg/dL
eGFR: 28 mL/min
Potassium: 5.2 mmol/L
Hemoglobin: 10.5 g/dL"""
        result = self.extract(text)
        self.assertTrue(len(result["analytes"]) >= 4)

    def test_thai_analyte(self):
        text = "ครีอะตินิน: 3.2 mg/dL"
        result = self.extract(text)
        analytes = {a["analyte"]: a for a in result["analytes"]}
        self.assertIn("Creatinine", analytes)

    def test_extraction_method_lab(self):
        result = self.extract("Creatinine: 1.0 mg/dL")
        self.assertEqual(result["extraction_method"], "regex_local")


class MockStorage:
    """Mock storage for testing."""
    def __init__(self):
        self._files = {}

    def put(self, path, data, content_type=None):
        self._files[path] = data

    def get(self, path):
        return self._files.get(path)

    def list_files(self, prefix=""):
        return [k for k in self._files if k.startswith(prefix)]


class MockDocIndex:
    """Mock document index for testing."""
    def __init__(self):
        self._docs = [
            {
                "document_id": "abc12345-test-0001",
                "category": "bills",
                "upload_date": "2026-10-20",
                "ocr_status": "ocr",
                "extracted_text": "บริษัท นิกกิโซ จำกัด\nเลขที่ใบเสร็จ: INV-001\nจำนวนเงิน 25,000.00 บาท\nภาษีมูลค่าเพิ่ม 1,750.00",
                "bill_company": "NIKKISO",
                "mime_type": "image/jpeg",
                "file_size": 150000,
                "owner_hash": "abc123",
            },
            {
                "document_id": "def67890-test-0002",
                "category": "lab",
                "upload_date": "2026-10-21",
                "ocr_status": "ocr",
                "extracted_text": "Creatinine: 2.5 mg/dL\nBUN: 38 mg/dL\neGFR: 22 mL/min\nHemoglobin: 9.8 g/dL",
                "bill_company": "",
                "mime_type": "image/png",
                "file_size": 200000,
                "owner_hash": "def456",
            },
            {
                "document_id": "ghi11111-test-0003",
                "category": "inbox",
                "upload_date": "2026-10-22",
                "ocr_status": "none",
                "extracted_text": "",
                "bill_company": "",
                "mime_type": "image/jpeg",
                "file_size": 80000,
                "owner_hash": "ghi789",
            },
        ]


class TestOCRReviewAPI(unittest.TestCase):
    """ทดสอบ API endpoints."""

    def setUp(self):
        from flask import Flask
        from ocr_review import register_ocr_review

        self.app = Flask(__name__)
        self.app.config["TESTING"] = True

        self.storage = MockStorage()
        self.doc_index = MockDocIndex()

        os.environ["ADMIN_SECRET"] = "test-secret-123"
        register_ocr_review(self.app, self.storage, self.doc_index, None)
        self.client = self.app.test_client()

    def tearDown(self):
        if "ADMIN_SECRET" in os.environ:
            del os.environ["ADMIN_SECRET"]

    def test_queue_unauthorized(self):
        r = self.client.get("/admin/api/v2/ocr/queue")
        self.assertEqual(r.status_code, 401)

    def test_queue_authorized(self):
        r = self.client.get("/admin/api/v2/ocr/queue?secret=test-secret-123")
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn("items", data)
        self.assertIn("summary", data)
        self.assertEqual(data["total"], 3)

    def test_queue_filter_category(self):
        r = self.client.get("/admin/api/v2/ocr/queue?secret=test-secret-123&category=bills")
        data = json.loads(r.data)
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["category"], "bills")

    def test_queue_filter_status_has_text(self):
        r = self.client.get("/admin/api/v2/ocr/queue?secret=test-secret-123&status=has_text")
        data = json.loads(r.data)
        self.assertEqual(data["total"], 2)  # bills + lab have text

    def test_queue_filter_status_no_text(self):
        r = self.client.get("/admin/api/v2/ocr/queue?secret=test-secret-123&status=no_text")
        data = json.loads(r.data)
        self.assertEqual(data["total"], 1)  # inbox has no text

    def test_document_detail_bills(self):
        r = self.client.get("/admin/api/v2/ocr/document/abc12345?secret=test-secret-123")
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data["category"], "bills")
        self.assertIn("structured_data", data)
        sd = data["structured_data"]
        self.assertIn(25000.0, sd["amounts"])
        self.assertEqual(sd["vat"], 1750.0)

    def test_document_detail_lab(self):
        r = self.client.get("/admin/api/v2/ocr/document/def67890?secret=test-secret-123")
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data["category"], "lab")
        sd = data["structured_data"]
        analytes = {a["analyte"]: a for a in sd["analytes"]}
        self.assertIn("Creatinine", analytes)
        self.assertEqual(analytes["Creatinine"]["value"], 2.5)

    def test_document_not_found(self):
        r = self.client.get("/admin/api/v2/ocr/document/zzzzzzz?secret=test-secret-123")
        self.assertEqual(r.status_code, 404)

    def test_extract_bills(self):
        r = self.client.get("/admin/api/v2/ocr/extract-bills?secret=test-secret-123")
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn("bills", data)
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["bills"][0]["bill_company"], "NIKKISO")

    def test_ocr_review_page_unauthorized(self):
        r = self.client.get("/admin/v2/ocr-review")
        self.assertEqual(r.status_code, 401)

    def test_ocr_review_page_authorized(self):
        r = self.client.get("/admin/v2/ocr-review?secret=test-secret-123")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"OCR Review", r.data)

    def test_queue_summary_counts(self):
        r = self.client.get("/admin/api/v2/ocr/queue?secret=test-secret-123")
        data = json.loads(r.data)
        summary = data["summary"]
        self.assertEqual(summary["total_docs"], 3)
        self.assertEqual(summary["with_text"], 2)
        self.assertEqual(summary["ocr_done"], 2)

    def test_doc_ids_truncated(self):
        """ตรวจสอบว่า doc_id ถูกตัดเหลือ 8 ตัว (ไม่แสดง full ID)."""
        r = self.client.get("/admin/api/v2/ocr/queue?secret=test-secret-123")
        data = json.loads(r.data)
        for item in data["items"]:
            self.assertTrue(len(item["doc_id"]) <= 8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
