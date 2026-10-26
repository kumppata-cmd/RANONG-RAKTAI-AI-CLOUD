"""CS-5 — บังคับ approval gate และแยก role ทางคลินิก

ข้อบกพร่องเดิม (CLINICAL_SAFETY_GAP_ANALYSIS CS-5):
  ผลวิเคราะห์ที่ยังไม่ผ่านแพทย์ (PENDING_VERIFICATION) ถูกส่งเนื้อหา
  เต็มให้ทุกคนใน AUTHORIZED_USERS โดยติดป้ายกำกับไว้เฉย ๆ
  และ AUTHORIZED_USERS เป็น allowlist แบน ไม่แยกผู้ป่วย/เจ้าหน้าที่/แพทย์
  → การกันไม่ให้ผู้ป่วยเห็นขึ้นกับการตั้งค่า env ไม่ใช่การบังคับในโค้ด
    เพิ่มผู้ป่วยเข้า allowlist คนเดียวก็เห็นผลที่ยังไม่ผ่านแพทย์ทันที

ข้อมูลทั้งหมดเป็นค่าสังเคราะห์

หมายเหตุสำคัญเรื่องการ import:
  ห้าม import server ที่ระดับ module โมดูลทดสอบหลายตัวในโปรเจกต์นี้
  ตั้ง env แล้วค่อย import/reload server ซึ่งเป็น singleton —
  ใครโหลดก่อนเป็นผู้กำหนด config ถ้าไฟล์นี้ (ชื่อเรียงมาก่อน) import
  ตอน unittest เก็บ test จะทำให้ test_security / test_flex_message
  ที่รันทีหลังได้ config ผิดและล้ม จึง import แบบ lazy ใน setUp
"""
import unittest


def _server():
    import server as _s
    return _s


class RoleFixture(unittest.TestCase):
    """เพิ่ม/ถอน role ชั่วคราวโดยไม่กระทบ test อื่น"""

    PHYS = "hash_physician_synthetic"
    ADMIN = "hash_admin_synthetic"
    STAFF = "hash_staff_synthetic"
    PATIENT = "hash_patient_synthetic"

    def setUp(self):
        self.server = _server()
        s = self.server
        self._phys_backup = set(s.PHYSICIAN_USERS)
        self._admin_backup = set(s.ADMIN_USERS)
        self._auth_backup = set(s.AUTHORIZED_USERS)

        s.PHYSICIAN_USERS.add(self.PHYS)
        s.ADMIN_USERS.add(self.ADMIN)
        # ทุก role อยู่ใน AUTHORIZED_USERS ได้หมด — นี่คือสิทธิ์เข้าระบบ
        # ไม่ใช่สิทธิ์ทางคลินิก
        for u in (self.PHYS, self.ADMIN, self.STAFF, self.PATIENT):
            s.AUTHORIZED_USERS.add(u)

    def tearDown(self):
        s = self.server
        s.PHYSICIAN_USERS.clear()
        s.PHYSICIAN_USERS.update(self._phys_backup)
        s.ADMIN_USERS.clear()
        s.ADMIN_USERS.update(self._admin_backup)
        s.AUTHORIZED_USERS.clear()
        s.AUTHORIZED_USERS.update(self._auth_backup)


class TestRoleSeparation(RoleFixture):

    def test_authorized_is_not_the_same_as_physician(self):
        """[หัวใจ CS-5] อยู่ใน allowlist ไม่ได้แปลว่าเป็นแพทย์

        เดิมโค้ดใช้ is_authorized_user() ตัดสินการเห็นเนื้อหาทางคลินิก
        ซึ่งรวมเจ้าหน้าที่และผู้ป่วยเข้าไปด้วย
        """
        s = self.server
        self.assertTrue(s.is_authorized_user(self.STAFF))
        self.assertFalse(s.is_physician_user(self.STAFF))

    def test_physician_recognised(self):
        self.assertTrue(self.server.is_physician_user(self.PHYS))

    def test_admin_recognised(self):
        self.assertTrue(self.server.is_admin_user(self.ADMIN))

    def test_staff_is_neither_physician_nor_admin(self):
        s = self.server
        self.assertFalse(s.is_physician_user(self.STAFF))
        self.assertFalse(s.is_admin_user(self.STAFF))

    def test_empty_hash_is_never_privileged(self):
        s = self.server
        self.assertFalse(s.is_physician_user(""))
        self.assertFalse(s.is_admin_user(""))
        self.assertFalse(s.can_view_unapproved_clinical(""))

    def test_unknown_user_is_never_privileged(self):
        self.assertFalse(
            self.server.can_view_unapproved_clinical("nobody_at_all")
        )


class TestUnapprovedClinicalAccess(RoleFixture):

    def test_physician_may_view_unapproved(self):
        """แพทย์ต้องเห็นได้ ไม่งั้นตรวจสอบเพื่ออนุมัติไม่ได้"""
        self.assertTrue(
            self.server.can_view_unapproved_clinical(self.PHYS)
        )

    def test_admin_may_view_unapproved(self):
        self.assertTrue(
            self.server.can_view_unapproved_clinical(self.ADMIN)
        )

    def test_staff_may_not_view_unapproved(self):
        """เจ้าหน้าที่ในระบบ แต่ไม่ใช่แพทย์ → ต้องไม่เห็นข้อสรุป"""
        self.assertFalse(
            self.server.can_view_unapproved_clinical(self.STAFF)
        )

    def test_patient_may_not_view_unapproved(self):
        """[CS-5] เพิ่มผู้ป่วยเข้า allowlist แล้วต้องไม่เห็นทันทีเหมือนเดิม"""
        s = self.server
        self.assertTrue(s.is_authorized_user(self.PATIENT))
        self.assertFalse(s.can_view_unapproved_clinical(self.PATIENT))


