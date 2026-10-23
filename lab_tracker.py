#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lab Tracking & Trend System — Kidney Clinical Intelligence Platform 2.0

ระบบติดตามผล Lab รายเดือน/3เดือน/6เดือน/รายปี:
  1. TrendPoint — จุดข้อมูล Lab ณ เวลาใดเวลาหนึ่ง
  2. LabTrend — แนวโน้ม Lab สำหรับ analyte หนึ่งรายการ
  3. TrendAnalysis — วิเคราะห์แนวโน้มทั้งหมดของผู้ป่วย
  4. TrendAlert — แจ้งเตือนแนวโน้มผิดปกติ

กฎความปลอดภัย:
  - ไม่ใช้ AI ภายนอก — วิเคราะห์ด้วย deterministic rules
  - ไม่เก็บ patient identity — ใช้ owner_hash เท่านั้น
  - ไม่สั่งยาหรือสั่งการรักษา — แจ้งเตือนและแนะนำเท่านั้น
  - ผลวิเคราะห์ต้องผ่าน Human Review
  - ใช้ข้อมูล DEMO/สังเคราะห์ในการทดสอบ
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional


# ═══════════════════════════════════════════════════════════
# Enums
# ═══════════════════════════════════════════════════════════

class TrendDirection(str, Enum):
    """ทิศทางแนวโน้ม."""
    INCREASING = "increasing"
    DECREASING = "decreasing"
    STABLE = "stable"
    INSUFFICIENT_DATA = "insufficient_data"


class AlertSeverity(str, Enum):
    """ระดับความรุนแรงของการแจ้งเตือน."""
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class TrackingPeriod(str, Enum):
    """ช่วงเวลาติดตาม."""
    MONTHLY = "monthly"        # รายเดือน
    QUARTERLY = "quarterly"    # ราย 3 เดือน
    SEMI_ANNUAL = "semi_annual"  # ราย 6 เดือน
    ANNUAL = "annual"          # รายปี


# ═══════════════════════════════════════════════════════════
# Data Classes
# ═══════════════════════════════════════════════════════════

@dataclass
class TrendPoint:
    """จุดข้อมูล Lab ณ เวลาใดเวลาหนึ่ง."""
    date: str                  # ISO date string (YYYY-MM-DD)
    value: float               # ค่า Lab
    unit: str                  # หน่วย
    source_document_id: str    # อ้างอิงถึงเอกสารต้นทาง
    status: str = "pending_review"  # สถานะการตรวจสอบ

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "value": self.value,
            "unit": self.unit,
            "source_document_id": self.source_document_id,
            "status": self.status,
        }


@dataclass
class TrendAlert:
    """การแจ้งเตือนแนวโน้ม."""
    analyte: str               # ชื่อ analyte
    severity: str              # AlertSeverity value
    message: str               # ข้อความแจ้งเตือนภาษาไทย
    direction: str             # TrendDirection value
    change_percent: float      # % เปลี่ยนแปลง
    period: str                # ช่วงเวลาที่วิเคราะห์
    recommendation: str = ""   # คำแนะนำ

    def to_dict(self) -> dict:
        return {
            "analyte": self.analyte,
            "severity": self.severity,
            "message": self.message,
            "direction": self.direction,
            "change_percent": round(self.change_percent, 1),
            "period": self.period,
            "recommendation": self.recommendation,
        }


@dataclass
class LabTrend:
    """แนวโน้ม Lab สำหรับ analyte หนึ่งรายการ."""
    analyte: str
    unit: str
    points: list[TrendPoint] = field(default_factory=list)
    direction: str = TrendDirection.INSUFFICIENT_DATA.value
    change_percent: float = 0.0
    alerts: list[TrendAlert] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "analyte": self.analyte,
            "unit": self.unit,
            "points": [p.to_dict() for p in self.points],
            "point_count": len(self.points),
            "direction": self.direction,
            "change_percent": round(self.change_percent, 1),
            "alerts": [a.to_dict() for a in self.alerts],
        }


