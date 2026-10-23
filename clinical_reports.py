#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Clinical Reports & KPI Dashboard — Kidney Clinical Intelligence Platform 2.0

ระบบรายงาน KPI และสรุปผลทางคลินิก:
  1. PatientKPI — ตัวชี้วัดรายบุคคล
  2. ClinicKPI — ตัวชี้วัดรวมของคลินิก (admin-only)
  3. generate_patient_report — สร้างรายงานรายบุคคล
  4. generate_clinic_summary — สร้างรายงานรวมคลินิก

กฎความปลอดภัย:
  - ไม่ใช้ AI ภายนอก — คำนวณด้วย deterministic rules
  - PatientKPI ใช้ owner_hash เท่านั้น — ไม่เก็บ patient identity
  - ClinicKPI แสดงเฉพาะ aggregate stats — ไม่มีข้อมูลเฉพาะบุคคล
  - ClinicKPI เข้าถึงได้เฉพาะ admin
  - Disclaimer ในทุกรายงาน
  - ใช้ข้อมูล DEMO/สังเคราะห์ในการทดสอบ
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional


# ═══════════════════════════════════════════════════════════
# Data Classes
# ═══════════════════════════════════════════════════════════

@dataclass
class AnalyteStats:
    """สถิติ analyte เดียว."""
    analyte: str
    latest_value: Optional[float] = None
    latest_date: str = ""
    latest_unit: str = ""
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    count: int = 0
    in_range: bool = True

    def to_dict(self) -> dict:
        return {
            "analyte": self.analyte,
            "latest_value": self.latest_value,
            "latest_date": self.latest_date,
            "latest_unit": self.latest_unit,
            "min_value": self.min_value,
            "max_value": self.max_value,
            "count": self.count,
            "in_range": self.in_range,
        }


@dataclass
class PatientKPI:
    """ตัวชี้วัดรายบุคคล — ไม่มี patient identity."""
    owner_hash: str
    report_date: str = ""
    total_documents: int = 0
    total_lab_reports: int = 0
    total_clinical_analyses: int = 0
    latest_ckd_stage: str = "UNKNOWN"
    analyte_stats: list[AnalyteStats] = field(default_factory=list)
    critical_count: int = 0
    warning_count: int = 0
    disclaimer: str = (
        "⚠️ รายงานนี้เป็นข้อมูลประกอบการพิจารณา "
        "ไม่ใช่การวินิจฉัย — กรุณาปรึกษาแพทย์"
    )

    def __post_init__(self):
        if not self.report_date:
            self.report_date = datetime.now().strftime("%Y-%m-%d")

    def to_dict(self) -> dict:
        return {
            "owner_hash_prefix": self.owner_hash[:8] if self.owner_hash else "",
            "report_date": self.report_date,
            "total_documents": self.total_documents,
            "total_lab_reports": self.total_lab_reports,
            "total_clinical_analyses": self.total_clinical_analyses,
            "latest_ckd_stage": self.latest_ckd_stage,
            "analyte_stats": [s.to_dict() for s in self.analyte_stats],
            "critical_count": self.critical_count,
            "warning_count": self.warning_count,
            "disclaimer": self.disclaimer,
        }


@dataclass
class ClinicKPI:
    """ตัวชี้วัดรวมของคลินิก — aggregate only, ไม่มีข้อมูลเฉพาะบุคคล."""
    report_date: str = ""
    total_patients: int = 0
    total_documents: int = 0
    total_lab_reports: int = 0
    total_clinical_analyses: int = 0
    ckd_stage_distribution: dict[str, int] = field(default_factory=dict)
    critical_alert_patients: int = 0
    analyte_abnormal_counts: dict[str, int] = field(default_factory=dict)
    disclaimer: str = (
        "⚠️ รายงานนี้เป็นข้อมูลรวมของคลินิก — "
        "ไม่มีข้อมูลเฉพาะบุคคล — ข้อมูลประกอบการพิจารณาเท่านั้น"
    )

    def __post_init__(self):
        if not self.report_date:
            self.report_date = datetime.now().strftime("%Y-%m-%d")

    def to_dict(self) -> dict:
        return {
            "report_date": self.report_date,
            "total_patients": self.total_patients,
            "total_documents": self.total_documents,
            "total_lab_reports": self.total_lab_reports,
            "total_clinical_analyses": self.total_clinical_analyses,
            "ckd_stage_distribution": self.ckd_stage_distribution,
            "critical_alert_patients": self.critical_alert_patients,
            "analyte_abnormal_counts": self.analyte_abnormal_counts,
            "disclaimer": self.disclaimer,
        }


