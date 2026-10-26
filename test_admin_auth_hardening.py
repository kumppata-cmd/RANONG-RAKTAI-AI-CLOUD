"""การยืนยันตัวตนของ Admin API — ขอบที่ทำให้ 401 กลายเป็น 500

ที่มา: หลังเปลี่ยนไปใช้ hmac.compare_digest ทั้งสองจุด พบว่า
compare_digest โยน TypeError ถ้า argument เป็น str ที่มีอักขระนอก ASCII
ผลคือคำขอที่ส่ง secret เป็นภาษาไทยจะได้ 500 Internal Server Error
แทน 401 Unauthorized — บอกผู้โจมตีว่าเกิดอะไรขึ้นข้างใน และทำให้
error log เต็มไปด้วย stack trace จากคำขอที่ควรถูกปฏิเสธเงียบ ๆ

เทสต์ชุดนี้ล็อกไว้ว่า
    secret ผิดทุกรูปแบบ (ว่าง, ผิด, ภาษาไทย, อีโมจิ, ยาวมาก) → 401
    secret ถูก → ผ่าน
    query string ไม่ถูกรับเป็นหลักฐานการยืนยันตัวตน

ไม่มี secret จริงในไฟล์นี้ — ใช้ค่าทดสอบที่ตั้งใน env ของเทสต์เท่านั้น
"""
import os
import unittest

os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "test-dummy")
os.environ.setdefault("LINE_CHANNEL_SECRET", "test-dummy")
os.environ.setdefault("GCS_BUCKET_NAME", "test-bucket")

import server

_TEST_SECRET = "admin-auth-hardening-test-secret"

# จุดตรวจตัวแทนของทั้งสองกลไก: API (header) และหน้าเว็บ (query string)
_API_PATH = "/admin/api/v2/vendors/list"
_PAGE_PATH = "/admin/registry"

_BAD_SECRETS = [
    ("ว่าง", ""),
    ("ผิดธรรมดา", "wrong-secret"),
    ("ภาษาไทย", "ความลับ"),
    ("อีโมจิ", "secret-🔐"),
    ("อักขระ latin-1", "sécret"),
    ("ยาวมาก", "x" * 4096),
    ("ขึ้นต้นตรงบางส่วน", _TEST_SECRET[:10]),
]


class _AdminAuthBase(unittest.TestCase):

    def setUp(self):
        self._prev = os.environ.get("ADMIN_SECRET")
        os.environ["ADMIN_SECRET"] = _TEST_SECRET
        self.client = server.app.test_client()

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("ADMIN_SECRET", None)
        else:
            os.environ["ADMIN_SECRET"] = self._prev


class TestAdminApiAuth(_AdminAuthBase):

    def test_secret_ผิดทุกแบบได้_401_ไม่ใช่_500(self):
        for label, bad in _BAD_SECRETS:
            with self.subTest(secret=label):
                r = self.client.get(
                    _API_PATH, headers={"X-Admin-Secret": bad},
                )
                self.assertEqual(
                    r.status_code, 401,
                    f"secret แบบ '{label}' ได้ {r.status_code} — ต้องเป็น 401",
                )

    def test_ไม่ส่ง_header_เลยได้_401(self):
        self.assertEqual(self.client.get(_API_PATH).status_code, 401)

    def test_secret_ถูกผ่าน(self):
        r = self.client.get(
            _API_PATH, headers={"X-Admin-Secret": _TEST_SECRET},
        )
        self.assertEqual(r.status_code, 200)

    def test_query_string_ไม่ถูกรับเป็นการยืนยันตัวตน(self):
        """secret ใน URL ติดอยู่ใน access log — ต้องไม่นับเป็นหลักฐาน"""
        r = self.client.get(f"{_API_PATH}?secret={_TEST_SECRET}")
        self.assertEqual(r.status_code, 401)

    def test_ไม่ได้ตั้ง_ADMIN_SECRET_ได้_500_ไม่ใช่ปล่อยผ่าน(self):
        os.environ.pop("ADMIN_SECRET", None)
        r = self.client.get(
            _API_PATH, headers={"X-Admin-Secret": _TEST_SECRET},
        )
        self.assertEqual(r.status_code, 500)


class TestAdminRegistryPageAuth(_AdminAuthBase):

    def test_secret_ผิดทุกแบบได้_401_ไม่ใช่_500(self):
        for label, bad in _BAD_SECRETS:
            with self.subTest(secret=label):
                r = self.client.get(_PAGE_PATH, query_string={"secret": bad})
                self.assertEqual(
                    r.status_code, 401,
                    f"secret แบบ '{label}' ได้ {r.status_code} — ต้องเป็น 401",
                )

    def test_secret_ถูกได้หน้าเว็บ(self):
        r = self.client.get(_PAGE_PATH, query_string={"secret": _TEST_SECRET})
        self.assertEqual(r.status_code, 200)

    def test_หน้าเว็บไม่ใส่_secret_ลง_query_string_ของ_API(self):
        """หน้าเว็บต้องส่ง secret ทาง header เท่านั้น

        ของเดิมใส่ทั้ง header และ query string ทำให้ ADMIN_SECRET
        ติดอยู่ใน access log ของทุกคำขอที่หน้านี้ยิง
        """
        body = self.client.get(
            _PAGE_PATH, query_string={"secret": _TEST_SECRET},
        ).get_data(as_text=True)
        self.assertNotIn('searchParams.set("secret"', body)
        self.assertIn('"X-Admin-Secret": SECRET', body)


if __name__ == "__main__":
    unittest.main()
