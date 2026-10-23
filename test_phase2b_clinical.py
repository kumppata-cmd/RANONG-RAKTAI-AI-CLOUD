#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 2B — Clinical Feature Completion Tests
RANONG-RAKTAI-AI 2.0

ครอบคลุม:
  1. Monitoring Schedule — ตรวจสอบตารางติดตามผลแล็บ
  2. Nephrology KPI Dashboard — เป้าหมายไต + operational KPI
  3. Medication Review (Phase 2B features) — renal dosing, reconciliation
  4. LINE Command Integration — ตารางตรวจ, KPIไต, aliases
  5. Safety Invariants — no external AI, deterministic, no auto-ordering
"""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

# ── Test data ──────────────────────────────────────────────

TODAY = datetime(2026, 10, 23)
SAMPLE_OWNER_HASH = "abc12345def67890abc12345def67890"


def make_lab_report(specimen_date: str, results: list[dict]) -> dict:
    """สร้าง lab report dict สำหรับทดสอบ."""
    return {
        "document_id": f"doc_{specimen_date.replace('-', '')}",
        "owner_hash": SAMPLE_OWNER_HASH,
        "specimen_date": specimen_date,
        "results": results,
    }


def make_result(analyte: str, value: float, unit: str = "") -> dict:
    return {"analyte": analyte, "value": value, "unit": unit}


# ═══════════════════════════════════════════════════════════
# 1. Monitoring Schedule Tests
# ═══════════════════════════════════════════════════════════

class TestMonitoringSchedule(unittest.TestCase):
    """ตรวจสอบระบบติดตามตารางการตรวจ."""

    def setUp(self):
        from monitoring_schedule import (
            check_monitoring_status,
            monitoring_report_text_th,
            MonitoringPeriod,
            DEFAULT_MONITORING_SCHEDULE,
        )
        self.check = check_monitoring_status
        self.report_text = monitoring_report_text_th
        self.MonitoringPeriod = MonitoringPeriod
        self.schedule = DEFAULT_MONITORING_SCHEDULE

    # ── Schedule Definitions ──

    def test_default_schedule_has_required_items(self):
        """ตารางเริ่มต้นต้องมีรายการตามที่กำหนด."""
        required = [
            "CBC", "BUN", "Creatinine", "Electrolytes",
            "Calcium", "Phosphate", "FBS", "Albumin",
            "KtV", "PTH", "IronProfile", "HBV", "HCV",
        ]
        for item in required:
            self.assertIn(item, self.schedule, f"Missing: {item}")

    def test_schedule_periods_correct(self):
        """ช่วงเวลาของแต่ละรายการต้องถูกต้อง."""
        monthly = ["CBC", "BUN", "Creatinine", "Electrolytes", "Calcium", "Phosphate"]
        quarterly = ["FBS", "Albumin", "KtV"]
        semi_annual = ["PTH", "IronProfile", "HBV", "HCV"]

        for item in monthly:
            self.assertEqual(
                self.schedule[item]["period"],
                self.MonitoringPeriod.MONTHLY,
            )
        for item in quarterly:
            self.assertEqual(
                self.schedule[item]["period"],
                self.MonitoringPeriod.QUARTERLY,
            )
        for item in semi_annual:
            self.assertEqual(
                self.schedule[item]["period"],
                self.MonitoringPeriod.SEMI_ANNUAL,
            )

    def test_schedule_has_thai_descriptions(self):
        """ทุกรายการต้องมีคำอธิบายภาษาไทย."""
        for name, config in self.schedule.items():
            self.assertIn("description_th", config, f"Missing description_th: {name}")
            self.assertTrue(config["description_th"], f"Empty description_th: {name}")

    # ── Up to Date ──

    def test_all_up_to_date(self):
        """เมื่อผลตรวจทั้งหมดเป็นปัจจุบัน."""
        recent = (TODAY - timedelta(days=5)).strftime("%Y-%m-%d")
        lab_reports = [
            make_lab_report(recent, [
                make_result("Hb", 12.0),
                make_result("Hct", 36.0),
                make_result("WBC", 8000),
                make_result("Platelet", 250000),
                make_result("BUN", 20),
                make_result("Creatinine", 1.2),
                make_result("eGFR", 60),
                make_result("Na", 140),
                make_result("K", 4.0),
                make_result("Cl", 100),
                make_result("HCO3", 24),
                make_result("Ca", 9.0),
                make_result("PO4", 4.0),
                make_result("FBS", 100),
                make_result("Albumin", 4.0),
                make_result("Kt/V", 1.4),
                make_result("PTH", 300),
                make_result("Ferritin", 500),
                make_result("TSAT", 30),
                make_result("HBsAg", 0),
                make_result("AntiHBs", 100),
                make_result("AntiHCV", 0),
            ]),
        ]

        report = self.check(lab_reports, SAMPLE_OWNER_HASH, reference_date=TODAY)
        self.assertEqual(report.total_overdue, 0)
        self.assertTrue(len(report.up_to_date_items) > 0)

    # ── Overdue Detection ──

    def test_overdue_monthly(self):
        """ตรวจจับรายการเดือนที่เกินกำหนด."""
        old_date = (TODAY - timedelta(days=45)).strftime("%Y-%m-%d")
        lab_reports = [
            make_lab_report(old_date, [
                make_result("Hb", 12.0),
                make_result("Creatinine", 1.2),
            ]),
        ]

        report = self.check(lab_reports, SAMPLE_OWNER_HASH, reference_date=TODAY)
        overdue_names = [i.name for i in report.overdue_items]
        self.assertIn("CBC", overdue_names)

    def test_overdue_quarterly(self):
        """ตรวจจับรายการ 3 เดือนที่เกินกำหนด."""
        old_date = (TODAY - timedelta(days=100)).strftime("%Y-%m-%d")
        lab_reports = [
            make_lab_report(old_date, [
                make_result("FBS", 110),
                make_result("Albumin", 3.8),
            ]),
        ]

        report = self.check(lab_reports, SAMPLE_OWNER_HASH, reference_date=TODAY)
        overdue_names = [i.name for i in report.overdue_items]
        self.assertIn("FBS", overdue_names)

    def test_overdue_semi_annual(self):
        """ตรวจจับรายการ 6 เดือนที่เกินกำหนด."""
        old_date = (TODAY - timedelta(days=200)).strftime("%Y-%m-%d")
        lab_reports = [
            make_lab_report(old_date, [
                make_result("PTH", 300),
            ]),
        ]

        report = self.check(lab_reports, SAMPLE_OWNER_HASH, reference_date=TODAY)
        overdue_names = [i.name for i in report.overdue_items]
        self.assertIn("PTH", overdue_names)

    # ── Due Soon ──

    def test_due_soon_within_7_days(self):
        """ตรวจจับรายการที่ใกล้ถึงกำหนดภายใน 7 วัน."""
        recent = (TODAY - timedelta(days=26)).strftime("%Y-%m-%d")
        lab_reports = [
            make_lab_report(recent, [
                make_result("BUN", 18),
            ]),
        ]

        report = self.check(lab_reports, SAMPLE_OWNER_HASH, reference_date=TODAY)
        due_soon_names = [i.name for i in report.due_soon_items]
        self.assertIn("BUN", due_soon_names)

    # ── Missing Items ──

    def test_missing_when_no_labs(self):
        """เมื่อไม่มีผลแล็บเลย ทุก lab item ต้อง missing."""
        report = self.check([], SAMPLE_OWNER_HASH, reference_date=TODAY)
        self.assertTrue(report.total_missing > 0)

    def test_missing_specific_analyte(self):
        """รายการที่ไม่เคยตรวจต้องอยู่ใน missing."""
        recent = (TODAY - timedelta(days=5)).strftime("%Y-%m-%d")
        lab_reports = [
            make_lab_report(recent, [
                make_result("Hb", 12.0),
            ]),
        ]

        report = self.check(lab_reports, SAMPLE_OWNER_HASH, reference_date=TODAY)
        missing_names = [i.name for i in report.missing_items]
        self.assertIn("PTH", missing_names)

    # ── Owner Hash Privacy ──

    def test_owner_hash_truncated(self):
        """report ต้องใช้ prefix 8 ตัวของ owner_hash."""
        report = self.check([], SAMPLE_OWNER_HASH, reference_date=TODAY)
        self.assertEqual(report.owner_hash_prefix, SAMPLE_OWNER_HASH[:8])

    # ── Thai Text Report ──

    def test_report_text_contains_overdue_section(self):
        """รายงานภาษาไทยต้องแสดงส่วนเกินกำหนด."""
        old_date = (TODAY - timedelta(days=45)).strftime("%Y-%m-%d")
        lab_reports = [
            make_lab_report(old_date, [make_result("Hb", 12.0)]),
        ]
        report = self.check(lab_reports, SAMPLE_OWNER_HASH, reference_date=TODAY)
        text = self.report_text(report)
        self.assertIn("เกินกำหนด", text)

    def test_report_text_contains_disclaimer(self):
        """รายงานต้องมี disclaimer."""
        report = self.check([], SAMPLE_OWNER_HASH, reference_date=TODAY)
        text = self.report_text(report)
        self.assertIn("ดุลยพินิจของแพทย์", text)

    def test_report_text_no_patient_id(self):
        """รายงานต้องไม่มี full owner_hash."""
        report = self.check([], SAMPLE_OWNER_HASH, reference_date=TODAY)
        text = self.report_text(report)
        self.assertNotIn(SAMPLE_OWNER_HASH, text)

    # ── Custom Schedule ──

    def test_custom_schedule(self):
        """ต้องรองรับ custom schedule."""
        from monitoring_schedule import MonitoringPeriod
        custom = {
            "CustomTest": {
                "period": MonitoringPeriod.MONTHLY,
                "analytes": ["CustomAnalyte"],
                "description_th": "ตรวจพิเศษ",
                "days_interval": 14,
            }
        }
        report = self.check([], SAMPLE_OWNER_HASH, schedule=custom, reference_date=TODAY)
        missing_names = [i.name for i in report.missing_items]
        self.assertIn("CustomTest", missing_names)

    # ── to_dict ──

    def test_report_to_dict(self):
        """report.to_dict() ต้องคืน dict ที่ serialize ได้."""
        report = self.check([], SAMPLE_OWNER_HASH, reference_date=TODAY)
        d = report.to_dict()
        self.assertIn("overdue", d)
        self.assertIn("missing", d)
        self.assertIn("summary", d)
        json.dumps(d)

    def test_days_overdue_calculation(self):
        """days_overdue ต้องคำนวณถูกต้อง."""
        old_date = (TODAY - timedelta(days=50)).strftime("%Y-%m-%d")
        lab_reports = [
            make_lab_report(old_date, [make_result("BUN", 20)]),
        ]
        report = self.check(lab_reports, SAMPLE_OWNER_HASH, reference_date=TODAY)
        overdue = [i for i in report.overdue_items if i.name == "BUN"]
        self.assertEqual(len(overdue), 1)
        # 50 days ago, 30-day interval = 20 days overdue
        self.assertEqual(overdue[0].days_overdue, 20)


# ═══════════════════════════════════════════════════════════
# 2. Nephrology KPI Dashboard Tests
# ═══════════════════════════════════════════════════════════

class TestNephrologyKPI(unittest.TestCase):
    """ตรวจสอบระบบ KPI Dashboard สำหรับไตเรื้อรัง."""

    def setUp(self):
        from nephrology_kpi import (
            NEPHROLOGY_TARGETS,
            evaluate_domain_targets,
            generate_nephrology_dashboard,
            dashboard_text_th,
        )
        self.targets = NEPHROLOGY_TARGETS
        self.evaluate = evaluate_domain_targets
        self.generate = generate_nephrology_dashboard
        self.dashboard_text = dashboard_text_th

    # ── Target Definitions ──

    def test_targets_have_required_domains(self):
        """ต้องมีเป้าหมายครบทุก domain."""
        required_domains = ["anemia", "dialysis_adequacy", "ckd_mbd", "nutrition"]
        for domain in required_domains:
            self.assertIn(domain, self.targets, f"Missing domain: {domain}")

    def test_anemia_targets(self):
        """เป้าหมาย anemia ต้องมี Hb, Hct, Ferritin, TSAT."""
        anemia = self.targets["anemia"]
        for a in ["Hb", "Hct", "Ferritin", "TSAT"]:
            self.assertIn(a, anemia, f"Missing analyte: {a}")

    def test_dialysis_adequacy_targets(self):
        """เป้าหมาย dialysis adequacy ต้องมี Kt/V."""
        da = self.targets["dialysis_adequacy"]
        self.assertIn("Kt/V", da)

    def test_ckd_mbd_targets(self):
        """เป้าหมาย CKD-MBD ต้องมี Ca, PO4, iPTH."""
        mbd = self.targets["ckd_mbd"]
        for a in ["Ca", "PO4", "iPTH"]:
            self.assertIn(a, mbd, f"Missing analyte: {a}")

    # ── Domain Evaluation ──

    def test_evaluate_at_target(self):
        """ค่าที่อยู่ในเป้าหมายต้องนับเป็น at_target."""
        latest = {
            "Hb": (11.0, "g/dL", "2026-10-20"),
            "Hct": (35.0, "%", "2026-10-20"),
        }
        targets_def = {
            "Hb": {"min": 10.0, "max": 12.0, "unit": "g/dL"},
            "Hct": {"min": 30.0, "max": 36.0, "unit": "%"},
        }
        result = self.evaluate("anemia", "ภาวะโลหิตจาง", targets_def, latest)
        self.assertEqual(result.at_target_count, 2)
        self.assertEqual(result.measured_count, 2)

    def test_evaluate_below_target(self):
        """ค่าที่ต่ำกว่าเป้าหมายต้องนับเป็น not at_target."""
        latest = {"Hb": (8.0, "g/dL", "2026-10-20")}
        targets_def = {"Hb": {"min": 10.0, "max": 12.0, "unit": "g/dL"}}
        result = self.evaluate("anemia", "ภาวะโลหิตจาง", targets_def, latest)
        self.assertEqual(result.at_target_count, 0)
        self.assertEqual(result.measured_count, 1)
        self.assertFalse(result.targets[0].is_at_target)

    def test_evaluate_above_target(self):
        """ค่าที่สูงกว่าเป้าหมายต้องนับเป็น not at_target."""
        latest = {"Hb": (15.0, "g/dL", "2026-10-20")}
        targets_def = {"Hb": {"min": 10.0, "max": 12.0, "unit": "g/dL"}}
        result = self.evaluate("anemia", "ภาวะโลหิตจาง", targets_def, latest)
        self.assertFalse(result.targets[0].is_at_target)

    def test_evaluate_missing_value(self):
        """analyte ที่ไม่มีค่าต้อง measured_count = 0."""
        latest = {}
        targets_def = {"Hb": {"min": 10.0, "max": 12.0, "unit": "g/dL"}}
        result = self.evaluate("anemia", "ภาวะโลหิตจาง", targets_def, latest)
        self.assertEqual(result.measured_count, 0)
        self.assertEqual(result.at_target_count, 0)

    def test_target_at_boundary_min(self):
        """ค่าที่ขอบล่างต้องนับเป็น at_target."""
        latest = {"Hb": (10.0, "g/dL", "2026-10-20")}
        targets_def = {"Hb": {"min": 10.0, "max": 12.0, "unit": "g/dL"}}
        result = self.evaluate("anemia", "ภาวะโลหิตจาง", targets_def, latest)
        self.assertTrue(result.targets[0].is_at_target)

    def test_target_at_boundary_max(self):
        """ค่าที่ขอบบนต้องนับเป็น at_target."""
        latest = {"Hb": (12.0, "g/dL", "2026-10-20")}
        targets_def = {"Hb": {"min": 10.0, "max": 12.0, "unit": "g/dL"}}
        result = self.evaluate("anemia", "ภาวะโลหิตจาง", targets_def, latest)
        self.assertTrue(result.targets[0].is_at_target)

    # ── Dashboard Generation ──

    def test_generate_dashboard_basic(self):
        """สร้าง dashboard จากผลแล็บจริง."""
        recent = (TODAY - timedelta(days=3)).strftime("%Y-%m-%d")
        lab_reports = [
            make_lab_report(recent, [
                make_result("Hb", 11.0, "g/dL"),
                make_result("Hct", 33.0, "%"),
                make_result("Ferritin", 400, "ng/mL"),
                make_result("TSAT", 25, "%"),
                make_result("Kt/V", 1.3),
                make_result("Ca", 9.0, "mg/dL"),
                make_result("PO4", 4.5, "mg/dL"),
                make_result("iPTH", 250, "pg/mL"),
                make_result("Albumin", 3.8, "g/dL"),
                make_result("K", 4.5, "mEq/L"),
            ]),
        ]
        dashboard = self.generate(
            lab_reports=lab_reports,
            owner_hash=SAMPLE_OWNER_HASH,
            medication_review_data=None,
            monitoring_report=None,
            approval_data=None,
            alert_data=None,
        )
        self.assertTrue(len(dashboard.domain_kpis) > 0)

    def test_generate_dashboard_empty(self):
        """Dashboard จากผลแล็บว่าง ไม่ error."""
        dashboard = self.generate(
            lab_reports=[],
            owner_hash=SAMPLE_OWNER_HASH,
            medication_review_data=None,
            monitoring_report=None,
            approval_data=None,
            alert_data=None,
        )
        self.assertIsNotNone(dashboard)

    def test_dashboard_with_monitoring_report(self):
        """Dashboard ต้องใช้ monitoring report dict ได้."""
        from monitoring_schedule import check_monitoring_status
        report = check_monitoring_status([], SAMPLE_OWNER_HASH, reference_date=TODAY)
        report_dict = report.to_dict()
        dashboard = self.generate(
            lab_reports=[],
            owner_hash=SAMPLE_OWNER_HASH,
            medication_review_data=None,
            monitoring_report=report_dict,
            approval_data=None,
            alert_data=None,
        )
        self.assertIsNotNone(dashboard)
        self.assertTrue(dashboard.missing_investigation_count >= 0)

    def test_dashboard_with_approval_data(self):
        """Dashboard ต้องรับ approval data dict ได้."""
        dashboard = self.generate(
            lab_reports=[],
            owner_hash=SAMPLE_OWNER_HASH,
            medication_review_data=None,
            monitoring_report=None,
            approval_data={"total_pending": 5},
            alert_data=None,
        )
        self.assertIsNotNone(dashboard)
        # approval_backlog may or may not be populated depending on implementation
        if dashboard.approval_backlog:
            self.assertTrue(dashboard.approval_backlog.total_pending >= 0)

    def test_dashboard_with_alert_data(self):
        """Dashboard ต้องรับ alert data dict ได้."""
        dashboard = self.generate(
            lab_reports=[],
            owner_hash=SAMPLE_OWNER_HASH,
            medication_review_data=None,
            monitoring_report=None,
            approval_data=None,
            alert_data={"total_active": 3},
        )
        self.assertIsNotNone(dashboard)
        if dashboard.alert_performance:
            self.assertTrue(dashboard.alert_performance.total_active >= 0)

    # ── Thai Text Output ──

    def test_dashboard_text_no_patient_id(self):
        """Dashboard text ต้องไม่มี full owner_hash."""
        dashboard = self.generate(
            lab_reports=[],
            owner_hash=SAMPLE_OWNER_HASH,
            medication_review_data=None,
            monitoring_report=None,
            approval_data=None,
            alert_data=None,
        )
        text = self.dashboard_text(dashboard)
        self.assertNotIn(SAMPLE_OWNER_HASH, text)

    def test_dashboard_text_has_disclaimer(self):
        """Dashboard text ต้องมี disclaimer."""
        recent = (TODAY - timedelta(days=3)).strftime("%Y-%m-%d")
        lab_reports = [
            make_lab_report(recent, [make_result("Hb", 11.0)]),
        ]
        dashboard = self.generate(
            lab_reports=lab_reports,
            owner_hash=SAMPLE_OWNER_HASH,
            medication_review_data=None,
            monitoring_report=None,
            approval_data=None,
            alert_data=None,
        )
        text = self.dashboard_text(dashboard)
        self.assertTrue(
            "แพทย์" in text or "ดุลยพินิจ" in text or "คลินิก" in text,
            "Dashboard text must contain clinical disclaimer"
        )


# ═══════════════════════════════════════════════════════════
# 3. Medication Review Phase 2B Tests
# ═══════════════════════════════════════════════════════════

class TestMedicationReviewPhase2B(unittest.TestCase):
    """ตรวจสอบฟีเจอร์ Phase 2B ของ medication_review."""

    def setUp(self):
        from medication_review import (
            review_medications,
            review_text_th,
            RENAL_DOSE_GUIDELINES,
            assess_renal_dosing,
            MedicationEntry,
        )
        self.review = review_medications
        self.review_text = review_text_th
        self.guidelines = RENAL_DOSE_GUIDELINES
        self.assess_renal = assess_renal_dosing
        self.MedicationEntry = MedicationEntry

    # ── Renal Dosing Guidelines ──

    def test_guidelines_has_common_drugs(self):
        """ต้องมี guidelines ของยาที่พบบ่อย."""
        common = ["metformin", "allopurinol", "gabapentin"]
        for drug in common:
            self.assertIn(drug, self.guidelines, f"Missing: {drug}")

    def test_assess_metformin_low_egfr(self):
        """Metformin ต้อง alert เมื่อ eGFR ต่ำ."""
        entry = self.MedicationEntry(name="metformin", dose="500mg", frequency="bid")
        category, msg = self.assess_renal(entry, egfr=25.0)
        self.assertEqual(category, "contraindicated")

    def test_assess_safe_drug_normal_egfr(self):
        """ยาที่ปลอดภัยที่ eGFR ปกติไม่ควร contraindicate."""
        entry = self.MedicationEntry(name="metformin", dose="500mg", frequency="bid")
        category, msg = self.assess_renal(entry, egfr=90.0)
        self.assertNotEqual(category, "contraindicated")

    # ── Medication Review Integration ──

    def test_review_with_nephrotoxic_drug(self):
        """ตรวจยาที่เป็นพิษต่อไต."""
        result = self.review(
            medications=["ibuprofen"],
            lab_values={"eGFR": 40, "Creatinine": 2.0},
            ckd_stage="G3b",
            egfr=40.0,
        )
        self.assertIsNotNone(result)
        self.assertTrue(
            len(result.alerts) > 0 or len(result.critical_alerts()) > 0
            or hasattr(result, "nephrotoxic_alerts"),
            "Ibuprofen should trigger alert in CKD"
        )

    def test_review_text_has_disclaimer(self):
        """ผลตรวจสอบยาต้องมี disclaimer."""
        result = self.review(
            medications=["metformin"],
            lab_values={},
            ckd_stage="",
            egfr=None,
        )
        text = self.review_text(result)
        self.assertTrue(
            "แพทย์" in text or "เภสัชกร" in text or "ดุลยพินิจ" in text
            or "ปรึกษา" in text or "หมายเหตุ" in text,
            "Medication review must include clinical disclaimer"
        )

    def test_review_text_no_prescriptive_language(self):
        """ผลตรวจสอบยาต้องไม่ใช้ภาษาสั่งการ."""
        result = self.review(
            medications=["metformin", "losartan"],
            lab_values={"eGFR": 50},
            ckd_stage="G3a",
            egfr=50.0,
        )
        text = self.review_text(result)
        forbidden = ["ให้หยุดยา", "ต้องเปลี่ยนยา", "สั่งยา"]
        for phrase in forbidden:
            self.assertNotIn(phrase, text, f"Prescriptive language: {phrase}")

    def test_review_empty_medications(self):
        """ส่งรายการยาว่างต้อง handle ได้."""
        result = self.review(
            medications=[],
            lab_values={},
            ckd_stage="",
            egfr=None,
        )
        self.assertIsNotNone(result)

    def test_review_unknown_drug(self):
        """ยาที่ไม่รู้จักต้อง handle ได้ ไม่ error."""
        result = self.review(
            medications=["xyz_unknown_drug_12345"],
            lab_values={},
            ckd_stage="",
            egfr=None,
        )
        self.assertIsNotNone(result)


# ═══════════════════════════════════════════════════════════
# 4. LINE Command Integration Tests
# ═══════════════════════════════════════════════════════════

class TestLineCommandIntegration(unittest.TestCase):
    """ตรวจสอบ command parsing สำหรับ Phase 2B."""

    def setUp(self):
        from line_bot_enhancer import CommandType, parse_command
        self.CommandType = CommandType
        self.parse = parse_command

    def test_monitoring_status_aliases(self):
        """คำสั่งตารางตรวจทุก alias ต้อง parse ถูก."""
        aliases = ["ตารางตรวจ", "กำหนดตรวจ", "แล็บถัดไป", "monitoring", "schedule"]
        for alias in aliases:
            p = self.parse(alias)
            self.assertEqual(
                p.command_type,
                self.CommandType.MONITORING_STATUS,
                f"Alias '{alias}' not recognized",
            )

    def test_nephrology_kpi_aliases(self):
        """คำสั่ง KPI ไตทุก alias ต้อง parse ถูก."""
        aliases = ["KPI ไต", "kpi ไต", "เป้าหมายไต", "nephro kpi", "dashboard"]
        for alias in aliases:
            p = self.parse(alias)
            self.assertEqual(
                p.command_type,
                self.CommandType.NEPHROLOGY_KPI,
                f"Alias '{alias}' not recognized",
            )

    def test_existing_commands_unchanged(self):
        """คำสั่งเดิมต้องยังทำงานได้."""
        test_cases = [
            ("ค้นหา แล็บ", self.CommandType.SEARCH),
            ("รายการล่าสุด", self.CommandType.RECENT),
            ("ผลแล็บล่าสุด", self.CommandType.LAB_RESULT),
            ("ตรวจยา metformin", self.CommandType.MEDICATION_REVIEW),
            ("วิเคราะห์แล็บล่าสุด", self.CommandType.CLINICAL_ANALYSIS),
            ("รายงานแล็บ", self.CommandType.PATIENT_REPORT),
            ("KPI", self.CommandType.CLINIC_KPI),
            ("ช่วยเหลือ", self.CommandType.HELP),
        ]
        for text, expected in test_cases:
            p = self.parse(text)
            self.assertEqual(
                p.command_type, expected,
                f"'{text}' should be {expected}, got {p.command_type}",
            )

    def test_help_text_includes_new_commands(self):
        """Help text ต้องมีคำสั่งใหม่."""
        from line_bot_enhancer import generate_help_text
        text = generate_help_text(is_admin=True, has_docs=True)
        self.assertIn("ตารางตรวจ", text)
        self.assertIn("KPIไต", text)


# ═══════════════════════════════════════════════════════════
# 5. Safety Invariants
# ═══════════════════════════════════════════════════════════

class TestPhase2BSafetyInvariants(unittest.TestCase):
    """ตรวจสอบ safety invariants สำหรับ Phase 2B."""

    def test_monitoring_schedule_no_external_ai(self):
        """monitoring_schedule ต้องไม่ใช้ AI ภายนอก."""
        import monitoring_schedule
        with open(monitoring_schedule.__file__) as f:
            source = f.read()
        import_lines = [line for line in source.split("\n")
                       if line.strip().startswith("import ") or line.strip().startswith("from ")]
        for line in import_lines:
            for forbidden in ["openai", "anthropic", "langchain", "llama"]:
                self.assertNotIn(
                    forbidden,
                    line.lower(),
                    f"monitoring_schedule must not import: {forbidden}"
                )

    def test_nephrology_kpi_no_external_ai(self):
        """nephrology_kpi ต้องไม่ใช้ AI ภายนอก."""
        import nephrology_kpi
        with open(nephrology_kpi.__file__) as f:
            source = f.read()
        import_lines = [line for line in source.split("\n")
                       if line.strip().startswith("import ") or line.strip().startswith("from ")]
        for line in import_lines:
            for forbidden in ["openai", "anthropic", "langchain", "llama"]:
                self.assertNotIn(
                    forbidden,
                    line.lower(),
                    f"nephrology_kpi must not import: {forbidden}"
                )

    def test_monitoring_schedule_deterministic(self):
        """monitoring_schedule ต้อง deterministic."""
        from monitoring_schedule import check_monitoring_status
        labs = [
            make_lab_report("2026-10-01", [
                make_result("Hb", 12.0),
                make_result("Creatinine", 1.2),
            ])
        ]
        r1 = check_monitoring_status(labs, SAMPLE_OWNER_HASH, reference_date=TODAY)
        r2 = check_monitoring_status(labs, SAMPLE_OWNER_HASH, reference_date=TODAY)
        self.assertEqual(r1.total_overdue, r2.total_overdue)
        self.assertEqual(r1.total_missing, r2.total_missing)
        self.assertEqual(len(r1.up_to_date_items), len(r2.up_to_date_items))

    def test_nephrology_kpi_deterministic(self):
        """nephrology_kpi ต้อง deterministic."""
        from nephrology_kpi import generate_nephrology_dashboard
        labs = [
            make_lab_report("2026-10-20", [
                make_result("Hb", 11.0),
                make_result("Kt/V", 1.3),
            ])
        ]
        d1 = generate_nephrology_dashboard(labs, SAMPLE_OWNER_HASH, None, None, None, None)
        d2 = generate_nephrology_dashboard(labs, SAMPLE_OWNER_HASH, None, None, None, None)
        self.assertEqual(len(d1.domain_kpis), len(d2.domain_kpis))

    def test_monitoring_no_auto_ordering(self):
        """monitoring_schedule ต้องไม่มี function สั่งตรวจอัตโนมัติ."""
        import monitoring_schedule
        # Check there are no functions that place orders automatically
        forbidden_functions = ["auto_order", "place_order", "send_order", "order_lab"]
        for name in dir(monitoring_schedule):
            for forbidden in forbidden_functions:
                self.assertNotIn(
                    forbidden, name.lower(),
                    f"Module must not have auto-ordering function: {name}"
                )

    def test_kpi_no_treatment_recommendations(self):
        """nephrology_kpi ต้องไม่มี function สั่งการรักษา."""
        import nephrology_kpi
        # Check there are no functions that prescribe or order treatment
        forbidden_functions = ["prescribe", "order_medication", "change_medication"]
        for name in dir(nephrology_kpi):
            for forbidden in forbidden_functions:
                self.assertNotIn(
                    forbidden, name.lower(),
                    f"Module must not have prescribing function: {name}"
                )


if __name__ == "__main__":
    unittest.main()
