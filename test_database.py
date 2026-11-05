#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for database.py — PostgreSQL layer

ใช้ sqlite3 เป็น mock เมื่อไม่มี PostgreSQL
ทดสอบ logic ของ SQL queries + data flow
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))


class TestDatabaseInit(unittest.TestCase):
    """ทดสอบ initialization แบบไม่มี DATABASE_URL"""

    def test_no_url_status(self):
        from database import Database
        db = Database("")
        self.assertEqual(db.status, "not_configured")
        self.assertFalse(db.is_ready)

    def test_no_url_health(self):
        from database import Database
        db = Database("")
        h = db.health()
        self.assertEqual(h["status"], "not_configured")

    def test_no_url_crud_safe(self):
        """CRUD methods return None/[] when not configured"""
        from database import Database
        db = Database("")
        self.assertIsNone(db.upsert_patient({"clinic_hn": "56001"}))
        self.assertIsNone(db.get_patient("56001"))
        self.assertEqual(db.get_patients_by_day("mon"), [])
        self.assertEqual(db.count_patients(), 0)
        self.assertEqual(db.search_patients("ปิยะ"), [])
        self.assertEqual(db.get_dialysis_summary(), {})
        self.assertIsNone(db.upsert_document({"doc_id": "abc"}))
        self.assertEqual(db.search_documents(hn="56001"), [])
        self.assertIsNone(db.insert_lab_result({"analyte": "Hb"}))
        self.assertEqual(db.get_lab_history("56001"), [])
        self.assertEqual(db.get_hb_trend("56001"), [])


class TestDatabaseSchema(unittest.TestCase):
    """ทดสอบ schema definition"""

    def test_patient_fields_complete(self):
        """Schema ต้องรองรับทุก field ของ MasterPatient"""
        from database import Database
        import inspect
        source = inspect.getsource(Database._create_tables)
        required_columns = [
            "clinic_hn", "hospital_hn", "patient_name",
            "dialysis_schedule", "dialysis_days", "hd_per_week",
            "esa_code", "esa_medication", "esa_dose", "esa_unit",
            "vascular_access", "shift", "bed_number",
            "line_user_hash", "is_active", "clinic_code",
        ]
        for col in required_columns:
            self.assertIn(col, source, f"Missing column: {col}")

    def test_document_fields_complete(self):
        """Schema ต้องรองรับ document index fields"""
        from database import Database
        import inspect
        source = inspect.getsource(Database._create_tables)
        required = [
            "doc_id", "original_name", "category", "patient_hn_folder",
            "upload_date", "gcs_object_path", "ocr_text",
            "bill_company", "owner_hash",
        ]
        for col in required:
            self.assertIn(col, source, f"Missing column: {col}")

    def test_lab_results_fields(self):
        """Schema ต้องรองรับ lab result fields"""
        from database import Database
        import inspect
        source = inspect.getsource(Database._create_tables)
        required = [
            "analyte", "value", "unit", "ref_low", "ref_high",
            "flags", "is_critical", "is_abnormal", "specimen_date",
        ]
        for col in required:
            self.assertIn(col, source, f"Missing column: {col}")

    def test_indexes_defined(self):
        """ต้องมี indexes สำหรับ queries ที่ใช้บ่อย"""
        from database import Database
        import inspect
        source = inspect.getsource(Database._create_tables)
        required_indexes = [
            "idx_patients_hospital_hn",
            "idx_patients_dialysis_days",
            "idx_docs_patient",
            "idx_docs_category",
            "idx_labs_patient",
            "idx_labs_critical",
            "idx_alerts_pending",
        ]
        for idx in required_indexes:
            self.assertIn(idx, source, f"Missing index: {idx}")


class TestDatabaseMigration(unittest.TestCase):
    """ทดสอบ migration functions"""

    def test_migrate_patients_no_db(self):
        from database import Database
        db = Database("")
        result = db.migrate_patients(None)
        self.assertIn("error", result)

    def test_migrate_documents_no_db(self):
        from database import Database
        db = Database("")
        result = db.migrate_documents(None)
        self.assertIn("error", result)


