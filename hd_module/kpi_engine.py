"""HD Smart Monitoring — KPI Engine.

Calculates Key Performance Indicators for hemodialysis quality:
1. Anemia Management (Hb target achievement, ESA usage)
2. Dialysis Adequacy (Kt/V, URR) — when data available
3. Vascular Access distribution
4. Alert Statistics
5. Session Statistics
6. Patient Demographics

References:
- KDIGO 2012 Anemia in CKD (Hb 10-12 g/dL)
- KDOQI 2015 HD Adequacy (Kt/V ≥ 1.2, URR ≥ 65%)
- Thai Renal Replacement Therapy Registry

Safety: รายงานสถิติรวมเท่านั้น — ไม่แสดงข้อมูลเฉพาะบุคคล
"""

import logging
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_BKK = timezone(timedelta(hours=7))


@dataclass
class HdKpiReport:
    """HD KPI Dashboard data."""
    report_date: str = ""
    period: str = ""  # "monthly", "weekly"

    # Patient Demographics
    total_patients: int = 0
    active_patients: int = 0

    # Schedule Distribution
    schedule_distribution: dict = field(default_factory=dict)

    # ESA Usage
    esa_distribution: dict = field(default_factory=dict)
    total_on_esa: int = 0

    # Vascular Access
    vascular_access_distribution: dict = field(default_factory=dict)

    # Anemia (Hb target 10-12 g/dL)
    hb_checked: int = 0
    hb_in_target: int = 0
    hb_below_target: int = 0
    hb_above_target: int = 0
    hb_target_rate: float = 0.0

    # Dialysis Adequacy
    ktv_checked: int = 0
    ktv_adequate: int = 0  # Kt/V ≥ 1.2
    ktv_rate: float = 0.0

    # Alert Statistics
    total_alerts: int = 0
    critical_alerts: int = 0
    warning_alerts: int = 0
    info_alerts: int = 0
    alerts_acknowledged: int = 0
    avg_ack_time_minutes: float = 0.0
    alert_by_rule: dict = field(default_factory=dict)

    # Session Statistics
    total_sessions: int = 0
    completed_sessions: int = 0
    cancelled_sessions: int = 0
    completion_rate: float = 0.0

    # HD/week distribution
    hd_per_week_distribution: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "report_date": self.report_date,
            "period": self.period,
            "demographics": {
                "total_patients": self.total_patients,
                "active_patients": self.active_patients,
                "schedule_distribution": self.schedule_distribution,
                "hd_per_week": self.hd_per_week_distribution,
            },
            "esa": {
                "total_on_esa": self.total_on_esa,
                "distribution": self.esa_distribution,
            },
            "vascular_access": self.vascular_access_distribution,
            "anemia": {
                "checked": self.hb_checked,
                "in_target": self.hb_in_target,
                "below_target": self.hb_below_target,
                "above_target": self.hb_above_target,
                "target_rate_pct": round(self.hb_target_rate, 1),
            },
            "adequacy": {
                "ktv_checked": self.ktv_checked,
                "ktv_adequate": self.ktv_adequate,
                "ktv_rate_pct": round(self.ktv_rate, 1),
            },
            "alerts": {
                "total": self.total_alerts,
                "critical": self.critical_alerts,
                "warning": self.warning_alerts,
                "info": self.info_alerts,
                "acknowledged": self.alerts_acknowledged,
                "avg_ack_time_min": round(self.avg_ack_time_minutes, 1),
                "by_rule": self.alert_by_rule,
            },
            "sessions": {
                "total": self.total_sessions,
                "completed": self.completed_sessions,
                "cancelled": self.cancelled_sessions,
                "completion_rate_pct": round(self.completion_rate, 1),
            },
        }


