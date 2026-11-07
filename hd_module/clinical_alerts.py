"""HD Smart Monitoring — Clinical Alert Engine (Rule-Based).

All thresholds are configurable.  AI ห้ามตัดสินใจรักษา ปรับ UF
ให้ยา หรือควบคุมเครื่องฟอกเลือดโดยอัตโนมัติ.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from .config import HdConfig

logger = logging.getLogger(__name__)


# ── Alert Rule Definition ─────────────────────────────────

@dataclass
class AlertRule:
    """A clinical alert rule with configurable threshold."""
    rule_id: str
    name_en: str
    name_th: str
    severity: str           # "critical", "warning", "info"
    description_th: str
    enabled: bool = True
    requires_approval: bool = True  # ต้องผ่าน clinical approval

    def __post_init__(self):
        assert self.severity in ("critical", "warning", "info")


@dataclass
class AlertResult:
    """Result of evaluating an alert rule."""
    rule_id: str
    triggered: bool
    severity: str
    message_th: str
    details: dict = field(default_factory=dict)


# ── Default Clinical Rules ────────────────────────────────

DEFAULT_RULES: list[AlertRule] = [
    AlertRule(
        rule_id="idh",
        name_en="Intradialytic Hypotension",
        name_th="ความดันตกระหว่างฟอก (IDH)",
        severity="warning",
        description_th="SBP ลดลง ≥20 mmHg ภายใน 30 นาที",
    ),
    AlertRule(
        rule_id="severe_idh",
        name_en="Severe Intradialytic Hypotension",
        name_th="ความดันตกรุนแรง (Severe IDH)",
        severity="critical",
        description_th="SBP < 90 mmHg",
    ),
    AlertRule(
        rule_id="high_uf_rate",
        name_en="High Ultrafiltration Rate",
        name_th="อัตราดึงน้ำสูงเกินไป",
        severity="warning",
        description_th="UF Rate > 13 mL/kg/hr",
    ),
    AlertRule(
        rule_id="excess_weight",
        name_en="Excessive Interdialytic Weight Gain",
        name_th="น้ำหนักเพิ่มเกินเกณฑ์ (IDWG)",
        severity="warning",
        description_th="IDWG > 4% ของ Dry Weight",
    ),
    AlertRule(
        rule_id="bradycardia",
        name_en="Bradycardia",
        name_th="หัวใจเต้นช้า",
        severity="critical",
        description_th="HR < 50 bpm",
    ),
    AlertRule(
        rule_id="tachycardia",
        name_en="Tachycardia",
        name_th="หัวใจเต้นเร็ว",
        severity="warning",
        description_th="HR > 120 bpm",
    ),
    AlertRule(
        rule_id="missing_data",
        name_en="Missing Vital Signs Data",
        name_th="ไม่มีการบันทึก vital signs",
        severity="info",
        description_th="ไม่มีการบันทึก vital signs > 60 นาที",
    ),
]

RULES_BY_ID = {r.rule_id: r for r in DEFAULT_RULES}


# ── Evaluation Functions ──────────────────────────────────

def check_idh(
    current_sbp: int,
    previous_sbps: list[tuple[datetime, int]],
    window_minutes: int = None,
) -> Optional[AlertResult]:
    """Check for Intradialytic Hypotension (SBP drop ≥20 in window)."""
    if window_minutes is None:
        window_minutes = HdConfig.ALERT_IDH_WINDOW_MINUTES
    threshold = HdConfig.ALERT_IDH_SBP_DROP

    if not current_sbp or not previous_sbps:
        return None

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=window_minutes)

    for ts, prev_sbp in previous_sbps:
        if ts >= cutoff and prev_sbp - current_sbp >= threshold:
            return AlertResult(
                rule_id="idh",
                triggered=True,
                severity="warning",
                message_th=(
                    f"⚠️ ความดันตก (IDH): SBP {prev_sbp}→{current_sbp} mmHg "
                    f"(ลด {prev_sbp - current_sbp} mmHg ใน {window_minutes} นาที)"
                ),
                details={"previous_sbp": prev_sbp, "current_sbp": current_sbp,
                         "drop": prev_sbp - current_sbp},
            )
    return None


def check_severe_idh(current_sbp: int) -> Optional[AlertResult]:
    """Check for Severe IDH (SBP < threshold)."""
    threshold = HdConfig.ALERT_SEVERE_IDH_SBP
    if current_sbp and current_sbp < threshold:
        return AlertResult(
            rule_id="severe_idh",
            triggered=True,
            severity="critical",
            message_th=f"🚨 ความดันต่ำวิกฤต: SBP {current_sbp} mmHg (< {threshold})",
            details={"sbp": current_sbp, "threshold": threshold},
        )
    return None


def check_high_uf_rate(
    uf_rate_ml_hr: float,
    patient_weight_kg: float,
) -> Optional[AlertResult]:
    """Check for excessive UF rate (> threshold mL/kg/hr)."""
    threshold = HdConfig.ALERT_HIGH_UF_RATE
    if not patient_weight_kg or patient_weight_kg <= 0:
        return None
    rate_per_kg = uf_rate_ml_hr / patient_weight_kg
    if rate_per_kg > threshold:
        return AlertResult(
            rule_id="high_uf_rate",
            triggered=True,
            severity="warning",
            message_th=(
                f"⚠️ UF Rate สูง: {rate_per_kg:.1f} mL/kg/hr "
                f"(เกิน {threshold} mL/kg/hr)"
            ),
            details={"uf_rate": uf_rate_ml_hr, "weight": patient_weight_kg,
                     "rate_per_kg": round(rate_per_kg, 1)},
        )
    return None


def check_excess_weight(
    pre_weight: float,
    dry_weight: float,
) -> Optional[AlertResult]:
    """Check for excessive interdialytic weight gain (IDWG > threshold % DW)."""
    threshold_pct = HdConfig.ALERT_EXCESS_WEIGHT_PCT
    if not dry_weight or dry_weight <= 0 or not pre_weight:
        return None
    idwg = pre_weight - dry_weight
    idwg_pct = (idwg / dry_weight) * 100
    if idwg_pct > threshold_pct:
        return AlertResult(
            rule_id="excess_weight",
            triggered=True,
            severity="warning",
            message_th=(
                f"⚠️ น้ำหนักเพิ่มเกิน: IDWG {idwg:.1f} kg "
                f"({idwg_pct:.1f}% ของ DW {dry_weight} kg)"
            ),
            details={"pre_weight": pre_weight, "dry_weight": dry_weight,
                     "idwg_kg": round(idwg, 1), "idwg_pct": round(idwg_pct, 1)},
        )
    return None


def check_heart_rate(hr: int) -> Optional[AlertResult]:
    """Check for bradycardia or tachycardia."""
    if not hr:
        return None
    if hr < HdConfig.ALERT_BRADYCARDIA_HR:
        return AlertResult(
            rule_id="bradycardia",
            triggered=True,
            severity="critical",
            message_th=f"🚨 หัวใจเต้นช้า: HR {hr} bpm (< {HdConfig.ALERT_BRADYCARDIA_HR})",
            details={"hr": hr, "threshold": HdConfig.ALERT_BRADYCARDIA_HR},
        )
    if hr > HdConfig.ALERT_TACHYCARDIA_HR:
        return AlertResult(
            rule_id="tachycardia",
            triggered=True,
            severity="warning",
            message_th=f"⚠️ หัวใจเต้นเร็ว: HR {hr} bpm (> {HdConfig.ALERT_TACHYCARDIA_HR})",
            details={"hr": hr, "threshold": HdConfig.ALERT_TACHYCARDIA_HR},
        )
    return None


def check_missing_data(
    last_vital_time: Optional[datetime],
) -> Optional[AlertResult]:
    """Check if vital signs haven't been recorded within threshold."""
    threshold = HdConfig.ALERT_MISSING_DATA_MINUTES
    if last_vital_time is None:
        return AlertResult(
            rule_id="missing_data",
            triggered=True,
            severity="info",
            message_th="ℹ️ ยังไม่มีการบันทึก vital signs",
            details={"last_recorded": None},
        )
    now = datetime.now(timezone.utc)
    if last_vital_time.tzinfo is None:
        last_vital_time = last_vital_time.replace(tzinfo=timezone.utc)
    elapsed = (now - last_vital_time).total_seconds() / 60
    if elapsed > threshold:
        return AlertResult(
            rule_id="missing_data",
            triggered=True,
            severity="info",
            message_th=(
                f"ℹ️ ไม่มีการบันทึก vital signs {int(elapsed)} นาที "
                f"(เกิน {threshold} นาที)"
            ),
            details={"elapsed_minutes": int(elapsed), "threshold": threshold},
        )
    return None


