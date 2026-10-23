#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Longitudinal Laboratory Monitoring Schedule — RANONG-RAKTAI-AI 2.0

ตารางการตรวจติดตามผลแล็บตามมาตรฐานการดูแลผู้ป่วยไตเรื้อรัง
- Configurable clinic workflow defaults (ไม่ใช่ universal medical mandates)
- Clinical leadership ต้องอนุมัติ testing intervals
- ไม่สั่งตรวจอัตโนมัติ — แจ้งเตือนเมื่อถึงกำหนด

กฎความปลอดภัย:
  - ไม่ใช้ AI ภายนอก — deterministic rules only
  - ไม่สั่งตรวจหรือสั่งการรักษา
  - ผลวิเคราะห์ต้องผ่าน Human Review
  - ไม่เก็บ patient identity — ใช้ owner_hash เท่านั้น
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional


# ═══════════════════════════════════════════════════════════
# Monitoring Period Definitions
# ═══════════════════════════════════════════════════════════

class MonitoringPeriod(str, Enum):
    """ช่วงเวลาติดตาม."""
    MONTHLY = "monthly"           # ทุกเดือน
    QUARTERLY = "quarterly"       # ทุก 3 เดือน
    SEMI_ANNUAL = "semi_annual"   # ทุก 6 เดือน
    ANNUAL = "annual"             # ทุกปี


# ── Configurable Clinic Workflow Defaults ──
# หมายเหตุ: เป็นค่าเริ่มต้นสำหรับ clinic workflow
# Clinical leadership ต้องอนุมัติ testing intervals และ guidelines ที่ใช้
# สามารถปรับแต่งได้โดย admin ผ่านการตั้งค่า

DEFAULT_MONITORING_SCHEDULE: dict[str, dict] = {
    # ── รายเดือน ──
    "CBC": {
        "period": MonitoringPeriod.MONTHLY,
        "analytes": ["Hb", "Hct", "WBC", "Platelet"],
        "description_th": "ตรวจนับเม็ดเลือด",
        "days_interval": 30,
    },
    "BUN": {
        "period": MonitoringPeriod.MONTHLY,
        "analytes": ["BUN"],
        "description_th": "สารยูเรียในเลือด",
        "days_interval": 30,
    },
    "Creatinine": {
        "period": MonitoringPeriod.MONTHLY,
        "analytes": ["Creatinine", "eGFR"],
        "description_th": "การทำงานของไต",
        "days_interval": 30,
    },
    "Electrolytes": {
        "period": MonitoringPeriod.MONTHLY,
        "analytes": ["Na", "K", "Cl", "HCO3"],
        "description_th": "เกลือแร่ในเลือด",
        "days_interval": 30,
    },
    "Calcium": {
        "period": MonitoringPeriod.MONTHLY,
        "analytes": ["Ca", "Calcium"],
        "description_th": "แคลเซียมในเลือด",
        "days_interval": 30,
    },
    "Phosphate": {
        "period": MonitoringPeriod.MONTHLY,
        "analytes": ["PO4", "Phosphate"],
        "description_th": "ฟอสเฟตในเลือด",
        "days_interval": 30,
    },

    # ── ทุก 3 เดือน ──
    "FBS": {
        "period": MonitoringPeriod.QUARTERLY,
        "analytes": ["FBS", "Glucose"],
        "description_th": "น้ำตาลในเลือด",
        "days_interval": 90,
    },
    "Albumin": {
        "period": MonitoringPeriod.QUARTERLY,
        "analytes": ["Albumin"],
        "description_th": "อัลบูมินในเลือด",
        "days_interval": 90,
    },
    "KtV": {
        "period": MonitoringPeriod.QUARTERLY,
        "analytes": ["Kt/V", "KtV"],
        "description_th": "ประสิทธิภาพการฟอกไต",
        "days_interval": 90,
    },
    "MedicationReview": {
        "period": MonitoringPeriod.QUARTERLY,
        "analytes": [],
        "description_th": "ทบทวนรายการยา",
        "days_interval": 90,
        "type": "review",
    },

    # ── ทุก 6 เดือน ──
    "PTH": {
        "period": MonitoringPeriod.SEMI_ANNUAL,
        "analytes": ["PTH", "iPTH"],
        "description_th": "ฮอร์โมนพาราไทรอยด์",
        "days_interval": 180,
    },
    "IronProfile": {
        "period": MonitoringPeriod.SEMI_ANNUAL,
        "analytes": ["Ferritin", "TIBC", "Iron", "TSAT"],
        "description_th": "ระดับธาตุเหล็ก",
        "days_interval": 180,
    },
    "HBV": {
        "period": MonitoringPeriod.SEMI_ANNUAL,
        "analytes": ["HBsAg", "AntiHBs"],
        "description_th": "ตรวจไวรัสตับอักเสบ B",
        "days_interval": 180,
    },
    "HCV": {
        "period": MonitoringPeriod.SEMI_ANNUAL,
        "analytes": ["AntiHCV"],
        "description_th": "ตรวจไวรัสตับอักเสบ C",
        "days_interval": 180,
    },

    # ── รายปี ──
    "AnnualReview": {
        "period": MonitoringPeriod.ANNUAL,
        "analytes": [],
        "description_th": "ประเมินสุขภาพรวมประจำปี",
        "days_interval": 365,
        "type": "review",
    },
}