@dataclass
class TrendAnalysis:
    """ผลวิเคราะห์แนวโน้มทั้งหมด."""
    owner_hash: str            # hash ของเจ้าของ (ไม่ใช่ patient_id)
    period: str                # TrackingPeriod value
    from_date: str             # วันที่เริ่มต้น
    to_date: str               # วันที่สิ้นสุด
    trends: list[LabTrend] = field(default_factory=list)
    alerts: list[TrendAlert] = field(default_factory=list)
    analyzed_at: str = ""
    disclaimer: str = (
        "⚠️ ข้อมูลแนวโน้มนี้เป็นข้อมูลประกอบการพิจารณา "
        "ไม่ใช่การวินิจฉัย — กรุณาปรึกษาแพทย์"
    )

    def __post_init__(self):
        if not self.analyzed_at:
            self.analyzed_at = datetime.now().isoformat()

    def has_alerts(self) -> bool:
        return len(self.alerts) > 0

    def critical_alerts(self) -> list[TrendAlert]:
        return [a for a in self.alerts
                if a.severity == AlertSeverity.CRITICAL.value]

    def to_dict(self) -> dict:
        return {
            "owner_hash_prefix": self.owner_hash[:8] if self.owner_hash else "",
            "period": self.period,
            "from_date": self.from_date,
            "to_date": self.to_date,
            "trends": [t.to_dict() for t in self.trends],
            "trend_count": len(self.trends),
            "alerts": [a.to_dict() for a in self.alerts],
            "alert_count": len(self.alerts),
            "critical_count": len(self.critical_alerts()),
            "analyzed_at": self.analyzed_at,
            "disclaimer": self.disclaimer,
        }

    def summary_text_th(self) -> str:
        """สร้างข้อความสรุปแนวโน้มภาษาไทย."""
        return _generate_trend_summary(self)


# ═══════════════════════════════════════════════════════════
# Trend Alert Thresholds — เกณฑ์แจ้งเตือนแนวโน้ม
# ═══════════════════════════════════════════════════════════

# analyte → { "rapid_change_%": threshold, "direction_concern": ... }
TREND_THRESHOLDS: dict[str, dict] = {
    "Creatinine": {
        "rapid_increase": 30.0,   # Cr เพิ่ม >30% → warning
        "critical_increase": 50.0, # Cr เพิ่ม >50% → critical
        "concern_direction": "increasing",
        "alert_th": "ค่า Creatinine มีแนวโน้มเพิ่มขึ้น — ไตอาจทำงานลดลง",
        "critical_th": "ค่า Creatinine เพิ่มขึ้นอย่างรวดเร็ว — พิจารณาประเมินการทำงานของไต",
    },
    "eGFR": {
        "rapid_decrease": 25.0,
        "critical_decrease": 40.0,
        "concern_direction": "decreasing",
        "alert_th": "ค่า eGFR มีแนวโน้มลดลง — การทำงานของไตอาจลดลง",
        "critical_th": "ค่า eGFR ลดลงอย่างรวดเร็ว — พิจารณาปรึกษาแพทย์โรคไต",
    },
    "Hemoglobin": {
        "rapid_decrease": 20.0,
        "critical_decrease": 30.0,
        "concern_direction": "decreasing",
        "alert_th": "ค่า Hemoglobin มีแนวโน้มลดลง — อาจต้องพิจารณาสาเหตุของภาวะซีด",
        "critical_th": "ค่า Hemoglobin ลดลงอย่างรวดเร็ว — พิจารณาประเมินภาวะซีด",
    },
    "Potassium": {
        "rapid_increase": 25.0,
        "critical_increase": 40.0,
        "concern_direction": "increasing",
        "alert_th": "ค่า Potassium มีแนวโน้มเพิ่มขึ้น",
        "critical_th": "ค่า Potassium เพิ่มขึ้นอย่างรวดเร็ว — เสี่ยงภาวะ Hyperkalemia",
    },
    "Phosphate": {
        "rapid_increase": 25.0,
        "critical_increase": 40.0,
        "concern_direction": "increasing",
        "alert_th": "ค่า Phosphate มีแนวโน้มเพิ่มขึ้น",
        "critical_th": "ค่า Phosphate เพิ่มขึ้นอย่างรวดเร็ว — เสี่ยงภาวะ CKD-MBD",
    },
    "Albumin": {
        "rapid_decrease": 20.0,
        "critical_decrease": 30.0,
        "concern_direction": "decreasing",
        "alert_th": "ค่า Albumin มีแนวโน้มลดลง — อาจมีภาวะขาดสารอาหาร",
        "critical_th": "ค่า Albumin ลดลงอย่างรวดเร็ว — พิจารณาประเมินภาวะโภชนาการ",
    },
    "Kt/V": {
        "rapid_decrease": 15.0,
        "critical_decrease": 25.0,
        "concern_direction": "decreasing",
        "alert_th": "ค่า Kt/V มีแนวโน้มลดลง — ประสิทธิภาพการฟอกเลือดอาจลดลง",
        "critical_th": "ค่า Kt/V ลดลงอย่างรวดเร็ว — พิจารณาปรับการฟอกเลือด",
    },
}

