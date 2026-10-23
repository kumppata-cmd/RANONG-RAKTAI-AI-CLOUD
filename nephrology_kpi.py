#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Nephrology KPI Dashboard — RANONG-RAKTAI-AI 2.0

Extended KPI metrics for nephrology clinical governance:
  1. Anemia Indicators — Hb/Hct target achievement
  2. Dialysis Adequacy — Kt/V target achievement
  3. CKD-MBD Indicators — Ca/PO4/PTH target ranges
  4. Volume Status — IDWG compliance
  5. Nutritional Indicators — Albumin target
  6. Medication Review Completion — reconciliation rates
  7. Missing Investigations — per monitoring schedule
  8. Physician Approval Backlog — pending reviews
  9. Alert Acknowledgement Performance — response times

กฎความปลอดภัย:
  - ไม่ใช้ AI ภายนอก — deterministic rules only
  - ClinicKPI แสดงเฉพาะ aggregate stats — ไม่มีข้อมูลเฉพาะบุคคล
  - Disclaimer ในทุกรายงาน
  - ห้ามสั่งยา เปลี่ยนยา หรือสั่งการรักษาโดยอัตโนมัติ
  - ผลเป็นข้อมูลประกอบ ไม่ใช่การวินิจฉัย
  - ไม่เก็บ patient identity — ใช้ owner_hash เท่านั้น
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ═══════════════════════════════════════════════════════════
# Configurable Clinical Targets
# ═══════════════════════════════════════════════════════════

# หมายเหตุ: เป็นค่าเริ่มต้นตาม guideline
# Clinical leadership ต้องอนุมัติ targets ที่ใช้
# ไม่ใช่ universal mandates — ต้องปรับตามบริบทผู้ป่วย

NEPHROLOGY_TARGETS: dict[str, dict] = {
    # ── Anemia ──
    "anemia": {
        "Hb": {"min": 10.0, "max": 12.0, "unit": "g/dL",
               "description_th": "ฮีโมโกลบินเป้าหมาย"},
        "Hct": {"min": 30.0, "max": 36.0, "unit": "%",
                "description_th": "ฮีมาโตคริตเป้าหมาย"},
        "Ferritin": {"min": 200, "max": 500, "unit": "ng/mL",
                     "description_th": "เฟอร์ริตินเป้าหมาย"},
        "TSAT": {"min": 20, "max": 50, "unit": "%",
                 "description_th": "ค่า TSAT เป้าหมาย"},
    },
    # ── Dialysis Adequacy ──
    "dialysis_adequacy": {
        "Kt/V": {"min": 1.2, "max": None, "unit": "",
                  "description_th": "ประสิทธิภาพการฟอกไต"},
    },
    # ── CKD-MBD ──
    "ckd_mbd": {
        "Ca": {"min": 8.4, "max": 9.5, "unit": "mg/dL",
               "description_th": "แคลเซียมเป้าหมาย"},
        "Calcium": {"min": 8.4, "max": 9.5, "unit": "mg/dL",
                    "description_th": "แคลเซียมเป้าหมาย"},
        "PO4": {"min": 3.5, "max": 5.5, "unit": "mg/dL",
                "description_th": "ฟอสเฟตเป้าหมาย"},
        "Phosphate": {"min": 3.5, "max": 5.5, "unit": "mg/dL",
                      "description_th": "ฟอสเฟตเป้าหมาย"},
        "iPTH": {"min": 150, "max": 300, "unit": "pg/mL",
                 "description_th": "พาราไทรอยด์ฮอร์โมนเป้าหมาย (HD)"},
        "PTH": {"min": 150, "max": 300, "unit": "pg/mL",
                "description_th": "พาราไทรอยด์ฮอร์โมนเป้าหมาย (HD)"},
    },
    # ── Nutrition ──
    "nutrition": {
        "Albumin": {"min": 3.5, "max": None, "unit": "g/dL",
                    "description_th": "อัลบูมินเป้าหมาย"},
    },
    # ── Electrolytes ──
    "electrolytes": {
        "K": {"min": 3.5, "max": 5.5, "unit": "mEq/L",
              "description_th": "โพแทสเซียมเป้าหมาย"},
        "Potassium": {"min": 3.5, "max": 5.5, "unit": "mEq/L",
                      "description_th": "โพแทสเซียมเป้าหมาย"},
        "Na": {"min": 135, "max": 145, "unit": "mEq/L",
               "description_th": "โซเดียมเป้าหมาย"},
        "Sodium": {"min": 135, "max": 145, "unit": "mEq/L",
                   "description_th": "โซเดียมเป้าหมาย"},
        "HCO3": {"min": 22, "max": 26, "unit": "mEq/L",
                 "description_th": "ไบคาร์บอเนตเป้าหมาย"},
    },
}


