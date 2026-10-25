#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Automated Clinical Reports — Phase C
Kidney Clinical Intelligence Platform 2.0

ระบบรายงาน KPI อัตโนมัติ:
  1. Monthly KPI Summary — สรุปรายเดือน
  2. Quarterly Trend Report — สรุปรายไตรมาส
  3. Patient Cohort Summary — สรุปตามกลุ่มผู้ป่วย
  4. LINE Flex Message — ส่งรายงานผ่าน LINE OA

กฎความปลอดภัย:
  - ส่งเฉพาะ aggregate data — ไม่มีข้อมูลเฉพาะบุคคล
  - Rule engine only — ไม่ใช้ AI ภายนอก
  - ห้ามส่งข้อมูลผู้ป่วยออกภายนอก
  - ส่งเฉพาะกลุ่ม PHYSICIAN_USERS ที่ลงทะเบียนแล้ว
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# Data Classes
# ═══════════════════════════════════════════════════════════

@dataclass
class KPIMetric:
    """ตัวชี้วัด KPI หนึ่งรายการ."""
    name_th: str
    value: float
    target: float
    unit: str = "%"
    trend: str = ""       # ↑ ↓ → or ""
    on_target: bool = False

    def to_dict(self) -> dict:
        return {
            "name_th": self.name_th,
            "value": round(self.value, 1),
            "target": round(self.target, 1),
            "unit": self.unit,
            "trend": self.trend,
            "on_target": self.on_target,
        }


@dataclass
class MonthlyReport:
    """รายงาน KPI รายเดือน."""
    period: str               # "2026-10"
    period_th: str             # "ตุลาคม 2569"
    generated_at: str
    total_patients: int = 0
    new_patients: int = 0
    ckd_stage_counts: dict[str, int] = field(default_factory=dict)
    metrics: list[KPIMetric] = field(default_factory=list)
    critical_events: int = 0
    highlights_th: list[str] = field(default_factory=list)
    disclaimer: str = (
        "⚠️ รายงานนี้เป็นข้อมูลสรุปประกอบ — "
        "ห้ามใช้แทนการตัดสินใจทางคลินิก"
    )

    def to_dict(self) -> dict:
        return {
            "period": self.period,
            "period_th": self.period_th,
            "generated_at": self.generated_at,
            "total_patients": self.total_patients,
            "new_patients": self.new_patients,
            "ckd_stage_counts": self.ckd_stage_counts,
            "metrics": [m.to_dict() for m in self.metrics],
            "critical_events": self.critical_events,
            "highlights_th": self.highlights_th,
        }


# ═══════════════════════════════════════════════════════════
# Thai Month Names
# ═══════════════════════════════════════════════════════════

THAI_MONTHS = {
    1: "มกราคม", 2: "กุมภาพันธ์", 3: "มีนาคม",
    4: "เมษายน", 5: "พฤษภาคม", 6: "มิถุนายน",
    7: "กรกฎาคม", 8: "สิงหาคม", 9: "กันยายน",
    10: "ตุลาคม", 11: "พฤศจิกายน", 12: "ธันวาคม",
}

THAI_QUARTERS = {1: "ไตรมาส 1", 2: "ไตรมาส 2", 3: "ไตรมาส 3", 4: "ไตรมาส 4"}


def _thai_month(dt: datetime) -> str:
    """ชื่อเดือนภาษาไทย + พ.ศ."""
    return f"{THAI_MONTHS.get(dt.month, '')} {dt.year + 543}"


def _thai_quarter(dt: datetime) -> str:
    """ไตรมาสภาษาไทย."""
    q = (dt.month - 1) // 3 + 1
    return f"{THAI_QUARTERS[q]} ปี {dt.year + 543}"


# ═══════════════════════════════════════════════════════════
# Report Generation
# ═══════════════════════════════════════════════════════════

