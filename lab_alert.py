#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lab Alert System — Kidney Clinical Intelligence Platform 2.0

ระบบแจ้งเตือนผลตรวจผิดปกติอัตโนมัติผ่าน LINE Push Message

กฎความปลอดภัย:
  - ไม่เก็บ LINE userId ใน logs หรือไฟล์
  - ไม่แสดงข้อมูลผู้ป่วย (ชื่อ, HN) ในข้อความแจ้งเตือน
  - ใช้ document_id[:8] เท่านั้นสำหรับอ้างอิง
  - ผลวิเคราะห์ต้องผ่าน Human Review ก่อนใช้ประกอบการรักษา
  - ห้าม AI สั่งยาหรือแนะนำการรักษาเฉพาะเจาะจง
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class AlertLevel(str, Enum):
    """ระดับความรุนแรงของการแจ้งเตือน."""
    CRITICAL = "critical"       # ค่าวิกฤต — ต้องดำเนินการทันที
    WARNING = "warning"         # ค่าผิดปกติ — ควรติดตาม
    INFO = "info"               # ข้อมูลทั่วไป — แจ้งทราบ


# Emoji สำหรับแต่ละระดับ
ALERT_EMOJI = {
    AlertLevel.CRITICAL: "🔴",
    AlertLevel.WARNING: "🟡",
    AlertLevel.INFO: "ℹ️",
}


@dataclass
class LabAlert:
    """การแจ้งเตือนผลตรวจแต่ละรายการ."""
    analyte: str            # ชื่อ analyte เช่น Potassium
    value: float            # ค่าที่วัดได้
    unit: str               # หน่วย
    reference_range: str    # ค่าปกติ
    level: AlertLevel       # ระดับความรุนแรง
    flag: str               # RuleFlag value
    message_th: str         # คำอธิบายภาษาไทย


@dataclass
class AlertSummary:
    """สรุปการแจ้งเตือนทั้งหมดจากผลแล็บหนึ่งชุด."""
    document_id: str                    # document_id (ย่อ 8 ตัว)
    specimen_date: str                  # วันที่เก็บตัวอย่าง
    alerts: list[LabAlert] = field(default_factory=list)
    total_results: int = 0              # จำนวนผลตรวจทั้งหมด

    @property
    def has_critical(self) -> bool:
        """มีค่าวิกฤตหรือไม่."""
        return any(a.level == AlertLevel.CRITICAL for a in self.alerts)

    @property
    def has_warning(self) -> bool:
        """มีค่าผิดปกติหรือไม่."""
        return any(a.level == AlertLevel.WARNING for a in self.alerts)

    @property
    def critical_count(self) -> int:
        return sum(1 for a in self.alerts if a.level == AlertLevel.CRITICAL)

    @property
    def warning_count(self) -> int:
        return sum(1 for a in self.alerts if a.level == AlertLevel.WARNING)

    @property
    def should_notify_user(self) -> bool:
        """ควรแจ้งเตือนผู้ใช้หรือไม่ — แจ้งเมื่อมี critical หรือ warning."""
        return len(self.alerts) > 0

    @property
    def should_notify_admin(self) -> bool:
        """ควรแจ้งเตือน admin หรือไม่ — แจ้งเฉพาะ critical."""
        return self.has_critical


# ── คำอธิบายภาษาไทยสำหรับ flag แต่ละประเภท ──

FLAG_MESSAGES_TH: dict[str, str] = {
    "critical_high": "สูงระดับวิกฤต",
    "critical_low": "ต่ำระดับวิกฤต",
    "abnormal_high": "สูงกว่าปกติ",
    "abnormal_low": "ต่ำกว่าปกติ",
    "unit_mismatch": "หน่วยไม่ตรง",
    "physiologic_impossible": "ค่าไม่สมจริง",
    "low_confidence": "ความมั่นใจต่ำ",
}


def _flag_to_alert_level(flag: str) -> Optional[AlertLevel]:
    """แปลง RuleFlag เป็น AlertLevel."""
    if "critical" in flag:
        return AlertLevel.CRITICAL
    elif "abnormal" in flag:
        return AlertLevel.WARNING
    return None