# ═══════════════════════════════════════════════════════════
# Period → days mapping
# ═══════════════════════════════════════════════════════════

PERIOD_DAYS: dict[str, int] = {
    TrackingPeriod.MONTHLY.value: 30,
    TrackingPeriod.QUARTERLY.value: 90,
    TrackingPeriod.SEMI_ANNUAL.value: 180,
    TrackingPeriod.ANNUAL.value: 365,
}

PERIOD_NAMES_TH: dict[str, str] = {
    TrackingPeriod.MONTHLY.value: "รายเดือน",
    TrackingPeriod.QUARTERLY.value: "ราย 3 เดือน",
    TrackingPeriod.SEMI_ANNUAL.value: "ราย 6 เดือน",
    TrackingPeriod.ANNUAL.value: "รายปี",
}


# ═══════════════════════════════════════════════════════════
# Core Functions
# ═══════════════════════════════════════════════════════════

def collect_trend_points(
    lab_reports: list[dict],
    analyte: str,
) -> list[TrendPoint]:
    """รวบรวมจุดข้อมูลสำหรับ analyte จาก lab reports หลายฉบับ.

    Args:
        lab_reports: list ของ lab report dicts (จาก LabReport.to_dict())
        analyte: ชื่อ analyte ที่ต้องการ

    Returns:
        list ของ TrendPoint เรียงตามวันที่
    """
    points: list[TrendPoint] = []
    for report in lab_reports:
        date = report.get("specimen_date", "")
        doc_id = report.get("source_document_id", "")
        status = report.get("status", "pending_review")

        for result in report.get("results", []):
            if result.get("analyte") == analyte:
                try:
                    value = float(result.get("value", 0))
                except (TypeError, ValueError):
                    continue
                points.append(TrendPoint(
                    date=date,
                    value=value,
                    unit=result.get("unit", ""),
                    source_document_id=doc_id,
                    status=status,
                ))
    # เรียงตามวันที่
    points.sort(key=lambda p: p.date)
    return points


def calculate_trend_direction(
    points: list[TrendPoint],
    threshold_percent: float = 5.0,
) -> tuple[str, float]:
    """คำนวณทิศทางแนวโน้มจากจุดข้อมูล.

    Args:
        points: list ของ TrendPoint เรียงตามวันที่
        threshold_percent: % ขั้นต่ำที่ถือว่าเปลี่ยนแปลง

    Returns:
        (direction, change_percent)
    """
    if len(points) < 2:
        return TrendDirection.INSUFFICIENT_DATA.value, 0.0

    first_value = points[0].value
    last_value = points[-1].value

    if first_value == 0:
        if last_value == 0:
            return TrendDirection.STABLE.value, 0.0
        return TrendDirection.INCREASING.value, 100.0

    change_pct = ((last_value - first_value) / abs(first_value)) * 100

    if abs(change_pct) < threshold_percent:
        return TrendDirection.STABLE.value, change_pct
    elif change_pct > 0:
        return TrendDirection.INCREASING.value, change_pct
    else:
        return TrendDirection.DECREASING.value, change_pct


