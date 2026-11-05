#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests for Lab Dashboard module (Feature 5).

ทดสอบ API endpoints + UI page.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))


class MockStorage:
    def __init__(self):
        self._files = {}

    def put(self, path, data, content_type=None):
        self._files[path] = data

    def get(self, path):
        return self._files.get(path)

    def list_files(self, prefix=""):
        return [k for k in sorted(self._files) if k.startswith(prefix)]


class MockDocIndex:
    def __init__(self):
        self._docs = [
            {"document_id": "lab-001-abcdef", "category": "lab", "upload_date": "2026-10-20", "ocr_status": "ocr", "extracted_text": "Creatinine: 1.5", "file_size": 100000, "owner_hash": "aaa"},
            {"document_id": "lab-002-ghijkl", "category": "lab", "upload_date": "2026-10-18", "ocr_status": "ocr", "extracted_text": "BUN: 30", "file_size": 120000, "owner_hash": "bbb"},
            {"document_id": "dia-001-mnopqr", "category": "dialysis", "upload_date": "2026-10-15", "ocr_status": "none", "extracted_text": "", "file_size": 80000, "owner_hash": "aaa"},
            {"document_id": "dia-002-stuvwx", "category": "dialysis", "upload_date": "2026-09-20", "ocr_status": "ocr", "extracted_text": "HD session", "file_size": 95000, "owner_hash": "bbb"},
            {"document_id": "bill-001-xyz", "category": "bills", "upload_date": "2026-10-22", "ocr_status": "ocr", "extracted_text": "Invoice", "file_size": 150000, "owner_hash": "aaa"},
        ]


def _make_lab_report(doc_id, date, analytes, status="pending_review"):
    return json.dumps({
        "source_document_id": doc_id,
        "specimen_date": date,
        "extraction_method": "regex_ocr",
        "status": status,
        "extracted_at": date + "T10:00:00",
        "results": analytes,
        "result_count": len(analytes),
        "warnings": [],
    }).encode("utf-8")


