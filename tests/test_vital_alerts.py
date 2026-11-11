"""Tests for hd_module.vital_alerts — detection + LINE message + dedupe."""
import unittest

from hd_module import vital_alerts as va


class EvaluateTests(unittest.TestCase):
    def sev(self, alerts, t):
        return [a["severity"] for a in alerts if a["alert_type"] == t]

    def test_sbp_80_is_critical(self):
        a = va.evaluate(sbp=80, dbp=40, hr=60)
        self.assertEqual(self.sev(a, "hypotension"), ["critical"])
        self.assertIn("ความดันต่ำวิกฤต", a[0]["title"])
        self.assertIn("80/40", a[0]["title"])

    def test_severe_threshold_from_settings(self):
        self.assertEqual(self.sev(va.evaluate(sbp=95, severe_sbp=100), "hypotension"), ["critical"])
        self.assertEqual(self.sev(va.evaluate(sbp=95, severe_sbp=90), "hypotension"), [])

    def test_idh_drop_from_pre_hd(self):
        a = va.evaluate(sbp=120, pre_sbp=150)          # drop 30 ≥ 20
        self.assertEqual(self.sev(a, "idh"), ["warning"])
        self.assertIn("150 → 120", a[0]["message"])
        self.assertEqual(va.evaluate(sbp=140, pre_sbp=150), [])   # drop 10 < 20

    def test_fast_drop_from_previous(self):
        a = va.evaluate(sbp=120, pre_sbp=125, prev_sbp=155)
        self.assertEqual(self.sev(a, "idh"), ["warning"])

    def test_critical_wins_over_idh(self):
        a = va.evaluate(sbp=85, pre_sbp=150)
        self.assertEqual([x["alert_type"] for x in a], ["hypotension"])

    def test_hypertension_hr_spo2_temp(self):
        a = va.evaluate(sbp=205, hr=145, spo2=85, temp=39.2)
        self.assertEqual(self.sev(a, "hypertension"), ["critical"])
        self.assertEqual(self.sev(a, "heart_rate"), ["critical"])
        self.assertEqual(self.sev(a, "spo2"), ["critical"])
        self.assertEqual(self.sev(a, "fever"), ["critical"])
        b = va.evaluate(sbp=185, hr=125, spo2=90, temp=38.2)
        self.assertEqual({x["severity"] for x in b}, {"warning"})

    def test_normal_and_garbage_input(self):
        self.assertEqual(va.evaluate(sbp=130, dbp=80, hr=72, spo2=98, temp=36.8), [])
        self.assertEqual(va.evaluate(sbp="", dbp=None, hr="abc"), [])
        self.assertEqual(va.evaluate(sbp="80"), va.evaluate(sbp=80))


class LineMessageTests(unittest.TestCase):
    def test_message_has_name_hn_bed_cause_values_link(self):
        ctx = {"session_id": 77, "bed": 3, "hn": "67398", "name": "นางจารีก แก้วมีศรี"}
        a = va.evaluate(sbp=80, dbp=40)[0]
        m = va.build_line_message(ctx, a, "https://x.test")
        for part in ["🔴", "วิกฤต", "นางจารีก แก้วมีศรี", "HN 67398", "เตียง 3",
                     "สาเหตุ: ความดันต่ำวิกฤต", "SBP 80 < 90", "https://x.test/hd/session/77#alerts"]:
            self.assertIn(part, m)

    def test_notify_pushes_once_per_cooldown(self):
        va._LAST_PUSH.clear()
        sent = []
        push = lambda uid, msg: sent.append((uid, msg))
        ctx = {"session_id": 1, "bed": 1, "hn": "1", "name": "A"}
        alerts = va.evaluate(sbp=80)
        self.assertEqual(va.notify_physician(push, ["U1", "U2"], ctx, alerts, "https://x"), 1)
        self.assertEqual(len(sent), 2)                       # both recipients
        self.assertEqual(va.notify_physician(push, ["U1"], ctx, alerts, "https://x"), 0)  # cooldown
        self.assertEqual(va.notify_physician(None, ["U1"], ctx, alerts, "https://x"), 0)
        va._LAST_PUSH.clear()

    def test_push_failure_does_not_raise(self):
        va._LAST_PUSH.clear()
        def bad(uid, msg):
            raise RuntimeError("LINE down")
        ctx = {"session_id": 2, "bed": 1, "hn": "1", "name": "A"}
        self.assertEqual(va.notify_physician(bad, ["U1"], ctx, va.evaluate(sbp=80), "https://x"), 0)


class StoreDedupeTests(unittest.TestCase):
    class Cur:
        def __init__(self, existing=None):
            self.existing = existing   # row returned for the dedupe SELECT
            self.inserts = []
            self._next = 100
        def execute(self, sql, params=None):
            self.last = sql
            if sql.strip().startswith("INSERT"):
                self.inserts.append(params)
        def fetchone(self):
            if self.last.strip().startswith("INSERT"):
                self._next += 1
                return (self._next,)
            return self.existing

    def test_inserts_new_alert(self):
        cur = self.Cur(existing=None)
        out = va.store_alerts(cur, 5, 9, va.evaluate(sbp=80))
        self.assertEqual(len(cur.inserts), 1)
        self.assertEqual(cur.inserts[0][:4], (5, 9, "hypotension", "critical"))
        self.assertEqual(out[0]["id"], 101)

    def test_skips_duplicate_but_allows_escalation(self):
        cur = self.Cur(existing=(1, "critical"))
        self.assertEqual(va.store_alerts(cur, 5, 9, va.evaluate(sbp=80)), [])
        cur = self.Cur(existing=(1, "warning"))
        self.assertEqual(len(va.store_alerts(cur, 5, 9, va.evaluate(sbp=80))), 1)   # warning → critical


if __name__ == "__main__":
    unittest.main()