class KpiEngine:
    """Calculate HD KPIs from GCS registry + PostgreSQL data."""

    HB_TARGET_MIN = 10.0
    HB_TARGET_MAX = 12.0
    KTV_ADEQUATE = 1.2

    def __init__(self, master_registry=None, db_url: str = ""):
        self._registry = master_registry
        self._db_url = db_url or os.environ.get(
            "HD_DATABASE_URL", os.environ.get("DATABASE_URL", ""))

    def generate_report(self, period: str = "monthly",
                        days: int = 30) -> HdKpiReport:
        """Generate KPI report from all available data sources."""
        now = datetime.now(_BKK)
        report = HdKpiReport(
            report_date=now.strftime("%Y-%m-%d"),
            period=period,
        )

        # 1. Patient demographics from GCS registry
        self._calc_demographics(report)

        # 2. Alert + Session stats from PostgreSQL
        self._calc_db_stats(report, days)

        return report

    def _calc_demographics(self, report: HdKpiReport):
        """Calculate patient demographics from MasterPatientRegistry."""
        if not self._registry:
            return

        try:
            patients = self._registry.list_all()
            report.total_patients = len(patients)
            report.active_patients = len(patients)

            # Schedule distribution
            sched_counter = Counter()
            hd_week_counter = Counter()
            esa_counter = Counter()
            va_counter = Counter()

            for p in patients:
                sched = getattr(p, 'dialysis_schedule', '') or ''
                if sched:
                    sched_counter[sched] += 1

                hd_wk = getattr(p, 'hd_per_week', 0) or 0
                if hd_wk:
                    hd_week_counter[str(hd_wk)] += 1

                esa = getattr(p, 'esa_code', '') or ''
                if esa:
                    esa_counter[esa] += 1
                    report.total_on_esa += 1

                va = getattr(p, 'vascular_access', '') or ''
                if va:
                    va_counter[va] += 1
                else:
                    va_counter["ไม่ระบุ"] += 1

            report.schedule_distribution = dict(
                sched_counter.most_common(10))
            report.hd_per_week_distribution = dict(
                hd_week_counter.most_common(5))
            report.esa_distribution = dict(esa_counter.most_common(10))
            report.vascular_access_distribution = dict(
                va_counter.most_common(10))

        except Exception as e:
            logger.error("KPI demographics error: %s", e)

    def _calc_db_stats(self, report: HdKpiReport, days: int):
        """Calculate alert + session stats from PostgreSQL."""
        if not self._db_url:
            return

        try:
            import psycopg2
            conn = psycopg2.connect(self._db_url, connect_timeout=5)
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)

            # Alert stats
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT severity, rule_id, is_acknowledged, triggered_at,
                           acknowledged_at
                    FROM hd_clinical_alerts
                    WHERE triggered_at >= %s
                """, (cutoff,))
                rows = cur.fetchall()

            report.total_alerts = len(rows)
            rule_counter = Counter()
            ack_times = []

            for severity, rule_id, is_acked, triggered_at, acked_at in rows:
                if severity == 'critical':
                    report.critical_alerts += 1
                elif severity == 'warning':
                    report.warning_alerts += 1
                else:
                    report.info_alerts += 1

                rule_counter[rule_id] += 1
                if is_acked:
                    report.alerts_acknowledged += 1
                    if triggered_at and acked_at:
                        delta = (acked_at - triggered_at).total_seconds() / 60
                        if delta > 0:
                            ack_times.append(delta)

            report.alert_by_rule = dict(rule_counter.most_common(10))
            if ack_times:
                report.avg_ack_time_minutes = sum(ack_times) / len(ack_times)

            # Session stats
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT status FROM hd_sessions
                    WHERE session_date >= %s
                """, (cutoff.date(),))
                sessions = cur.fetchall()

            report.total_sessions = len(sessions)
            for (status,) in sessions:
                if status == 'completed':
                    report.completed_sessions += 1
                elif status == 'cancelled':
                    report.cancelled_sessions += 1

            if report.total_sessions > 0:
                report.completion_rate = (
                    report.completed_sessions / report.total_sessions * 100
                )

            # Kt/V stats (from sessions with kt_v value)
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT kt_v FROM hd_sessions
                    WHERE session_date >= %s AND kt_v IS NOT NULL
                """, (cutoff.date(),))
                ktv_rows = cur.fetchall()

            report.ktv_checked = len(ktv_rows)
            for (ktv,) in ktv_rows:
                if ktv >= self.KTV_ADEQUATE:
                    report.ktv_adequate += 1
            if report.ktv_checked > 0:
                report.ktv_rate = report.ktv_adequate / report.ktv_checked * 100

            conn.close()

        except Exception as e:
            logger.warning("KPI DB stats error: %s", e)

    def calc_hb_target_from_labs(self, doc_index, get_hb_fn) -> dict:
        """Calculate Hb target achievement from lab documents.

        Args:
            doc_index: DocumentIndex instance
            get_hb_fn: function(hn, limit) → list of {"date","value"}
        Returns:
            {"checked": N, "in_target": N, "rate": float}
        """
        if not self._registry or not get_hb_fn:
            return {"checked": 0, "in_target": 0, "rate": 0}

        checked = 0
        in_target = 0
        below = 0
        above = 0

        try:
            patients = self._registry.list_all()
            for p in patients:
                hn = getattr(p, 'clinic_hn', '') or ''
                if not hn:
                    continue
                hb_hist = get_hb_fn(hn, limit=1)
                if hb_hist:
                    hb = hb_hist[-1].get("value", 0)
                    if hb > 0:
                        checked += 1
                        if self.HB_TARGET_MIN <= hb <= self.HB_TARGET_MAX:
                            in_target += 1
                        elif hb < self.HB_TARGET_MIN:
                            below += 1
                        else:
                            above += 1
        except Exception as e:
            logger.warning("KPI Hb calc error: %s", e)

        rate = (in_target / checked * 100) if checked > 0 else 0
        return {
            "checked": checked,
            "in_target": in_target,
            "below_target": below,
            "above_target": above,
            "rate": round(rate, 1),
        }


def format_kpi_for_line(report: HdKpiReport) -> str:
    """Format KPI report as LINE text message (Thai)."""
    lines = [
        "📊 HD KPI Dashboard",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"📅 {report.report_date} ({report.period})",
        "",
        f"👥 ผู้ป่วยทั้งหมด: {report.total_patients} คน",
        f"💊 ใช้ยา ESA: {report.total_on_esa} คน",
    ]

    # ESA distribution (top 3)
    if report.esa_distribution:
        lines.append("\n💊 ยา ESA:")
        for esa, count in list(report.esa_distribution.items())[:5]:
            lines.append(f"  • {esa}: {count} คน")

    # Schedule
    if report.schedule_distribution:
        lines.append("\n🗓️ ตารางฟอก:")
        for sched, count in list(report.schedule_distribution.items())[:5]:
            lines.append(f"  • {sched}: {count} คน")

    # Vascular Access
    if report.vascular_access_distribution:
        lines.append("\n🩸 Vascular Access:")
        for va, count in list(report.vascular_access_distribution.items())[:5]:
            lines.append(f"  • {va}: {count}")

    # Alerts
    if report.total_alerts > 0:
        lines.append(f"\n⚠️ Alerts ({report.period}):")
        lines.append(f"  🚨 Critical: {report.critical_alerts}")
        lines.append(f"  ⚠️ Warning: {report.warning_alerts}")
        lines.append(f"  ℹ️ Info: {report.info_alerts}")
        lines.append(
            f"  ✅ Acknowledged: {report.alerts_acknowledged}/{report.total_alerts}")
        if report.avg_ack_time_minutes > 0:
            lines.append(
                f"  ⏱️ Avg ack time: {report.avg_ack_time_minutes:.0f} min")

    # Sessions
    if report.total_sessions > 0:
        lines.append(f"\n🏥 Sessions:")
        lines.append(
            f"  ✅ {report.completed_sessions}/{report.total_sessions} "
            f"({report.completion_rate:.0f}%)")

    # Kt/V
    if report.ktv_checked > 0:
        lines.append(f"\n📈 Kt/V Adequacy:")
        lines.append(
            f"  ≥1.2: {report.ktv_adequate}/{report.ktv_checked} "
            f"({report.ktv_rate:.0f}%)")

    lines.append("\n⚕️ รายงานสถิติรวม — ไม่ใช่ข้อมูลเฉพาะบุคคล")
    return "\n".join(lines)


def format_kpi_flex(report: HdKpiReport) -> dict:
    """Format KPI as LINE Flex Message."""
    # Build stat cards
    stats = [
        {"label": "ผู้ป่วย", "value": str(report.total_patients), "icon": "👥"},
        {"label": "ESA", "value": str(report.total_on_esa), "icon": "💊"},
    ]
    if report.total_alerts > 0:
        stats.append({
            "label": "Alerts",
            "value": str(report.total_alerts),
            "icon": "⚠️",
        })
    if report.total_sessions > 0:
        stats.append({
            "label": "Sessions",
            "value": f"{report.completion_rate:.0f}%",
            "icon": "🏥",
        })

    stat_boxes = []
    for s in stats:
        stat_boxes.append({
            "type": "box", "layout": "vertical",
            "contents": [
                {"type": "text", "text": s["icon"], "align": "center", "size": "xl"},
                {"type": "text", "text": s["value"], "align": "center",
                 "weight": "bold", "size": "lg"},
                {"type": "text", "text": s["label"], "align": "center",
                 "size": "xs", "color": "#888"},
            ],
            "flex": 1,
        })

    # ESA breakdown
    esa_rows = []
    for esa, count in list(report.esa_distribution.items())[:5]:
        pct = (count / report.total_on_esa * 100) if report.total_on_esa else 0
        esa_rows.append({
            "type": "box", "layout": "horizontal", "contents": [
                {"type": "text", "text": esa, "size": "sm", "flex": 3},
                {"type": "text", "text": f"{count} ({pct:.0f}%)", "size": "sm",
                 "align": "end", "flex": 2},
            ],
        })

    body_contents = [
        {"type": "text", "text": "📊 HD KPI Dashboard", "weight": "bold",
         "size": "lg", "color": "#1a1a2e"},
        {"type": "text", "text": report.report_date, "size": "xs",
         "color": "#888"},
        {"type": "separator", "margin": "md"},
        {"type": "box", "layout": "horizontal", "margin": "md",
         "contents": stat_boxes},
        {"type": "separator", "margin": "md"},
        {"type": "text", "text": "💊 ESA Distribution", "weight": "bold",
         "size": "sm", "margin": "md"},
    ] + esa_rows

    body_contents.append(
        {"type": "text", "text": "⚕️ สถิติรวม — ไม่ใช่ข้อมูลเฉพาะบุคคล",
         "size": "xs", "color": "#999", "margin": "lg", "align": "center"}
    )

    return {
        "type": "bubble",
        "size": "mega",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": body_contents,
        },
    }
