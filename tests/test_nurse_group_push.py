"""Nurse group via Messaging API (replaces LINE Notify) + Drive uploader composition."""
import unittest
from unittest import mock

from flask import Flask

from hd_module import blueprint as bp
from hd_module import daily_records as dr


class NurseGroupPushTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.sent = []
        self.app.config["LINE_PUSH_FN"] = lambda to, msg: (self.sent.append((to, msg)) or (to != "Cbad"))

    def _settings(self, **kv):
        return lambda key, default="": kv.get(key, default)

    def test_group_id_set_pushes_to_group(self):
        with self.app.app_context(), mock.patch.object(bp, "get_setting_value", self._settings(nurse_group_id="Cgroup1", alert_recipients="U1,U2")):
            r = bp.push_nurse_group("hello")
        self.assertEqual(r["method"], "group"); self.assertTrue(r["sent"])
        self.assertEqual(self.sent, [("Cgroup1", "hello")])

    def test_no_group_falls_back_to_recipients(self):
        with self.app.app_context(), mock.patch.object(bp, "get_setting_value", self._settings(alert_recipients="U1, U2")):
            r = bp.push_nurse_group("x")
        self.assertEqual(r["method"], "push"); self.assertEqual(r["count"], 2)
        with self.app.app_context(), mock.patch.object(bp, "get_setting_value", self._settings(alert_recipients="U1")):
            r = bp.push_nurse_group("x", fallback_recipients=False)
        self.assertFalse(r["sent"]); self.assertIn("nurse_group_id", r["error"])

    def test_group_rejected_reports_error_without_fallback(self):
        with self.app.app_context(), mock.patch.object(bp, "get_setting_value", self._settings(nurse_group_id="Cbad", alert_recipients="U1")):
            r = bp.push_nurse_group("x", fallback_recipients=False)
        self.assertFalse(r["sent"]); self.assertEqual(r["method"], "group")

    def test_no_line_notify_left_in_hd_module(self):
        import pathlib
        src = pathlib.Path(bp.__file__).read_text(encoding="utf-8")
        self.assertNotIn("notify-api.line.me", src)


class CombinedUploadTests(unittest.TestCase):
    def test_drive_failure_is_reported_but_gcs_counts(self):
        def gcs(p, d, c): pass
        def drive(p, d, c): raise RuntimeError("403")
        up = dr.combined_upload_fn(gcs, drive)
        self.assertEqual(up("a", b"x", "t")["gcs"], True)
        self.assertIn("403", up("a", b"x", "t")["drive"])
        m = dr.export_day(None, __import__("datetime").date(2026, 11, 12), [], b"csv", up)
        self.assertEqual((m["ok"], m["failed"]), (1, 0))
        self.assertIn("drive", m["files"][0]["error"])


if __name__ == "__main__":
    unittest.main()