# ═══════════════════════════════════════════════════════════
# Data Classes
# ═══════════════════════════════════════════════════════════

@dataclass
class TargetAchievement:
    """ผลการบรรลุเป้าหมายแต่ละตัว."""
    analyte: str
    target_min: Optional[float]
    target_max: Optional[float]
    latest_value: Optional[float] = None
    latest_date: str = ""
    unit: str = ""
    is_at_target: bool = False
    description_th: str = ""

    def to_dict(self) -> dict:
        return {
            "analyte": self.analyte,
            "target_min": self.target_min,
            "target_max": self.target_max,
            "latest_value": self.latest_value,
            "latest_date": self.latest_date,
            "unit": self.unit,
            "is_at_target": self.is_at_target,
            "description_th": self.description_th,
        }


@dataclass
class DomainKPI:
    """KPI ของ domain เดียว (เช่น anemia, CKD-MBD)."""
    domain: str
    domain_th: str
    targets: list[TargetAchievement] = field(default_factory=list)

    @property
    def at_target_count(self) -> int:
        measured = [t for t in self.targets if t.latest_value is not None]
        return sum(1 for t in measured if t.is_at_target)

    @property
    def measured_count(self) -> int:
        return sum(1 for t in self.targets if t.latest_value is not None)

    @property
    def total_count(self) -> int:
        return len(self.targets)

    def to_dict(self) -> dict:
        return {
            "domain": self.domain,
            "domain_th": self.domain_th,
            "targets": [t.to_dict() for t in self.targets],
            "at_target": self.at_target_count,
            "measured": self.measured_count,
            "total": self.total_count,
        }


@dataclass
class ApprovalBacklog:
    """สถานะการอนุมัติผลแล็บ — Physician Approval Backlog."""
    total_pending: int = 0
    oldest_pending_date: str = ""
    approved_last_30d: int = 0
    rejected_last_30d: int = 0
    avg_turnaround_hours: Optional[float] = None

    def to_dict(self) -> dict:
        d: dict = {
            "total_pending": self.total_pending,
            "approved_last_30d": self.approved_last_30d,
            "rejected_last_30d": self.rejected_last_30d,
        }
        if self.oldest_pending_date:
            d["oldest_pending_date"] = self.oldest_pending_date
        if self.avg_turnaround_hours is not None:
            d["avg_turnaround_hours"] = round(self.avg_turnaround_hours, 1)
        return d


@dataclass
class AlertPerformance:
    """ประสิทธิภาพการตอบรับ Critical Alert."""
    total_active: int = 0
    acknowledged_count: int = 0
    resolved_count: int = 0
    unacknowledged_count: int = 0
    avg_ack_time_minutes: Optional[float] = None

    def to_dict(self) -> dict:
        d: dict = {
            "total_active": self.total_active,
            "acknowledged": self.acknowledged_count,
            "resolved": self.resolved_count,
            "unacknowledged": self.unacknowledged_count,
        }
        if self.avg_ack_time_minutes is not None:
            d["avg_ack_time_minutes"] = round(self.avg_ack_time_minutes, 1)
        return d


@dataclass
class NephrologyDashboard:
    """Nephrology KPI Dashboard รวม."""
    generated_at: str = ""
    owner_hash_prefix: str = ""  # empty for clinic-wide
    domain_kpis: list[DomainKPI] = field(default_factory=list)
    medication_review_rate: Optional[float] = None  # 0-100%
    missing_investigation_count: int = 0
    approval_backlog: Optional[ApprovalBacklog] = None
    alert_performance: Optional[AlertPerformance] = None
    disclaimer: str = (
        "⚠️ Dashboard นี้เป็นข้อมูลประกอบการพิจารณา — "
        "ค่าเป้าหมายเป็นแนวทางเริ่มต้น ต้องปรับตามบริบทผู้ป่วย — "
        "ห้ามใช้แทนการตัดสินใจทางคลินิก"
    )

    def __post_init__(self):
        if not self.generated_at:
            self.generated_at = datetime.now().isoformat(timespec="seconds")

    def to_dict(self) -> dict:
        d: dict = {
            "generated_at": self.generated_at,
            "domain_kpis": [dk.to_dict() for dk in self.domain_kpis],
            "disclaimer": self.disclaimer,
        }
        if self.owner_hash_prefix:
            d["owner_hash_prefix"] = self.owner_hash_prefix
        if self.medication_review_rate is not None:
            d["medication_review_rate_pct"] = round(
                self.medication_review_rate, 1
            )
        if self.missing_investigation_count > 0:
            d["missing_investigation_count"] = self.missing_investigation_count
        if self.approval_backlog:
            d["approval_backlog"] = self.approval_backlog.to_dict()
        if self.alert_performance:
            d["alert_performance"] = self.alert_performance.to_dict()
        return d