# ═══════════════════════════════════════════════════════════
# Data Classes
# ═══════════════════════════════════════════════════════════

@dataclass
class InvestigationStatus:
    """สถานะการตรวจแต่ละรายการ."""
    name: str
    period: str  # MonitoringPeriod value
    description_th: str
    last_done: str = ""        # ISO date of last result
    next_due: str = ""         # ISO date when next is due
    days_overdue: int = 0      # จำนวนวันที่เกินกำหนด (0 = ยังไม่เกิน)
    is_overdue: bool = False
    is_due_soon: bool = False  # ภายใน 7 วัน

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MonitoringReport:
    """รายงานสถานะการติดตามผลตรวจ."""
    owner_hash_prefix: str  # 8 chars only
    generated_at: str = ""
    overdue_items: list[InvestigationStatus] = field(default_factory=list)
    due_soon_items: list[InvestigationStatus] = field(default_factory=list)
    up_to_date_items: list[InvestigationStatus] = field(default_factory=list)
    missing_items: list[InvestigationStatus] = field(default_factory=list)

    def __post_init__(self):
        if not self.generated_at:
            self.generated_at = datetime.now().isoformat(timespec="seconds")

    @property
    def total_overdue(self) -> int:
        return len(self.overdue_items)

    @property
    def total_missing(self) -> int:
        return len(self.missing_items)

    def to_dict(self) -> dict:
        return {
            "owner_hash_prefix": self.owner_hash_prefix,
            "generated_at": self.generated_at,
            "overdue": [i.to_dict() for i in self.overdue_items],
            "due_soon": [i.to_dict() for i in self.due_soon_items],
            "up_to_date": [i.to_dict() for i in self.up_to_date_items],
            "missing": [i.to_dict() for i in self.missing_items],
            "summary": {
                "total_overdue": self.total_overdue,
                "total_missing": self.total_missing,
            },
        }


# ═══════════════════════════════════════════════════════════
# Functions
# ═══════════════════════════════════════════════════════════