def check_trend_alerts(
    analyte: str,
    direction: str,
    change_percent: float,
    period: str,
) -> list[TrendAlert]:
    """ตรวจสอบว่าแนวโน้มควรแจ้งเตือนหรือไม่.

    Args:
        analyte: ชื่อ analyte
        direction: TrendDirection value
        change_percent: % เปลี่ยนแปลง
        period: TrackingPeriod value

    Returns:
        list ของ TrendAlert
    """
    alerts: list[TrendAlert] = []
    thresholds = TREND_THRESHOLDS.get(analyte)
    if thresholds is None:
        return alerts

    concern_dir = thresholds.get("concern_direction", "")
    abs_change = abs(change_percent)

    # ตรวจสอบทิศทางที่น่าเป็นห่วง
    if direction == TrendDirection.INCREASING.value and concern_dir == "increasing":
        critical_thresh = thresholds.get("critical_increase", 999)
        rapid_thresh = thresholds.get("rapid_increase", 999)

        if abs_change >= critical_thresh:
            alerts.append(TrendAlert(
                analyte=analyte,
                severity=AlertSeverity.CRITICAL.value,
                message=thresholds.get("critical_th", ""),
                direction=direction,
                change_percent=change_percent,
                period=period,
                recommendation="พิจารณาตรวจเพิ่มเติมและปรึกษาแพทย์",
            ))
        elif abs_change >= rapid_thresh:
            alerts.append(TrendAlert(
                analyte=analyte,
                severity=AlertSeverity.WARNING.value,
                message=thresholds.get("alert_th", ""),
                direction=direction,
                change_percent=change_percent,
                period=period,
                recommendation="พิจารณาติดตามค่าใกล้ชิดขึ้น",
            ))

    elif direction == TrendDirection.DECREASING.value and concern_dir == "decreasing":
        critical_thresh = thresholds.get("critical_decrease", 999)
        rapid_thresh = thresholds.get("rapid_decrease", 999)

        if abs_change >= critical_thresh:
            alerts.append(TrendAlert(
                analyte=analyte,
                severity=AlertSeverity.CRITICAL.value,
                message=thresholds.get("critical_th", ""),
                direction=direction,
                change_percent=change_percent,
                period=period,
                recommendation="พิจารณาตรวจเพิ่มเติมและปรึกษาแพทย์",
            ))
        elif abs_change >= rapid_thresh:
            alerts.append(TrendAlert(
                analyte=analyte,
                severity=AlertSeverity.WARNING.value,
                message=thresholds.get("alert_th", ""),
                direction=direction,
                change_percent=change_percent,
                period=period,
                recommendation="พิจารณาติดตามค่าใกล้ชิดขึ้น",
            ))

    return alerts


def analyze_trends(
    lab_reports: list[dict],
    period: str = TrackingPeriod.QUARTERLY.value,
    owner_hash: str = "",
    reference_date: Optional[str] = None,
) -> TrendAnalysis:
    """วิเคราะห์แนวโน้ม Lab จาก reports หลายฉบับ.

    Args:
        lab_reports: list ของ lab report dicts
        period: TrackingPeriod value
        owner_hash: hash ของเจ้าของเอกสาร
        reference_date: วันที่อ้างอิง (default=today)

    Returns:
        TrendAnalysis
    """
    if reference_date:
        ref_date = datetime.fromisoformat(reference_date)
    else:
        ref_date = datetime.now()

    days = PERIOD_DAYS.get(period, 90)
    from_date = (ref_date - timedelta(days=days)).strftime("%Y-%m-%d")
    to_date = ref_date.strftime("%Y-%m-%d")

    # กรองเฉพาะ reports ในช่วงเวลา
    filtered_reports = []
    for r in lab_reports:
        specimen_date = r.get("specimen_date", "")
        if specimen_date and from_date <= specimen_date <= to_date:
            filtered_reports.append(r)

    # หา analytes ทั้งหมด
    all_analytes: set[str] = set()
    for r in filtered_reports:
        for result in r.get("results", []):
            a = result.get("analyte", "")
            if a:
                all_analytes.add(a)

    # วิเคราะห์แนวโน้มแต่ละ analyte
    trends: list[LabTrend] = []
    all_alerts: list[TrendAlert] = []

    for analyte in sorted(all_analytes):
        points = collect_trend_points(filtered_reports, analyte)
        if not points:
            continue

        direction, change_pct = calculate_trend_direction(points)
        alerts = check_trend_alerts(analyte, direction, change_pct, period)

        trend = LabTrend(
            analyte=analyte,
            unit=points[0].unit if points else "",
            points=points,
            direction=direction,
            change_percent=change_pct,
            alerts=alerts,
        )
        trends.append(trend)
        all_alerts.extend(alerts)

    return TrendAnalysis(
        owner_hash=owner_hash,
        period=period,
        from_date=from_date,
        to_date=to_date,
        trends=trends,
        alerts=all_alerts,
    )