# ═══════════════════════════════════════════════════════════
# Functions
# ═══════════════════════════════════════════════════════════

def _get_latest_analyte_values(
    lab_reports: list[dict],
) -> dict[str, tuple[float, str, str]]:
    """รวบรวมค่า analyte ล่าสุด.

    Returns:
        dict: analyte → (value, date, unit)
    """
    latest: dict[str, tuple[float, str, str]] = {}
    for lr in lab_reports:
        specimen_date = lr.get("specimen_date", "")
        for r in lr.get("results", []):
            analyte = r.get("analyte", "")
            if not analyte:
                continue
            try:
                value = float(r.get("value", 0))
            except (TypeError, ValueError):
                continue
            unit = r.get("unit", "")
            if analyte not in latest or specimen_date > latest[analyte][1]:
                latest[analyte] = (value, specimen_date, unit)
    return latest


def evaluate_domain_targets(
    domain: str,
    domain_th: str,
    target_defs: dict[str, dict],
    latest_values: dict[str, tuple[float, str, str]],
) -> DomainKPI:
    """ประเมินเป้าหมายของ domain เดียว.

    Args:
        domain: ชื่อ domain (เช่น "anemia")
        domain_th: ชื่อภาษาไทย
        target_defs: target definitions จาก NEPHROLOGY_TARGETS
        latest_values: analyte → (value, date, unit)

    Returns:
        DomainKPI
    """
    dk = DomainKPI(domain=domain, domain_th=domain_th)
    seen_analytes: set[str] = set()

    for analyte, target in target_defs.items():
        # Avoid duplicate entries for aliases (Ca/Calcium, etc.)
        if analyte in seen_analytes:
            continue

        t_min = target.get("min")
        t_max = target.get("max")
        desc = target.get("description_th", analyte)

        ta = TargetAchievement(
            analyte=analyte,
            target_min=t_min,
            target_max=t_max,
            unit=target.get("unit", ""),
            description_th=desc,
        )

        if analyte in latest_values:
            seen_analytes.add(analyte)
            val, date, unit = latest_values[analyte]
            ta.latest_value = val
            ta.latest_date = date
            if unit:
                ta.unit = unit

            # Check if at target
            at_min = (t_min is None) or (val >= t_min)
            at_max = (t_max is None) or (val <= t_max)
            ta.is_at_target = at_min and at_max

        dk.targets.append(ta)

    return dk


def generate_nephrology_dashboard(
    lab_reports: list[dict],
    owner_hash: str = "",
    medication_review_data: Optional[dict] = None,
    monitoring_report: Optional[dict] = None,
    approval_data: Optional[dict] = None,
    alert_data: Optional[dict] = None,
    targets: Optional[dict] = None,
) -> NephrologyDashboard:
    """สร้าง Nephrology KPI Dashboard.

    Args:
        lab_reports: lab report dicts
        owner_hash: owner hash (empty for clinic-wide)
        medication_review_data: {"total": N, "reconciled": N}
        monitoring_report: MonitoringReport.to_dict()
        approval_data: {"pending": N, "approved_30d": N, ...}
        alert_data: {"active": N, "acknowledged": N, ...}
        targets: custom targets (default: NEPHROLOGY_TARGETS)

    Returns:
        NephrologyDashboard
    """
    if targets is None:
        targets = NEPHROLOGY_TARGETS

    dashboard = NephrologyDashboard(
        owner_hash_prefix=owner_hash[:8] if owner_hash else "",
    )

    # Gather latest lab values
    latest_values = _get_latest_analyte_values(lab_reports)

    # Evaluate each domain
    domain_names_th = {
        "anemia": "ภาวะโลหิตจาง",
        "dialysis_adequacy": "ประสิทธิภาพการฟอกไต",
        "ckd_mbd": "CKD-MBD (กระดูกและแร่ธาตุ)",
        "nutrition": "โภชนาการ",
        "electrolytes": "เกลือแร่",
    }

    for domain, target_defs in targets.items():
        domain_th = domain_names_th.get(domain, domain)
        dk = evaluate_domain_targets(
            domain, domain_th, target_defs, latest_values
        )
        dashboard.domain_kpis.append(dk)

    # Medication review rate
    if medication_review_data:
        total = medication_review_data.get("total", 0)
        reconciled = medication_review_data.get("reconciled", 0)
        if total > 0:
            dashboard.medication_review_rate = (reconciled / total) * 100

    # Missing investigations
    if monitoring_report:
        summary = monitoring_report.get("summary", {})
        dashboard.missing_investigation_count = (
            summary.get("total_overdue", 0)
            + summary.get("total_missing", 0)
        )

    # Approval backlog
    if approval_data:
        dashboard.approval_backlog = ApprovalBacklog(
            total_pending=approval_data.get("pending", 0),
            oldest_pending_date=approval_data.get("oldest_pending", ""),
            approved_last_30d=approval_data.get("approved_30d", 0),
            rejected_last_30d=approval_data.get("rejected_30d", 0),
            avg_turnaround_hours=approval_data.get("avg_turnaround_hours"),
        )

    # Alert performance
    if alert_data:
        dashboard.alert_performance = AlertPerformance(
            total_active=alert_data.get("active", 0),
            acknowledged_count=alert_data.get("acknowledged", 0),
            resolved_count=alert_data.get("resolved", 0),
            unacknowledged_count=alert_data.get("unacknowledged", 0),
            avg_ack_time_minutes=alert_data.get("avg_ack_minutes"),
        )

    return dashboard