def generate_monthly_report(
    storage: Any,
    patient_registry: Any = None,
    reference_date: Optional[datetime] = None,
) -> MonthlyReport:
    """สร้างรายงาน KPI รายเดือน.

    Args:
        storage: GCS storage instance
        patient_registry: patient registry instance
        reference_date: วันที่อ้างอิง (default: เดือนที่แล้ว)

    Returns:
        MonthlyReport

    Safety: aggregate only — ไม่มีข้อมูลระบุตัวผู้ป่วย
    """
    if reference_date is None:
        today = datetime.now()
        # Last month
        if today.month == 1:
            reference_date = datetime(today.year - 1, 12, 1)
        else:
            reference_date = datetime(today.year, today.month - 1, 1)

    period = reference_date.strftime("%Y-%m")
    period_th = _thai_month(reference_date)

    report = MonthlyReport(
        period=period,
        period_th=period_th,
        generated_at=datetime.now().isoformat(),
    )

    # ── Patient data ──
    if patient_registry:
        try:
            patients = patient_registry.list_patients()
            report.total_patients = len(patients)

            # CKD stage distribution
            for p in patients:
                stage = p.get("ckd_stage", "Unknown")
                report.ckd_stage_counts[stage] = report.ckd_stage_counts.get(stage, 0) + 1

            # New patients this month
            for p in patients:
                reg_date = p.get("registered_date", "")
                if reg_date and reg_date.startswith(period):
                    report.new_patients += 1
        except Exception as e:
            logger.warning(f"Registry error in monthly report: {e}")

    # ── Lab KPI from GCS ──
    anemia_total, anemia_on_target = 0, 0
    dialysis_total, dialysis_adequate = 0, 0
    approved_labs, total_labs = 0, 0
    critical_count = 0

    try:
        if hasattr(storage, '_bucket') and storage._bucket:
            lab_blobs = list(storage._bucket.list_blobs(
                prefix="_lab_results/", max_results=500
            ))
            total_labs = len(lab_blobs)

            for lb in lab_blobs:
                try:
                    ldata = json.loads(lb.download_as_text())

                    # Filter by period if possible
                    lab_date = ldata.get("report_date", ldata.get("upload_date", ""))
                    # Include all if no date filter matches
                    if lab_date and not lab_date.startswith(period):
                        # Still count totals but skip period-specific
                        if ldata.get("status") == "approved":
                            approved_labs += 1
                        continue

                    if ldata.get("status") == "approved":
                        approved_labs += 1

                    results = ldata.get("results", [])
                    for r in results:
                        analyte = r.get("analyte", "").upper()
                        value = r.get("value")
                        if analyte in ("HB", "HEMOGLOBIN", "HGB") and value:
                            anemia_total += 1
                            try:
                                if 10.0 <= float(value) <= 12.0:
                                    anemia_on_target += 1
                            except (ValueError, TypeError):
                                pass
                        if analyte in ("KT/V", "KTV") and value:
                            dialysis_total += 1
                            try:
                                if float(value) >= 1.2:
                                    dialysis_adequate += 1
                            except (ValueError, TypeError):
                                pass
                except Exception:
                    pass

            # Alerts
            alert_blobs = list(storage._bucket.list_blobs(
                prefix="_alerts/", max_results=200
            ))
            for ab in alert_blobs:
                try:
                    adata = json.loads(ab.download_as_text())
                    cdate = adata.get("created_at", "")
                    if cdate and cdate.startswith(period):
                        critical_count += adata.get("critical_count", 0)
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"GCS error in monthly report: {e}")

    report.critical_events = critical_count

    # ── Build metrics ──
    anemia_rate = round(anemia_on_target / anemia_total * 100, 1) if anemia_total > 0 else 0
    dialysis_rate = round(dialysis_adequate / dialysis_total * 100, 1) if dialysis_total > 0 else 0
    approval_rate = round(approved_labs / total_labs * 100, 1) if total_labs > 0 else 0

    report.metrics = [
        KPIMetric("Anemia On-Target (Hb 10-12)", anemia_rate, 80.0,
                  on_target=anemia_rate >= 80),
        KPIMetric("Dialysis Adequate (Kt/V ≥1.2)", dialysis_rate, 80.0,
                  on_target=dialysis_rate >= 80),
        KPIMetric("Lab Approval Rate", approval_rate, 90.0,
                  on_target=approval_rate >= 90),
    ]

    # ── Highlights ──
    report.highlights_th = []
    if report.new_patients > 0:
        report.highlights_th.append(f"ผู้ป่วยใหม่ {report.new_patients} ราย")
    if critical_count > 0:
        report.highlights_th.append(f"เหตุการณ์วิกฤต {critical_count} ครั้ง")
    if anemia_rate >= 80:
        report.highlights_th.append(f"✅ Anemia on-target ≥80% ({anemia_rate}%)")
    else:
        report.highlights_th.append(f"⚠️ Anemia on-target ต่ำกว่าเป้า ({anemia_rate}%)")

    return report


