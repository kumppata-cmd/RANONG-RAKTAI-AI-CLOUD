"""Regression tests for the critical alert delivery pipeline.

ครอบข้อบกพร่อง P0 สี่ข้อจาก CLINICAL_SAFETY_GAP_ANALYSIS:

  CS-1 ไม่มี driver — retry/escalation ไม่เคยทำงาน
  CS-2 acknowledge_alert ไม่มี call site — แพทย์รับทราบไม่ได้เลย
  CS-3 escalation ผูกกับ delivery failure เท่านั้น ไม่มี ack timeout
  CS-4 ไม่มีช่องทางส่ง → mark SENT ทั้งที่ไม่ได้ส่งอะไร (fail-open)

ทุกเคสใช้ข้อมูลสังเคราะห์ ไม่มีข้อมูลผู้ป่วยจริง
"""
import json
import re
import unittest
from datetime import datetime, timedelta

from alert_delivery import (
    AlertDeliveryService,
    AlertRetryManager,
    AlertState,
    CriticalAlert,
    format_active_alerts,
)


class MockStorage:
    """Storage ในหน่วยความจำ — เลียนแบบ interface ของ GCSStorage"""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put(self, path, data, content_type=None):
        self.objects[path] = data
        return True

    def get(self, path):
        return self.objects.get(path)

    def list_files(self, prefix=""):
        return [k for k in self.objects if k.startswith(prefix)]


class MockAudit:
    def __init__(self):
        self.entries = []

    def log(self, action, actor, details=None):
        self.entries.append((action, actor, details or {}))
        return True

    def actions(self):
        return [a for a, _, _ in self.entries]


def make_service(storage=None, audit=None, simulate=False, recipients=None):
    return AlertDeliveryService(
        storage or MockStorage(),
        audit or MockAudit(),
        clinician_recipients=recipients if recipients is not None
        else {"physician": ["U_PHYS_TEST"]},
        allow_simulated_delivery=simulate,
    )


def new_alert(service, doc_id="DOC_T1", analyte="Potassium"):
    return service.create_alert(
        doc_id=doc_id, owner_hash="owner_test",
        analyte=analyte, value=7.5, unit="mEq/L",
        alert_level="critical_high",
    )


# ═══════════════════════════════════════════════════════════
# CS-4 — fail-open: ต้องไม่บันทึกว่าส่งแล้วถ้าไม่มีช่องทางส่ง
# ═══════════════════════════════════════════════════════════

