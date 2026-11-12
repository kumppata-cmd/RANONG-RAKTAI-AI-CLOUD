"""Once-per-day guards: HD scheduler marker + server daily digest marker."""
import unittest
from datetime import date
from types import SimpleNamespace


class _Err(Exception):
    def __init__(self, code):
        super().__init__(code); self.code = code


class _Blob:
    def __init__(self, store, name): self.store, self.name = store, name
    def upload_from_string(self, data, content_type=None, if_generation_match=None):
        if if_generation_match == 0 and self.name in self.store:
            raise _Err(412)
        self.store[self.name] = data


class _Bucket:
    def __init__(self): self.store = {}
    def blob(self, name): return _Blob(self.store, name)


class HdClaimOnceTests(unittest.TestCase):
    def test_second_claim_same_day_is_refused(self):
        from hd_module import _claim_once
        app = SimpleNamespace(config={"STORAGE_OBJ": SimpleNamespace(_bucket=_Bucket())})
        d = date(2026, 11, 12)
        self.assertTrue(_claim_once(app, "daily_export", d))
        self.assertFalse(_claim_once(app, "daily_export", d))          # other worker / restart
        self.assertTrue(_claim_once(app, "daily_export", date(2026, 11, 13)))
        self.assertTrue(_claim_once(app, "pre_hd_reminder", d))        # different job

    def test_no_gcs_allows_run(self):
        from hd_module import _claim_once
        app = SimpleNamespace(config={})
        self.assertTrue(_claim_once(app, "daily_export", date(2026, 11, 12)))


if __name__ == "__main__":
    unittest.main()
