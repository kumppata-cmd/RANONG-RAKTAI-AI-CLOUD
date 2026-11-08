"""HD Smart Monitoring — BI Reporting & Quality Indicators.

Phase 8: Business Intelligence reports for regulatory compliance:
1. สปสช. (NHSO) e-Claim HD report
2. HA Accreditation Quality Indicators
3. ประกันสังคม (SSO) summary
4. Excel data export
5. TRT Registry format (Thai Renal Replacement Therapy)

References:
- สปสช. คู่มือการเบิกค่าบริการ HD
- HA Standards 2022 (สถาบันรับรองคุณภาพสถานพยาบาล)
- TRT Registry reporting format
- KDOQI HD Adequacy Clinical Practice Guideline

Safety: รายงานสถิติรวม — ห้ามส่งข้อมูลเฉพาะบุคคลออกนอกระบบ
"""

import json
import logging
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_BKK = timezone(timedelta(hours=7))


# ═══════════════════════════════════════════════════════════
# 1. HA Quality Indicators (ตัวชี้วัดคุณภาพ HA)
# ═══════════════════════════════════════════════════════════

@dataclass
class HaQualityReport:
    """HA Accreditation Quality Indicators for HD Unit."""
    period: str = ""
    clinic_name: str = "คลินิกระนอง รักษ์ไต"
    hcode: str = "27970"

    # Clinical Indicators
    hb_target_rate: float = 0.0       # % ผู้ป่วยที่ Hb 10-12
    ktv_adequate_rate: float = 0.0    # % ที่ Kt/V ≥ 1.2
    avg_session_time_hr: float = 0.0  # เวลาฟอกเฉลี่ย
    catheter_rate: float = 0.0        # % ที่ใช้ catheter (ต่ำ = ดี)
    avf_rate: float = 0.0             # % ที่ใช้ AVF (สูง = ดี)

    # Safety Indicators
    idh_rate_per_100: float = 0.0     # IDH events per 100 sessions
    alert_ack_rate: float = 0.0       # % alerts acknowledged
    avg_ack_time_min: float = 0.0     # เวลาตอบรับเฉลี่ย

    # Process Indicators
    session_completion_rate: float = 0.0
    patient_compliance_rate: float = 0.0  # % มาครบตามนัด

    # Volume
    total_patients: int = 0
    total_sessions_month: int = 0
    sessions_per_patient: float = 0.0

    # Benchmarks (Thai HD registry averages)
    benchmarks: dict = field(default_factory=lambda: {
        "hb_target_rate": {"target": 60, "national_avg": 45, "unit": "%"},
        "ktv_adequate_rate": {"target": 80, "national_avg": 65, "unit": "%"},
        "avf_rate": {"target": 70, "national_avg": 55, "unit": "%"},
        "catheter_rate": {"target": 15, "national_avg": 25, "unit": "% (ต่ำ=ดี)"},
        "idh_rate": {"target": 5, "national_avg": 8, "unit": "ต่อ 100 sessions"},
        "session_completion": {"target": 95, "national_avg": 90, "unit": "%"},
    })

    def to_dict(self) -> dict:
        return {
            "period": self.period,
            "clinic": self.clinic_name,
            "hcode": self.hcode,
            "clinical": {
                "hb_target_rate": round(self.hb_target_rate, 1),
                "ktv_adequate_rate": round(self.ktv_adequate_rate, 1),
                "avg_session_time_hr": round(self.avg_session_time_hr, 1),
                "catheter_rate": round(self.catheter_rate, 1),
                "avf_rate": round(self.avf_rate, 1),
            },
            "safety": {
                "idh_rate_per_100": round(self.idh_rate_per_100, 1),
                "alert_ack_rate": round(self.alert_ack_rate, 1),
                "avg_ack_time_min": round(self.avg_ack_time_min, 1),
            },
            "process": {
                "session_completion_rate": round(self.session_completion_rate, 1),
                "patient_compliance_rate": round(self.patient_compliance_rate, 1),
            },
            "volume": {
                "total_patients": self.total_patients,
                "total_sessions": self.total_sessions_month,
                "sessions_per_patient": round(self.sessions_per_patient, 1),
            },
            "benchmarks": self.benchmarks,
        }


# ═══════════════════════════════════════════════════════════
# 2. NHSO (สปสช.) HD Report Format
# ═══════════════════════════════════════════════════════════