class TestCS4FailClosedDelivery(unittest.TestCase):

    def test_no_sender_must_not_mark_sent(self):
        """ไม่มี send_push_fn และไม่ได้อนุญาต simulate → ต้องไม่เป็น SENT

        นี่คือหัวใจของ CS-4: เดิมเมื่อ LINE_CHANNEL_ACCESS_TOKEN ไม่ได้
        ตั้ง ระบบจะบันทึก alert วิกฤตว่า SENT และ audit log ว่า
        alert_sent ทั้งที่ไม่มีใครได้รับอะไรเลย
        """
        storage, audit = MockStorage(), MockAudit()
        svc = make_service(storage, audit, simulate=False)
        alert = new_alert(svc)

        result = svc.attempt_delivery(alert.alert_id)  # ไม่ส่ง send_push_fn

        self.assertFalse(result.get("success"))
        stored = svc._load_alert(alert.alert_id)
        self.assertEqual(stored.state, AlertState.DELIVERY_FAILED.value)
        self.assertNotIn("alert_sent", audit.actions())

    def test_no_sender_records_specific_reason(self):
        """ต้องบันทึกเหตุผลที่เจาะจง เพื่อให้ debug ได้ว่าไม่มี sender"""
        svc = make_service(simulate=False)
        alert = new_alert(svc)
        svc.attempt_delivery(alert.alert_id)

        stored = svc._load_alert(alert.alert_id)
        self.assertEqual(stored.last_delivery_error, "no_sender_configured")

    def test_simulate_requires_explicit_optin(self):
        """โหมดทดสอบต้อง opt-in ชัดเจน จึงจะถือว่าส่งสำเร็จได้"""
        svc = make_service(simulate=True)
        alert = new_alert(svc)
        result = svc.attempt_delivery(alert.alert_id)

        self.assertTrue(result.get("success"))
        self.assertEqual(
            svc._load_alert(alert.alert_id).state, AlertState.SENT.value
        )

    def test_default_is_fail_closed(self):
        """ค่าเริ่มต้นของ constructor ต้องเป็น fail-closed"""
        svc = AlertDeliveryService(MockStorage(), MockAudit(),
                                   clinician_recipients={"physician": ["U"]})
        self.assertFalse(svc._allow_simulated_delivery)

    def test_real_sender_failure_still_fails(self):
        """sender ที่คืน False ต้องถือว่าส่งไม่สำเร็จ (พฤติกรรมเดิม)"""
        svc = make_service(simulate=False)
        alert = new_alert(svc)
        svc.attempt_delivery(alert.alert_id,
                             send_push_fn=lambda uid, msg: False)
        self.assertEqual(
            svc._load_alert(alert.alert_id).state,
            AlertState.DELIVERY_FAILED.value,
        )

    def test_error_reason_reflects_current_attempt(self):
        """เหตุผลล้มเหลวต้องเป็นของความพยายามครั้งล่าสุด ไม่ค้างของเก่า"""
        svc = make_service(simulate=False)
        alert = new_alert(svc)
        # ครั้งแรก: ไม่มี sender
        svc.attempt_delivery(alert.alert_id)
        self.assertEqual(
            svc._load_alert(alert.alert_id).last_delivery_error,
            "no_sender_configured",
        )
        # ครั้งที่สอง: มี sender แต่ล้มเหลว → เหตุผลต้องเปลี่ยน
        svc.attempt_delivery(alert.alert_id,
                             send_push_fn=lambda uid, msg: False)
        self.assertEqual(
            svc._load_alert(alert.alert_id).last_delivery_error,
            "LINE push failed",
        )


# ═══════════════════════════════════════════════════════════
# CS-3 — escalation เมื่อไม่มีใครรับทราบ
# ═══════════════════════════════════════════════════════════

class TestCS3AckTimeoutEscalation(unittest.TestCase):

    def _sent_alert(self, svc, sent_secs_ago, ack_timeout=900):
        alert = new_alert(svc)
        alert.state = AlertState.SENT.value
        alert.sent_at = (
            datetime.now() - timedelta(seconds=sent_secs_ago)
        ).isoformat(timespec="seconds")
        alert.ack_timeout_sec = ack_timeout
        svc._save_alert(alert)
        return alert

    def test_overdue_ack_is_detected(self):
        """ส่งถึงแล้วเกินเวลารับทราบ → ต้องเข้าเงื่อนไข escalate"""
        svc = make_service(simulate=True)
        alert = self._sent_alert(svc, sent_secs_ago=1200, ack_timeout=900)
        self.assertTrue(alert.should_escalate_unacknowledged())

    def test_within_window_is_not_escalated(self):
        """ยังอยู่ในเวลา → ต้องไม่ escalate (กันเตือนซ้ำเร็วเกินไป)"""
        svc = make_service(simulate=True)
        alert = self._sent_alert(svc, sent_secs_ago=60, ack_timeout=900)
        self.assertFalse(alert.should_escalate_unacknowledged())

    def test_acknowledged_alert_never_escalates_on_timeout(self):
        """รับทราบแล้วต้องไม่ escalate แม้เวลาผ่านไปนาน"""
        svc = make_service(simulate=True)
        alert = self._sent_alert(svc, sent_secs_ago=99999)
        alert.state = AlertState.ACKNOWLEDGED.value
        self.assertFalse(alert.should_escalate_unacknowledged())

    def test_zero_timeout_disables_check(self):
        """ack_timeout_sec = 0 ต้องปิดการตรวจ (ไว้ให้ปรับต่อรายเคส)"""
        svc = make_service(simulate=True)
        alert = self._sent_alert(svc, sent_secs_ago=99999, ack_timeout=0)
        self.assertFalse(alert.should_escalate_unacknowledged())

    def test_retry_manager_escalates_overdue_ack(self):
        """[CS-3 ตัวจริง] driver ต้องเปลี่ยนสถานะเป็น ESCALATED

        เดิม should_escalate() ผูกกับ DELIVERY_FAILED เท่านั้น
        alert ที่ 'ส่งถึงแต่ไม่มีใครดู' จึงไม่เคย escalate
        """
        storage, audit = MockStorage(), MockAudit()
        svc = make_service(storage, audit, simulate=True)
        alert = self._sent_alert(svc, sent_secs_ago=1200, ack_timeout=900)

        mgr = AlertRetryManager(svc)
        mgr._retry_interval = 0
        pushed = []
        results = mgr.check_and_retry(
            send_push_fn=lambda uid, msg: pushed.append(msg) or True
        )

        stored = svc._load_alert(alert.alert_id)
        self.assertEqual(stored.state, AlertState.ESCALATED.value)
        self.assertEqual(stored.escalation_reason, "acknowledgement_overdue")
        self.assertTrue(
            any(r.get("reason") == "acknowledgement_overdue" for r in results)
        )
        self.assertIn("alert_escalated", audit.actions())

    def test_escalation_message_has_no_patient_identifiers(self):
        """ข้อความ escalation ต้องไม่มีชื่อผู้ป่วยหรือค่าผลตรวจ"""
        svc = make_service(simulate=True)
        alert = self._sent_alert(svc, sent_secs_ago=1200)
        mgr = AlertRetryManager(svc)
        mgr._retry_interval = 0
        pushed = []
        mgr.check_and_retry(
            send_push_fn=lambda uid, msg: pushed.append(msg) or True
        )
        self.assertTrue(pushed, "ต้องมีการส่งข้อความ escalation")
        joined = "\n".join(pushed)
        self.assertNotIn("owner_test", joined)
        self.assertNotIn("7.5", joined)

    def test_escalation_message_tells_how_to_acknowledge(self):
        """escalation ต้องบอกวิธีรับทราบ ไม่ใช่แจ้งแล้วจบ"""
        svc = make_service(simulate=True)
        alert = self._sent_alert(svc, sent_secs_ago=1200)
        mgr = AlertRetryManager(svc)
        mgr._retry_interval = 0
        pushed = []
        mgr.check_and_retry(
            send_push_fn=lambda uid, msg: pushed.append(msg) or True
        )
        self.assertIn("รับทราบ", "\n".join(pushed))


