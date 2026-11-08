"""HD Smart Monitoring — Auto Monthly Report Generator.

Generates comprehensive monthly HD report:
- Patient statistics
- ESA usage & Hb target achievement
- Dialysis adequacy (Kt/V)
- Alert summary
- Vascular access distribution
- Cost estimate (ESA)

Output: LINE text + structured JSON for dashboard

Safety: รายงานสถิติรวม — ไม่แสดงข้อมูลเฉพาะบุคคล
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from .kpi_engine import KpiEngine, HdKpiReport

logger = logging.getLogger(__name__)

_BKK = timezone(timedelta(hours=7))

# ESA cost estimates (Thai Baht per unit)
ESA_COST_PER_DOSE = {
    "E4": 850,      # Epiao 4000u
    "H4": 900,      # Hemax 4000u
    "HM": 800,      # Hema-plus 4000u
    "H10": 2200,    # Hemax 10000u
    "H4+H10": 3100, # Combo
    "hypercrit": 750,
    "eposis": 700,
    "Eposis": 700,
}


def generate_monthly_report(
    master_registry,
    db_url: str = "",
    month: Optional[int] = None,
    year: Optional[int] = None,
) -> dict:
    """Generate comprehensive monthly HD report.

    Returns dict with all sections + formatted text.
    """
    now = datetime.now(_BKK)
    if month is None:
        # Previous month
        first_of_this = now.replace(day=1)
        last_month = first_of_this - timedelta(days=1)
        month = last_month.month
        year = last_month.year
    if year is None:
        year = now.year

    thai_months = [
        "", "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน",
        "พฤษภาคม", "มิถุนายน", "กรกฎาคม", "สิงหาคม",
        "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม",
    ]
    month_th = thai_months[month] if 1 <= month <= 12 else str(month)
    year_th = year + 543

    # Generate KPI
    engine = KpiEngine(master_registry=master_registry, db_url=db_url)
    kpi = engine.generate_report(period="monthly", days=30)

    # ESA cost estimate
    esa_cost = _estimate_esa_cost(kpi)

    # Build report
    report = {
        "period": f"{month_th} {year_th}",
        "month": month,
        "year": year,
        "generated_at": now.isoformat(),
        "kpi": kpi.to_dict(),
        "esa_cost": esa_cost,
    }

    report["text_th"] = _format_report_text(kpi, month_th, year_th, esa_cost)

    return report


def _estimate_esa_cost(kpi: HdKpiReport) -> dict:
    """Estimate monthly ESA cost from distribution."""
    total_cost = 0
    details = {}

    for esa_code, count in kpi.esa_distribution.items():
        cost_per = ESA_COST_PER_DOSE.get(esa_code, 800)
        # Assume 3 doses/week × 4.3 weeks/month
        monthly_doses = count * 3 * 4.3
        monthly_cost = monthly_doses * cost_per
        total_cost += monthly_cost
        details[esa_code] = {
            "patients": count,
            "cost_per_dose": cost_per,
            "monthly_est": round(monthly_cost),
        }

    return {
        "total_monthly_est": round(total_cost),
        "details": details,
        "currency": "THB",
        "note": "ประมาณการ — ยังไม่รวมค่าอุปกรณ์และบริการ",
    }


def _format_report_text(
    kpi: HdKpiReport,
    month_th: str,
    year_th: int,
    esa_cost: dict,
) -> str:
    """Format monthly report as LINE text message."""
    lines = [
        f"📊 รายงานสรุป HD ประจำเดือน",
        f"━━━ {month_th} {year_th} ━━━",
        "",
        "👥 ผู้ป่วย",
        f"  • ทั้งหมด: {kpi.total_patients} คน",
        f"  • ใช้ ESA: {kpi.total_on_esa} คน",
    ]

    # Schedule
    if kpi.schedule_distribution:
        lines.append("")
        lines.append("🗓️ ตารางฟอก")
        for sched, count in list(kpi.schedule_distribution.items())[:5]:
            lines.append(f"  • {sched}: {count} คน")

    # ESA
    if kpi.esa_distribution:
        lines.append("")
        lines.append("💊 ยา ESA")
        for esa, count in list(kpi.esa_distribution.items())[:5]:
            lines.append(f"  • {esa}: {count} คน")

    # Cost estimate
    if esa_cost.get("total_monthly_est", 0) > 0:
        total = esa_cost["total_monthly_est"]
        lines.append(f"\n💰 ประมาณค่า ESA/เดือน: ฿{total:,.0f}")

    # Alerts
    if kpi.total_alerts > 0:
        lines.append(f"\n⚠️ Alerts เดือนนี้: {kpi.total_alerts}")
        lines.append(f"  🚨 Critical: {kpi.critical_alerts}")
        lines.append(f"  ⚠️ Warning: {kpi.warning_alerts}")
        if kpi.alerts_acknowledged > 0:
            ack_pct = kpi.alerts_acknowledged / kpi.total_alerts * 100
            lines.append(f"  ✅ Acknowledged: {ack_pct:.0f}%")

    # Sessions
    if kpi.total_sessions > 0:
        lines.append(f"\n🏥 Sessions: {kpi.completed_sessions}/{kpi.total_sessions}")
        lines.append(f"  Completion rate: {kpi.completion_rate:.0f}%")

    # Kt/V
    if kpi.ktv_checked > 0:
        lines.append(f"\n📈 Kt/V Adequacy: {kpi.ktv_rate:.0f}%")
        lines.append(f"  ({kpi.ktv_adequate}/{kpi.ktv_checked} ≥1.2)")

    lines.append(f"\n━━━━━━━━━━━━━━━━")
    lines.append(f"📅 สร้างเมื่อ: {datetime.now(_BKK).strftime('%d/%m/%Y %H:%M')}")
    lines.append("⚕️ รายงานสถิติรวม — ไม่ใช่ข้อมูลเฉพาะบุคคล")

    return "\n".join(lines)


def format_monthly_report_flex(report: dict) -> dict:
    """Format monthly report as LINE Flex Message."""
    kpi_data = report.get("kpi", {})
    demographics = kpi_data.get("demographics", {})
    esa_data = kpi_data.get("esa", {})
    alerts_data = kpi_data.get("alerts", {})
    esa_cost = report.get("esa_cost", {})

    body = [
        {"type": "text", "text": "📊 รายงาน HD ประจำเดือน",
         "weight": "bold", "size": "lg"},
        {"type": "text", "text": report.get("period", ""),
         "size": "sm", "color": "#888"},
        {"type": "separator", "margin": "md"},
        {"type": "box", "layout": "horizontal", "margin": "md", "contents": [
            _stat_box("👥", str(demographics.get("total_patients", 0)), "ผู้ป่วย"),
            _stat_box("💊", str(esa_data.get("total_on_esa", 0)), "ESA"),
            _stat_box("⚠️", str(alerts_data.get("total", 0)), "Alerts"),
        ]},
    ]

    if esa_cost.get("total_monthly_est", 0) > 0:
        body.append({"type": "separator", "margin": "md"})
        body.append({"type": "box", "layout": "horizontal", "margin": "md", "contents": [
            {"type": "text", "text": "💰 ค่า ESA/เดือน", "size": "sm", "flex": 3},
            {"type": "text", "text": f"฿{esa_cost['total_monthly_est']:,.0f}",
             "size": "sm", "weight": "bold", "align": "end", "flex": 2},
        ]})

    body.append({"type": "text", "text": "⚕️ สถิติรวม",
                 "size": "xs", "color": "#999", "margin": "lg", "align": "center"})

    return {
        "type": "bubble",
        "size": "mega",
        "body": {"type": "box", "layout": "vertical", "contents": body},
    }


def _stat_box(icon: str, value: str, label: str) -> dict:
    return {
        "type": "box", "layout": "vertical", "flex": 1,
        "contents": [
            {"type": "text", "text": icon, "align": "center", "size": "xl"},
            {"type": "text", "text": value, "align": "center",
             "weight": "bold", "size": "lg"},
            {"type": "text", "text": label, "align": "center",
             "size": "xs", "color": "#888"},
        ],
    }