# ═══════════════════════════════════════════════════════════
# LINE Message Formatting
# ═══════════════════════════════════════════════════════════

def report_to_line_text(report: MonthlyReport) -> str:
    """แปลงรายงานเป็นข้อความ LINE (plain text).

    Safety: aggregate only — ไม่มีข้อมูลระบุตัว
    """
    lines = [
        f"📊 รายงาน KPI คลินิกโรคไต",
        f"ประจำ: {report.period_th}",
        f"สร้างเมื่อ: {report.generated_at[:16]}",
        "",
        f"👥 ผู้ป่วยทั้งหมด: {report.total_patients} ราย",
    ]

    if report.new_patients > 0:
        lines.append(f"   ผู้ป่วยใหม่: +{report.new_patients} ราย")

    # CKD Stages
    if report.ckd_stage_counts:
        lines.append("")
        lines.append("📋 สัดส่วน CKD Stage:")
        for stage in ["G1", "G2", "G3a", "G3b", "G4", "G5", "G5D"]:
            count = report.ckd_stage_counts.get(stage, 0)
            if count > 0:
                lines.append(f"   {stage}: {count} ราย")

    # KPI Metrics
    lines.append("")
    lines.append("📈 ตัวชี้วัด KPI:")
    for m in report.metrics:
        status = "✅" if m.on_target else "⚠️"
        lines.append(f"   {status} {m.name_th}: {m.value}{m.unit} (เป้า {m.target}{m.unit})")

    # Highlights
    if report.highlights_th:
        lines.append("")
        lines.append("💡 ข้อสังเกต:")
        for h in report.highlights_th:
            lines.append(f"   • {h}")

    # Critical events
    if report.critical_events > 0:
        lines.append("")
        lines.append(f"🚨 เหตุการณ์วิกฤต: {report.critical_events} ครั้ง")

    lines.append("")
    lines.append(report.disclaimer)
    return "\n".join(lines)