# ═══════════════════════════════════════════════════════════
# CS-2 — แพทย์ต้องรับทราบได้จริง
# ═══════════════════════════════════════════════════════════

class TestCS2Acknowledgment(unittest.TestCase):

    def test_notification_tells_clinician_how_to_acknowledge(self):
        """ข้อความแจ้งเตือนบอกให้ตอบ 'รับทราบ <รหัส>'"""
        svc = make_service(simulate=True)
        alert = new_alert(svc)
        self.assertIn("รับทราบ", alert.notification_text)
        self.assertIn(alert.alert_id[:8], alert.notification_text)

    def test_prefix_from_notification_resolves_to_full_id(self):
        """[หัวใจ CS-2] รหัสที่แสดงให้แพทย์ต้องแปลงกลับเป็น alert_id ได้

        เดิมข้อความบอกให้แพทย์ตอบ 'รับทราบ <8 ตัวแรก>' แต่ไม่มีทาง
        แปลงกลับ และไม่มี handler รับคำสั่ง — คำตอบของแพทย์ถูกทิ้งเงียบ
        """
        svc = make_service(simulate=True)
        alert = new_alert(svc)
        svc.attempt_delivery(alert.alert_id)

        # ดึงรหัสจากข้อความจริงที่ส่งให้แพทย์
        m = re.search(r"รับทราบ ([0-9a-f]{8})", alert.notification_text)
        self.assertIsNotNone(m, "ข้อความต้องมีรหัสให้แพทย์ใช้")
        shown = m.group(1)

        resolved = svc.find_alert_id_by_prefix(shown)
        self.assertEqual(resolved, alert.alert_id)

    def test_acknowledge_via_resolved_prefix_succeeds(self):
        """เส้นทางครบวงจร: รหัสจากข้อความ → รับทราบสำเร็จ"""
        storage, audit = MockStorage(), MockAudit()
        svc = make_service(storage, audit, simulate=True)
        alert = new_alert(svc)
        svc.attempt_delivery(alert.alert_id)

        full = svc.find_alert_id_by_prefix(alert.alert_id[:8])
        result = svc.acknowledge_alert(full, "phys_hash_001")

        self.assertEqual(result.get("status"), "acknowledged")
        self.assertEqual(
            svc._load_alert(alert.alert_id).state,
            AlertState.ACKNOWLEDGED.value,
        )
        self.assertIn("alert_acknowledged", audit.actions())

    def test_unknown_prefix_returns_none(self):
        svc = make_service(simulate=True)
        new_alert(svc)
        self.assertIsNone(svc.find_alert_id_by_prefix("ffffffff"))

    def test_too_short_prefix_rejected(self):
        """คำนำหน้าสั้นเกินไปต้องไม่ถูกเดา — นี่คือการรับทราบผลวิกฤต"""
        svc = make_service(simulate=True)
        new_alert(svc)
        self.assertIsNone(svc.find_alert_id_by_prefix("ab"))
        self.assertIsNone(svc.find_alert_id_by_prefix(""))

    def test_ambiguous_prefix_refuses_to_guess(self):
        """ถ้าคำนำหน้าตรงหลายรายการ ต้องไม่เดาว่าเป็นอันไหน"""
        storage = MockStorage()
        svc = make_service(storage, simulate=True)
        for suffix in ("aa", "bb"):
            a = CriticalAlert(
                alert_id="dupprefix" + suffix, doc_id="D", owner_hash="o",
                analyte="K", value=7.0, unit="mEq/L",
                alert_level="critical_high",
            )
            svc._save_alert(a)
        self.assertIsNone(svc.find_alert_id_by_prefix("dupprefix"))

    def test_active_alert_listing_shows_the_ack_code(self):
        """รายการแจ้งเตือนต้องแสดงรหัสที่ใช้รับทราบได้

        เดิมแสดงแต่ doc_id ทำให้แพทย์ไม่มีรหัสที่ใช้ตอบกลับ
        """
        svc = make_service(simulate=True)
        alert = new_alert(svc)
        svc.attempt_delivery(alert.alert_id)

        text = format_active_alerts(svc.get_active_alerts())
        self.assertIn(alert.alert_id[:8], text)

    def test_resolve_requires_acknowledge_first(self):
        """ต้องรับทราบก่อนจึงปิดเคสได้ (ลำดับทางคลินิก)"""
        svc = make_service(simulate=True)
        alert = new_alert(svc)
        svc.attempt_delivery(alert.alert_id)
        self.assertIn("error", svc.resolve_alert(alert.alert_id, "phys_1"))

    def test_acknowledge_then_resolve(self):
        svc = make_service(simulate=True)
        alert = new_alert(svc)
        svc.attempt_delivery(alert.alert_id)
        svc.acknowledge_alert(alert.alert_id, "phys_1")
        res = svc.resolve_alert(alert.alert_id, "phys_1")
        self.assertEqual(res.get("status"), "resolved")