def check_monitoring_status(
    lab_reports: list[dict],
    owner_hash: str,
    schedule: dict | None = None,
    reference_date: datetime | None = None,
) -> MonitoringReport:
    """ตรวจสอบสถานะการติดตามผลตรวจ.

    Args:
        lab_reports: รายการ lab report dicts ของผู้ป่วย
        owner_hash: owner hash (จะใช้แค่ prefix 8 ตัว)
        schedule: custom schedule (ถ้าไม่ระบุจะใช้ DEFAULT)
        reference_date: วันที่อ้างอิง (ถ้าไม่ระบุจะใช้วันนี้)

    Returns:
        MonitoringReport
    """
    if schedule is None:
        schedule = DEFAULT_MONITORING_SCHEDULE
    if reference_date is None:
        reference_date = datetime.now()

    report = MonitoringReport(owner_hash_prefix=owner_hash[:8])

    # รวบรวม last-seen date ของแต่ละ analyte
    last_seen: dict[str, str] = {}  # analyte → latest ISO date
    for lr in lab_reports:
        results = lr.get("results", [])
        specimen_date = lr.get("specimen_date", "")
        for r in results:
            analyte = r.get("analyte", "")
            if analyte and specimen_date:
                if analyte not in last_seen or specimen_date > last_seen[analyte]:
                    last_seen[analyte] = specimen_date

    for item_name, config in schedule.items():
        period = config.get("period", MonitoringPeriod.MONTHLY)
        analytes = config.get("analytes", [])
        desc = config.get("description_th", item_name)
        days_interval = config.get("days_interval", 30)
        item_type = config.get("type", "lab")

        # หา last_done ล่าสุดจาก analytes ทั้งหมดของ item นี้
        latest_date = ""
        if item_type == "lab" and analytes:
            for a in analytes:
                if a in last_seen and (not latest_date or last_seen[a] > latest_date):
                    latest_date = last_seen[a]

        status = InvestigationStatus(
            name=item_name,
            period=period if isinstance(period, str) else period.value,
            description_th=desc,
            last_done=latest_date,
        )

        if not latest_date:
            # ไม่เคยมีผล — missing
            if item_type == "lab":
                status.is_overdue = True
                report.missing_items.append(status)
            continue

        # คำนวณ next_due
        try:
            last_dt = datetime.fromisoformat(latest_date[:10])
            next_due_dt = last_dt + timedelta(days=days_interval)
            status.next_due = next_due_dt.strftime("%Y-%m-%d")

            diff = (reference_date - next_due_dt).days
            if diff > 0:
                status.is_overdue = True
                status.days_overdue = diff
                report.overdue_items.append(status)
            elif diff >= -7:
                status.is_due_soon = True
                report.due_soon_items.append(status)
            else:
                report.up_to_date_items.append(status)
        except (ValueError, TypeError):
            report.missing_items.append(status)

    return report


def monitoring_report_text_th(report: MonitoringReport) -> str:
    """สร้างข้อความรายงานการติดตามภาษาไทย.

    หมายเหตุ: ตารางเป็น clinic workflow defaults
    ไม่ใช่ universal medical mandates
    """
    lines = ["📋 สถานะการตรวจติดตาม"]
    lines.append(f"ณ วันที่: {report.generated_at[:10]}")
    lines.append("")

    if report.overdue_items:
        lines.append(f"⚠️ เกินกำหนด ({len(report.overdue_items)} รายการ):")
        for item in report.overdue_items:
            lines.append(
                f"  • {item.description_th} — เกิน {item.days_overdue} วัน"
            )
        lines.append("")

    if report.due_soon_items:
        lines.append(f"🔔 ใกล้ถึงกำหนด ({len(report.due_soon_items)} รายการ):")
        for item in report.due_soon_items:
            lines.append(
                f"  • {item.description_th} — กำหนด {item.next_due}"
            )
        lines.append("")

    if report.missing_items:
        lines.append(f"❓ ยังไม่มีผล ({len(report.missing_items)} รายการ):")
        for item in report.missing_items:
            lines.append(f"  • {item.description_th}")
        lines.append("")

    if report.up_to_date_items:
        lines.append(f"✅ เป็นปัจจุบัน ({len(report.up_to_date_items)} รายการ)")

    lines.append("")
    lines.append(
        "⚕️ หมายเหตุ: ตารางตรวจเป็นแนวทางเริ่มต้นของคลินิก "
        "การตรวจจริงขึ้นอยู่กับดุลยพินิจของแพทย์"
    )

    return "\n".join(lines)