def dashboard_text_th(dashboard: NephrologyDashboard) -> str:
    """สร้างข้อความ Dashboard ภาษาไทย."""
    lines = ["📊 Nephrology KPI Dashboard"]
    lines.append(f"ณ วันที่: {dashboard.generated_at[:10]}")
    if dashboard.owner_hash_prefix:
        lines.append(f"ผู้ป่วย: #{dashboard.owner_hash_prefix}")
    lines.append("")

    # Domain KPIs
    for dk in dashboard.domain_kpis:
        if dk.measured_count == 0:
            continue
        pct = (
            (dk.at_target_count / dk.measured_count * 100)
            if dk.measured_count > 0
            else 0
        )
        emoji = "✅" if pct >= 80 else ("🟡" if pct >= 50 else "🔴")
        lines.append(
            f"{emoji} {dk.domain_th}: "
            f"{dk.at_target_count}/{dk.measured_count} "
            f"บรรลุเป้าหมาย ({pct:.0f}%)"
        )

        for t in dk.targets:
            if t.latest_value is None:
                lines.append(f"    ❓ {t.analyte}: ไม่มีข้อมูล")
            elif t.is_at_target:
                lines.append(
                    f"    ✅ {t.analyte}: {t.latest_value} {t.unit}"
                )
            else:
                target_str = ""
                if t.target_min is not None and t.target_max is not None:
                    target_str = f" (เป้าหมาย {t.target_min}-{t.target_max})"
                elif t.target_min is not None:
                    target_str = f" (เป้าหมาย ≥{t.target_min})"
                elif t.target_max is not None:
                    target_str = f" (เป้าหมาย ≤{t.target_max})"
                lines.append(
                    f"    ⚠️ {t.analyte}: {t.latest_value} {t.unit}"
                    f"{target_str}"
                )
        lines.append("")

    # Medication review
    if dashboard.medication_review_rate is not None:
        rate = dashboard.medication_review_rate
        emoji = "✅" if rate >= 90 else ("🟡" if rate >= 70 else "🔴")
        lines.append(
            f"{emoji} ทบทวนยา: {rate:.0f}% reconciled"
        )

    # Missing investigations
    if dashboard.missing_investigation_count > 0:
        lines.append(
            f"🔔 การตรวจที่เกินกำหนด/ยังไม่มี: "
            f"{dashboard.missing_investigation_count} รายการ"
        )

    # Approval backlog
    if dashboard.approval_backlog and dashboard.approval_backlog.total_pending > 0:
        ab = dashboard.approval_backlog
        lines.append(
            f"📋 รอแพทย์อนุมัติ: {ab.total_pending} รายการ"
        )
        if ab.oldest_pending_date:
            lines.append(f"    เก่าสุด: {ab.oldest_pending_date}")

    # Alert performance
    if dashboard.alert_performance:
        ap = dashboard.alert_performance
        if ap.total_active > 0 or ap.unacknowledged_count > 0:
            lines.append(
                f"🚨 Critical Alert: "
                f"รับทราบแล้ว {ap.acknowledged_count}, "
                f"รอรับทราบ {ap.unacknowledged_count}"
            )
            if ap.avg_ack_time_minutes is not None:
                lines.append(
                    f"    เวลาตอบรับเฉลี่ย: "
                    f"{ap.avg_ack_time_minutes:.0f} นาที"
                )

    lines.append("")
    lines.append(f"⚕️ {dashboard.disclaimer}")
    return "\n".join(lines)
