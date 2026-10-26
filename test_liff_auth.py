"""SEC-1 — LIFF identity verification regression tests.

ข้อบกพร่องเดิม (SECURITY_GAP_ANALYSIS SEC-1):
  1. /liff/api/my-labs รับ owner_hash จาก query string ตรง ๆ แล้วใช้
     กรองข้อมูลผู้ป่วย = ให้ผู้เรียกประกาศเองว่าตัวเองเป็นใคร
  2. ไม่มีการยืนยัน LIFF ID token ที่ใดในระบบเลย
  3. client ส่ง LINE userId ดิบ ขณะที่ระบบเก็บ salted hash → ใช้ไม่ได้จริง
  4. มี dev_test_user bypass ฝังในหน้าเว็บ
  5. คืน str(e) กลับไปให้ client

ข้อมูลทั้งหมดเป็นค่าสังเคราะห์ ไม่มี token หรือข้อมูลผู้ป่วยจริง
"""
import unittest

from liff_app import (
    LiffAuthError,
    verify_liff_id_token,
)

CHANNEL_ID = "1234567890"
OTHER_CHANNEL = "9999999999"
FAKE_TOKEN = "synthetic.test.token"


class FakeResponse:
    def __init__(self, status_code=200, payload=None, raise_json=False):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._payload


def poster(status_code=200, payload=None, raise_json=False, capture=None):
    """สร้าง http_post ปลอม พร้อมบันทึกสิ่งที่ถูกส่งไป"""
    def _post(url, data=None, timeout=None):
        if capture is not None:
            capture.append({"url": url, "data": data, "timeout": timeout})
        return FakeResponse(status_code, payload, raise_json)
    return _post


def ok_payload(sub="U_synthetic_user", aud=CHANNEL_ID):
    return {"sub": sub, "aud": aud, "iss": "https://access.line.me"}


class TestVerifyLiffIdToken(unittest.TestCase):

    def test_valid_token_returns_sub(self):
        sub = verify_liff_id_token(
            FAKE_TOKEN, http_post=poster(200, ok_payload()),
            channel_id=CHANNEL_ID,
        )
        self.assertEqual(sub, "U_synthetic_user")

    def test_token_is_verified_against_line_with_our_channel_id(self):
        """ต้องส่ง token และ client_id ของเราไปให้ LINE ตรวจ

        ห้ามถอดรหัส token เองโดยไม่ตรวจลายเซ็น
        """
        sent = []
        verify_liff_id_token(
            FAKE_TOKEN, http_post=poster(200, ok_payload(), capture=sent),
            channel_id=CHANNEL_ID,
        )
        self.assertEqual(len(sent), 1)
        self.assertIn("api.line.me", sent[0]["url"])
        self.assertEqual(sent[0]["data"]["id_token"], FAKE_TOKEN)
        self.assertEqual(sent[0]["data"]["client_id"], CHANNEL_ID)
        self.assertIsNotNone(sent[0]["timeout"], "ต้องมี timeout")

    def test_token_for_another_channel_is_rejected(self):
        """[สำคัญ] token ที่ออกให้ channel อื่นต้องใช้กับระบบเราไม่ได้

        ถ้าไม่ตรวจ aud ผู้โจมตีสามารถใช้ token จาก LIFF app อื่น
        เข้าถึงข้อมูลผู้ป่วยในระบบนี้ได้
        """
        with self.assertRaises(LiffAuthError):
            verify_liff_id_token(
                FAKE_TOKEN,
                http_post=poster(200, ok_payload(aud=OTHER_CHANNEL)),
                channel_id=CHANNEL_ID,
            )

    def test_aud_as_list_is_supported(self):
        sub = verify_liff_id_token(
            FAKE_TOKEN,
            http_post=poster(200, ok_payload(aud=[CHANNEL_ID, "other"])),
            channel_id=CHANNEL_ID,
        )
        self.assertEqual(sub, "U_synthetic_user")

    def test_line_rejection_is_rejected(self):
        """LINE ตอบไม่ใช่ 200 (token ปลอม/หมดอายุ) → ต้องไม่ผ่าน"""
        for status in (400, 401, 403, 500):
            with self.subTest(status=status):
                with self.assertRaises(LiffAuthError):
                    verify_liff_id_token(
                        FAKE_TOKEN, http_post=poster(status, {}),
                        channel_id=CHANNEL_ID,
                    )

    def test_missing_sub_is_rejected(self):
        with self.assertRaises(LiffAuthError):
            verify_liff_id_token(
                FAKE_TOKEN,
                http_post=poster(200, {"aud": CHANNEL_ID}),
                channel_id=CHANNEL_ID,
            )

    def test_non_json_response_is_rejected(self):
        with self.assertRaises(LiffAuthError):
            verify_liff_id_token(
                FAKE_TOKEN, http_post=poster(200, raise_json=True),
                channel_id=CHANNEL_ID,
            )

    def test_network_failure_fails_closed(self):
        """เรียก LINE ไม่ได้ → ต้องปฏิเสธ ไม่ใช่ปล่อยผ่าน"""
        def boom(url, data=None, timeout=None):
            raise OSError("network down")
        with self.assertRaises(LiffAuthError):
            verify_liff_id_token(
                FAKE_TOKEN, http_post=boom, channel_id=CHANNEL_ID,
            )

    def test_missing_channel_id_fails_closed(self):
        """ไม่ได้ตั้ง LIFF_CHANNEL_ID → ต้องปฏิเสธทุกคำขอ

        ห้าม degrade เป็นยอมรับ token โดยไม่ตรวจ aud
        """
        with self.assertRaises(LiffAuthError):
            verify_liff_id_token(
                FAKE_TOKEN, http_post=poster(200, ok_payload()),
                channel_id="",
            )

    def test_empty_token_rejected_without_calling_line(self):
        """token ว่างต้องไม่ยิงไปหา LINE โดยเปล่าประโยชน์"""
        sent = []
        for bad in ("", None):
            with self.subTest(token=bad):
                with self.assertRaises(LiffAuthError):
                    verify_liff_id_token(
                        bad, http_post=poster(200, ok_payload(), capture=sent),
                        channel_id=CHANNEL_ID,
                    )
        self.assertEqual(sent, [])

    def test_oversized_token_rejected(self):
        """กันส่ง payload ใหญ่ผิดปกติไปยัง LINE"""
        with self.assertRaises(LiffAuthError):
            verify_liff_id_token(
                "x" * 9000, http_post=poster(200, ok_payload()),
                channel_id=CHANNEL_ID,
            )


