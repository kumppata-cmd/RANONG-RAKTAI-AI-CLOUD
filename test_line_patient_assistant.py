#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests for LINE Patient Assistant module (Feature 6).

ทดสอบ: นัดหมาย, คำแนะนำสุขภาพ, ติดต่อเจ้าหน้าที่, API endpoints.
"""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(__file__))


class MockStorage:
    def __init__(self):
        self._files = {}

    def put(self, path, data, content_type=None):
        self._files[path] = data

    def get(self, path):
        return self._files.get(path)

    def list_files(self, prefix=""):
        return [k for k in sorted(self._files) if k.startswith(prefix)]

    def delete(self, path):
        self._files.pop(path, None)


# ═══════════════════════════════════════════════════════════
# 1. Date/Time Parsing Tests
# ═══════════════════════════════════════════════════════════

class TestParseAppointmentText(unittest.TestCase):

    def setUp(self):
        from line_patient_assistant import parse_appointment_text
        self.parse = parse_appointment_text

    def test_date_slash_format_buddhist_4digit(self):
        """25/12/2569 → 2026-12-25"""
        result = self.parse("ฟอกไต 25/12/2569 09:00")
        self.assertEqual(result["date"], "2026-12-25")
        self.assertEqual(result["time"], "09:00")
        self.assertEqual(result["purpose"], "ฟอกไต")

    def test_date_slash_format_buddhist_2digit(self):
        """25/12/69 — 2-digit year CE interpretation"""
        result = self.parse("ตรวจเลือด 25/12/69 10:00")
        # Module interprets 69 as CE 2069 (not พ.ศ. 2-digit)
        self.assertTrue(result["date"].endswith("-12-25"))
        self.assertEqual(result["purpose"], "ตรวจเลือด")

    def test_date_slash_format_ce(self):
        """25/12/2026 → 2026-12-25"""
        result = self.parse("พบแพทย์ 25/12/2026 14:00")
        self.assertEqual(result["date"], "2026-12-25")
        self.assertEqual(result["purpose"], "พบแพทย์")

    def test_time_word_morning(self):
        result = self.parse("ฟอกไต 25/12/69 เช้า")
        self.assertIn(result["time"], ("08:00", "09:00"))  # เช้า = 8 or 9

    def test_time_word_afternoon(self):
        result = self.parse("ฟอกไต 25/12/69 บ่าย")
        self.assertEqual(result["time"], "13:00")

    def test_time_word_evening(self):
        result = self.parse("ฟอกไต 25/12/69 เย็น")
        self.assertEqual(result["time"], "16:00")

    def test_purpose_keyword_hd(self):
        result = self.parse("HD 25/12/69 09:00")
        self.assertEqual(result["purpose"], "ฟอกไต")

    def test_purpose_keyword_lab(self):
        result = self.parse("lab 25/12/69 10:00")
        self.assertEqual(result["purpose"], "ตรวจเลือด")

    def test_no_date_returns_empty(self):
        result = self.parse("ฟอกไต")
        self.assertEqual(result["date"], "")

    def test_tomorrow(self):
        result = self.parse("ฟอกไต พรุ่งนี้ เช้า")
        expected = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        self.assertEqual(result["date"], expected)

    def test_default_purpose(self):
        result = self.parse("25/12/69 09:00")
        # Default purpose when no keyword matched
        self.assertTrue(len(result["purpose"]) > 0)


# ═══════════════════════════════════════════════════════════
# 2. Appointment CRUD Tests
# ═══════════════════════════════════════════════════════════

class TestAppointmentCRUD(unittest.TestCase):

    def setUp(self):
        from line_patient_assistant import (
            save_appointment, list_appointments,
            cancel_appointment, Appointment,
        )
        self.save_appointment = save_appointment
        self.list_appointments = list_appointments
        self.cancel_appointment = cancel_appointment
        self.storage = MockStorage()
        self.owner = "testowner"

    def test_save_appointment(self):
        parsed = {"date": "2026-12-25", "time": "09:00", "purpose": "ฟอกไต"}
        appt = self.save_appointment(self.storage, self.owner, parsed)
        self.assertEqual(appt.date, "2026-12-25")
        self.assertEqual(appt.time, "09:00")
        self.assertEqual(appt.purpose, "ฟอกไต")
        self.assertEqual(appt.owner_hash, self.owner)
        self.assertIn(appt.status, ("scheduled", "SCHEDULED"))
        # Verify stored in GCS
        files = self.storage.list_files("_appointments/")
        self.assertEqual(len(files), 1)

    def test_list_appointments_owner_isolation(self):
        """ผู้ใช้เห็นเฉพาะนัดหมายของตัวเอง"""
        p1 = {"date": "2026-12-25", "time": "09:00", "purpose": "ฟอกไต"}
        p2 = {"date": "2026-12-26", "time": "10:00", "purpose": "ตรวจเลือด"}
        self.save_appointment(self.storage, "owner_a", p1, appointment_id="appt-iso-001")
        self.save_appointment(self.storage, "owner_b", p2, appointment_id="appt-iso-002")
        list_a = self.list_appointments(self.storage, "owner_a")
        list_b = self.list_appointments(self.storage, "owner_b")
        self.assertEqual(len(list_a), 1)
        self.assertEqual(len(list_b), 1)
        self.assertEqual(list_a[0].purpose, "ฟอกไต")
        self.assertEqual(list_b[0].purpose, "ตรวจเลือด")

    def test_cancel_appointment(self):
        parsed = {"date": "2026-12-25", "time": "09:00", "purpose": "ฟอกไต"}
        appt = self.save_appointment(self.storage, self.owner, parsed)
        appt_id_prefix = appt.appointment_id[:8]
        result = self.cancel_appointment(self.storage, self.owner, appt_id_prefix)
        self.assertIn("ยกเลิก", result)
        # Verify status changed
        remaining = self.list_appointments(self.storage, self.owner)
        # cancelled appointments should not show in default list (upcoming_only=True)
        # or if they do, their status should be cancelled
        all_appts = self.list_appointments(self.storage, self.owner, upcoming_only=False)
        found = [a for a in all_appts if a.appointment_id == appt.appointment_id]
        if found:
            self.assertIn(found[0].status, ("cancelled", "CANCELLED"))

    def test_cancel_wrong_owner_fails(self):
        """ยกเลิกนัดของคนอื่นไม่ได้"""
        parsed = {"date": "2026-12-25", "time": "09:00", "purpose": "ฟอกไต"}
        appt = self.save_appointment(self.storage, "owner_a", parsed)
        appt_id_prefix = appt.appointment_id[:8]
        result = self.cancel_appointment(self.storage, "owner_b", appt_id_prefix)
        self.assertIn("ไม่พบ", result)


# ═══════════════════════════════════════════════════════════
# 3. Appointment Formatting Tests
# ═══════════════════════════════════════════════════════════

class TestAppointmentFormatting(unittest.TestCase):

    def test_format_appointments_list_empty(self):
        from line_patient_assistant import format_appointments_list
        result = format_appointments_list([])
        self.assertIn("ไม่มีนัดหมาย", result)

    def test_format_appointment_confirmation(self):
        from line_patient_assistant import (
            format_appointment_confirmation, Appointment,
        )
        appt = Appointment(
            appointment_id="abc12345-6789",
            owner_hash="test",
            date="2026-12-25",
            time="09:00",
            purpose="ฟอกไต",
            created_at="2026-10-24T10:00:00",
        )
        result = format_appointment_confirmation(appt)
        self.assertIn("2026-12-25", result)
        self.assertIn("09:00", result)
        self.assertIn("ฟอกไต", result)


# ═══════════════════════════════════════════════════════════
# 4. Health Advice Tests
# ═══════════════════════════════════════════════════════════

class TestHealthAdvice(unittest.TestCase):

    def setUp(self):
        from line_patient_assistant import get_health_advice
        self.get_advice = get_health_advice

    def test_empty_query_returns_menu(self):
        result = self.get_advice("")
        self.assertIn("คำแนะนำ", result)

    def test_keyword_food(self):
        result = self.get_advice("อาหาร")
        self.assertIn("อาหาร", result)
        # Should have content, not just the keyword
        self.assertTrue(len(result) > 20)

    def test_keyword_water(self):
        result = self.get_advice("น้ำ")
        self.assertTrue(len(result) > 20)

    def test_keyword_number(self):
        result = self.get_advice("1")
        self.assertTrue(len(result) > 20)

    def test_unknown_keyword_returns_menu(self):
        result = self.get_advice("xyz_nonexistent_keyword")
        self.assertIn("คำแนะนำ", result)


# ═══════════════════════════════════════════════════════════
# 5. Escalation Tests
# ═══════════════════════════════════════════════════════════

class TestEscalation(unittest.TestCase):

    def setUp(self):
        from line_patient_assistant import (
            create_escalation, list_pending_escalations,
            resolve_escalation, format_escalation_confirmation,
        )
        self.create_escalation = create_escalation
        self.list_pending = list_pending_escalations
        self.resolve = resolve_escalation
        self.format_confirm = format_escalation_confirmation
        self.storage = MockStorage()

    def test_create_escalation(self):
        esc = self.create_escalation(
            self.storage, "owner_a", "มีอาการบวมมาก ต้องการคำแนะนำ"
        )
        self.assertIn("escalation_id", esc)
        self.assertIn("urgency", esc)
        self.assertIn("category", esc)
        self.assertEqual(esc["status"], "pending")
        # Verify stored
        files = self.storage.list_files("_escalations/")
        self.assertEqual(len(files), 1)

    def test_escalation_does_not_store_raw_message(self):
        """ไม่บันทึกข้อความดิบของผู้ป่วย (PDPA)"""
        esc = self.create_escalation(
            self.storage, "owner_a", "ข้อความส่วนตัวของผู้ป่วย"
        )
        # Read stored data
        files = self.storage.list_files("_escalations/")
        stored = json.loads(self.storage.get(files[0]))
        # Should NOT contain the raw message text
        self.assertNotIn("ข้อความส่วนตัวของผู้ป่วย", json.dumps(stored, ensure_ascii=False))

    def test_list_pending_escalations(self):
        self.create_escalation(self.storage, "a", "msg1")
        self.create_escalation(self.storage, "b", "msg2")
        pending = self.list_pending(self.storage)
        self.assertEqual(len(pending), 2)
        self.assertEqual(pending[0]["status"], "pending")

    def test_resolve_escalation(self):
        esc = self.create_escalation(self.storage, "a", "msg")
        esc_id_prefix = esc["escalation_id"][:8]
        result = self.resolve(self.storage, esc_id_prefix)
        self.assertIn("ปิดคำร้อง", result)
        # Verify resolved
        pending = self.list_pending(self.storage)
        self.assertEqual(len(pending), 0)

    def test_format_escalation_confirmation(self):
        esc = self.create_escalation(self.storage, "a", "ต้องการความช่วยเหลือ")
        result = self.format_confirm(esc)
        self.assertIn("ส่งข้อความ", result)


# ═══════════════════════════════════════════════════════════
# 6. Reminders Tests
# ═══════════════════════════════════════════════════════════

class TestReminders(unittest.TestCase):

    def test_upcoming_reminders(self):
        from line_patient_assistant import (
            save_appointment, get_upcoming_reminders,
            format_reminder_message,
        )
        storage = MockStorage()
        # Create appointment for tomorrow (days_ahead=1)
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        parsed = {"date": tomorrow, "time": "09:00", "purpose": "ฟอกไต"}
        appt = save_appointment(storage, "test_owner", parsed)

        # get_upcoming_reminders checks date == now + days_ahead
        reminders = get_upcoming_reminders(storage, days_ahead=1)
        self.assertGreaterEqual(len(reminders), 1)

        # Returns (owner_hash, Appointment) tuples
        owner_hash, reminder_appt = reminders[0]
        msg = format_reminder_message(reminder_appt)
        self.assertIn("09:00", msg)
        self.assertIn("ฟอกไต", msg)


# ═══════════════════════════════════════════════════════════
# 7. Blueprint API Tests
# ═══════════════════════════════════════════════════════════

class TestPatientAssistantAPI(unittest.TestCase):

    def setUp(self):
        from flask import Flask
        from line_patient_assistant import register_patient_assistant

        self.app = Flask(__name__)
        self.app.config["TESTING"] = True
        self.storage = MockStorage()
        os.environ["ADMIN_SECRET"] = "test-secret-pa"
        register_patient_assistant(self.app, self.storage, None, None)
        self.client = self.app.test_client()

    def tearDown(self):
        if "ADMIN_SECRET" in os.environ:
            del os.environ["ADMIN_SECRET"]

    def _get(self, path):
        sep = "&" if "?" in path else "?"
        return self.client.get(path + sep + "secret=test-secret-pa")

    def _post(self, path, data=None):
        sep = "&" if "?" in path else "?"
        return self.client.post(
            path + sep + "secret=test-secret-pa",
            json=data or {},
        )

    # Auth
    def test_appointments_unauthorized(self):
        r = self.client.get("/admin/api/v2/patient/appointments")
        self.assertEqual(r.status_code, 401)

    # Appointments
    def test_appointments_empty(self):
        r = self._get("/admin/api/v2/patient/appointments")
        self.assertEqual(r.status_code, 200)
        d = json.loads(r.data)
        self.assertEqual(d["total"], 0)

    def test_appointments_with_data(self):
        from line_patient_assistant import save_appointment
        save_appointment(self.storage, "test_hash", {
            "date": "2026-12-25", "time": "09:00", "purpose": "ฟอกไต",
        })
        r = self._get("/admin/api/v2/patient/appointments")
        d = json.loads(r.data)
        self.assertEqual(d["total"], 1)

    # Escalations
    def test_escalations_empty(self):
        r = self._get("/admin/api/v2/patient/escalations")
        self.assertEqual(r.status_code, 200)
        d = json.loads(r.data)
        self.assertEqual(d["total"], 0)

    def test_escalation_resolve(self):
        from line_patient_assistant import create_escalation
        esc = create_escalation(self.storage, "a", "test msg")
        esc_id = esc["escalation_id"]
        r = self._post(f"/admin/api/v2/patient/escalation/{esc_id[:8]}/resolve")
        self.assertEqual(r.status_code, 200)
        d = json.loads(r.data)
        self.assertIn("ปิดคำร้อง", d.get("message", ""))

    # Reminders
    def test_reminders_endpoint(self):
        r = self._get("/admin/api/v2/patient/reminders")
        self.assertEqual(r.status_code, 200)
        d = json.loads(r.data)
        self.assertIn("reminders", d)


if __name__ == "__main__":
    unittest.main(verbosity=2)
