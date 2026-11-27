"""🛡 Settings Guard (26 พ.ย. 2569) — กันค่าตั้งค่าสำคัญหาย/ผิดรูปแบบ

เหตุการณ์จริง: 25 พ.ย. 17:52 nurse_group_id/alert_recipients ถูกบันทึกค่าว่างทับ
→ คำสั่งแพทย์ไม่เข้ากลุ่มพยาบาล 16 ชม. · 26 พ.ย. 07:33 Group ID ขาด 1 ตัว → LINE 400.
"""
import unittest
from unittest import mock

from hd_module import settings_guard as sg

GID = "C" + "0c6e7000e7c673d29590f7b25eeb4aa7"   # 33 ตัว
UID = "U" + "1b1068cde88a06e88e53c014d1b83824"   # 33 ตัว


class TestValidate(unittest.TestCase):
    def test_group_id_ok(self):
        self.assertEqual(sg.validate("nurse_group_id", GID), (True, None))
        self.assertEqual(len(GID), 33)

    def test_group_id_short_by_one_is_rejected(self):
        ok, err = sg.validate("nurse_group_id", GID[:-1])   # เหตุการณ์ 26 พ.ย. 07:33
        self.assertFalse(ok)
        self.assertIn("32 ตัว", err)
        self.assertIn("มี 32 ตัว", err)

    def test_group_id_pasted_with_whitespace_is_normalized(self):
        ok, _ = sg.validate("nurse_group_id", "  " + GID[:10] + "\n" + GID[10:] + " ")
        self.assertTrue(ok)
        self.assertEqual(sg.normalize("nurse_group_id", GID + "\n"), GID)

    def test_room_id_accepted(self):
        self.assertTrue(sg.validate("nurse_group_id", "R" + GID[1:])[0])

    def test_user_id_wrong_prefix_rejected(self):
        self.assertFalse(sg.validate("physician_line_id", "C" + UID[1:])[0])
        self.assertTrue(sg.validate("physician_line_id", UID)[0])

    def test_alert_recipients_list(self):
        self.assertTrue(sg.validate("alert_recipients", UID + ", " + UID)[0])
        ok, err = sg.validate("alert_recipients", UID + ",Uabc")
        self.assertFalse(ok)
        self.assertIn("1 รายการ", err)
        self.assertEqual(sg.normalize("alert_recipients", UID + " ,\n " + UID), UID + "," + UID)

    def test_empty_is_ok_here_clear_decided_elsewhere(self):
        self.assertEqual(sg.validate("nurse_group_id", ""), (True, None))

    def test_non_critical_key_untouched(self):
        self.assertEqual(sg.validate("clinic_name", "x"), (True, None))
        self.assertFalse(sg.is_critical("clinic_name"))
        self.assertTrue(sg.is_critical("nurse_group_id"))


class TestCheckAll(unittest.TestCase):
    def test_all_good(self):
        vals = {"nurse_group_id": GID, "alert_recipients": UID, "physician_line_id": UID}
        self.assertEqual(sg.check_all(vals.get), [])

    def test_empty_and_bad_reported(self):
        vals = {"nurse_group_id": "", "alert_recipients": UID, "physician_line_id": UID[:-2]}
        probs = sg.check_all(vals.get)
        self.assertEqual(len(probs), 2)
        self.assertTrue(any("nurse_group_id" in p and "ว่าง" in p for p in probs))
        self.assertTrue(any("physician_line_id" in p and "ผิดรูปแบบ" in p for p in probs))
        msg = sg.physician_warning(probs)
        self.assertIn("🚨", msg)
        self.assertIn("⚙️ ตั้งค่าระบบ", msg)
        self.assertEqual(sg.physician_warning([]), "")


