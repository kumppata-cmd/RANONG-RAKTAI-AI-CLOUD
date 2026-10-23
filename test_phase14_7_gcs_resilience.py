#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 14.7 — GCS Write Resilience Regression Tests

ทดสอบ: เมื่อ GCS write ล้มเหลวใน on-demand re-extraction
ผู้ใช้ต้องยังเห็นผลแล็บ (lab_data ถูก set ก่อน storage.put)

Root Cause ที่แก้:
  lab_data = _re_json.encode("utf-8") ถูกวางหลัง storage.put()
  เมื่อ storage.put() raise RuntimeError → lab_data ไม่ถูก set → user เห็น "ไม่มีผลแล็บ"

Fix:
  ย้าย lab_data assignment ก่อน storage.put()
  ทำ GCS write เป็น best-effort cache

ข้อมูลทดสอบ: ใช้เฉพาะข้อมูลสังเคราะห์
ห้ามใช้ข้อมูลผู้ป่วยจริง
"""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch, call
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lab_extractor import extract_lab_results_from_text
from lab_models import LabReport, ExtractionMethod, ReviewStatus

# ── Synthetic test data ──
TEST_CKD_TEXT = """KIDNEY FUNCTION PANEL
Laboratory Report

Patient ID: [REDACTED]
Specimen Date: 2026-01-15
Specimen Type: Blood

Creatinine
5.2
mg/dL
0.6-1.2

BUN
45
mg/dL
7-20

eGFR
10
mL/min/1.73m2
>60

Potassium
4.5
mmol/L
3.5-5.0