# ═══════════════════════════════════════════════════════════
# CS-2 — การ parse คำสั่งฝั่ง LINE
# ═══════════════════════════════════════════════════════════

class TestAckCommandParsing(unittest.TestCase):
    """ทดสอบตรรกะ parse แบบเดียวกับ _parse_alert_ack ใน server.py

    ทำซ้ำที่นี่เพื่อทดสอบได้โดยไม่ต้อง import server (ซึ่งต้องมี env
    ครบและเปิด thread) — ตรรกะต้องตรงกับใน server.py
    """

    @staticmethod
    def parse(text):
        m = re.match(
            r"^\s*(รับทราบ|ack|acknowledge|แก้ไขแล้ว|resolve)\s+"
            r"([0-9a-fA-F]{6,32})\s*$",
            (text or "").strip(), re.IGNORECASE,
        )
        if not m:
            return None
        verb = m.group(1).lower()
        return (m.group(2),
                "resolve" if verb in ("แก้ไขแล้ว", "resolve") else "ack")

    def test_thai_acknowledge(self):
        self.assertEqual(self.parse("รับทราบ a1b2c3d4"), ("a1b2c3d4", "ack"))

    def test_english_ack(self):
        self.assertEqual(self.parse("ack a1b2c3d4"), ("a1b2c3d4", "ack"))
        self.assertEqual(
            self.parse("acknowledge A1B2C3D4"), ("A1B2C3D4", "ack")
        )

    def test_resolve_variants(self):
        self.assertEqual(
            self.parse("แก้ไขแล้ว a1b2c3d4"), ("a1b2c3d4", "resolve")
        )
        self.assertEqual(
            self.parse("resolve a1b2c3d4"), ("a1b2c3d4", "resolve")
        )

    def test_rejects_non_commands(self):
        """ต้องไม่จับคำสั่งอื่นผิด — สำคัญเพราะอยู่ใน elif chain เดียวกัน"""
        for text in (
            "รับทราบ",                  # ไม่มีรหัส
            "รับทราบครับ",              # ประโยคสนทนา
            "แจ้งเตือนค้าง",
            "ผู้ป่วย 57042",
            "ค้นหา เบาหวาน",
            "รับทราบ zzzz",             # ไม่ใช่ hex
            "รับทราบ abc",              # สั้นเกินไป
            "",
        ):
            self.assertIsNone(self.parse(text), f"ต้องไม่จับ: {text!r}")

    def test_tolerates_whitespace(self):
        self.assertEqual(
            self.parse("  รับทราบ   a1b2c3d4  "), ("a1b2c3d4", "ack")
        )