class TestOwnerHashDerivation(unittest.TestCase):
    """owner_hash ต้องมาจาก sub ที่ยืนยันแล้ว ไม่ใช่จากผู้เรียก"""

    @staticmethod
    def hash_user_id(user_id, salt="test_salt"):
        """เลียนแบบ server.hash_user_id — ตรรกะต้องตรงกัน"""
        import hashlib
        return hashlib.sha256(
            ((user_id or "unknown") + salt).encode("utf-8")
        ).hexdigest()[:32]

    def test_derived_hash_is_salted_and_truncated(self):
        h = self.hash_user_id("U_synthetic_user")
        self.assertEqual(len(h), 32)
        self.assertNotIn("U_synthetic_user", h)

    def test_raw_userid_never_equals_stored_hash(self):
        """[SEC-1 ข้อ 3] เหตุผลที่ฟีเจอร์เดิมใช้งานไม่ได้จริง

        client เดิมส่ง userId ดิบ แต่ที่เก็บคือ salted hash
        การเทียบจึงไม่มีทางตรงสำหรับผู้ป่วยจริง
        """
        raw = "U_synthetic_user"
        stored = self.hash_user_id(raw)
        self.assertNotEqual(raw, stored)

    def test_same_user_same_hash(self):
        a = self.hash_user_id("U_synthetic_user")
        b = self.hash_user_id("U_synthetic_user")
        self.assertEqual(a, b)

    def test_different_users_different_hash(self):
        a = self.hash_user_id("U_patient_a")
        b = self.hash_user_id("U_patient_b")
        self.assertNotEqual(a, b)


class TestLiffClientPageSafety(unittest.TestCase):
    """ตรวจหน้าเว็บ LIFF ที่ส่งให้ผู้ป่วย"""

    def _html(self):
        from liff_app import get_liff_html
        return get_liff_html("https://example.test")

    def test_page_does_not_send_owner_hash_in_url(self):
        """[SEC-1] ตัวระบุตัวตนต้องไม่อยู่ใน query string อีกต่อไป"""
        html = self._html()
        self.assertNotIn("my-labs?owner_hash=", html)

    def test_page_posts_id_token(self):
        html = self._html()
        self.assertIn("id_token", html)
        self.assertIn("getIDToken", html)

    def test_dev_test_user_bypass_removed(self):
        """[SEC-1] bypass ที่ฝังไว้ต้องไม่มีในหน้าที่ส่งให้ผู้ป่วย"""
        self.assertNotIn("dev_test_user", self._html())

    def test_page_handles_unauthorized_explicitly(self):
        """ต้องมีการจัดการกรณียืนยันตัวตนไม่ผ่าน"""
        self.assertIn("401", self._html())


if __name__ == "__main__":
    unittest.main(verbosity=2)