# ── Alert Engine ──────────────────────────────────────────

class AlertEngine:
    """Evaluate all clinical rules for a session/patient."""

    def __init__(self, rules: Optional[list[AlertRule]] = None):
        self.rules = {r.rule_id: r for r in (rules or DEFAULT_RULES)}
        self._alerts: list[AlertResult] = []

    def evaluate_vitals(
        self,
        current_sbp: Optional[int] = None,
        current_dbp: Optional[int] = None,
        current_hr: Optional[int] = None,
        previous_sbps: Optional[list[tuple[datetime, int]]] = None,
        uf_rate_ml_hr: Optional[float] = None,
        patient_weight_kg: Optional[float] = None,
        pre_weight: Optional[float] = None,
        dry_weight: Optional[float] = None,
        last_vital_time: Optional[datetime] = None,
    ) -> list[AlertResult]:
        """Evaluate all enabled rules against current vital signs.

        Returns list of triggered alerts (empty if all normal).
        """
        self._alerts = []

        # Severe IDH
        if current_sbp and self.rules.get("severe_idh", AlertRule(
                "x", "", "", "critical", "")).enabled:
            result = check_severe_idh(current_sbp)
            if result:
                self._alerts.append(result)

        # IDH (only if not already severe)
        if (current_sbp and previous_sbps
                and not any(a.rule_id == "severe_idh" for a in self._alerts)
                and self.rules.get("idh", AlertRule(
                    "x", "", "", "warning", "")).enabled):
            result = check_idh(current_sbp, previous_sbps)
            if result:
                self._alerts.append(result)

        # Heart rate
        if current_hr and self.rules.get("bradycardia", AlertRule(
                "x", "", "", "critical", "")).enabled:
            result = check_heart_rate(current_hr)
            if result:
                self._alerts.append(result)

        # High UF rate
        if (uf_rate_ml_hr and patient_weight_kg
                and self.rules.get("high_uf_rate", AlertRule(
                    "x", "", "", "warning", "")).enabled):
            result = check_high_uf_rate(uf_rate_ml_hr, patient_weight_kg)
            if result:
                self._alerts.append(result)

        # Excess weight gain
        if (pre_weight and dry_weight
                and self.rules.get("excess_weight", AlertRule(
                    "x", "", "", "warning", "")).enabled):
            result = check_excess_weight(pre_weight, dry_weight)
            if result:
                self._alerts.append(result)

        # Missing data
        if self.rules.get("missing_data", AlertRule(
                "x", "", "", "info", "")).enabled:
            result = check_missing_data(last_vital_time)
            if result:
                self._alerts.append(result)

        return self._alerts

    @property
    def has_critical(self) -> bool:
        return any(a.severity == "critical" for a in self._alerts)

    @property
    def has_warning(self) -> bool:
        return any(a.severity == "warning" for a in self._alerts)

    def get_alerts(self) -> list[AlertResult]:
        return list(self._alerts)


# ── LINE Formatting ───────────────────────────────────────

def format_alert_for_line(
    alert: AlertResult,
    patient_name: str = "",
    hn: str = "",
    bed: Optional[int] = None,
) -> str:
    """Format an alert for LINE push notification (Thai)."""
    header = {
        "critical": "🚨 แจ้งเตือนวิกฤต",
        "warning": "⚠️ แจ้งเตือน",
        "info": "ℹ️ แจ้งเตือน",
    }.get(alert.severity, "แจ้งเตือน")

    lines = [
        f"{header}",
        "━━━━━━━━━━━━━━",
    ]
    if patient_name or hn:
        lines.append(f"👤 {patient_name} (HN {hn})")
    if bed:
        lines.append(f"🛏️ เตียง {bed}")
    lines.append(f"\n{alert.message_th}")
    lines.append(f"\n🕐 {datetime.now(timezone.utc).strftime('%H:%M')}")

    rule = RULES_BY_ID.get(alert.rule_id)
    if rule and rule.requires_approval:
        lines.append("\n⚡ กรุณาตรวจสอบและดำเนินการ")

    return "\n".join(lines)