# ═══════════════════════════════════════════════════════════
# Reference Ranges (for in-range check)
# ═══════════════════════════════════════════════════════════

# analyte → (low, high) — ช่วงปกติ
NORMAL_RANGES: dict[str, tuple[float, float]] = {
    "Creatinine": (0.6, 1.2),
    "BUN": (7, 20),
    "eGFR": (60, 999),  # >=60 is normal
    "Hemoglobin": (12.0, 17.0),
    "Hematocrit": (36.0, 50.0),
    "Potassium": (3.5, 5.0),
    "Sodium": (135, 145),
    "Calcium": (8.5, 10.5),
    "Phosphate": (2.5, 4.5),
    "Albumin": (3.5, 5.5),
    "HbA1c": (4.0, 6.5),
    "Fasting Glucose": (70, 100),
    "Kt/V": (1.2, 999),  # >=1.2 is adequate
    "iPTH": (150, 300),  # target for dialysis patients
    "TSAT": (20, 50),
    "Ferritin": (200, 500),  # target for dialysis patients
    "Uric Acid": (3.0, 7.0),
    "CO2": (22, 29),
}


# ═══════════════════════════════════════════════════════════
# Patient Report Generation
# ═══════════════════════════════════════════════════════════

def generate_patient_report(
    lab_reports: list[dict],
    clinical_analyses: list[dict],
    doc_count: int,
    owner_hash: str,
) -> PatientKPI:
    """สร้างรายงาน KPI รายบุคคล.

    Args:
        lab_reports: list ของ lab report dicts (จาก GCS)
        clinical_analyses: list ของ clinical analysis dicts (จาก GCS)
        doc_count: จำนวนเอกสารทั้งหมด
        owner_hash: hash ของเจ้าของ

    Returns:
        PatientKPI
    """
    kpi = PatientKPI(
        owner_hash=owner_hash,
        total_documents=doc_count,
        total_lab_reports=len(lab_reports),
        total_clinical_analyses=len(clinical_analyses),
    )

    # หา latest CKD stage จาก clinical analyses
    if clinical_analyses:
        # เรียงตามวันที่ — ล่าสุดก่อน
        sorted_ca = sorted(
            clinical_analyses,
            key=lambda x: x.get("specimen_date", ""),
            reverse=True,
        )
        kpi.latest_ckd_stage = sorted_ca[0].get("ckd_stage", "UNKNOWN")

        # นับ critical/warning findings
        for ca in clinical_analyses:
            for finding in ca.get("findings", []):
                sev = finding.get("severity", "")
                if sev == "critical":
                    kpi.critical_count += 1
                elif sev == "warning":
                    kpi.warning_count += 1

    # รวบรวมสถิติ analyte
    analyte_data: dict[str, list[tuple[str, float, str]]] = {}
    for report in lab_reports:
        date = report.get("specimen_date", "")
        for result in report.get("results", []):
            analyte = result.get("analyte", "")
            if not analyte:
                continue
            try:
                value = float(result.get("value", 0))
            except (TypeError, ValueError):
                continue
            unit = result.get("unit", "")
            if analyte not in analyte_data:
                analyte_data[analyte] = []
            analyte_data[analyte].append((date, value, unit))

    for analyte in sorted(analyte_data.keys()):
        entries = analyte_data[analyte]
        entries.sort(key=lambda x: x[0])  # sort by date
        values = [v for _, v, _ in entries]
        latest = entries[-1]

        # Check if latest is in normal range
        normal = NORMAL_RANGES.get(analyte)
        in_range = True
        if normal:
            in_range = normal[0] <= latest[1] <= normal[1]

        stat = AnalyteStats(
            analyte=analyte,
            latest_value=latest[1],
            latest_date=latest[0],
            latest_unit=latest[2],
            min_value=min(values),
            max_value=max(values),
            count=len(values),
            in_range=in_range,
        )
        kpi.analyte_stats.append(stat)

    return kpi


