"""Security hardening — owner_hash contract และ salt ที่ต้องคงที่

ครอบสองข้อจาก SECURITY_GAP_ANALYSIS:

  §3.1 ไม่มีจุดใดยืนยันรูปแบบ owner_hash ที่รับเข้ามา ค่าที่ผิดรูป
       (เช่น LINE userId ดิบ หรือพิมพ์ตกหล่น) ถูกใช้เงียบ ๆ
       /admin/assign-owner ผูก owner แบบถาวรและไม่ยอมผูกซ้ำ
       → ใส่ผิดครั้งเดียว เอกสารเข้าถึงไม่ได้ตลอดไป

  §3.2 โหมด testing-docs จัดเก็บเอกสารจริงแต่ใช้ salt สุ่มที่เปลี่ยน
       ทุก restart → owner_hash ไม่ตรงอีกเลย เอกสารหายแบบเงียบ ๆ

ข้อมูลทั้งหมดเป็นค่าสังเคราะห์

หมายเหตุ: import server แบบ lazy เพื่อไม่ให้รบกวนลำดับ import
ของโมดูลทดสอบอื่นที่ตั้ง env ก่อนโหลด server
"""
import re
import subprocess
import sys
import unittest


def _server():
    import server as _s
    return _s


class TestOwnerHashValidator(unittest.TestCase):
    """§3.1 — สัญญารูปแบบของ owner_hash"""

    def setUp(self):
        self.server = _server()

    def test_accepts_real_hash_output(self):
        """ผลลัพธ์จริงของ hash_user_id ต้องผ่านเสมอ"""
        s = self.server
        for uid in ("U_patient_a", "U_patient_b", "", "unknown"):
            with self.subTest(uid=uid):
                self.assertTrue(
                    s.is_valid_owner_hash(s.hash_user_id(uid))
                )

    def test_rejects_raw_line_user_id(self):
        """[SEC-1 ชนิดเดียวกัน] LINE userId ดิบต้องไม่ผ่าน

        ถ้ามีตัวตรวจนี้ตั้งแต่แรก บั๊ก LIFF จะถูกจับได้ทันทีวันแรก
        แทนที่จะเงียบจนมา audit เจอ
        """
        self.assertFalse(
            self.server.is_valid_owner_hash("U1234567890abcdef1234567890abcdef")
        )

    def test_rejects_wrong_length(self):
        s = self.server
        self.assertFalse(s.is_valid_owner_hash("deadbeef"))          # สั้น
        self.assertFalse(s.is_valid_owner_hash("d" * 64))            # ยาว
        self.assertFalse(s.is_valid_owner_hash("d" * 31))            # ขาด 1
        self.assertFalse(s.is_valid_owner_hash("d" * 33))            # เกิน 1

    def test_rejects_uppercase_hex(self):
        """ต้องตรงรูปแบบที่ hash_user_id ผลิตจริง (ตัวพิมพ์เล็ก)"""
        self.assertFalse(self.server.is_valid_owner_hash("DEADBEEF" * 4))

    def test_rejects_non_hex_characters(self):
        s = self.server
        self.assertFalse(s.is_valid_owner_hash("z" * 32))
        self.assertFalse(s.is_valid_owner_hash("dead-beef" + "0" * 23))

    def test_rejects_empty_and_none(self):
        s = self.server
        self.assertFalse(s.is_valid_owner_hash(""))
        self.assertFalse(s.is_valid_owner_hash(None))

    def test_hash_output_shape_is_stable(self):
        """สัญญา: 32 ตัว hex ตัวพิมพ์เล็ก — ถ้าเปลี่ยน validator ต้องเปลี่ยนด้วย"""
        h = self.server.hash_user_id("U_synthetic")
        self.assertEqual(len(h), 32)
        self.assertRegex(h, r"^[0-9a-f]{32}$")


class TestAssignOwnerGuard(unittest.TestCase):
    """§3.1 — จุดที่ความเสียหายถาวร"""

    SECRET = "test_admin_secret_synthetic"

    def setUp(self):
        self.server = _server()
        self._secret_backup = self.server.ADMIN_SECRET
        self.server.ADMIN_SECRET = self.SECRET
        self.client = self.server.app.test_client()
        self.headers = {"X-Admin-Secret": self.SECRET}

    def tearDown(self):
        self.server.ADMIN_SECRET = self._secret_backup

    def _post(self, owner_hash):
        return self.client.post(
            "/admin/assign-owner", headers=self.headers,
            json={"doc_id": "doc_synthetic", "owner_hash": owner_hash},
        )

    def test_rejects_raw_user_id(self):
        r = self._post("U1234567890abcdef")
        self.assertEqual(r.status_code, 400)
        self.assertIn("owner_hash", str(r.get_json()))

    def test_rejects_truncated_hash(self):
        """พิมพ์ตกหล่น — เคสที่เกิดได้ง่ายที่สุดตอนทำงานจริง"""
        self.assertEqual(self._post("deadbeef").status_code, 400)

    def test_rejects_uppercase(self):
        self.assertEqual(self._post("DEADBEEF" * 4).status_code, 400)

    def test_accepts_valid_hash(self):
        """ค่าที่ถูกต้องต้องไม่ถูกบล็อก — ไม่ทำให้ของเดิมพัง"""
        r = self._post("deadbeef" * 4)
        self.assertNotEqual(r.status_code, 400)

    def test_batch_is_rejected_atomically(self):
        """ถ้ามีรายการผิดรูปแม้รายการเดียว ต้องไม่ผูกรายการใดเลย

        การผูกเป็นแบบถาวรและไม่ยอมผูกซ้ำ จึงต้องตรวจทั้งชุดก่อนเริ่ม
        """
        r = self.client.post(
            "/admin/assign-owner", headers=self.headers,
            json={"assignments": [
                {"doc_id": "doc_ok", "owner_hash": "deadbeef" * 4},
                {"doc_id": "doc_bad", "owner_hash": "U_raw_userid"},
            ]},
        )
        self.assertEqual(r.status_code, 400)
        body = r.get_json()
        self.assertIn("invalid_doc_ids", body)
        self.assertEqual(body.get("assigned"), None)

    def test_requires_admin_secret(self):
        """การตรวจรูปแบบต้องไม่เปิดทางข้าม auth"""
        r = self.client.post(
            "/admin/assign-owner",
            json={"doc_id": "d", "owner_hash": "deadbeef" * 4},
        )
        self.assertEqual(r.status_code, 401)