class TestLabDashboardAPI(unittest.TestCase):

    def setUp(self):
        from flask import Flask
        from lab_dashboard import register_lab_dashboard

        self.app = Flask(__name__)
        self.app.config["TESTING"] = True

        self.storage = MockStorage()
        self.doc_index = MockDocIndex()

        # Add lab reports
        self.storage.put("_lab_results/lab-001.json", _make_lab_report(
            "lab-001-abcdef", "2026-10-20",
            [{"analyte": "Creatinine", "value": 2.5, "unit": "mg/dL", "reference_range": "0.6-1.2", "confidence": 0.9, "extraction_method": "regex_ocr", "flags": ["abnormal_high"]},
             {"analyte": "BUN", "value": 38, "unit": "mg/dL", "reference_range": "7-20", "confidence": 0.85, "extraction_method": "regex_ocr", "flags": ["abnormal_high"]}],
            status="approved",
        ))
        self.storage.put("_lab_results/lab-002.json", _make_lab_report(
            "lab-002-ghijkl", "2026-10-18",
            [{"analyte": "Creatinine", "value": 2.3, "unit": "mg/dL", "reference_range": "0.6-1.2", "confidence": 0.88, "extraction_method": "regex_ocr", "flags": []},
             {"analyte": "eGFR", "value": 28, "unit": "mL/min", "reference_range": ">60", "confidence": 0.8, "extraction_method": "regex_ocr", "flags": ["abnormal_low"]}],
            status="pending_review",
        ))

        # Add alerts
        self.storage.put("_alerts/alert-001.json", json.dumps({
            "alert_type": "critical_high", "analyte": "Potassium", "value": 6.5,
            "document_id": "lab-001-abcdef", "created_at": "2026-10-20T10:00:00",
        }).encode("utf-8"))

        os.environ["ADMIN_SECRET"] = "test-secret-lab"
        register_lab_dashboard(self.app, self.storage, self.doc_index, None)
        self.client = self.app.test_client()

    def tearDown(self):
        if "ADMIN_SECRET" in os.environ:
            del os.environ["ADMIN_SECRET"]

    def _get(self, path):
        return self.client.get(path, headers={"X-Admin-Secret": "test-secret-lab"})

    # ── Auth ──
    def test_summary_unauthorized(self):
        r = self.client.get("/admin/api/v2/lab-tracking/summary")
        self.assertEqual(r.status_code, 401)

    # ── Summary ──
    def test_summary(self):
        r = self._get("/admin/api/v2/lab-tracking/summary")
        self.assertEqual(r.status_code, 200)
        d = json.loads(r.data)
        self.assertEqual(d["lab_reports"]["total"], 2)
        self.assertEqual(d["lab_reports"]["approved"], 1)
        self.assertEqual(d["lab_reports"]["pending_review"], 1)
        self.assertEqual(d["dialysis_documents"], 2)
        self.assertEqual(d["alerts"], 1)
        self.assertEqual(d["analytes"]["flagged"], 3)  # Cr abnormal_high, BUN abnormal_high, eGFR abnormal_low

    # ── Reports ──
    def test_reports_list(self):
        r = self._get("/admin/api/v2/lab-tracking/reports")
        d = json.loads(r.data)
        self.assertEqual(d["total"], 2)
        self.assertTrue(len(d["items"]) == 2)
        # Check IDs are truncated
        for item in d["items"]:
            self.assertTrue(len(item["source_document_id"]) <= 8)

    def test_reports_filter_status(self):
        r = self._get("/admin/api/v2/lab-tracking/reports?status=approved")
        d = json.loads(r.data)
        self.assertEqual(d["total"], 1)

    # ── Report Detail ──
    def test_report_detail(self):
        r = self._get("/admin/api/v2/lab-tracking/report/lab-001")
        self.assertEqual(r.status_code, 200)
        d = json.loads(r.data)
        self.assertEqual(len(d["results"]), 2)
        self.assertEqual(d["status"], "approved")

    def test_report_not_found(self):
        r = self._get("/admin/api/v2/lab-tracking/report/zzzzz")
        self.assertEqual(r.status_code, 404)

    # ── Trends ──
    def test_trends(self):
        r = self._get("/admin/api/v2/lab-tracking/trends")
        d = json.loads(r.data)
        self.assertIn("Creatinine", d["analytes"])
        # 2 creatinine data points
        self.assertEqual(len(d["trends"]["Creatinine"]), 2)

    def test_trends_filter_analyte(self):
        r = self._get("/admin/api/v2/lab-tracking/trends?analyte=eGFR")
        d = json.loads(r.data)
        self.assertIn("eGFR", d["analytes"])
        self.assertNotIn("Creatinine", d["analytes"])

    # ── Dialysis ──
    def test_dialysis(self):
        r = self._get("/admin/api/v2/lab-tracking/dialysis")
        d = json.loads(r.data)
        self.assertEqual(d["total_records"], 2)
        self.assertIn("2026-10", d["month_counts"])
        self.assertIn("2026-09", d["month_counts"])

    # ── Alerts ──
    def test_alerts(self):
        r = self._get("/admin/api/v2/lab-tracking/alerts")
        d = json.loads(r.data)
        self.assertEqual(d["total"], 1)
        self.assertEqual(d["alerts"][0]["analyte"], "Potassium")
        # Check document_id truncated
        self.assertTrue(len(d["alerts"][0]["document_id"]) <= 8)

    # ── UI Page ──
    def test_lab_dashboard_page_auth(self):
        r = self.client.get("/admin/v2/lab-dashboard")
        self.assertEqual(r.status_code, 401)

    def test_lab_dashboard_page_ok(self):
        r = self._get("/admin/v2/lab-dashboard")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Lab & Dialysis", r.data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