# ═══════════════════════════════════════════════════════════
# Summary Text Generation
# ═══════════════════════════════════════════════════════════

def _generate_trend_summary(analysis: TrendAnalysis) -> str:
    """สร้างข้อความสรุปแนวโน้มภาษาไทย."""
    period_name = PERIOD_NAMES_TH.get(analysis.period, analysis.period)
    lines = [
        f"📊 สรุปแนวโน้มผลตรวจ ({period_name})",
        f"ช่วงเวลา: {analysis.from_date} ถึง {analysis.to_date}",
        "",
    ]

    if not analysis.trends:
        lines.append("ไม่พบข้อมูลผลตรวจในช่วงเวลาที่เลือก")
        lines.append("")
        lines.append(f"⚠️ {analysis.disclaimer}")
        return "\n".join(lines)

    # แจ้งเตือนก่อน
    critical_alerts = analysis.critical_alerts()
    warning_alerts = [
        a for a in analysis.alerts
        if a.severity == AlertSeverity.WARNING.value
    ]

    if critical_alerts:
        lines.append("🔴 แจ้งเตือนสำคัญ:")
        for a in critical_alerts:
            lines.append(
                f"  • {a.analyte}: {a.message} "
                f"(เปลี่ยน {a.change_percent:+.1f}%)"
            )
        lines.append("")

    if warning_alerts:
        lines.append("🟡 ข้อสังเกต:")
        for a in warning_alerts:
            lines.append(
                f"  • {a.analyte}: {a.message} "
                f"(เปลี่ยน {a.change_percent:+.1f}%)"
            )
        lines.append("")

    # สรุปแนวโน้มแต่ละ analyte
    direction_emoji = {
        TrendDirection.INCREASING.value: "📈",
        TrendDirection.DECREASING.value: "📉",
        TrendDirection.STABLE.value: "➡️",
        TrendDirection.INSUFFICIENT_DATA.value: "❓",
    }

    lines.append("📋 แนวโน้มรายตัว:")
    for trend in analysis.trends:
        emoji = direction_emoji.get(trend.direction, "❓")
        if len(trend.points) >= 2:
            first = trend.points[0]
            last = trend.points[-1]
            lines.append(
                f"  {emoji} {trend.analyte}: "
                f"{first.value} → {last.value} {trend.unit} "
                f"({trend.change_percent:+.1f}%) "
                f"[{len(trend.points)} ครั้ง]"
            )
        elif len(trend.points) == 1:
            p = trend.points[0]
            lines.append(
                f"  {emoji} {trend.analyte}: {p.value} {trend.unit} "
                f"[1 ครั้ง — ไม่มีข้อมูลเปรียบเทียบ]"
            )

    lines.append("")
    lines.append(f"⚠️ {analysis.disclaimer}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
# Phase 2B: Monitoring Schedule Configuration
# ═══════════════════════════════════════════════════════════
# ตารางตรวจเริ่มต้นสำหรับคลินิก — สามารถปรับได้โดยทีมแพทย์
# ไม่ใช่มาตรฐานการรักษาสากล

@dataclass
class MonitoringItem:
    """รายการตรวจในตารางติดตาม."""
    analyte: str              # ชื่อการตรวจ
    period: str               # TrackingPeriod value
    description_th: str = ""  # คำอธิบายภาษาไทย
    is_required: bool = True  # จำเป็นหรือแนะนำ
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "analyte": self.analyte,
            "period": self.period,
            "description_th": self.description_th,
            "is_required": self.is_required,
        }


