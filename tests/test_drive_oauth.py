"""Google Drive: OAuth-as-user mode selection and quota-error explanation."""
import os
import unittest
from unittest import mock

import google_drive_sync as gds


class DriveModeTests(unittest.TestCase):
    def test_mode_service_account_without_oauth(self):
        with mock.patch.object(gds, "OAUTH_CLIENT_ID", ""), mock.patch.object(gds, "OAUTH_CLIENT_SECRET", ""):
            self.assertEqual(gds.drive_mode(), "service_account")
            self.assertFalse(gds.oauth_configured())

    def test_mode_oauth_when_client_and_token(self):
        with mock.patch.object(gds, "OAUTH_CLIENT_ID", "cid"), mock.patch.object(gds, "OAUTH_CLIENT_SECRET", "sec"):
            gds.set_refresh_token_getter(lambda: "rt-123")
            try:
                self.assertEqual(gds.drive_mode(), "oauth")
                self.assertTrue(gds.oauth_configured())
            finally:
                gds.set_refresh_token_getter(None)
            self.assertEqual(gds.drive_mode(), "service_account")   # no token → SA

    def test_env_refresh_token_wins(self):
        with mock.patch.dict(os.environ, {"GOOGLE_DRIVE_REFRESH_TOKEN": "env-rt"}):
            self.assertEqual(gds._oauth_refresh_token(), "env-rt")


if __name__ == "__main__":
    unittest.main()
