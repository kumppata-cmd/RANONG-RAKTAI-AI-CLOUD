"""'รับทราบ <รหัส>' ต้องหา alert เจอ (17 พ.ย. 2569: เดิมตอบ 'ไม่พบการแจ้งเตือน' ทุกครั้ง)

สาเหตุ: storage.list_files() รับเฉพาะชื่อหมวด — ส่ง '_alerts/' ไปได้โฟลเดอร์ 06_AI_REVIEW แทน
FakeGCS ด้านล่างจำลองพฤติกรรมนั้นตรงตามจริง"""
import json
import unittest

from alert_delivery import AlertDeliveryService, AlertState


class FakeGCS:
    def __init__(self):
        self.blobs = {"06_AI_REVIEW/x.jpg": b"img"}

    def put(self, path, data, content_type="application/json", create_only=False):
        self.blobs[path] = data
        return path

    def get(self, path):
        return self.blobs.get(path)

    def list_files(self, category):          # พฤติกรรมจริงของ StorageGCS
        return [p.split("/")[-1] for p in self.blobs if p.startswith("06_AI_REVIEW/")]

    def list_prefix(self, prefix):
        return [p for p in self.blobs if p.startswith(prefix)]


class Audit:
    def log(self, *a, **k):
        pass


class AckLookupTests(unittest.TestCase):
    def setUp(self):
        self.st = FakeGCS()
        self.svc = AlertDeliveryService(self.st, Audit(), clinician_recipients={"physician": ["Udoc"]})
        self.sent = []
        self.a1 = self.svc.create_alert("cd24f7fb", "own", "eGFR", 2.0, "mL/min", "critical")
        self.a2 = self.svc.create_alert("cd24f7fb", "own", "Potassium", 6.8, "mmol/L", "critical")
        for a in (self.a1, self.a2):
            self.svc.attempt_delivery(a.alert_id, send_push_fn=lambda u, m: self.sent.append((u, m)) or True)
        # ไฟล์สรุปเอกสารรุ่นเก่าที่ server เขียนไว้ในโฟลเดอร์เดียวกัน
        self.st.put("_alerts/cd24f7fb-1111-2222.json", json.dumps({"document_id": "cd24f7fb"}).encode())

    def test_alert_code_found(self):
        self.assertEqual(self.svc.find_alert_id_by_prefix(self.a1.alert_id[:8]), self.a1.alert_id)
        self.assertEqual(self.svc.find_alerts_by_ref(self.a1.alert_id[:8]), [self.a1.alert_id])

    def test_document_code_acks_all_alerts_of_that_document(self):
        ids = self.svc.find_alerts_by_ref("cd24f7fb")
        self.assertEqual(sorted(ids), sorted([self.a1.alert_id, self.a2.alert_id]))
        for i in ids:
            self.assertFalse(self.svc.acknowledge_alert(i, "doctorhash").get("error"))
        self.assertEqual(self.svc.find_alerts_by_ref("cd24f7fb"), [])   # รับทราบแล้ว ไม่ค้าง

    def test_unknown_code(self):
        self.assertEqual(self.svc.find_alerts_by_ref("deadbeef"), [])

    def test_active_alerts_listed(self):
        ids = {a["alert_id"] for a in self.svc.get_active_alerts()}
        self.assertIn(self.a1.alert_id, ids)

    def test_dialysis_patient_egfr_is_urgent(self):
        a = self.svc.create_alert("aaaa1111", "own", "eGFR", 2.0, "mL/min", "critical",
                                  patient_context={"is_dialysis_patient": True})
        self.assertNotEqual(a.severity, "critical")
        self.assertEqual(a.state, AlertState.QUEUED.value)


class OcrRefTests(unittest.TestCase):
    def test_shifted_single_number_replaced(self):
        import lab_extractor as le
        self.assertNotEqual(le._clean_ocr_ref("145", "BUN"), "145")
        self.assertEqual(le._clean_ocr_ref("9-23", "BUN"), "9-23")
        self.assertEqual(le._clean_ocr_ref(">90", "eGFR"), ">90")


if __name__ == "__main__":
    unittest.main()


class FlakyGCS(FakeGCS):
    """GCS 429: เขียน object เดิมซ้ำเร็วเกิน → ล้มครั้งแรก"""
    def __init__(self):
        super().__init__()
        self.fail_next = 0

    def put(self, path, data, content_type="application/json", create_only=False):
        if self.fail_next > 0:
            self.fail_next -= 1
            raise RuntimeError("429 rateLimitExceeded")
        return super().put(path, data, content_type, create_only)


class StuckQueuedTests(unittest.TestCase):
    def test_sent_state_saved_after_rate_limit(self):
        import alert_delivery as ad
        st = FlakyGCS()
        svc = AlertDeliveryService(st, Audit(), clinician_recipients={"physician": ["Udoc"]})
        a = svc.create_alert("bbbb2222", "own", "Potassium", 6.9, "mmol/L", "critical")
        st.fail_next = 1
        orig = ad.time if hasattr(ad, "time") else None
        import time as _t
        real_sleep = _t.sleep
        _t.sleep = lambda s: None
        try:
            svc.attempt_delivery(a.alert_id, send_push_fn=lambda u, m: True)
        finally:
            _t.sleep = real_sleep
        self.assertEqual(json.loads(st.get(f"_alerts/{a.alert_id}.json"))["state"], AlertState.SENT.value)
        del orig

    def test_queued_alert_can_be_acknowledged(self):
        st = FakeGCS()
        svc = AlertDeliveryService(st, Audit(), clinician_recipients={"physician": ["Udoc"]})
        a = svc.create_alert("cccc3333", "own", "eGFR", 2.0, "mL/min", "critical")   # ค้าง queued
        self.assertEqual(svc.find_alerts_by_ref("cccc3333"), [a.alert_id])
        r = svc.acknowledge_alert(a.alert_id, "doctorhash")
        self.assertFalse(r.get("error"), r)
        again = svc.acknowledge_alert(a.alert_id, "doctorhash")
        self.assertIn("รับทราบไปแล้ว", again.get("error", ""))