@dataclass
class NhsoHdRecord:
    """สปสช. e-Claim record for one HD session."""
    hn: str = ""
    cid: str = ""             # เลขบัตรประชาชน (masked)
    session_date: str = ""
    diagnosis_icd10: str = "N18.5"  # CKD Stage 5
    procedure_icd9: str = "39.95"   # Hemodialysis
    hd_duration_hr: float = 4.0
    dialyzer_type: str = ""
    vascular_access: str = ""
    esa_drug: str = ""
    esa_dose: str = ""
    claim_amount: float = 1500.0    # ค่าฟอกต่อครั้ง (สปสช.)


# ═══════════════════════════════════════════════════════════
# 3. TRT Registry Format
# ═══════════════════════════════════════════════════════════

@dataclass
class TrtPatientRecord:
    """Thai Renal Replacement Therapy Registry format."""
    hospital_code: str = "11238"
    hn: str = ""
    initials: str = ""        # ชื่อย่อ (privacy)
    age: Optional[int] = None
    gender: str = ""
    primary_cause: str = ""   # สาเหตุ CKD (DM, HT, GN, etc.)
    hd_start_date: str = ""
    vascular_access: str = ""
    hd_frequency: int = 0
    hd_duration_hr: float = 4.0
    last_ktv: Optional[float] = None
    last_hb: Optional[float] = None
    last_albumin: Optional[float] = None
    esa_type: str = ""
    esa_dose_weekly: float = 0.0
    status: str = "active"


# ═══════════════════════════════════════════════════════════
# Report Generator
# ═══════════════════════════════════════════════════════════