class TestGateDecisionLogic(unittest.TestCase):
    """ตรรกะการตัดสินใจเดียวกับที่ใช้ใน handle_search_command

    ทำซ้ำที่นี่เพื่อทดสอบทุกการผสม status × role ได้ครบโดยไม่ต้อง
    ตั้ง storage/report จริง — ตรรกะต้องตรงกับใน server.py
    """

    @staticmethod
    def withholds(status, is_reviewer):
        needs_review = status in (
            "PENDING_VERIFICATION", "STALE_AFTER_CORRECTION",
        )
        return needs_review and not is_reviewer

    def test_pending_is_withheld_from_non_reviewer(self):
        self.assertTrue(self.withholds("PENDING_VERIFICATION", False))

    def test_stale_is_withheld_from_non_reviewer(self):
        """ผลที่ล้าสมัยอันตรายกว่า เพราะเคยผ่านแพทย์แต่ข้อมูลเปลี่ยนแล้ว"""
        self.assertTrue(self.withholds("STALE_AFTER_CORRECTION", False))

    def test_pending_is_shown_to_reviewer(self):
        self.assertFalse(self.withholds("PENDING_VERIFICATION", True))

    def test_stale_is_shown_to_reviewer(self):
        self.assertFalse(self.withholds("STALE_AFTER_CORRECTION", True))

    def test_verified_is_shown_to_everyone(self):
        """ผ่านแพทย์แล้วต้องแสดงให้ผู้ที่มีสิทธิ์เข้าระบบได้ตามปกติ"""
        self.assertFalse(self.withholds("PHYSICIAN_VERIFIED", False))
        self.assertFalse(self.withholds("PHYSICIAN_VERIFIED", True))

    def test_unknown_status_is_not_withheld(self):
        """สถานะอื่น ๆ (เช่นผลเก่าที่ไม่มี field นี้) ไม่เปลี่ยนพฤติกรรมเดิม"""
        self.assertFalse(self.withholds("", False))
        self.assertFalse(self.withholds(None, False))

    def test_all_combinations(self):
        """ตารางครบทุกการผสม — กัน regression"""
        expected = {
            ("PENDING_VERIFICATION", False): True,
            ("PENDING_VERIFICATION", True): False,
            ("STALE_AFTER_CORRECTION", False): True,
            ("STALE_AFTER_CORRECTION", True): False,
            ("PHYSICIAN_VERIFIED", False): False,
            ("PHYSICIAN_VERIFIED", True): False,
        }
        for (status, reviewer), want in expected.items():
            with self.subTest(status=status, reviewer=reviewer):
                self.assertEqual(self.withholds(status, reviewer), want)


class TestWithheldMessageSafety(unittest.TestCase):
    """ข้อความที่ส่งแทนเนื้อหาต้องไม่รั่วข้อสรุปทางคลินิก"""

    WITHHELD_PENDING = (
        "⏳ ผลวิเคราะห์ของเอกสารนี้ยังไม่ผ่านการตรวจสอบโดยแพทย์\n\n"
        "เพื่อความปลอดภัย ระบบจะยังไม่แสดงข้อสรุป "
        "จนกว่าแพทย์จะตรวจสอบและยืนยัน\n\n"
        "รหัสเอกสาร: abcd1234"
    )

    def test_message_contains_no_clinical_conclusion(self):
        for term in ("eGFR", "CKD", "Potassium", "Creatinine", "mg/dL",
                     "วิกฤต", "ผิดปกติ"):
            self.assertNotIn(term, self.WITHHELD_PENDING)

    def test_message_explains_why_and_what_next(self):
        """ผู้ใช้ต้องเข้าใจว่าทำไมไม่เห็น ไม่ใช่คิดว่าระบบพัง"""
        self.assertIn("แพทย์", self.WITHHELD_PENDING)
        self.assertIn("ยืนยัน", self.WITHHELD_PENDING)

    def test_message_keeps_document_reference(self):
        """ยังบอกรหัสเอกสารได้ เพื่อให้ติดตามกับเจ้าหน้าที่ได้"""
        self.assertIn("รหัสเอกสาร", self.WITHHELD_PENDING)


class TestGateWiredInSource(unittest.TestCase):
    """ตรวจว่า gate ถูกต่อไว้จริงในโค้ด ไม่ใช่มีแต่ helper"""

    @staticmethod
    def _source():
        with open("server.py", encoding="utf-8") as f:
            return f.read()

    def test_withhold_is_audited(self):
        """การกันเนื้อหาต้องถูกบันทึก ตรวจสอบย้อนหลังได้"""
        self.assertIn("clinical_analysis_withheld", self._source())

    def test_gate_uses_role_helper_not_authorized_check(self):
        """ต้องใช้ can_view_unapproved_clinical ไม่ใช่ is_authorized_user"""
        src = self._source()
        i = src.index("_needs_review")
        window = src[i:i + 800]
        self.assertIn("can_view_unapproved_clinical", window)
        self.assertNotIn("is_authorized_user", window)


if __name__ == "__main__":
    unittest.main(verbosity=2)