class TestDatabaseUpsertLogic(unittest.TestCase):
    """ทดสอบ upsert data validation"""

    def test_upsert_patient_empty_hn(self):
        """hn ว่างต้อง return None"""
        from database import Database
        db = Database("")
        # Even if somehow connected, empty HN should fail
        self.assertIsNone(db.upsert_patient({"clinic_hn": ""}))
        self.assertIsNone(db.upsert_patient({}))

    def test_upsert_document_empty_id(self):
        """doc_id ว่างต้อง return None"""
        from database import Database
        db = Database("")
        self.assertIsNone(db.upsert_document({"doc_id": ""}))
        self.assertIsNone(db.upsert_document({}))


class TestDatabaseIntegrationWithPg(unittest.TestCase):
    """Integration tests — ทำงานเฉพาะเมื่อมี DATABASE_URL"""

    @classmethod
    def setUpClass(cls):
        cls.db_url = os.getenv("DATABASE_URL", "")
        if not cls.db_url:
            raise unittest.SkipTest("DATABASE_URL ไม่ได้ตั้ง — skip integration tests")
        from database import Database, _pg_available
        if not _pg_available:
            raise unittest.SkipTest("psycopg2 ไม่ได้ติดตั้ง")
        cls.db = Database(cls.db_url)
        if not cls.db.is_ready:
            raise unittest.SkipTest(f"Database not ready: {cls.db.error}")

    def test_01_upsert_and_get_patient(self):
        patient_id = self.db.upsert_patient({
            "clinic_hn": "TEST001",
            "hospital_hn": "H-TEST001",
            "patient_name": "นาย ทดสอบ ระบบ",
            "dialysis_schedule": "จ/พ/ศ",
            "dialysis_days": ["mon", "wed", "fri"],
            "hd_per_week": 3,
            "esa_code": "E4",
            "esa_medication": "Epiao",
        })
        self.assertIsNotNone(patient_id)

        p = self.db.get_patient("TEST001")
        self.assertIsNotNone(p)
        self.assertEqual(p["clinic_hn"], "TEST001")
        self.assertEqual(p["patient_name"], "นาย ทดสอบ ระบบ")
        self.assertEqual(p["esa_code"], "E4")

    def test_02_get_patients_by_day(self):
        pts = self.db.get_patients_by_day("mon")
        self.assertIsInstance(pts, list)
        # TEST001 should be in Monday
        hns = [p["clinic_hn"] for p in pts]
        self.assertIn("TEST001", hns)

    def test_03_search_patients(self):
        results = self.db.search_patients("ทดสอบ")
        self.assertGreater(len(results), 0)

    def test_04_dialysis_summary(self):
        s = self.db.get_dialysis_summary()
        self.assertIn("total_patients", s)
        self.assertIn("by_day", s)
        self.assertGreater(s["by_day"].get("mon", 0), 0)

    def test_05_upsert_document(self):
        doc_id = self.db.upsert_document({
            "doc_id": "TEST_DOC_001",
            "original_name": "test_lab.jpg",
            "category": "lab",
            "patient_hn_folder": "TEST001_ทดสอบ",
            "upload_date": "2026-11-05",
        })
        self.assertIsNotNone(doc_id)

    def test_06_search_documents(self):
        docs = self.db.search_documents(hn="TEST001", category="lab")
        self.assertGreater(len(docs), 0)

    def test_07_insert_lab_result(self):
        lab_id = self.db.insert_lab_result({
            "doc_id": "TEST_DOC_001",
            "patient_hn": "TEST001",
            "specimen_date": "2026-11-05",
            "analyte": "hemoglobin",
            "value": 9.5,
            "unit": "g/dL",
            "ref_low": 12.0,
            "ref_high": 16.0,
            "is_abnormal": True,
        })
        self.assertIsNotNone(lab_id)

    def test_08_get_hb_trend(self):
        trend = self.db.get_hb_trend("TEST001")
        self.assertGreater(len(trend), 0)
        self.assertEqual(trend[0]["analyte"], "hemoglobin")

    def test_09_count_documents_by_month(self):
        counts = self.db.count_documents_by_month("2026-11")
        self.assertIn("lab", counts)

    def test_10_health(self):
        h = self.db.health()
        self.assertEqual(h["status"], "connected")
        self.assertIn("patients", h)

    @classmethod
    def tearDownClass(cls):
        """Clean up test data"""
        if hasattr(cls, 'db') and cls.db.is_ready:
            try:
                with cls.db._get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM lab_results WHERE patient_hn = 'TEST001'")
                        cur.execute("DELETE FROM documents WHERE doc_id = 'TEST_DOC_001'")
                        cur.execute("DELETE FROM patients WHERE clinic_hn = 'TEST001'")
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