def patient_report_text_th(kpi: PatientKPI) -> str:
    """สร้างข้อความรายงานรายบุคคลภาษาไทย."""
    lines = [
        "📋 รายงานสรุปผลตรวจ",
        f"วันที่: {kpi.report_date}",
        "",
    ]

    # สรุปจำนวน
    lines.append(f"📄 เอกสารทั้งหมด: {kpi.total_documents}")
    lines.append(f"🔬 ผลตรวจ Lab: {kpi.total_lab_reports} ฉบับ")
    lines.append(f"📊 วิเคราะห์ทางคลินิก: {kpi.total_clinical_analyses} ฉบับ")
    lines.append("")

    # CKD Stage
    if kpi.latest_ckd_stage != "UNKNOWN":
        stage_emoji = {
            "G1": "🟢", "G2": "🟡", "G3a": "🟠", "G3b": "🟠",
            "G4": "🔴", "G5": "🔴",
        }
        emoji = stage_emoji.get(kpi.latest_ckd_stage, "❓")
        lines.append(f"🏥 CKD Stage ล่าสุด: {emoji} {kpi.latest_ckd_stage}")
        lines.append("")

    # Critical/Warning
    if kpi.critical_count > 0 or kpi.warning_count > 0:
        lines.append("⚠️ ผลตรวจที่ต้องติดตาม:")
        if kpi.critical_count > 0:
            lines.append(f"  🔴 Critical: {kpi.critical_count} รายการ")
        if kpi.warning_count > 0:
            lines.append(f"  🟡 Warning: {kpi.warning_count} รายการ")
        lines.append("")

    # ผลตรวจล่าสุด
    if kpi.analyte_stats:
        lines.append("🔬 ผลตรวจล่าสุด:")
        for stat in kpi.analyte_stats:
            if stat.latest_value is not None:
                range_indicator = "✅" if stat.in_range else "⚠️"
                val_str = f"{stat.latest_value}"
                if stat.count > 1:
                    val_str += f" (min {stat.min_value} / max {stat.max_value})"
                lines.append(
                    f"  {range_indicator} {stat.analyte}: "
                    f"{val_str} {stat.latest_unit} [{stat.count}x]"
                )
        lines.append("")

    lines.append(f"⚠️ {kpi.disclaimer}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
# Clinic Summary (Admin Only)
# ═══════════════════════════════════════════════════════════

def generate_clinic_summary(
    all_patient_kpis: list[PatientKPI],
    total_docs: int,
) -> ClinicKPI:
    """สร้างรายงาน KPI รวมคลินิก — admin only.

    Args:
        all_patient_kpis: list ของ PatientKPI จากทุกผู้ป่วย
        total_docs: จำนวนเอกสารทั้งหมดในระบบ

    Returns:
        ClinicKPI (aggregate only — no patient-specific data)
    """
    clinic = ClinicKPI(
        total_patients=len(all_patient_kpis),
        total_documents=total_docs,
    )

    ckd_dist: dict[str, int] = {}
    abnormal_counts: dict[str, int] = {}
    critical_patients = 0
    total_lab = 0
    total_ca = 0

    for pkpi in all_patient_kpis:
        total_lab += pkpi.total_lab_reports
        total_ca += pkpi.total_clinical_analyses

        # CKD distribution
        stage = pkpi.latest_ckd_stage
        ckd_dist[stage] = ckd_dist.get(stage, 0) + 1

        # Critical alert patients
        if pkpi.critical_count > 0:
            critical_patients += 1

        # Abnormal analyte counts
        for stat in pkpi.analyte_stats:
            if not stat.in_range:
                abnormal_counts[stat.analyte] = (
                    abnormal_counts.get(stat.analyte, 0) + 1
                )

    clinic.total_lab_reports = total_lab
    clinic.total_clinical_analyses = total_ca
    clinic.ckd_stage_distribution = ckd_dist
    clinic.critical_alert_patients = critical_patients
    clinic.analyte_abnormal_counts = abnormal_counts

    return clinic


def clinic_summary_text_th(kpi: ClinicKPI) -> str:
    """สร้างข้อความรายงานรวมคลินิกภาษาไทย — admin only."""
    lines = [
        "📊 รายงาน KPI คลินิกระนอง รักษ์ไต",
        f"วันที่: {kpi.report_date}",
        "",
        f"👥 จำนวนผู้ป่วย: {kpi.total_patients} ราย",
        f"📄 เอกสารทั้งหมด: {kpi.total_documents} ฉบับ",
        f"🔬 ผลตรวจ Lab: {kpi.total_lab_reports} ฉบับ",
        f"📊 วิเคราะห์ทางคลินิก: {kpi.total_clinical_analyses} ฉบับ",
        "",
    ]

    # CKD Stage Distribution
    if kpi.ckd_stage_distribution:
        lines.append("🏥 การกระจาย CKD Stage:")
        for stage in ["G1", "G2", "G3a", "G3b", "G4", "G5", "UNKNOWN"]:
            count = kpi.ckd_stage_distribution.get(stage, 0)
            if count > 0:
                bar = "█" * min(count, 20)
                lines.append(f"  {stage}: {bar} ({count})")
        lines.append("")

    # Critical patients
    if kpi.critical_alert_patients > 0:
        lines.append(
            f"🔴 ผู้ป่วยที่มี Critical Alert: "
            f"{kpi.critical_alert_patients} ราย"
        )
        lines.append("")

    # Abnormal analytes
    if kpi.analyte_abnormal_counts:
        lines.append("⚠️ ผลตรวจผิดปกติ (จำนวนผู้ป่วย):")
        for analyte, count in sorted(
            kpi.analyte_abnormal_counts.items(),
            key=lambda x: x[1],
            reverse=True,
        ):
            lines.append(f"  • {analyte}: {count} ราย")
        lines.append("")

    lines.append(f"⚠️ {kpi.disclaimer}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
# Phase 2B: Nephrology KPI Dashboard
# ═══════════════════════════════════════════════════════════

# Target ranges for nephrology KPIs — configurable by clinical leadership
# ห้ามถือว่าผู้ป่วย CKD และ HD ทุกรายมีเป้าหมายเดียวกัน

NEPHROLOGY_TARGETS: dict[str, dict] = {
    # Anemia indicators
    "Hemoglobin": {
        "target_min": 10.0, "target_max": 12.0,
        "unit": "g/dL",
        "category": "anemia",
        "label_th": "ฮีโมโกลบิน (เป้าหมาย HD)",
    },
    "Hematocrit": {
        "target_min": 30.0, "target_max": 36.0,
        "unit": "%",
        "category": "anemia",
        "label_th": "ฮีมาโตคริต",
    },
    "Ferritin": {
        "target_min": 200, "target_max": 500,
        "unit": "ng/mL",
        "category": "anemia",
        "label_th": "ธาตุเหล็กสะสม",
    },
    "TSAT": {
        "target_min": 20, "target_max": 50,
        "unit": "%",
        "category": "anemia",
        "label_th": "Transferrin Saturation",
    },
    # Dialysis adequacy
    "Kt/V": {
        "target_min": 1.2, "target_max": 999,
        "unit": "",
        "category": "adequacy",
        "label_th": "ประสิทธิภาพฟอกเลือด",
    },
    # CKD-MBD
    "Calcium": {
        "target_min": 8.4, "target_max": 10.2,
        "unit": "mg/dL",
        "category": "ckd_mbd",
        "label_th": "แคลเซียม",
    },
    "Phosphate": {
        "target_min": 3.5, "target_max": 5.5,
        "unit": "mg/dL",
        "category": "ckd_mbd",
        "label_th": "ฟอสเฟต",
    },
    "iPTH": {
        "target_min": 150, "target_max": 600,
        "unit": "pg/mL",
        "category": "ckd_mbd",
        "label_th": "Intact PTH",
    },
    # Nutrition
    "Albumin": {
        "target_min": 3.5, "target_max": 999,
        "unit": "g/dL",
        "category": "nutrition",
        "label_th": "อัลบูมิน — ภาวะโภชนาการ",
    },
}

CATEGORY_NAMES_TH: dict[str, str] = {
    "anemia": "📊 ตัวชี้วัดภาวะซีด (Anemia)",
    "adequacy": "🔬 ประสิทธิภาพการฟอกเลือด (Dialysis Adequacy)",
    "ckd_mbd": "🦴 ตัวชี้วัด CKD-MBD",
    "nutrition": "🍽️ ตัวชี้วัดภาวะโภชนาการ",
}


@dataclass
class NephrologyKPIItem:
    """ตัวชี้วัด KPI หนึ่งรายการ."""
    analyte: str
    category: str
    label_th: str = ""
    latest_value: Optional[float] = None
    latest_date: str = ""
    unit: str = ""
    target_min: float = 0
    target_max: float = 999
    on_target: bool = False
    patient_count: int = 0        # จำนวนผู้ป่วยที่มีข้อมูล
    on_target_count: int = 0      # จำนวนที่อยู่ในเป้าหมาย

    def to_dict(self) -> dict:
        d: dict = {
            "analyte": self.analyte,
            "category": self.category,
            "label_th": self.label_th,
            "target_range": f"{self.target_min}-{self.target_max}",
            "unit": self.unit,
            "on_target": self.on_target,
        }
        if self.latest_value is not None:
            d["latest_value"] = self.latest_value
            d["latest_date"] = self.latest_date
        if self.patient_count > 0:
            d["patient_count"] = self.patient_count
            d["on_target_count"] = self.on_target_count
            pct = (self.on_target_count / self.patient_count * 100
                   if self.patient_count > 0 else 0)
            d["on_target_percent"] = round(pct, 1)
        return d


@dataclass
class NephrologyDashboard:
    """แดชบอร์ด KPI โรคไต — Phase 2B.

    กฎความปลอดภัย:
      - เป้าหมายเป็น configurable defaults
      - ห้ามถือว่าผู้ป่วยทุกรายมีเป้าหมายเดียวกัน
      - ข้อมูลประกอบการพิจารณาเท่านั้น
    """
    report_date: str = ""
    # Per-patient mode
    owner_hash: str = ""
    kpi_items: list[NephrologyKPIItem] = field(default_factory=list)
    # Operational KPIs
    medication_review_total: int = 0
    medication_review_completed: int = 0
    missing_investigation_count: int = 0
    pending_approval_count: int = 0
    critical_alert_unacknowledged: int = 0
    critical_alert_total: int = 0
    avg_acknowledgement_hours: Optional[float] = None
    disclaimer: str = (
        "⚠️ รายงาน KPI นี้เป็นข้อมูลประกอบ — "
        "เป้าหมายอาจแตกต่างตามผู้ป่วยแต่ละราย"
    )

    def __post_init__(self):
        if not self.report_date:
            self.report_date = datetime.now().strftime("%Y-%m-%d")

    def to_dict(self) -> dict:
        d: dict = {
            "report_date": self.report_date,
            "kpi_items": [k.to_dict() for k in self.kpi_items],
            "medication_review_total": self.medication_review_total,
            "medication_review_completed": self.medication_review_completed,
            "missing_investigation_count": self.missing_investigation_count,
            "pending_approval_count": self.pending_approval_count,
            "critical_alert_unacknowledged": self.critical_alert_unacknowledged,
            "critical_alert_total": self.critical_alert_total,
            "disclaimer": self.disclaimer,
        }
        if self.owner_hash:
            d["owner_hash_prefix"] = self.owner_hash[:8]
        if self.avg_acknowledgement_hours is not None:
            d["avg_acknowledgement_hours"] = round(
                self.avg_acknowledgement_hours, 1
            )
        med_pct = 0
        if self.medication_review_total > 0:
            med_pct = (
                self.medication_review_completed
                / self.medication_review_total * 100
            )
        d["medication_review_percent"] = round(med_pct, 1)
        return d


def generate_nephrology_dashboard(
    lab_reports: list[dict],
    owner_hash: str = "",
    pending_approvals: int = 0,
    unacked_alerts: int = 0,
    total_alerts: int = 0,
    med_review_total: int = 0,
    med_review_completed: int = 0,
    missing_investigations: int = 0,
    avg_ack_hours: Optional[float] = None,
) -> NephrologyDashboard:
    """สร้างแดชบอร์ด KPI โรคไต — Phase 2B.

    Args:
        lab_reports: list ของ lab report dicts
        owner_hash: hash ของเจ้าของ (empty=clinic-wide)
        pending_approvals: จำนวน lab ที่รอ physician approval
        unacked_alerts: จำนวน critical alerts ที่ยังไม่ acknowledge
        total_alerts: จำนวน critical alerts ทั้งหมด
        med_review_total: จำนวนผู้ป่วยทั้งหมดที่ต้อง review ยา
        med_review_completed: จำนวนที่ review แล้ว
        missing_investigations: จำนวนการตรวจที่ขาด
        avg_ack_hours: เวลาเฉลี่ยในการ acknowledge (ชั่วโมง)

    Returns:
        NephrologyDashboard
    """
    dashboard = NephrologyDashboard(
        owner_hash=owner_hash,
        pending_approval_count=pending_approvals,
        critical_alert_unacknowledged=unacked_alerts,
        critical_alert_total=total_alerts,
        medication_review_total=med_review_total,
        medication_review_completed=med_review_completed,
        missing_investigation_count=missing_investigations,
        avg_acknowledgement_hours=avg_ack_hours,
    )

    # Build latest value per analyte from lab reports
    latest_values: dict[str, tuple[float, str, str]] = {}
    for report in lab_reports:
        specimen_date = report.get("specimen_date", "")
        for result in report.get("results", []):
            analyte = result.get("analyte", "")
            if not analyte:
                continue
            try:
                value = float(result.get("value", 0))
            except (TypeError, ValueError):
                continue
            unit = result.get("unit", "")
            existing = latest_values.get(analyte)
            if existing is None or specimen_date > existing[1]:
                latest_values[analyte] = (value, specimen_date, unit)

    # Generate KPI items for configured targets
    for analyte, target in NEPHROLOGY_TARGETS.items():
        item = NephrologyKPIItem(
            analyte=analyte,
            category=target["category"],
            label_th=target.get("label_th", analyte),
            target_min=target["target_min"],
            target_max=target["target_max"],
            unit=target.get("unit", ""),
        )

        latest = latest_values.get(analyte)
        if latest:
            item.latest_value = latest[0]
            item.latest_date = latest[1]
            item.unit = latest[2] or item.unit
            item.on_target = (
                target["target_min"] <= latest[0] <= target["target_max"]
            )

        dashboard.kpi_items.append(item)

    return dashboard


def generate_clinic_nephrology_dashboard(
    all_patient_kpis: list[PatientKPI],
    pending_approvals: int = 0,
    unacked_alerts: int = 0,
    total_alerts: int = 0,
    med_review_total: int = 0,
    med_review_completed: int = 0,
    missing_investigations: int = 0,
    avg_ack_hours: Optional[float] = None,
) -> NephrologyDashboard:
    """สร้างแดชบอร์ด KPI โรคไตรวมทั้งคลินิก — Phase 2B.

    Args:
        all_patient_kpis: list ของ PatientKPI ทุกผู้ป่วย
        (other args same as generate_nephrology_dashboard)

    Returns:
        NephrologyDashboard (clinic-wide aggregate)
    """
    dashboard = NephrologyDashboard(
        pending_approval_count=pending_approvals,
        critical_alert_unacknowledged=unacked_alerts,
        critical_alert_total=total_alerts,
        medication_review_total=med_review_total,
        medication_review_completed=med_review_completed,
        missing_investigation_count=missing_investigations,
        avg_acknowledgement_hours=avg_ack_hours,
    )

    # Aggregate per-analyte stats across patients
    analyte_data: dict[str, list[tuple[float, bool]]] = {}
    for pkpi in all_patient_kpis:
        for stat in pkpi.analyte_stats:
            if stat.latest_value is None:
                continue
            target = NEPHROLOGY_TARGETS.get(stat.analyte)
            if target is None:
                continue
            on_target = (
                target["target_min"] <= stat.latest_value
                <= target["target_max"]
            )
            if stat.analyte not in analyte_data:
                analyte_data[stat.analyte] = []
            analyte_data[stat.analyte].append(
                (stat.latest_value, on_target)
            )

    for analyte, target in NEPHROLOGY_TARGETS.items():
        data = analyte_data.get(analyte, [])
        item = NephrologyKPIItem(
            analyte=analyte,
            category=target["category"],
            label_th=target.get("label_th", analyte),
            target_min=target["target_min"],
            target_max=target["target_max"],
            unit=target.get("unit", ""),
            patient_count=len(data),
            on_target_count=sum(1 for _, ot in data if ot),
        )
        if data:
            item.on_target = item.on_target_count == item.patient_count
        dashboard.kpi_items.append(item)

    return dashboard


def nephrology_dashboard_text_th(
    dashboard: NephrologyDashboard,
) -> str:
    """สร้างข้อความแดชบอร์ด KPI โรคไตภาษาไทย."""
    lines = [
        "🏥 แดชบอร์ด KPI โรคไต",
        f"วันที่: {dashboard.report_date}",
        "",
    ]

    # Operational KPIs first
    lines.append("📋 ตัวชี้วัดปฏิบัติงาน:")

    # Medication review completion
    if dashboard.medication_review_total > 0:
        pct = (
            dashboard.medication_review_completed
            / dashboard.medication_review_total * 100
        )
        lines.append(
            f"  💊 ทบทวนยาครบ: "
            f"{dashboard.medication_review_completed}"
            f"/{dashboard.medication_review_total} "
            f"({pct:.0f}%)"
        )

    # Missing investigations
    if dashboard.missing_investigation_count > 0:
        lines.append(
            f"  📅 การตรวจที่ขาด: "
            f"{dashboard.missing_investigation_count} รายการ"
        )

    # Physician approval backlog
    if dashboard.pending_approval_count > 0:
        lines.append(
            f"  📝 Lab รอ Physician Approval: "
            f"{dashboard.pending_approval_count}"
        )

    # Alert acknowledgement
    if dashboard.critical_alert_total > 0:
        ack_count = (
            dashboard.critical_alert_total
            - dashboard.critical_alert_unacknowledged
        )
        lines.append(
            f"  🔔 Critical Alert: "
            f"ack {ack_count}/{dashboard.critical_alert_total}"
        )
        if dashboard.critical_alert_unacknowledged > 0:
            lines.append(
                f"     ⚠️ ยังไม่ acknowledge: "
                f"{dashboard.critical_alert_unacknowledged}"
            )
    if dashboard.avg_acknowledgement_hours is not None:
        lines.append(
            f"  ⏱️ เวลาเฉลี่ย acknowledge: "
            f"{dashboard.avg_acknowledgement_hours:.1f} ชั่วโมง"
        )

    lines.append("")

    # Clinical KPIs by category
    items_by_category: dict[str, list[NephrologyKPIItem]] = {}
    for item in dashboard.kpi_items:
        cat = item.category
        if cat not in items_by_category:
            items_by_category[cat] = []
        items_by_category[cat].append(item)

    for cat_key in ["anemia", "adequacy", "ckd_mbd", "nutrition"]:
        items = items_by_category.get(cat_key, [])
        if not items:
            continue

        cat_name = CATEGORY_NAMES_TH.get(cat_key, cat_key)
        lines.append(cat_name)

        for item in items:
            if item.patient_count > 0:
                # Clinic-wide mode
                pct = (item.on_target_count / item.patient_count * 100
                       if item.patient_count > 0 else 0)
                lines.append(
                    f"  {item.label_th}: "
                    f"{item.on_target_count}/{item.patient_count} "
                    f"on target ({pct:.0f}%)"
                )
            elif item.latest_value is not None:
                # Individual mode
                emoji = "✅" if item.on_target else "⚠️"
                target_str = f"{item.target_min}"
                if item.target_max < 999:
                    target_str += f"-{item.target_max}"
                else:
                    target_str = f"≥{item.target_min}"
                lines.append(
                    f"  {emoji} {item.label_th}: "
                    f"{item.latest_value} {item.unit} "
                    f"(เป้า {target_str})"
                )
            else:
                lines.append(
                    f"  ❓ {item.label_th}: ไม่มีข้อมูล"
                )

        lines.append("")

    lines.append(f"⚠️ {dashboard.disclaimer}")
    return "\n".join(lines)
