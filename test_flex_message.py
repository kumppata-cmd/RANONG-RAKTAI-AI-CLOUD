#!/usr/bin/env python3
"""Tests for Flex Message file download feature.

ทดสอบ:
- Download token generation/validation (stateless HMAC)
- Flex Message structure
- Download proxy endpoint
- URL length fallback logic
- Webhook handler dispatch (flex vs text)

ใช้ข้อมูลจำลองเท่านั้น — ไม่ใช้ข้อมูลผู้ป่วยจริง
"""

import json
import time
import unittest
from unittest.mock import patch, MagicMock

# ─── Setup test environment ──────────────────────────────────
import os
os.environ.setdefault("LINE_CHANNEL_SECRET", "test-secret-for-testing")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "test-token-for-testing")
os.environ.setdefault("USER_ID_SALT", "test-salt-for-unit-tests-only")
os.environ.setdefault("ADMIN_SECRET", "test-admin-secret")
os.environ.setdefault("STORAGE_BACKEND", "gcs")
os.environ.setdefault("STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("AUTHORIZED_USERS", "testhash123")

import server


# ─── Demo test document (DEMO TEST-CKD-001) ─────────────────
DEMO_DOC = {
    "document_id": "demo-test-ckd-001-abcdef",
    "original_filename": "TEST-CKD-001.pdf",
    "gcs_object_path": "04_LAB_RESULTS/doc_20261021_demo-test-ckd-001.pdf",
    "category": "lab",
    "upload_date": "2026-10-21",
    "mime_type": "application/pdf",
    "file_size": 12345,
    "owner_hash": "testhash123",
}


class TestDownloadToken(unittest.TestCase):
    """ทดสอบ stateless HMAC download token."""

    def test_generate_and_validate(self):
        """Token ที่สร้างต้อง validate สำเร็จ."""
        gcs_path = DEMO_DOC["gcs_object_path"]
        token = server.generate_download_token(gcs_path, expiry_seconds=900)

        self.assertIsInstance(token, str)
        self.assertIn(".", token)  # format: payload.signature

        result = server.validate_download_token(token)
        self.assertEqual(result, gcs_path)

    def test_expired_token(self):
        """Token ที่หมดอายุต้อง validate ไม่ผ่าน."""
        gcs_path = DEMO_DOC["gcs_object_path"]
        token = server.generate_download_token(gcs_path, expiry_seconds=1)

        # Simulate time passing
        with patch("server.time") as mock_time:
            mock_time.time.return_value = time.time() + 10
            result = server.validate_download_token(token)
        self.assertIsNone(result)

    def test_tampered_token(self):
        """Token ที่ถูกแก้ไขต้อง validate ไม่ผ่าน."""
        gcs_path = DEMO_DOC["gcs_object_path"]
        token = server.generate_download_token(gcs_path)

        # Tamper with signature
        parts = token.split(".")
        tampered = parts[0] + ".00000000000000000000000000000000"
        result = server.validate_download_token(tampered)
        self.assertIsNone(result)

    def test_invalid_format(self):
        """Token ที่ไม่มี dot ต้อง validate ไม่ผ่าน."""
        self.assertIsNone(server.validate_download_token("invalid-no-dot"))
        self.assertIsNone(server.validate_download_token(""))
        self.assertIsNone(server.validate_download_token("a.b.c"))  # extra dot in payload is OK if HMAC matches

    def test_different_paths_different_tokens(self):
        """Path ต่างกันต้องได้ token ต่างกัน."""
        t1 = server.generate_download_token("path/a.pdf")
        t2 = server.generate_download_token("path/b.pdf")
        self.assertNotEqual(t1, t2)


class TestFlexMessage(unittest.TestCase):
    """ทดสอบ Flex Message structure."""

    def test_build_file_flex_message(self):
        """Flex Message ต้องมี structure ที่ LINE API ยอมรับ."""
        url = "https://storage.googleapis.com/test-bucket/test.pdf?signed=yes"
        msg = server.build_file_flex_message(DEMO_DOC, url)

        self.assertEqual(msg["type"], "flex")
        self.assertIn("altText", msg)
        self.assertIn("ผลตรวจแล็บ", msg["altText"])
        self.assertIn("contents", msg)

        contents = msg["contents"]
        self.assertEqual(contents["type"], "bubble")
        self.assertIn("body", contents)
        self.assertIn("footer", contents)

        # Check button
        footer = contents["footer"]
        button = footer["contents"][0]
        self.assertEqual(button["type"], "button")
        self.assertEqual(button["action"]["type"], "uri")
        self.assertEqual(button["action"]["label"], "เปิด PDF")
        self.assertEqual(button["action"]["uri"], url)

    def test_flex_message_alttext_length(self):
        """altText ต้องไม่เกิน 400 chars (LINE limit)."""
        url = "https://example.com/test.pdf"
        msg = server.build_file_flex_message(DEMO_DOC, url)
        self.assertLessEqual(len(msg["altText"]), 400)

    def test_flex_message_all_categories(self):
        """ทดสอบทุก category."""
        url = "https://example.com/test.pdf"
        for cat_key, cat_name in server._CAT_NAMES.items():
            doc = {**DEMO_DOC, "category": cat_key}
            msg = server.build_file_flex_message(doc, url)
            self.assertIn(cat_name, msg["altText"])

    def test_flex_message_unknown_category(self):
        """Category ที่ไม่รู้จักต้องแสดงค่าดิบ."""
        url = "https://example.com/test.pdf"
        doc = {**DEMO_DOC, "category": "unknown_cat"}
        msg = server.build_file_flex_message(doc, url)
        self.assertIn("unknown_cat", msg["altText"])


class TestGetDownloadUrl(unittest.TestCase):
    """ทดสอบ logic เลือก URL: Signed URL ตรง vs proxy."""

    @patch("server.generate_signed_url")
    def test_short_signed_url_used_directly(self, mock_signed):
        """Signed URL สั้นกว่า 1000 chars → ใช้ตรง."""
        short_url = "https://storage.googleapis.com/bucket/file.pdf?" + "x" * 200
        mock_signed.return_value = short_url

        result = server.get_download_url("test/path.pdf")
        self.assertEqual(result, short_url)

    @patch("server.generate_signed_url")
    def test_long_signed_url_uses_proxy(self, mock_signed):
        """Signed URL ยาวเกิน 1000 chars → ใช้ proxy endpoint."""
        long_url = "https://storage.googleapis.com/bucket/file.pdf?" + "x" * 1000
        mock_signed.return_value = long_url

        with patch.dict(os.environ, {"RENDER_EXTERNAL_URL": "https://myapp.onrender.com"}):
            result = server.get_download_url("test/path.pdf")

        self.assertTrue(result.startswith("https://myapp.onrender.com/dl/"))
        # Validate the token in the URL
        token = result.split("/dl/")[1]
        gcs_path = server.validate_download_token(token)
        self.assertEqual(gcs_path, "test/path.pdf")

    @patch("server.generate_signed_url")
    def test_signed_url_none(self, mock_signed):
        """generate_signed_url ล้มเหลว → คืน None."""
        mock_signed.return_value = None
        result = server.get_download_url("test/path.pdf")
        self.assertIsNone(result)


class TestDownloadProxyEndpoint(unittest.TestCase):
    """ทดสอบ /dl/<token> endpoint."""

    def setUp(self):
        self.app = server.app.test_client()

    def test_valid_token_redirects(self):
        """Token ถูกต้อง → 302 redirect ไป Signed URL."""
        gcs_path = DEMO_DOC["gcs_object_path"]
        token = server.generate_download_token(gcs_path, expiry_seconds=900)

        with patch("server.generate_signed_url") as mock_signed:
            mock_signed.return_value = "https://storage.googleapis.com/test-bucket/signed?token=abc"
            resp = self.app.get(f"/dl/{token}")

        self.assertEqual(resp.status_code, 302)
        self.assertIn("storage.googleapis.com", resp.headers["Location"])

    def test_invalid_token_returns_403(self):
        """Token ไม่ถูกต้อง → 403."""
        resp = self.app.get("/dl/invalid-token-here")
        self.assertEqual(resp.status_code, 403)
        self.assertIn("ลิงก์หมดอายุ", resp.data.decode())

    def test_expired_token_returns_403(self):
        """Token หมดอายุ → 403."""
        gcs_path = DEMO_DOC["gcs_object_path"]
        token = server.generate_download_token(gcs_path, expiry_seconds=1)

        # Wait for expiry
        import time as t
        t.sleep(2)

        resp = self.app.get(f"/dl/{token}")
        self.assertEqual(resp.status_code, 403)

    def test_signed_url_failure_returns_500(self):
        """Signed URL สร้างไม่ได้ → 500."""
        gcs_path = DEMO_DOC["gcs_object_path"]
        token = server.generate_download_token(gcs_path, expiry_seconds=900)

        with patch("server.generate_signed_url", return_value=None):
            resp = self.app.get(f"/dl/{token}")

        self.assertEqual(resp.status_code, 500)
        self.assertIn("ไม่สามารถดาวน์โหลดได้", resp.data.decode())


class TestHandleSearchFlexDispatch(unittest.TestCase):
    """ทดสอบว่า handle_search_command คืน Flex Message dict สำหรับ 'ขอไฟล์'."""

    @patch.object(server.doc_index, "get_with_owner_check")
    @patch("server.get_download_url")
    def test_file_request_returns_flex_dict(self, mock_url, mock_get):
        """คำสั่ง 'ขอไฟล์' ต้องคืน dict ไม่ใช่ string."""
        mock_get.return_value = (DEMO_DOC, True)
        mock_url.return_value = "https://example.com/download.pdf"

        result = server.handle_search_command("ขอไฟล์ demo-test", "testhash123")

        self.assertIsInstance(result, dict)
        self.assertEqual(result["type"], "flex")
        self.assertIn("contents", result)
        self.assertIn("altText", result)

    @patch.object(server.doc_index, "get_with_owner_check")
    def test_file_not_found_returns_string(self, mock_get):
        """เอกสารไม่พบ → คืน string (ข้อความ error)."""
        mock_get.return_value = (None, False)
        result = server.handle_search_command("ขอไฟล์ nonexist", "testhash123")
        self.assertIsInstance(result, str)
        self.assertIn("ไม่พบ", result)

    @patch.object(server.doc_index, "get_with_owner_check")
    def test_unauthorized_returns_string(self, mock_get):
        """ไม่มีสิทธิ์ → คืน string."""
        mock_get.return_value = (DEMO_DOC, False)
        result = server.handle_search_command("ขอไฟล์ demo-test", "otherhash")
        self.assertIsInstance(result, str)
        self.assertIn("ไม่มีสิทธิ์", result)

    @patch.object(server.doc_index, "get_with_owner_check")
    @patch("server.get_download_url")
    def test_url_generation_fails_returns_string(self, mock_url, mock_get):
        """สร้าง URL ไม่ได้ → คืน string."""
        mock_get.return_value = (DEMO_DOC, True)
        mock_url.return_value = None

        result = server.handle_search_command("ขอไฟล์ demo-test", "testhash123")
        self.assertIsInstance(result, str)
        self.assertIn("ไม่สามารถ", result)

    def test_search_command_returns_string(self):
        """คำสั่งค้นหาปกติยังคืน string."""
        with patch.object(server.doc_index, "search", return_value=[]):
            result = server.handle_search_command("ค้นหา test", "testhash123")
        self.assertIsInstance(result, str)

    def test_empty_doc_id_returns_string(self):
        """ขอไฟล์ โดยไม่ระบุรหัส → คืน string."""
        result = server.handle_search_command("ขอไฟล์ ", "testhash123")
        self.assertIsInstance(result, str)
        self.assertIn("กรุณาระบุรหัส", result)


class TestReplyLineMessage(unittest.TestCase):
    """ทดสอบ reply_line_message function."""

    @patch("server.http_requests.post")
    def test_sends_flex_message(self, mock_post):
        """ต้องส่ง Flex Message ผ่าน LINE Reply API."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        flex_msg = {
            "type": "flex",
            "altText": "test",
            "contents": {"type": "bubble", "body": {"type": "box", "layout": "vertical", "contents": []}},
        }

        # Force non-testing mode for this test
        original_testing = server.IS_TESTING
        server.IS_TESTING = False
        try:
            server.reply_line_message("test-reply-token", flex_msg)
        finally:
            server.IS_TESTING = original_testing

        mock_post.assert_called_once()
        call_data = json.loads(mock_post.call_args.kwargs.get("data", mock_post.call_args[1].get("data", b"")))
        self.assertEqual(call_data["messages"][0]["type"], "flex")


class TestNoSecretsInLogs(unittest.TestCase):
    """ตรวจว่า download proxy ไม่ log URL หรือ path."""

    def test_proxy_403_no_token_in_log(self):
        """Log message ของ 403 ต้องไม่มี token."""
        with patch("server.logger") as mock_logger:
            app = server.app.test_client()
            app.get("/dl/some-secret-token-value")

            # Check all log calls
            for call in mock_logger.info.call_args_list:
                msg = str(call)
                self.assertNotIn("some-secret-token-value", msg)


if __name__ == "__main__":
    unittest.main()