# ═══════════════════════════════════════════════════════════
# CS-1 — driver ต้องขับ retry จริง
# ═══════════════════════════════════════════════════════════

class TestCS1DriverRunsRetries(unittest.TestCase):

    def test_failed_delivery_is_retried_by_driver(self):
        """alert ที่ส่งไม่สำเร็จต้องถูกส่งซ้ำเมื่อ driver ทำงาน

        เดิม check_and_retry มี call site เดียวใน /health/ready ซึ่ง
        ไม่มีอะไรเรียก → retry ไม่เคยเกิดขึ้นเลย
        """
        storage, audit = MockStorage(), MockAudit()
        svc = make_service(storage, audit, simulate=False)
        alert = new_alert(svc)

        # ส่งครั้งแรกล้มเหลว
        svc.attempt_delivery(alert.alert_id,
                             send_push_fn=lambda uid, msg: False)
        failed = svc._load_alert(alert.alert_id)
        self.assertEqual(failed.state, AlertState.DELIVERY_FAILED.value)

        # ย้อนเวลาให้พ้น backoff
        failed.created_at = (
            datetime.now() - timedelta(hours=2)
        ).isoformat(timespec="seconds")
        svc._save_alert(failed)

        mgr = AlertRetryManager(svc)
        mgr._retry_interval = 0
        mgr.check_and_retry(send_push_fn=lambda uid, msg: True)

        self.assertEqual(
            svc._load_alert(alert.alert_id).state, AlertState.SENT.value
        )

    def test_retry_respects_backoff(self):
        """ยังไม่พ้น backoff ต้องไม่ส่งซ้ำ (กันยิงถี่เกินไป)"""
        svc = make_service(simulate=False)
        alert = new_alert(svc)
        svc.attempt_delivery(alert.alert_id,
                             send_push_fn=lambda uid, msg: False)

        mgr = AlertRetryManager(svc)
        mgr._retry_interval = 0
        calls = []
        mgr.check_and_retry(
            send_push_fn=lambda uid, msg: calls.append(uid) or True
        )
        self.assertEqual(calls, [], "ยังไม่ควร retry ภายใน backoff")

    def test_driver_tick_is_safe_with_no_alerts(self):
        svc = make_service(simulate=True)
        mgr = AlertRetryManager(svc)
        mgr._retry_interval = 0
        self.assertEqual(mgr.check_and_retry(lambda u, m: True), [])

    def test_missing_recipients_fails_closed(self):
        """ไม่มี physician recipient → ต้องไม่ถือว่าส่งสำเร็จ"""
        svc = make_service(simulate=True, recipients={})
        alert = new_alert(svc)
        result = svc.attempt_delivery(alert.alert_id,
                                      send_push_fn=lambda u, m: True)
        self.assertIn("error", result)
        self.assertEqual(
            svc._load_alert(alert.alert_id).state,
            AlertState.DELIVERY_FAILED.value,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
