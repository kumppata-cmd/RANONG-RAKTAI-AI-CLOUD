#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Security Tests — Kidney Clinical Intelligence Platform 2.0

ทดสอบความปลอดภัย:
  1. Authorization — ผู้ใช้ที่ไม่ได้รับอนุญาตต้องถูกปฏิเสธ
  2. Data Isolation — Owner-based document isolation
  3. Input Sanitization — ป้องกัน injection, path traversal
  4. Credential Protection — ไม่เปิดเผย secrets ใน logs/responses
  5. LINE Signature Verification — ป้องกัน replay attack
  6. PDPA Data Minimization — ไม่เก็บข้อมูลส่วนบุคคล
  7. External AI Protection — Claude API ต้อง disabled
  8. GCS Security — ไม่เปิด public access

ใช้ข้อมูล DEMO เท่านั้น — ไม่ใช้ข้อมูลผู้ป่วยจริง
"""

import os
import sys
import re
import hmac
import hashlib
import base64
import json
import unittest
from unittest.mock import patch, MagicMock

# ── Setup test environment ──
os.environ.setdefault("LINE_CHANNEL_SECRET", "test_secret_for_unit_test")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "test_token_for_unit_test")
os.environ.setdefault("ANTHROPIC_API_KEY", "")
os.environ.setdefault("ENABLE_AI_CLASSIFY", "false")
os.environ.setdefault("STORAGE_BACKEND", "local")

import server


def _make_signature(body: bytes, secret: str = "test_secret_for_unit_test") -> str:
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("utf-8")


# ══════════════════════════════════════════════════════════════
# 1. Authorization Tests
# ══════════════════════════════════════════════════════════════

class TestAuthorization(unittest.TestCase):
    """ทดสอบระบบ Authorization."""

    def test_unauthorized_user_blocked_from_search(self):
        """ผู้ใช้ที่ไม่อยู่ใน AUTHORIZED_USERS ต้องถูกปฏิเสธ."""
        result = server.handle_search_command("ค้นหา test", "unauthorized_hash")
        self.assertIsNotNone(result)
        self.assertIn("ไม่มีสิทธิ์", result)

    def test_unauthorized_user_blocked_from_lab(self):
        """ผู้ใช้ไม่มีสิทธิ์ดูผลแล็บ."""
        result = server.handle_search_command("ผลแล็บล่าสุด", "unauthorized_hash")
        self.assertIn("ไม่มีสิทธิ์", result)

    def test_unauthorized_user_blocked_from_approve(self):
        """ผู้ใช้ไม่มีสิทธิ์ยืนยันผลแล็บ."""
        result = server.handle_search_command("ยืนยันผลแล็บ abc123", "unauthorized_hash")
        self.assertIn("ไม่มีสิทธิ์", result)

    def test_unauthorized_user_blocked_from_kpi(self):
        """ผู้ใช้ไม่มีสิทธิ์ดู KPI."""
        result = server.handle_search_command("KPI", "unauthorized_hash")
        self.assertIn("ไม่มีสิทธิ์", result)

    def test_unauthorized_user_blocked_from_stats(self):
        """ผู้ใช้ไม่มีสิทธิ์ดูสถิติ."""
        result = server.handle_search_command("สถิติ", "unauthorized_hash")
        self.assertIn("ไม่มีสิทธิ์", result)

    def test_help_allowed_without_auth(self):
        """คำสั่ง help ไม่ต้องมีสิทธิ์."""
        result = server.handle_search_command("ช่วยเหลือ", "unauthorized_hash")
        self.assertIsNotNone(result)
        self.assertNotIn("ไม่มีสิทธิ์", result)

    @patch("server.is_authorized_user", return_value=True)
    def test_authorized_user_can_search(self, mock_auth):
        """ผู้ใช้ที่ได้รับอนุญาตสามารถค้นหาได้."""
        with patch.object(server.doc_index, "search", return_value=[]):
            result = server.handle_search_command("ค้นหา test", "authed_hash")
        self.assertNotIn("ไม่มีสิทธิ์", result)


# ══════════════════════════════════════════════════════════════
# 2. Data Isolation Tests
# ══════════════════════════════════════════════════════════════

class TestDataIsolation(unittest.TestCase):
    """ทดสอบ Owner-based document isolation."""

    def test_owner_check_blocks_other_user(self):
        """ผู้ใช้คนอื่นดูเอกสารไม่ได้ → ส่งคืน authorized=False."""
        doc = {
            "doc_id": "isolation-test-001",
            "owner_hash": "owner_abc",
            "original_filename": "test.pdf",
            "category": "lab_report",
        }
        with patch.object(server.doc_index, "get_with_owner_check") as mock_get:
            mock_get.return_value = (doc, False)  # found but not authorized
            with patch("server.is_authorized_user", return_value=True):
                result = server.handle_search_command(
                    "ขอไฟล์ isolation-test-001", "other_user_hash")
        self.assertIn("ไม่มีสิทธิ์", result)

    def test_user_id_hashing_one_way(self):
        """hash_user_id ต้องเป็น one-way — ไม่สามารถ reverse ได้."""
        h1 = server.hash_user_id("U1234567890")
        h2 = server.hash_user_id("U1234567890")
        h3 = server.hash_user_id("U0987654321")

        # Same input → same output
        self.assertEqual(h1, h2)
        # Different input → different output
        self.assertNotEqual(h1, h3)
        # ไม่ใช่ plain text
        self.assertNotIn("U1234567890", h1)

    def test_hash_uses_hmac_not_plain_hash(self):
        """hash_user_id ต้องใช้ HMAC (ไม่ใช่ sha256 ธรรมดา)."""
        h = server.hash_user_id("test_user_id")
        plain_sha = hashlib.sha256("test_user_id".encode()).hexdigest()
        # ต้องไม่ตรงกับ plain SHA-256
        self.assertNotEqual(h, plain_sha)


# ══════════════════════════════════════════════════════════════
# 3. Input Sanitization Tests
# ══════════════════════════════════════════════════════════════

class TestInputSanitization(unittest.TestCase):
    """ทดสอบ input sanitization."""

    def test_path_traversal_in_doc_id(self):
        """doc_id ที่มี path traversal ต้องไม่ผ่าน."""
        malicious_ids = [
            "../../../etc/passwd",
            "..%2F..%2Fetc%2Fpasswd",
            "doc_id/../../secret",
            "doc_id\n\r",
        ]
        for doc_id in malicious_ids:
            with self.subTest(doc_id=doc_id):
                with patch("server.is_authorized_user", return_value=True):
                    result = server.handle_search_command(
                        f"ขอไฟล์ {doc_id}", "testhash")
                # ต้องไม่ crash และไม่คืน file content
                self.assertIsInstance(result, str)

    def test_xss_in_search_query(self):
        """XSS payload ในคำค้นหาต้องไม่ถูก execute."""
        xss_payloads = [
            '<script>alert("xss")</script>',
            '"><img src=x onerror=alert(1)>',
            "'; DROP TABLE docs; --",
        ]
        for payload in xss_payloads:
            with self.subTest(payload=payload[:30]):
                with patch("server.is_authorized_user", return_value=True):
                    with patch.object(server.doc_index, "search", return_value=[]):
                        result = server.handle_search_command(
                            f"ค้นหา {payload}", "testhash")
                self.assertIsInstance(result, str)
                # ต้องไม่มี raw script tag ใน output
                self.assertNotIn("<script>", result.lower())

    def test_oversized_input_handled(self):
        """Input ที่ยาวมากต้องไม่ crash."""
        huge_input = "ค้นหา " + "A" * 10000
        with patch("server.is_authorized_user", return_value=True):
            with patch.object(server.doc_index, "search", return_value=[]):
                result = server.handle_search_command(huge_input, "testhash")
        self.assertIsInstance(result, str)

    def test_null_bytes_in_input(self):
        """Null bytes ในข้อมูลต้องไม่ crash."""
        with patch("server.is_authorized_user", return_value=True):
            with patch.object(server.doc_index, "search", return_value=[]):
                result = server.handle_search_command("ค้นหา \x00test", "testhash")
        self.assertIsInstance(result, str)

    def test_unicode_edge_cases(self):
        """Unicode ต่างๆ ต้องจัดการได้."""
        unicode_inputs = [
            "ค้นหา 日本語",
            "ค้นหา émojis 🔬🩸",
            "ค้นหา العربية",
            "ค้นหา ​​",  # zero-width space
        ]
        for text in unicode_inputs:
            with self.subTest(text=text[:30]):
                with patch("server.is_authorized_user", return_value=True):
                    with patch.object(server.doc_index, "search", return_value=[]):
                        result = server.handle_search_command(text, "testhash")
                self.assertIsInstance(result, str)


# ══════════════════════════════════════════════════════════════
# 4. Credential Protection Tests
# ══════════════════════════════════════════════════════════════

class TestCredentialProtection(unittest.TestCase):
    """ทดสอบว่า credentials ไม่ถูกเปิดเผย."""

    def test_no_secrets_in_health_endpoint(self):
        """/health ต้องไม่แสดง secrets."""
        with server.app.test_client() as client:
            resp = client.get("/health")
            data = resp.get_json()
            data_str = json.dumps(data)

            self.assertNotIn("test_secret", data_str)
            self.assertNotIn("test_token", data_str)
            self.assertNotIn("ANTHROPIC", data_str.upper())

    def test_error_response_no_secrets(self):
        """Error response ต้องไม่มี secrets."""
        with server.app.test_client() as client:
            resp = client.post("/webhook", data=b"invalid",
                               content_type="application/json")
            data_str = resp.data.decode()
            self.assertNotIn("test_secret", data_str)
            self.assertNotIn("test_token", data_str)

    def test_download_token_opaque(self):
        """Download token ต้องเป็น opaque — ไม่มี path info."""
        token = server.generate_download_token("bucket/path/to/file.pdf")
        self.assertNotIn("bucket", token)
        self.assertNotIn("path/to", token)
        self.assertNotIn("file.pdf", token)


# ══════════════════════════════════════════════════════════════
# 5. LINE Signature Verification Tests
# ══════════════════════════════════════════════════════════════

class TestLineSignature(unittest.TestCase):
    """ทดสอบ LINE Webhook Signature Verification."""

    def test_missing_signature_rejected(self):
        """Request ที่ไม่มี signature ต้อง 401."""
        with server.app.test_client() as client:
            resp = client.post("/webhook",
                               data=json.dumps({"events": []}).encode(),
                               content_type="application/json")
            self.assertIn(resp.status_code, [400, 401, 403])

    def test_invalid_signature_rejected(self):
        """Signature ผิด ต้อง 403."""
        body = json.dumps({"events": []}).encode()
        with server.app.test_client() as client:
            resp = client.post("/webhook", data=body,
                               content_type="application/json",
                               headers={"X-Line-Signature": "invalid_sig"})
            self.assertIn(resp.status_code, [400, 403])

    def test_valid_signature_accepted(self):
        """Signature ถูกต้อง ต้อง 200."""
        body = json.dumps({"events": []}).encode()
        sig = _make_signature(body)
        with server.app.test_client() as client:
            resp = client.post("/webhook", data=body,
                               content_type="application/json",
                               headers={"X-Line-Signature": sig})
            self.assertEqual(resp.status_code, 200)


# ══════════════════════════════════════════════════════════════
# 6. PDPA Data Minimization Tests
# ══════════════════════════════════════════════════════════════

class TestPDPACompliance(unittest.TestCase):
    """ทดสอบ PDPA Data Minimization."""

    def test_alert_no_patient_data(self):
        """Alert messages ต้องไม่มีข้อมูลผู้ป่วย."""
        from lab_models import LabReport, LabResult
        from lab_alert import check_lab_alerts, format_user_alert_th

        results = [
            LabResult(
                analyte="Potassium", value=7.5, unit="mmol/L",
                reference_range="3.5-5.0", confidence=0.95,
                extraction_method="test", flags=["critical_high"],
            ),
        ]
        report = LabReport(
            source_document_id="pdpa-test-001234567890",
            specimen_date="2025-03-15",
            extraction_method="test",
            results=results,
        )
        summary = check_lab_alerts(report)
        text = format_user_alert_th(summary)

        # ต้องไม่มีข้อมูลระบุตัวผู้ป่วย
        self.assertNotIn("HN", text)
        self.assertNotIn("ชื่อ", text)
        self.assertNotIn("สกุล", text)
        self.assertNotIn("เลขบัตร", text)
        self.assertNotIn("ที่อยู่", text)
        # doc_id ต้อง truncate เหลือ 8 ตัว
        self.assertNotIn("pdpa-test-001234567890", text)
        self.assertIn("pdpa-tes", text)  # doc_id[:8]

    def test_hash_user_id_not_reversible(self):
        """User ID hash ต้องไม่สามารถ reverse ได้."""
        original = "Uabcdef1234567890"
        hashed = server.hash_user_id(original)
        # Hash ต้องยาวกว่าและต่างจาก original
        self.assertNotEqual(original, hashed)
        self.assertNotIn(original, hashed)

    def test_lab_result_no_patient_identifiers(self):
        """LabResult model ไม่มี field สำหรับข้อมูลผู้ป่วย."""
        from lab_models import LabResult
        import dataclasses
        field_names = {f.name for f in dataclasses.fields(LabResult)}
        # ต้องไม่มี fields ที่เก็บข้อมูลผู้ป่วย
        patient_fields = {"patient_id", "patient_name", "hn", "hospital_number",
                          "id_card", "address", "phone", "email"}
        intersection = field_names & patient_fields
        self.assertEqual(len(intersection), 0,
                         f"LabResult has patient fields: {intersection}")


# ══════════════════════════════════════════════════════════════
# 7. External AI Protection Tests
# ══════════════════════════════════════════════════════════════

class TestExternalAIProtection(unittest.TestCase):
    """ทดสอบว่า External AI ถูก disabled."""

    def test_external_ai_disabled(self):
        """EXTERNAL_AI_CLASSIFICATION_ALLOWED ต้องเป็น False."""
        self.assertFalse(server.EXTERNAL_AI_CLASSIFICATION_ALLOWED)

    def test_ai_classify_disabled(self):
        """AI classification ต้องไม่ถูกเรียกใช้."""
        # ENABLE_AI_CLASSIFY set to "false" ใน env
        self.assertFalse(server.ENABLE_AI_CLASSIFY)

    def test_source_code_hardcoded_false(self):
        """ตรวจสอบว่า source code hardcode EXTERNAL_AI = False."""
        with open("server.py", "r") as f:
            source = f.read()
        # ต้องมี hardcoded False
        self.assertIn("EXTERNAL_AI_CLASSIFICATION_ALLOWED = False", source)


# ══════════════════════════════════════════════════════════════
# 8. Enrollment Token Security Tests
# ══════════════════════════════════════════════════════════════

class TestEnrollmentSecurity(unittest.TestCase):
    """ทดสอบ Enrollment Token Security."""

    def setUp(self):
        """Reset enrollment store to ensure test isolation."""
        import json
        store_path = server.enrollment_store._store_path
        if os.path.exists(store_path):
            with open(store_path, 'w') as f:
                json.dump({"tokens": {}, "failed_attempts": {}}, f)

    def test_invalid_token_silent_reject(self):
        """Token ไม่ถูกต้อง → ไม่ตอบกลับ (ป้องกัน brute force)."""
        # ใช้ hash เฉพาะต่อ test เพื่อหลีกเลี่ยง rate limit จาก test อื่น
        result = server.handle_search_command("ยืนยัน INVALID_TOKEN", "hash_invalid_test_1")
        self.assertIsNone(result)

    def test_empty_token_no_response(self):
        """Token ว่าง → ไม่ตอบกลับ."""
        result = server.handle_search_command("ยืนยัน ", "hash_empty_test_2")
        self.assertIsNone(result)

    def test_enrollment_does_not_reveal_token_format(self):
        """Response ไม่เปิดเผยรูปแบบ token."""
        result = server.handle_search_command("ยืนยัน WRONG", "hash_format_test_3")
        # ไม่ควรบอกว่า token ต้องเป็นรูปแบบอะไร
        self.assertIsNone(result)


# ══════════════════════════════════════════════════════════════
# 9. Rate Limiting Tests
# ══════════════════════════════════════════════════════════════

class TestRateLimiting(unittest.TestCase):
    """ทดสอบ Rate Limiting."""

    def test_enrollment_rate_limit_exists(self):
        """enrollment_store ต้องมี rate limiting."""
        self.assertTrue(hasattr(server, 'enrollment_store'))
        # ต้องมี method validate_and_consume
        self.assertTrue(hasattr(server.enrollment_store, 'validate_and_consume'))


# ══════════════════════════════════════════════════════════════
# 10. Download Token Security
# ══════════════════════════════════════════════════════════════

class TestDownloadTokenSecurity(unittest.TestCase):
    """ทดสอบ Download Token Security."""

    def test_token_expires(self):
        """Token ต้องมีเวลาหมดอายุ."""
        import time
        token = server.generate_download_token("test/path.pdf", expiry_seconds=1)
        # Token ต้องใช้ได้ทันที
        path = server.validate_download_token(token)
        self.assertEqual(path, "test/path.pdf")
        # รอให้หมดอายุ
        time.sleep(2)
        path = server.validate_download_token(token)
        self.assertIsNone(path)

    def test_invalid_token_rejected(self):
        """Token ปลอมต้องถูกปฏิเสธ."""
        path = server.validate_download_token("fake_token_here")
        self.assertIsNone(path)

    def test_tampered_token_rejected(self):
        """Token ที่ถูกแก้ไขต้องถูกปฏิเสธ."""
        token = server.generate_download_token("test/path.pdf")
        # แก้ไข 1 ตัวอักษร
        tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
        path = server.validate_download_token(tampered)
        self.assertIsNone(path)


if __name__ == "__main__":
    unittest.main()