# Default monitoring schedule — configurable by clinical leadership
DEFAULT_MONITORING_SCHEDULE: list[MonitoringItem] = [
    # ── Monthly ──
    MonitoringItem("CBC", TrackingPeriod.MONTHLY.value,
                   "ตรวจนับเม็ดเลือด"),
    MonitoringItem("BUN", TrackingPeriod.MONTHLY.value,
                   "Blood Urea Nitrogen"),
    MonitoringItem("Creatinine", TrackingPeriod.MONTHLY.value,
                   "Creatinine"),
    MonitoringItem("Potassium", TrackingPeriod.MONTHLY.value,
                   "โปตัสเซียม"),
    MonitoringItem("Sodium", TrackingPeriod.MONTHLY.value,
                   "โซเดียม"),
    MonitoringItem("Calcium", TrackingPeriod.MONTHLY.value,
                   "แคลเซียม"),
    MonitoringItem("Phosphate", TrackingPeriod.MONTHLY.value,
                   "ฟอสเฟต"),
    # ── Quarterly ──
    MonitoringItem("Fasting Glucose", TrackingPeriod.QUARTERLY.value,
                   "น้ำตาลในเลือด (อดอาหาร)", notes="FBS"),
    MonitoringItem("Albumin", TrackingPeriod.QUARTERLY.value,
                   "อัลบูมิน — ภาวะโภชนาการ"),
    MonitoringItem("Kt/V", TrackingPeriod.QUARTERLY.value,
                   "ประสิทธิภาพการฟอกเลือด"),
    MonitoringItem("Hemoglobin", TrackingPeriod.QUARTERLY.value,
                   "ฮีโมโกลบิน — ภาวะซีด"),
    # ── Semi-Annual ──
    MonitoringItem("iPTH", TrackingPeriod.SEMI_ANNUAL.value,
                   "Intact PTH — กระดูกและแร่ธาตุ"),
    MonitoringItem("Ferritin", TrackingPeriod.SEMI_ANNUAL.value,
                   "ธาตุเหล็กสะสม"),
    MonitoringItem("TSAT", TrackingPeriod.SEMI_ANNUAL.value,
                   "Transferrin Saturation"),
    MonitoringItem("HBsAg", TrackingPeriod.SEMI_ANNUAL.value,
                   "ตรวจไวรัสตับอักเสบ B"),
    MonitoringItem("Anti-HCV", TrackingPeriod.SEMI_ANNUAL.value,
                   "ตรวจไวรัสตับอักเสบ C"),
    # ── Annual ──
    MonitoringItem("ECG", TrackingPeriod.ANNUAL.value,
                   "คลื่นไฟฟ้าหัวใจ"),
    MonitoringItem("CXR", TrackingPeriod.ANNUAL.value,
                   "เอกซเรย์ปอด"),
    MonitoringItem("HbA1c", TrackingPeriod.ANNUAL.value,
                   "น้ำตาลสะสม", is_required=False,
                   notes="เฉพาะผู้ป่วยเบาหวาน"),
]


@dataclass
class MissingInvestigation:
    """การตรวจที่ขาดหายตามตาราง."""
    analyte: str
    expected_period: str      # TrackingPeriod value
    last_done_date: str = ""  # วันที่ตรวจครั้งล่าสุด (empty=ไม่เคยตรวจ)
    days_overdue: int = 0     # จำนวนวันเกินกำหนด
    description_th: str = ""
    is_required: bool = True

    def to_dict(self) -> dict:
        return {
            "analyte": self.analyte,
            "expected_period": self.expected_period,
            "last_done_date": self.last_done_date,
            "days_overdue": self.days_overdue,
            "description_th": self.description_th,
            "is_required": self.is_required,
        }


@dataclass
class ScheduleCompliance:
    """ผลการตรวจสอบความครบถ้วนตามตาราง.

    กฎความปลอดภัย:
      - ตารางเป็น configurable defaults ไม่ใช่มาตรฐานสากล
      - ต้องได้รับการอนุมัติจากทีมแพทย์
    """
    reference_date: str = ""
    total_items: int = 0
    completed_items: int = 0
    missing_items: list[MissingInvestigation] = field(default_factory=list)
    compliance_percent: float = 0.0
    disclaimer: str = (
        "⚠️ ตารางตรวจนี้เป็นค่าเริ่มต้นของคลินิก — "
        "ไม่ใช่มาตรฐานสากล — ต้องได้รับการอนุมัติจากทีมแพทย์"
    )

    def __post_init__(self):
        if not self.reference_date:
            self.reference_date = datetime.now().strftime("%Y-%m-%d")

    def to_dict(self) -> dict:
        return {
            "reference_date": self.reference_date,
            "total_items": self.total_items,
            "completed_items": self.completed_items,
            "missing_count": len(self.missing_items),
            "compliance_percent": round(self.compliance_percent, 1),
            "missing_items": [m.to_dict() for m in self.missing_items],
            "disclaimer": self.disclaimer,
        }