Hemoglobin
10.5
g/dL
12-16
"""

SYNTH_DOC_ID = "test-phase147-synth-00000000"
SYNTH_OWNER = "sha256_synth_phase147_00000000"
LAB_RESULTS_PREFIX = "_lab_results/"


def _simulate_on_demand_reextraction(doc, storage_get_fn, storage_put_fn):
    """
    จำลอง on-demand re-extraction logic จาก server.py (line 2079-2185)
    ตรงตามโค้ด Phase 14.7 fix + conditional approval updates
    Returns: (lab_data, persistence_ok)
      - lab_data: bytes or None
      - persistence_ok: True if GCS write succeeded, False if failed, None if no extraction
    """
    full_doc_id = doc.get("document_id", "unknown")
    lab_path = f"{LAB_RESULTS_PREFIX}{full_doc_id}.json"

    lab_data = storage_get_fn(lab_path)
    _persistence_ok = None  # None = no extraction attempted

    if lab_data is None:
        _reextracted = False
        _doc_cat = doc.get("category", "")
        _doc_text = doc.get("extracted_text", "")
        _doc_text_len = len(_doc_text.strip()) if _doc_text else 0

        if _doc_cat == "lab" and _doc_text and _doc_text_len >= 50:
            try:
                _re_method = ExtractionMethod.REGEX_PDF_TEXT.value
                _re_report = extract_lab_results_from_text(
                    _doc_text, full_doc_id, _re_method
                )
                if _re_report.results:
                    _re_json = _re_report.to_json()
                    # ── Phase 14.7 FIX: set lab_data BEFORE GCS write ──
                    lab_data = _re_json.encode("utf-8")
                    _reextracted = True
                    _persistence_ok = False
                    # GCS write is best-effort
                    try:
                        storage_put_fn(
                            lab_path,
                            lab_data,
                            content_type="application/json",
                            create_only=True,
                        )
                        _persistence_ok = True
                    except Exception:
                        pass  # GCS write failure — lab_data already set
            except Exception:
                pass

    return lab_data, _persistence_ok


def _simulate_pre_fix_logic(doc, storage_get_fn, storage_put_fn):
    """
    จำลอง on-demand re-extraction logic ก่อนแก้ไข (Phase 14.6 bug)
    lab_data ถูก set หลัง storage.put() → GCS failure = lab_data is None
    """
    full_doc_id = doc.get("document_id", "unknown")
    lab_path = f"{LAB_RESULTS_PREFIX}{full_doc_id}.json"

    lab_data = storage_get_fn(lab_path)

    if lab_data is None:
        _doc_cat = doc.get("category", "")
        _doc_text = doc.get("extracted_text", "")
        _doc_text_len = len(_doc_text.strip()) if _doc_text else 0

        if _doc_cat == "lab" and _doc_text and _doc_text_len >= 50:
            try:
                _re_method = ExtractionMethod.REGEX_PDF_TEXT.value
                _re_report = extract_lab_results_from_text(
                    _doc_text, full_doc_id, _re_method
                )
                if _re_report.results:
                    _re_json = _re_report.to_json()
                    # BUG: storage.put() BEFORE lab_data assignment
                    storage_put_fn(
                        lab_path,
                        _re_json.encode("utf-8"),
                        content_type="application/json",
                        create_only=True,
                    )
                    # This line NEVER reached when put() raises
                    lab_data = _re_json.encode("utf-8")
            except Exception:
                pass

    return lab_data


class TestPhase147GCSWriteResilience(unittest.TestCase):
    """
    Phase 14.7 Core Fix: lab_data ต้องถูก set ก่อน GCS write
    เพื่อให้ GCS failure ไม่บล็อกการแสดงผลแล็บ
    """

    def _make_lab_doc(self):
        return {
            "document_id": SYNTH_DOC_ID,
            "category": "lab",
            "extracted_text": TEST_CKD_TEXT,
            "owner_hash": SYNTH_OWNER,
        }

    def test_fix_gcs_write_failure_still_returns_lab_data(self):
        """
        CRITICAL REGRESSION TEST:
        GCS write ล้มเหลว (RuntimeError) → lab_data ต้องยังมีค่า
        นี่คือ root cause ของ Phase 14.7
        """
        doc = self._make_lab_doc()

        def mock_get(path):
            return None  # No cached lab JSON

        def mock_put_raises(path, data, content_type=None, create_only=False):
            raise RuntimeError("GCS put ล้มเหลว — ไม่สามารถบันทึก metadata ได้")

        result, persistence_ok = _simulate_on_demand_reextraction(doc, mock_get, mock_put_raises)

        # MUST NOT be None — this was the bug
        self.assertIsNotNone(result, (
            "lab_data is None after GCS write failure! "
            "Phase 14.7 fix regression: lab_data must be set BEFORE storage.put()"
        ))

        # persistence_ok must be False (GCS failed)
        self.assertFalse(persistence_ok,
            "persistence_ok should be False when GCS write fails")

        # Verify it's valid lab data
        parsed = json.loads(result)
        self.assertIn("results", parsed)
        self.assertGreater(len(parsed["results"]), 0)

    def test_pre_fix_logic_fails_on_gcs_error(self):
        """
        ยืนยันว่า logic ก่อนแก้ไขจะ fail เมื่อ GCS write ล้มเหลว
        (ทดสอบว่า regression test จับ bug ได้จริง)
        """
        doc = self._make_lab_doc()

        def mock_get(path):
            return None

        def mock_put_raises(path, data, content_type=None, create_only=False):
            raise RuntimeError("GCS put ล้มเหลว")

        result = _simulate_pre_fix_logic(doc, mock_get, mock_put_raises)

        # Pre-fix logic returns None when GCS fails — this WAS the bug
        self.assertIsNone(result, (
            "Pre-fix logic should return None when GCS write fails "
            "(if this passes, the test isn't testing the right thing)"
        ))

    def test_gcs_write_success_also_works(self):
        """
        GCS write สำเร็จ → lab_data ต้องมีค่าเช่นกัน
        """
        doc = self._make_lab_doc()
        stored = {}

        def mock_get(path):
            return None

        def mock_put_ok(path, data, content_type=None, create_only=False):
            stored[path] = data
            return f"gs://test-bucket/{path}"

        result, persistence_ok = _simulate_on_demand_reextraction(doc, mock_get, mock_put_ok)

        self.assertIsNotNone(result)
        # persistence_ok must be True (GCS succeeded)
        self.assertTrue(persistence_ok,
            "persistence_ok should be True when GCS write succeeds")
        # GCS cache should also be written
        lab_path = f"{LAB_RESULTS_PREFIX}{SYNTH_DOC_ID}.json"
        self.assertIn(lab_path, stored)

    def test_cached_lab_json_skips_reextraction(self):
        """
        เมื่อ GCS มี cached lab JSON อยู่แล้ว → ไม่ต้อง re-extract
        """
        doc = self._make_lab_doc()
        cached_data = b'{"results": [{"analyte": "Creatinine"}], "status": "pending_review"}'

        def mock_get(path):
            return cached_data

        put_called = []
        def mock_put(path, data, content_type=None, create_only=False):
            put_called.append(path)

        result, persistence_ok = _simulate_on_demand_reextraction(doc, mock_get, mock_put)

        self.assertEqual(result, cached_data)
        # persistence_ok is None when no extraction was attempted
        self.assertIsNone(persistence_ok,
            "persistence_ok should be None when cache hit (no extraction)")
        self.assertEqual(len(put_called), 0, "put() should not be called when cache exists")


class TestPhase147LabDataContent(unittest.TestCase):
    """
    ทดสอบว่า lab_data ที่ได้จาก on-demand re-extraction
    มีค่า Creatinine, Potassium, Hemoglobin ครบถ้วน
    """

    def test_result_contains_creatinine(self):
        """ผลต้องมี Creatinine = 5.2 mg/dL."""
        doc = {
            "document_id": SYNTH_DOC_ID,
            "category": "lab",
            "extracted_text": TEST_CKD_TEXT,
        }

        def mock_get(p): return None
        def mock_put(p, d, **kw): raise RuntimeError("GCS fail")

        result, persistence_ok = _simulate_on_demand_reextraction(doc, mock_get, mock_put)
        self.assertFalse(persistence_ok)
        parsed = json.loads(result)
        creatinine = [r for r in parsed["results"] if r["analyte"] == "Creatinine"]
        self.assertEqual(len(creatinine), 1)
        self.assertAlmostEqual(creatinine[0]["value"], 5.2)

    def test_result_contains_potassium(self):
        """ผลต้องมี Potassium = 4.5 mmol/L."""
        doc = {
            "document_id": SYNTH_DOC_ID,
            "category": "lab",
            "extracted_text": TEST_CKD_TEXT,
        }

        def mock_get(p): return None
        def mock_put(p, d, **kw): raise RuntimeError("GCS fail")

        result, persistence_ok = _simulate_on_demand_reextraction(doc, mock_get, mock_put)
        self.assertFalse(persistence_ok)
        parsed = json.loads(result)
        potassium = [r for r in parsed["results"] if r["analyte"] == "Potassium"]
        self.assertEqual(len(potassium), 1)
        self.assertAlmostEqual(potassium[0]["value"], 4.5)

    def test_result_contains_hemoglobin(self):
        """ผลต้องมี Hemoglobin = 10.5 g/dL."""
        doc = {
            "document_id": SYNTH_DOC_ID,
            "category": "lab",
            "extracted_text": TEST_CKD_TEXT,
        }

        def mock_get(p): return None
        def mock_put(p, d, **kw): raise RuntimeError("GCS fail")

        result, persistence_ok = _simulate_on_demand_reextraction(doc, mock_get, mock_put)
        self.assertFalse(persistence_ok)
        parsed = json.loads(result)
        hemoglobin = [r for r in parsed["results"] if r["analyte"] == "Hemoglobin"]
        self.assertEqual(len(hemoglobin), 1)
        self.assertAlmostEqual(hemoglobin[0]["value"], 10.5)


class TestPhase147ExceptionHandling(unittest.TestCase):
    """
    ทดสอบว่า exception ต่าง ๆ ใน on-demand re-extraction
    ถูกจัดการอย่างถูกต้องตาม Phase 14.7 fix
    """

    def test_extraction_exception_returns_none(self):
        """
        เมื่อ extract_lab_results_from_text ล้มเหลว → lab_data = None
        (ไม่ใช่ crash)
        """
        doc = {
            "document_id": SYNTH_DOC_ID,
            "category": "lab",
            "extracted_text": "short",  # Too short → guard blocks
        }

        def mock_get(p): return None
        def mock_put(p, d, **kw): pass

        result, persistence_ok = _simulate_on_demand_reextraction(doc, mock_get, mock_put)
        self.assertIsNone(result)
        # No extraction attempted → persistence_ok is None
        self.assertIsNone(persistence_ok,
            "persistence_ok should be None when text too short for extraction")

    def test_multiple_gcs_error_types_handled(self):
        """
        ทดสอบ GCS error หลายประเภท — ทุกประเภทต้องไม่บล็อก lab_data
        """
        doc = {
            "document_id": SYNTH_DOC_ID,
            "category": "lab",
            "extracted_text": TEST_CKD_TEXT,
        }

        error_types = [
            RuntimeError("GCS put ล้มเหลว"),
            ConnectionError("Network unreachable"),
            TimeoutError("GCS timeout"),
            OSError("Permission denied"),
        ]

        for error in error_types:
            def mock_get(p): return None
            def make_put(err):
                def mock_put(p, d, **kw): raise err
                return mock_put

            result, persistence_ok = _simulate_on_demand_reextraction(
                doc, mock_get, make_put(error)
            )
            self.assertIsNotNone(result, (
                f"lab_data is None after {type(error).__name__}! "
                f"All GCS errors must be handled as best-effort."
            ))
            self.assertFalse(persistence_ok, (
                f"persistence_ok should be False after {type(error).__name__}"
            ))


if __name__ == "__main__":
    unittest.main(verbosity=2)
