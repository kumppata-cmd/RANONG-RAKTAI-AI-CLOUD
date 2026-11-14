import time
import unittest
from datetime import timedelta

from hd_module import ops_status as ops


class OpsStatusTests(unittest.TestCase):
    def setUp(self):
        with ops._LOCK:
            ops._STATE.update({"started_at": None, "last_tick": None, "tick_count": 0, "restarts": 0, "jobs": {},
                               "line": {"last_ok": None, "last_error": None, "last_error_at": None, "ok_count": 0,
                                        "fail_count": 0, "quota": None}})

    def test_not_started_is_a_problem(self):
        s = ops.snapshot()
        self.assertFalse(s["healthy"])
        self.assertIn("scheduler ยังไม่เริ่ม", s["problems"])

    def test_tick_makes_it_healthy(self):
        ops.tick()
        s = ops.snapshot()
        self.assertTrue(s["healthy"])
        self.assertLessEqual(s["tick_age_sec"], 2)

    def test_stale_tick_detected(self):
        ops.tick()
        with ops._LOCK:
            ops._STATE["last_tick"] = ops._iso(ops._now() - timedelta(minutes=45))
        s = ops.snapshot()
        self.assertFalse(s["healthy"])
        self.assertTrue(any("ไม่ทำงานมา" in p for p in s["problems"]))

    def test_run_guarded_records_result_and_errors(self):
        ops.tick()
        r = ops.run_guarded("a", lambda: {"sent": 3})
        self.assertEqual(r, {"sent": 3})
        self.assertTrue(ops.snapshot()["jobs"]["a"]["ok"])
        self.assertIn("sent=3", ops.snapshot()["jobs"]["a"]["detail"])

        def boom():
            raise RuntimeError("x")
        self.assertIsNone(ops.run_guarded("b", boom))
        j = ops.snapshot()["jobs"]["b"]
        self.assertFalse(j["ok"])
        self.assertIn("RuntimeError", j["detail"])
        self.assertTrue(ops.snapshot()["healthy"])   # a failed job is reported, not fatal

    def test_run_guarded_timeout_does_not_block(self):
        ops.tick()
        t0 = time.time()
        r = ops.run_guarded("slow", lambda: time.sleep(2) or "late", timeout=0.2)
        self.assertIsNone(r)
        self.assertLess(time.time() - t0, 1.5)
        self.assertIsNotNone(ops.snapshot()["jobs"]["slow"]["running_since"])

    def test_line_failure_and_quota(self):
        ops.set_reserve(60)
        ops.tick()
        ops.line_result(False, 429, '{"message":"You have reached your monthly limit."}')
        s = ops.snapshot()
        self.assertFalse(s["healthy"])
        self.assertTrue(any("LINE ส่งไม่สำเร็จ" in p for p in s["problems"]))
        ops.line_result(True)
        self.assertTrue(ops.snapshot()["healthy"])
        ops.line_quota({"type": "limited", "value": 200, "used": 200})
        s = ops.snapshot()
        self.assertTrue(s["line"]["quota_exhausted"])
        self.assertTrue(any(p.startswith("LINE ครบโควตาข้อความรายเดือน") for p in s["problems"]))
        self.assertIn("200/200", ops.line_text(s))

    def test_check_line_quota_uses_api(self):
        class R:
            def __init__(self, d):
                self.d = d

            def json(self):
                return self.d

        class H:
            def get(self, url, headers=None, timeout=None):
                return R({"type": "limited", "value": 200} if url.endswith("/quota") else {"totalUsage": 57})
        info = ops.check_line_quota("tok", http=H())
        self.assertEqual(info, {"type": "limited", "value": 200, "used": 57})
        self.assertIsNone(ops.check_line_quota("", http=H()))


class QuotaGuardTests(unittest.TestCase):
    def setUp(self):
        with ops._LOCK:
            ops._STATE["line"] = {"last_ok": None, "last_error": None, "last_error_at": None, "ok_count": 0,
                                  "fail_count": 0, "quota": None}
            ops._STATE["reserve"] = 60

    def test_unknown_quota_allows(self):
        self.assertTrue(ops.allow_push())

    def test_low_quota_blocks_informational_but_not_critical(self):
        ops.line_quota({"type": "limited", "value": 300, "used": 250})
        self.assertFalse(ops.allow_push())
        with ops.critical():
            self.assertTrue(ops.allow_push())
        self.assertFalse(ops.allow_push())
        self.assertEqual(ops.snapshot()["line"]["suppressed"], 2)
        self.assertTrue(any("ใกล้หมด" in p for p in ops.snapshot()["problems"]))

    def test_successful_push_counts_against_cached_quota(self):
        ops.line_quota({"type": "limited", "value": 300, "used": 238})
        self.assertTrue(ops.allow_push())
        ops.line_result(True)
        ops.line_result(True)
        self.assertEqual(ops.quota_remaining(), 60)
        self.assertFalse(ops.allow_push())

    def test_reserve_setting(self):
        ops.line_quota({"type": "limited", "value": 300, "used": 250})
        ops.set_reserve(10)
        self.assertTrue(ops.allow_push())


if __name__ == "__main__":
    unittest.main()