class BiReportEngine:
    """Generate BI reports from registry + PostgreSQL data."""

    def __init__(self, master_registry=None, db_url: str = ""):
        self._registry = master_registry
        self._db_url = db_url or os.environ.get(
            "HD_DATABASE_URL", os.environ.get("DATABASE_URL", ""))

    def generate_ha_report(self, month: int = 0, year: int = 0) -> HaQualityReport:
        """Generate HA quality indicators report."""
        now = datetime.now(_BKK)
        if not month:
            month = now.month
        if not year:
            year = now.year

        thai_months = [
            "", "ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
            "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค.",
        ]
        report = HaQualityReport(
            period=f"{thai_months[month]} {year + 543}",
        )

        # Demographics from registry
        if self._registry:
            patients = self._registry.list_all()
            report.total_patients = len(patients)

            # Vascular access rates
            va_counter = Counter()
            for p in patients:
                va = (getattr(p, 'vascular_access', '') or '').lower()
                if 'avf' in va:
                    va_counter['avf'] += 1
                elif 'avg' in va:
                    va_counter['avg'] += 1
                elif 'catheter' in va or 'cvc' in va or 'perm' in va:
                    va_counter['catheter'] += 1
                else:
                    va_counter['unknown'] += 1

            total_va = sum(va_counter.values()) or 1
            report.avf_rate = va_counter.get('avf', 0) / total_va * 100
            report.catheter_rate = va_counter.get('catheter', 0) / total_va * 100

        # DB stats
        self._fill_db_stats(report, month, year)

        return report

    def _fill_db_stats(self, report: HaQualityReport, month: int, year: int):
        """Fill stats from PostgreSQL."""
        if not self._db_url:
            return
        try:
            import psycopg2
            conn = psycopg2.connect(self._db_url, connect_timeout=5)

            # Session stats
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*), COUNT(*) FILTER (WHERE status = 'completed'),
                           AVG(EXTRACT(EPOCH FROM (end_time - start_time))/3600)
                    FROM hd_sessions
                    WHERE EXTRACT(MONTH FROM session_date) = %s
                      AND EXTRACT(YEAR FROM session_date) = %s
                """, (month, year))
                row = cur.fetchone()
                if row:
                    report.total_sessions_month = row[0] or 0
                    completed = row[1] or 0
                    if report.total_sessions_month > 0:
                        report.session_completion_rate = (
                            completed / report.total_sessions_month * 100)
                    report.avg_session_time_hr = float(row[2] or 0)

            # Sessions per patient
            if report.total_patients > 0 and report.total_sessions_month > 0:
                report.sessions_per_patient = (
                    report.total_sessions_month / report.total_patients)

            # Kt/V adequacy
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*), COUNT(*) FILTER (WHERE kt_v >= 1.2)
                    FROM hd_sessions
                    WHERE kt_v IS NOT NULL
                      AND EXTRACT(MONTH FROM session_date) = %s
                      AND EXTRACT(YEAR FROM session_date) = %s
                """, (month, year))
                row = cur.fetchone()
                if row and row[0]:
                    report.ktv_adequate_rate = (row[1] or 0) / row[0] * 100

            # Alert stats
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*),
                           COUNT(*) FILTER (WHERE rule_id = 'idh' OR rule_id = 'severe_idh'),
                           COUNT(*) FILTER (WHERE is_acknowledged = TRUE),
                           AVG(EXTRACT(EPOCH FROM (acknowledged_at - triggered_at))/60)
                             FILTER (WHERE is_acknowledged = TRUE)
                    FROM hd_clinical_alerts
                    WHERE EXTRACT(MONTH FROM triggered_at) = %s
                      AND EXTRACT(YEAR FROM triggered_at) = %s
                """, (month, year))
                row = cur.fetchone()
                if row:
                    total_alerts = row[0] or 0
                    idh_events = row[1] or 0
                    acked = row[2] or 0
                    if total_alerts > 0:
                        report.alert_ack_rate = acked / total_alerts * 100
                    report.avg_ack_time_min = float(row[3] or 0)
                    if report.total_sessions_month > 0:
                        report.idh_rate_per_100 = (
                            idh_events / report.total_sessions_month * 100)

            conn.close()
        except Exception as e:
            logger.warning("BI DB stats error: %s", e)

    def generate_trt_records(self) -> list[dict]:
        """Generate TRT Registry format records (anonymized)."""
        if not self._registry:
            return []

        records = []
        for p in self._registry.list_all():
            name = getattr(p, 'patient_name', '') or ''
            initials = name[:2] + "..." if len(name) > 2 else name

            records.append(TrtPatientRecord(
                hn=getattr(p, 'clinic_hn', ''),
                initials=initials,
                gender="",
                primary_cause="CKD-G5",
                vascular_access=getattr(p, 'vascular_access', ''),
                hd_frequency=getattr(p, 'hd_per_week', 0) or 0,
                esa_type=getattr(p, 'esa_code', ''),
            ).__dict__)

        return records

    def export_patient_list_json(self) -> list[dict]:
        """Export patient list as JSON (for Excel conversion)."""
        if not self._registry:
            return []

        rows = []
        for p in self._registry.list_all():
            rows.append({
                "hn_clinic": getattr(p, 'clinic_hn', ''),
                "hn_hospital": getattr(p, 'hospital_hn', ''),
                "name": getattr(p, 'patient_name', ''),
                "schedule": getattr(p, 'dialysis_schedule', ''),
                "hd_per_week": getattr(p, 'hd_per_week', 0),
                "esa": getattr(p, 'esa_code', ''),
                "vascular_access": getattr(p, 'vascular_access', ''),
            })
        return rows


# ═══════════════════════════════════════════════════════════
# LINE Formatting
# ═══════════════════════════════════════════════════════════

def format_ha_report_for_line(report: HaQualityReport) -> str:
    """Format HA quality report as LINE text."""
    lines = [
        "🏥 HA Quality Indicators — HD Unit",
        f"━━━ {report.period} ━━━",
        f"📍 {report.clinic_name} ({report.hcode})",
        "",
        "📊 Clinical Indicators",
    ]

    # Compare with benchmarks
    def _indicator(label, value, benchmark_key):
        b = report.benchmarks.get(benchmark_key, {})
        target = b.get("target", 0)
        unit = b.get("unit", "%")
        if value >= target:
            icon = "✅"
        elif value >= target * 0.8:
            icon = "🟡"
        else:
            icon = "🔴"
        return f"  {icon} {label}: {value:.0f}{unit} (เป้า: {target}{unit})"

    lines.append(_indicator("Hb target (10-12)", report.hb_target_rate, "hb_target_rate"))
    lines.append(_indicator("Kt/V ≥1.2", report.ktv_adequate_rate, "ktv_adequate_rate"))
    lines.append(_indicator("AVF rate", report.avf_rate, "avf_rate"))
    lines.append(_indicator("Catheter rate", report.catheter_rate, "catheter_rate"))

    lines.append("\n⚠️ Safety Indicators")
    lines.append(_indicator("IDH rate", report.idh_rate_per_100, "idh_rate"))
    lines.append(_indicator("Session completion", report.session_completion_rate, "session_completion"))
    if report.alert_ack_rate > 0:
        lines.append(f"  ✅ Alert ack rate: {report.alert_ack_rate:.0f}%")
    if report.avg_ack_time_min > 0:
        lines.append(f"  ⏱️ Avg ack time: {report.avg_ack_time_min:.0f} min")

    lines.append(f"\n📈 Volume")
    lines.append(f"  • ผู้ป่วย: {report.total_patients} คน")
    lines.append(f"  • Sessions: {report.total_sessions_month}/เดือน")
    if report.sessions_per_patient > 0:
        lines.append(f"  • ฟอก/คน: {report.sessions_per_patient:.1f} ครั้ง/เดือน")

    lines.append("\n⚕️ รายงานคุณภาพ HA — สถิติรวม")
    return "\n".join(lines)