class TestUpdateSettingsApi(unittest.TestCase):
    """POST /hd/api/v1/settings ปฏิเสธค่าว่าง/ผิดรูปแบบของค่าสำคัญ ก่อนแตะ DB."""

    def test_guard_rejects_before_db(self):
        # เรียกฟังก์ชันตรง (ข้าม decorator) ใน request context
        from flask import Flask
        from hd_module import blueprint as bp
        app = Flask(__name__)
        fn = bp.update_settings.__wrapped__ if hasattr(bp.update_settings, "__wrapped__") else None
        if fn is None:
            self.skipTest("decorator ไม่เปิด __wrapped__ — ทดสอบผ่าน settings_guard แทน")
        import sys
        fake_pg = mock.MagicMock()
        with mock.patch.dict(sys.modules, {"psycopg2": fake_pg}), \
             mock.patch.dict("os.environ", {"HD_DATABASE_URL": "postgresql://x"}):
            with app.test_request_context(json={"key": "nurse_group_id", "value": ""}):
                resp, code = fn()
                self.assertEqual(code, 400)
                self.assertEqual(resp.get_json()["code"], "empty_critical")
            with app.test_request_context(json={"key": "nurse_group_id", "value": GID[:-1]}):
                resp, code = fn()
                self.assertEqual(code, 400)
                self.assertEqual(resp.get_json()["code"], "invalid_format")
            with app.test_request_context(json={"key": "alert_recipients", "value": "Uabc, " + UID}):
                resp, code = fn()
                self.assertEqual(code, 400)
            fake_pg.connect.assert_not_called()
            # confirm_clear:true → ผ่าน guard ไปถึง DB (mock)
            with app.test_request_context(json={"key": "nurse_group_id", "value": "", "confirm_clear": True}):
                with mock.patch.object(bp, "log_action"):
                    out = fn()
                resp = out[0] if isinstance(out, tuple) else out
                self.assertEqual(resp.get_json().get("status"), "saved")
            fake_pg.connect.assert_called_once()


class TestRelayFailureDetail(unittest.TestCase):
    def test_hint_for_missing_group(self):
        from flask import Flask
        from hd_module import blueprint as bp
        with Flask(__name__).app_context():
            d = bp._relay_failure_detail("ยังไม่ตั้ง nurse_group_id")
        self.assertIn("⚙️", d)

    def test_hint_for_line_400_to_invalid(self):
        from flask import Flask
        from hd_module import blueprint as bp, ops_status as ops
        ops.line_result(False, 400, '{"message":"The property, \'to\', in the request body is invalid"}')
        with Flask(__name__).app_context():
            d = bp._relay_failure_detail("LINE API ปฏิเสธ")
        self.assertIn("LINE ตอบ", d)
        self.assertIn("C + 32", d)

    def test_notify_physician_rate_limited(self):
        from flask import Flask
        from hd_module import blueprint as bp
        app = Flask(__name__)
        sent = []
        app.config["LINE_PUSH_FN"] = lambda uid, msg: sent.append((uid, msg)) or True
        bp._RELAY_FAIL_NOTIFIED.clear()
        with app.app_context(), mock.patch.object(bp, "get_setting_value", lambda k, d="": UID if k == "physician_line_id" else ""):
            self.assertTrue(bp._notify_physician_relay_failed(5, "65249", "ยังไม่ตั้ง nurse_group_id"))
            self.assertFalse(bp._notify_physician_relay_failed(5, "65249", "x"))   # ภายใน 10 นาที ไม่ส่งซ้ำ
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], UID)
        self.assertIn("เตียง 5", sent[0][1])
        self.assertIn("ยังไม่ถึง LINE กลุ่มพยาบาล", sent[0][1])
        bp._RELAY_FAIL_NOTIFIED.clear()


class TestOpsSnapshotConfigProblems(unittest.TestCase):
    def test_config_problems_surface_in_problems(self):
        from hd_module import ops_status as ops
        ops.set_config_problems(["⚙️ nurse_group_id ว่าง"])
        s = ops.snapshot()
        self.assertIn("⚙️ nurse_group_id ว่าง", s["problems"])
        self.assertEqual(s["config_problems"], ["⚙️ nurse_group_id ว่าง"])
        self.assertFalse(s["healthy"])
        ops.set_config_problems([])
        self.assertEqual(ops.snapshot()["config_problems"], [])


if __name__ == "__main__":
    unittest.main()


class TestOpsSharedState:
    """27 พ.ย. 2569: /health บน worker ที่ไม่รัน scheduler ต้องไม่รายงาน 'scheduler ยังไม่เริ่ม' (issue #29/#30 สัญญาณหลอก)"""

    def test_snapshot_reads_scheduler_state_from_shared_file(self, tmp_path):
        from hd_module import ops_status as ops
        orig = ops._SHARED_PATH
        ops._SHARED_PATH = str(tmp_path / "ops.json")
        try:
            ops.tick()
            ops.job_done("lab_sync", True, "ok", None)
            s0 = ops.snapshot()
            assert not any("scheduler" in x for x in s0["problems"]) and s0["scheduler_in_this_worker"]
            with ops._LOCK:
                ops._STATE.update({"last_tick": None, "started_at": None, "tick_count": 0, "jobs": {}})
            snap = ops.snapshot()
            assert "scheduler ยังไม่เริ่ม" not in snap["problems"] and not snap["scheduler_in_this_worker"]
            assert "lab_sync" in snap["jobs"] and snap["tick_age_sec"] is not None
            (tmp_path / "ops.json").unlink()
            assert "scheduler ยังไม่เริ่ม" in ops.snapshot()["problems"]
        finally:
            ops._SHARED_PATH = orig