class TestHnRegistryGuard(unittest.TestCase):
    """§3.1 — ผูกบัญชีผิดคน = เอกสารไปอยู่แฟ้มคนอื่น"""

    SECRET = "test_admin_secret_synthetic"

    def setUp(self):
        import os
        self.server = _server()
        self._secret_backup = self.server.ADMIN_SECRET
        self.server.ADMIN_SECRET = self.SECRET
        # endpoint นี้อ่าน ADMIN_SECRET จาก os.getenv ตรง ๆ
        # (ไม่ใช่ตัวแปรระดับโมดูล) จึงต้องตั้ง env ด้วย
        self._env_backup = os.environ.get("ADMIN_SECRET")
        os.environ["ADMIN_SECRET"] = self.SECRET
        self.client = self.server.app.test_client()

    def tearDown(self):
        import os
        self.server.ADMIN_SECRET = self._secret_backup
        if self._env_backup is None:
            os.environ.pop("ADMIN_SECRET", None)
        else:
            os.environ["ADMIN_SECRET"] = self._env_backup

    def test_rejects_raw_user_id(self):
        r = self.client.post(
            "/admin/api/v2/hn-registry/register",
            json={"owner_hash": "U1234567890abcdef", "clinic_hn": "57042"},
            headers={"X-Admin-Secret": self.SECRET},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("ผิดรูปแบบ", str(r.get_json()))


class TestSaltRequiredWhenStoringDocuments(unittest.TestCase):
    """§3.2 — โหมดที่จัดเก็บเอกสารจริงต้องมี salt คงที่

    รันเป็น subprocess เพราะ USER_ID_SALT ถูกอ่านตอน import server
    ซึ่งเป็น singleton — เปลี่ยนในกระบวนการเดิมไม่ได้
    """

    @staticmethod
    def _salt_missing(app_env, salt=None):
        env_lines = [f"APP_ENV={app_env!r}"]
        code = (
            "import os;"
            f"os.environ['APP_ENV']={app_env!r};"
            + (f"os.environ['USER_ID_SALT']={salt!r};" if salt
               else "os.environ.pop('USER_ID_SALT', None);")
            + "import server;"
            "print('RESULT:', server._USER_ID_SALT_MISSING)"
        )
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=120,
        )
        m = re.search(r"RESULT: (True|False)", out.stdout)
        if not m:
            raise AssertionError(
                f"ไม่พบผลลัพธ์\nstdout={out.stdout[-500:]}\n"
                f"stderr={out.stderr[-500:]}"
            )
        return m.group(1) == "True"

    def test_production_without_salt_fails_closed(self):
        """พฤติกรรมเดิม — ต้องไม่เปลี่ยน"""
        self.assertTrue(self._salt_missing("production"))

    def test_production_with_salt_is_fine(self):
        """ต้องไม่ทำให้ production ที่ตั้งค่าถูกต้องพัง"""
        self.assertFalse(
            self._salt_missing("production", salt="synthetic_salt_value")
        )

    def test_testing_docs_without_salt_now_fails_closed(self):
        """[§3.2 หัวใจ] โหมดนี้จัดเก็บเอกสารจริง จึงต้องมี salt คงที่

        เดิมใช้ salt สุ่มที่เปลี่ยนทุก restart → เอกสารที่เก็บไว้
        เข้าถึงไม่ได้ถาวรโดยไม่มีใครรู้
        """
        self.assertTrue(self._salt_missing("testing-docs"))

    def test_testing_docs_with_salt_is_fine(self):
        self.assertFalse(
            self._salt_missing("testing-docs", salt="synthetic_salt_value")
        )

    def test_plain_testing_without_salt_still_allowed(self):
        """โหมดทดสอบที่ไม่จัดเก็บเอกสาร ยังใช้ salt สุ่มได้ตามเดิม

        ไม่มีเอกสารให้สูญหาย จึงไม่ต้องบังคับ — และการบังคับจะทำให้
        ชุดทดสอบทั้งหมดรันไม่ได้
        """
        self.assertFalse(self._salt_missing("testing"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
