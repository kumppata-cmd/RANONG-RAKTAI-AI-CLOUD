"""B11: CI failure endpoint requires the shared token and pushes LINE."""
import os
import unittest
from unittest import mock

from flask import Flask

from hd_module import blueprint as bp


class CiStatusTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__); self.app.register_blueprint(bp.hd_bp)
        self.sent = []
        self.app.config["LINE_PUSH_FN"] = lambda u, m: self.sent.append((u, m)) or True
        self.c = self.app.test_client()

    def test_rejects_without_token(self):
        with mock.patch.dict(os.environ, {"CI_NOTIFY_TOKEN": "abc"}):
            self.assertEqual(self.c.post("/hd/api/v1/ops/ci-status", json={"sha": "x"}).status_code, 401)
            self.assertEqual(self.c.post("/hd/api/v1/ops/ci-status", json={}, headers={"X-CI-Token": "wrong"}).status_code, 401)
        with mock.patch.dict(os.environ, {"CI_NOTIFY_TOKEN": ""}):
            self.assertEqual(self.c.post("/hd/api/v1/ops/ci-status", json={}, headers={"X-CI-Token": ""}).status_code, 401)

    def test_pushes_on_valid_token(self):
        with mock.patch.dict(os.environ, {"CI_NOTIFY_TOKEN": "abc"}), \
             mock.patch.object(bp, "get_setting_value", lambda k, d="": "U1" if k == "alert_recipients" else d), \
             mock.patch.object(bp, "log_action", lambda **kw: None):
            r = self.c.post("/hd/api/v1/ops/ci-status", headers={"X-CI-Token": "abc"},
                            json={"sha": "f3c7ca0abcd", "branch": "main", "failed_jobs": "test", "run_url": "https://gh/run/1", "message": "feat: x"})
        self.assertEqual(r.status_code, 200); self.assertEqual(r.get_json()["sent"], 1)
        self.assertIn("CI ล้ม", self.sent[0][1]); self.assertIn("f3c7ca0", self.sent[0][1]); self.assertIn("https://gh/run/1", self.sent[0][1])


if __name__ == "__main__":
    unittest.main()