def report_to_flex_message(report: MonthlyReport) -> dict:
    """แปลงรายงานเป็น LINE Flex Message.

    Returns dict ที่ส่งผ่าน LINE Messaging API ได้เลย.

    Safety: aggregate only
    """
    # Build KPI rows
    kpi_contents = []
    for m in report.metrics:
        status_color = "#22c55e" if m.on_target else "#eab308"
        kpi_contents.append({
            "type": "box",
            "layout": "horizontal",
            "contents": [
                {"type": "text", "text": "✅" if m.on_target else "⚠️",
                 "size": "sm", "flex": 0},
                {"type": "text", "text": m.name_th,
                 "size": "xs", "color": "#94a3b8", "flex": 5, "wrap": True},
                {"type": "text", "text": f"{m.value}{m.unit}",
                 "size": "sm", "color": status_color,
                 "flex": 2, "align": "end", "weight": "bold"},
            ],
            "margin": "md",
        })

    # Build stage bar
    stage_contents = []
    for stage in ["G1", "G2", "G3a", "G3b", "G4", "G5", "G5D"]:
        count = report.ckd_stage_counts.get(stage, 0)
        if count > 0:
            stage_contents.append({
                "type": "text",
                "text": f"{stage}: {count}",
                "size": "xxs",
                "color": "#94a3b8",
            })

    flex = {
        "type": "flex",
        "altText": f"รายงาน KPI — {report.period_th}",
        "contents": {
            "type": "bubble",
            "size": "mega",
            "header": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {"type": "text", "text": "📊 รายงาน KPI คลินิกโรคไต",
                     "weight": "bold", "color": "#38bdf8", "size": "md"},
                    {"type": "text", "text": report.period_th,
                     "color": "#94a3b8", "size": "xs", "margin": "sm"},
                ],
                "backgroundColor": "#0f172a",
                "paddingAll": "16px",
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    # Patient count
                    {
                        "type": "box",
                        "layout": "horizontal",
                        "contents": [
                            {"type": "text", "text": "👥 ผู้ป่วยทั้งหมด",
                             "size": "sm", "color": "#e2e8f0", "flex": 4},
                            {"type": "text", "text": f"{report.total_patients} ราย",
                             "size": "sm", "color": "#38bdf8",
                             "weight": "bold", "align": "end", "flex": 2},
                        ],
                    },
                    {"type": "separator", "margin": "lg", "color": "#334155"},
                    # KPI section
                    {"type": "text", "text": "📈 ตัวชี้วัด KPI",
                     "size": "sm", "weight": "bold", "color": "#e2e8f0",
                     "margin": "lg"},
                    *kpi_contents,
                    {"type": "separator", "margin": "lg", "color": "#334155"},
                    # Stages
                    {"type": "text", "text": "📋 CKD Stage Distribution",
                     "size": "sm", "weight": "bold", "color": "#e2e8f0",
                     "margin": "lg"},
                    {
                        "type": "box",
                        "layout": "horizontal",
                        "contents": stage_contents[:4] if stage_contents else [
                            {"type": "text", "text": "ไม่มีข้อมูล",
                             "size": "xxs", "color": "#64748b"},
                        ],
                        "margin": "md",
                        "spacing": "md",
                    },
                ],
                "backgroundColor": "#1e293b",
                "paddingAll": "16px",
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {"type": "text",
                     "text": "⚠️ ข้อมูลประกอบ — ห้ามใช้แทนการตัดสินใจคลินิก",
                     "size": "xxs", "color": "#f87171", "wrap": True},
                ],
                "backgroundColor": "#0f172a",
                "paddingAll": "12px",
            },
        },
    }
    return flex


def send_report_via_line(
    report: MonthlyReport,
    line_bot_api: Any,
    recipient_ids: list[str],
    use_flex: bool = True,
) -> dict:
    """ส่งรายงานผ่าน LINE OA.

    Args:
        report: MonthlyReport
        line_bot_api: LINE Bot API instance
        recipient_ids: LINE user IDs ของแพทย์
        use_flex: ใช้ Flex Message (True) หรือ text (False)

    Returns:
        dict สรุปการส่ง

    Safety:
        - ส่งเฉพาะ aggregate data
        - ส่งเฉพาะ recipient ที่ลงทะเบียนแล้ว
    """
    results = {"sent": 0, "failed": 0, "errors": []}

    for uid in recipient_ids:
        try:
            if use_flex:
                flex_msg = report_to_flex_message(report)
                # LINE SDK push_message
                from linebot.models import FlexSendMessage
                msg = FlexSendMessage(
                    alt_text=f"รายงาน KPI — {report.period_th}",
                    contents=flex_msg["contents"],
                )
                line_bot_api.push_message(uid, msg)
            else:
                from linebot.models import TextSendMessage
                text = report_to_line_text(report)
                msg = TextSendMessage(text=text)
                line_bot_api.push_message(uid, msg)
            results["sent"] += 1
        except Exception as e:
            results["failed"] += 1
            results["errors"].append(str(e))
            logger.error(f"LINE push error to {uid[:8]}: {e}")

    return results
