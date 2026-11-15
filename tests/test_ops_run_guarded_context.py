"""run_guarded(timeout=…) must run the job with the caller's Flask app context.

Regression: 14–15 Nov 2026 every timed scheduler job (lab_sync, deploy_announce,
bed_plan_push, db_backup …) failed with "Working outside of application context"
because the helper thread did not inherit the context."""
import unittest

from flask import Flask, current_app

from hd_module import ops_status as ops


class RunGuardedContextTests(unittest.TestCase):
    def test_timed_job_sees_app_context(self):
        app = Flask("ctx-test")
        app.config["MARK"] = "ok-123"
        with app.app_context():
            r = ops.run_guarded("ctx_job", lambda: current_app.config["MARK"], timeout=5)
        self.assertEqual(r, "ok-123")

    def test_timed_job_without_app_context_still_runs(self):
        self.assertEqual(ops.run_guarded("plain_job", lambda x: x * 2, 21, timeout=5), 42)

    def test_failure_is_recorded_not_raised(self):
        def boom():
            raise RuntimeError("x")
        app = Flask("ctx-test2")
        with app.app_context():
            self.assertIsNone(ops.run_guarded("boom_job", boom, timeout=5))


if __name__ == "__main__":
    unittest.main()