def check_schedule_compliance(
    lab_reports: list[dict],
    schedule: list[MonitoringItem] | None = None,
    reference_date: Optional[str] = None,
) -> ScheduleCompliance:
    """ตรวจสอบความครบถ้วนของการตรวจตามตาราง.

    Args:
        lab_reports: list ของ lab report dicts
        schedule: ตารางตรวจ (None = ใช้ค่าเริ่มต้น)
        reference_date: วันที่อ้างอิง (default=today)

    Returns:
        ScheduleCompliance

    กฎความปลอดภัย:
      - ตารางเป็น configurable defaults ไม่ใช่มาตรฐานสากล
    """
    if schedule is None:
        schedule = DEFAULT_MONITORING_SCHEDULE

    if reference_date:
        ref_date = datetime.fromisoformat(reference_date)
    else:
        ref_date = datetime.now()

    # Build last-done map: analyte → latest specimen_date
    last_done: dict[str, str] = {}
    for report in lab_reports:
        specimen_date = report.get("specimen_date", "")
        if not specimen_date:
            continue
        for result in report.get("results", []):
            analyte = result.get("analyte", "")
            if analyte:
                existing = last_done.get(analyte, "")
                if specimen_date > existing:
                    last_done[analyte] = specimen_date

    compliance = ScheduleCompliance(
        reference_date=ref_date.strftime("%Y-%m-%d"),
        total_items=len(schedule),
    )

    completed = 0
    missing: list[MissingInvestigation] = []

    for item in schedule:
        period_days = PERIOD_DAYS.get(item.period, 90)
        cutoff_date = (ref_date - timedelta(days=period_days)).strftime(
            "%Y-%m-%d"
        )

        done_date = last_done.get(item.analyte, "")
        if done_date and done_date >= cutoff_date:
            completed += 1
        else:
            # Calculate days overdue
            overdue = 0
            if done_date:
                try:
                    last_dt = datetime.fromisoformat(done_date)
                    expected_next = last_dt + timedelta(days=period_days)
                    if ref_date > expected_next:
                        overdue = (ref_date - expected_next).days
                except (ValueError, TypeError):
                    pass
            else:
                overdue = period_days  # never done

            missing.append(MissingInvestigation(
                analyte=item.analyte,
                expected_period=item.period,
                last_done_date=done_date,
                days_overdue=overdue,
                description_th=item.description_th,
                is_required=item.is_required,
            ))

    compliance.completed_items = completed
    compliance.missing_items = missing
    if compliance.total_items > 0:
        compliance.compliance_percent = (
            completed / compliance.total_items * 100
        )

    return compliance


def schedule_compliance_text_th(compliance: ScheduleCompliance) -> str:
    """สร้างข้อความสรุปความครบถ้วนภาษาไทย."""
    lines = [
        "📅 สรุปความครบถ้วนการตรวจตามตาราง",
        f"วันที่: {compliance.reference_date}",
        "",
        f"📊 ครบถ้วน: {compliance.completed_items}/{compliance.total_items} "
        f"({compliance.compliance_percent:.0f}%)",
        "",
    ]

    if compliance.missing_items:
        required_missing = [
            m for m in compliance.missing_items if m.is_required
        ]
        optional_missing = [
            m for m in compliance.missing_items if not m.is_required
        ]

        if required_missing:
            lines.append(
                f"🔴 การตรวจที่ขาด ({len(required_missing)} รายการ):"
            )
            for m in required_missing:
                period_name = PERIOD_NAMES_TH.get(
                    m.expected_period, m.expected_period
                )
                last_info = (
                    f"ครั้งล่าสุด: {m.last_done_date}"
                    if m.last_done_date else "ยังไม่เคยตรวจ"
                )
                lines.append(
                    f"  • {m.analyte} ({period_name}) — "
                    f"{last_info}"
                )
                if m.days_overdue > 0:
                    lines.append(
                        f"    เกินกำหนด {m.days_overdue} วัน"
                    )
            lines.append("")

        if optional_missing:
            lines.append(
                f"🟡 การตรวจแนะนำที่ยังไม่ได้ทำ "
                f"({len(optional_missing)} รายการ):"
            )
            for m in optional_missing:
                lines.append(f"  • {m.analyte}: {m.description_th}")
            lines.append("")
    else:
        lines.append("✅ การตรวจครบถ้วนตามตาราง")
        lines.append("")

    lines.append(f"⚠️ {compliance.disclaimer}")
    return "\n".join(lines)