def check_lab_alerts(lab_report) -> AlertSummary:
    """ตรวจสอบผลแล็บและสร้างรายการแจ้งเตือน.

    Args:
        lab_report: LabReport object จาก lab_models

    Returns:
        AlertSummary พร้อมรายการแจ้งเตือน

    หมายเหตุ:
        - ค่า physiologic_impossible และ unit_mismatch ถูกข้ามเพราะ
          อาจเป็นปัญหาจากการ extract ไม่ใช่ค่าจริง
        - low_confidence ถูกข้ามเพื่อลด noise
    """
    summary = AlertSummary(
        document_id=lab_report.source_document_id[:8],
        specimen_date=lab_report.specimen_date,
        total_results=len(lab_report.results),
    )

    for result in lab_report.results:
        for flag in result.flags:
            level = _flag_to_alert_level(flag)
            if level is None:
                continue  # ข้าม non-actionable flags

            message_th = FLAG_MESSAGES_TH.get(flag, flag)
            alert = LabAlert(
                analyte=result.analyte,
                value=result.value,
                unit=result.unit,
                reference_range=result.reference_range,
                level=level,
                flag=flag,
                message_th=message_th,
            )
            summary.alerts.append(alert)

    # เรียงลำดับ: critical ก่อน warning
    summary.alerts.sort(key=lambda a: (
        0 if a.level == AlertLevel.CRITICAL else 1,
        a.analyte,
    ))

    return summary


def format_user_alert_th(summary: AlertSummary) -> str:
    """สร้างข้อความแจ้งเตือนสำหรับผู้ใช้ (ภาษาไทย).

    ข้อความไม่มีข้อมูลระบุตัวผู้ป่วย — ใช้ document_id[:8] เท่านั้น
    """
    if not summary.alerts:
        return ""

    lines: list[str] = []

    # หัวข้อตามระดับความรุนแรง
    if summary.has_critical:
        lines.append("🔴 แจ้งเตือน: พบผลตรวจวิกฤต")
    else:
        lines.append("🟡 แจ้งเตือน: พบผลตรวจผิดปกติ")

    lines.append(f"📋 เอกสาร: {summary.document_id}")
    if summary.specimen_date and summary.specimen_date != "unknown":
        lines.append(f"📅 วันที่: {summary.specimen_date}")
    lines.append("")

    # รายละเอียดแต่ละรายการ
    for alert in summary.alerts:
        emoji = ALERT_EMOJI.get(alert.level, "")
        lines.append(
            f"{emoji} {alert.analyte}: {alert.value} {alert.unit} "
            f"({alert.message_th})"
        )
        lines.append(f"   ค่าปกติ: {alert.reference_range}")

    lines.append("")

    # สรุป
    parts = []
    if summary.critical_count > 0:
        parts.append(f"วิกฤต {summary.critical_count} รายการ")
    if summary.warning_count > 0:
        parts.append(f"ผิดปกติ {summary.warning_count} รายการ")
    lines.append(f"รวม: {', '.join(parts)} จากทั้งหมด {summary.total_results} รายการ")

    lines.append("")
    lines.append("⏳ ผลตรวจรอการยืนยันโดยแพทย์")
    lines.append(f"พิมพ์ 'ผลแล็บ {summary.document_id}' เพื่อดูรายละเอียด")

    return "\n".join(lines)


def format_admin_alert_th(summary: AlertSummary, owner_hash_prefix: str = "") -> str:
    """สร้างข้อความแจ้งเตือนสำหรับ admin (ภาษาไทย).

    แจ้งเฉพาะเมื่อมีค่าวิกฤต — admin ต้องตรวจสอบ
    ใช้ owner_hash[:8] สำหรับอ้างอิง (ไม่ใช่ข้อมูลผู้ป่วย)
    """
    if not summary.has_critical:
        return ""

    lines: list[str] = []
    lines.append("🔴 [ADMIN] พบผลตรวจวิกฤต — ต้องตรวจสอบ")
    lines.append(f"📋 เอกสาร: {summary.document_id}")
    if owner_hash_prefix:
        lines.append(f"👤 ผู้ส่ง: {owner_hash_prefix}")
    if summary.specimen_date and summary.specimen_date != "unknown":
        lines.append(f"📅 วันที่: {summary.specimen_date}")
    lines.append("")

    # แสดงเฉพาะค่าวิกฤต
    for alert in summary.alerts:
        if alert.level != AlertLevel.CRITICAL:
            continue
        lines.append(
            f"🔴 {alert.analyte}: {alert.value} {alert.unit} "
            f"({alert.message_th})"
        )
        lines.append(f"   ค่าปกติ: {alert.reference_range}")

    lines.append("")
    lines.append(
        f"พิมพ์ 'ผลแล็บ {summary.document_id}' เพื่อดูผลตรวจ\n"
        f"พิมพ์ 'ยืนยันผลแล็บ {summary.document_id}' เพื่อยืนยัน"
    )

    return "\n".join(lines)
