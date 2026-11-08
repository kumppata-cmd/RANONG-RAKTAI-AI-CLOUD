"""HD Smart Monitoring — Predictive Analytics & Clinical Decision Support.

Phase 6 Models:
1. CKD Progression Predictor — eGFR decline rate + time to ESKD
2. Dialysis Adequacy Optimizer — Kt/V + URR tracking
3. Nutrition & Fluid Advisor — K+, PO4, Ca → diet recommendations

References:
- KDIGO 2024 CKD Guidelines
- KDOQI 2015 HD Adequacy
- Thai Dietary Guidelines for CKD

Safety: AI แนะนำเท่านั้น — แพทย์ตัดสินใจทุกครั้ง
"""

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# 6.1 CKD Progression Predictor
# ═══════════════════════════════════════════════════════════

CKD_STAGES = [
    ("G1", 90, 999, "ปกติหรือสูง"),
    ("G2", 60, 89, "ลดลงเล็กน้อย"),
    ("G3a", 45, 59, "ลดลงเล็กน้อย-ปานกลาง"),
    ("G3b", 30, 44, "ลดลงปานกลาง-มาก"),
    ("G4", 15, 29, "ลดลงมาก"),
    ("G5", 0, 14, "ไตวาย (ESKD)"),
]


def get_ckd_stage(egfr: float) -> tuple[str, str]:
    """Return (stage_code, description_th) for an eGFR value."""
    for code, low, high, desc in CKD_STAGES:
        if low <= egfr <= high:
            return code, desc
    if egfr >= 90:
        return "G1", "ปกติหรือสูง"
    return "G5", "ไตวาย (ESKD)"


@dataclass
class EgfrDataPoint:
    date: datetime
    egfr: float  # mL/min/1.73m²
    creatinine: Optional[float] = None


@dataclass
class CkdPrediction:
    """CKD Progression prediction result."""
    current_egfr: float
    current_stage: str
    stage_description: str
    decline_rate: float  # mL/min/year (negative = declining)
    predicted_egfr_6mo: float
    predicted_egfr_12mo: float
    months_to_eskd: Optional[float] = None  # months until eGFR < 15
    risk_level: str = "stable"  # "rapid_decline", "moderate_decline", "stable", "improving"
    recommendation_th: str = ""
    confidence: str = "low"
    details: dict = field(default_factory=dict)


class CkdProgressionPredictor:
    """Predict CKD progression based on eGFR trend.

    KDIGO defines rapid decline as > 5 mL/min/year.
    Normal age-related decline is ~1 mL/min/year.
    """

    RAPID_DECLINE_THRESHOLD = -5.0  # mL/min/year
    MODERATE_DECLINE_THRESHOLD = -3.0
    ESKD_THRESHOLD = 15.0  # eGFR below this = G5/ESKD

    def predict(self, data_points: list[EgfrDataPoint]) -> Optional[CkdPrediction]:
        if not data_points:
            return None

        sorted_pts = sorted(data_points, key=lambda p: p.date)
        latest = sorted_pts[-1]
        current_egfr = latest.egfr
        stage, stage_desc = get_ckd_stage(current_egfr)

        if len(sorted_pts) < 2:
            return CkdPrediction(
                current_egfr=round(current_egfr, 1),
                current_stage=stage,
                stage_description=stage_desc,
                decline_rate=0,
                predicted_egfr_6mo=current_egfr,
                predicted_egfr_12mo=current_egfr,
                confidence="low",
                recommendation_th="ข้อมูลไม่เพียงพอ — ต้องมีผล eGFR อย่างน้อย 2 ครั้ง",
            )

        # Calculate decline rate (mL/min/year) using linear regression
        decline_rate, r_sq = self._calc_decline_rate(sorted_pts)

        # Predict future eGFR
        pred_6mo = max(0, current_egfr + decline_rate * 0.5)
        pred_12mo = max(0, current_egfr + decline_rate * 1.0)

        # Time to ESKD
        months_to_eskd = None
        if decline_rate < -0.5 and current_egfr > self.ESKD_THRESHOLD:
            years = (current_egfr - self.ESKD_THRESHOLD) / abs(decline_rate)
            months_to_eskd = round(years * 12, 0)

        # Risk classification
        if decline_rate <= self.RAPID_DECLINE_THRESHOLD:
            risk = "rapid_decline"
        elif decline_rate <= self.MODERATE_DECLINE_THRESHOLD:
            risk = "moderate_decline"
        elif decline_rate > 1.0:
            risk = "improving"
        else:
            risk = "stable"

        # Confidence
        n = len(sorted_pts)
        span_days = (sorted_pts[-1].date - sorted_pts[0].date).days
        if n >= 4 and span_days >= 180 and r_sq > 0.5:
            confidence = "high"
        elif n >= 3 and span_days >= 90:
            confidence = "medium"
        else:
            confidence = "low"

        # Recommendation
        rec = self._recommend(current_egfr, stage, decline_rate, risk, months_to_eskd)

        return CkdPrediction(
            current_egfr=round(current_egfr, 1),
            current_stage=stage,
            stage_description=stage_desc,
            decline_rate=round(decline_rate, 1),
            predicted_egfr_6mo=round(pred_6mo, 1),
            predicted_egfr_12mo=round(pred_12mo, 1),
            months_to_eskd=months_to_eskd,
            risk_level=risk,
            recommendation_th=rec,
            confidence=confidence,
            details={
                "n_points": n,
                "span_days": span_days,
                "r_squared": round(r_sq, 3),
            },
        )

    def _calc_decline_rate(self, sorted_pts: list[EgfrDataPoint]) -> tuple[float, float]:
        """Linear regression → mL/min/year decline rate."""
        if len(sorted_pts) < 2:
            return 0.0, 0.0

        base = sorted_pts[0].date
        xs = [(p.date - base).days / 365.25 for p in sorted_pts]
        ys = [p.egfr for p in sorted_pts]
        n = len(xs)

        # Weighted regression (recent points weighted higher)
        ws = [math.exp(0.5 * i / max(n - 1, 1)) for i in range(n)]
        sw = sum(ws)
        swx = sum(w * x for w, x in zip(ws, xs))
        swy = sum(w * y for w, y in zip(ws, ys))
        swxx = sum(w * x * x for w, x in zip(ws, xs))
        swxy = sum(w * x * y for w, x, y in zip(ws, xs, ys))

        denom = sw * swxx - swx * swx
        if abs(denom) < 1e-10:
            return 0.0, 0.0

        slope = (sw * swxy - swx * swy) / denom
        intercept = (swy - slope * swx) / sw

        mean_y = swy / sw
        ss_res = sum(w * (y - (slope * x + intercept)) ** 2
                     for w, x, y in zip(ws, xs, ys))
        ss_tot = sum(w * (y - mean_y) ** 2 for w, y in zip(ws, ys))
        r_sq = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

        return slope, max(0, r_sq)

    def _recommend(self, egfr, stage, rate, risk, months_eskd):
        risk_icons = {
            "rapid_decline": "🔴",
            "moderate_decline": "🟡",
            "stable": "🟢",
            "improving": "📈",
        }
        icon = risk_icons.get(risk, "")

        lines = []
        if risk == "rapid_decline":
            lines.append(f"{icon} eGFR ลดเร็ว ({rate:+.1f} mL/min/ปี)")
            lines.append("• ตรวจสอบสาเหตุ: dehydration, NSAIDs, obstruction, AKI on CKD")
            lines.append("• พิจารณาส่งต่อ nephrologist (ถ้ายังไม่ได้)")
            if months_eskd and months_eskd < 24:
                lines.append(f"• ⚠️ คาดว่าถึง ESKD ใน ~{months_eskd:.0f} เดือน — เตรียม vascular access")
        elif risk == "moderate_decline":
            lines.append(f"{icon} eGFR ลดปานกลาง ({rate:+.1f} mL/min/ปี)")
            lines.append("• ควบคุม BP <130/80, DM target HbA1c <7%")
            lines.append("• ลด proteinuria (ACEi/ARB)")
            if months_eskd:
                lines.append(f"• คาดว่าถึง ESKD ใน ~{months_eskd:.0f} เดือน")
        elif risk == "improving":
            lines.append(f"{icon} eGFR ดีขึ้น ({rate:+.1f} mL/min/ปี)")
            lines.append("• การรักษาได้ผล — คงแนวทางเดิม")
        else:
            lines.append(f"{icon} eGFR คงที่ ({rate:+.1f} mL/min/ปี)")
            lines.append("• ติดตามต่อเนื่องทุก 3-6 เดือน")

        if stage in ("G4", "G5"):
            lines.append(f"• CKD Stage {stage}: เตรียมพร้อม RRT (HD/PD/KT)")

        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
# 6.2 Dialysis Adequacy Optimizer
# ═══════════════════════════════════════════════════════════

@dataclass
class AdequacyResult:
    """Dialysis adequacy assessment."""
    ktv: Optional[float] = None
    urr: Optional[float] = None  # % Urea Reduction Ratio
    ktv_adequate: bool = False
    urr_adequate: bool = False
    overall: str = "unknown"  # "adequate", "inadequate", "borderline"
    recommendation_th: str = ""
    trend: str = ""  # "improving", "stable", "declining"


class DialysisAdequacyOptimizer:
    """Track and optimize Kt/V and URR.

    KDOQI targets:
    - Kt/V ≥ 1.2 (single-pool, minimum)
    - Kt/V ≥ 1.4 (target for thrice-weekly)
    - URR ≥ 65%
    """

    KTV_MINIMUM = 1.2
    KTV_TARGET = 1.4
    URR_MINIMUM = 65.0

    def assess(
        self,
        ktv: Optional[float] = None,
        urr: Optional[float] = None,
        pre_bun: Optional[float] = None,
        post_bun: Optional[float] = None,
        session_duration_hr: Optional[float] = None,
        blood_flow_ml_min: Optional[float] = None,
        dialyzer_type: str = "",
        history: Optional[list[dict]] = None,
    ) -> AdequacyResult:
        """Assess dialysis adequacy and recommend adjustments."""

        # Calculate URR from BUN if not given
        if urr is None and pre_bun and post_bun and pre_bun > 0:
            urr = (1 - post_bun / pre_bun) * 100

        result = AdequacyResult(ktv=ktv, urr=urr)

        if ktv is not None:
            result.ktv_adequate = ktv >= self.KTV_MINIMUM
        if urr is not None:
            result.urr_adequate = urr >= self.URR_MINIMUM

        # Overall assessment
        if ktv is not None:
            if ktv >= self.KTV_TARGET:
                result.overall = "adequate"
            elif ktv >= self.KTV_MINIMUM:
                result.overall = "borderline"
            else:
                result.overall = "inadequate"
        elif urr is not None:
            result.overall = "adequate" if urr >= self.URR_MINIMUM else "inadequate"

        # Trend from history
        if history and len(history) >= 2:
            recent = [h.get("ktv", 0) for h in history[-3:] if h.get("ktv")]
            if len(recent) >= 2:
                if recent[-1] > recent[0] + 0.1:
                    result.trend = "improving"
                elif recent[-1] < recent[0] - 0.1:
                    result.trend = "declining"
                else:
                    result.trend = "stable"

        # Recommendations
        result.recommendation_th = self._recommend(
            ktv, urr, result.overall, session_duration_hr,
            blood_flow_ml_min, dialyzer_type
        )

        return result

    def _recommend(self, ktv, urr, overall, duration, flow, dialyzer):
        lines = []

        if overall == "adequate":
            lines.append("✅ Dialysis adequacy เพียงพอ")
            if ktv is not None:
                lines.append(f"   Kt/V {ktv:.2f} (เป้า ≥{self.KTV_MINIMUM})")
            if urr is not None:
                lines.append(f"   URR {urr:.0f}% (เป้า ≥{self.URR_MINIMUM}%)")

        elif overall == "borderline":
            lines.append(f"⚠️ Kt/V {ktv:.2f} — borderline (เป้า ≥{self.KTV_TARGET})")
            lines.append("พิจารณาปรับ:")
            if duration and duration < 4:
                lines.append(f"  • เพิ่มเวลาฟอกจาก {duration:.1f} → {min(duration+0.5, 5):.1f} ชม.")
            if flow and flow < 300:
                lines.append(f"  • เพิ่ม blood flow จาก {flow:.0f} → {min(flow+50, 400):.0f} mL/min")
            lines.append("  • พิจารณาเปลี่ยน dialyzer (high-flux)")

        elif overall == "inadequate":
            ktv_str = f"Kt/V {ktv:.2f}" if ktv else ""
            urr_str = f"URR {urr:.0f}%" if urr else ""
            lines.append(f"🔴 Inadequate: {ktv_str} {urr_str}")
            lines.append("ต้องปรับเร่งด่วน:")
            if duration and duration < 4:
                lines.append(f"  • เพิ่มเวลาฟอกเป็น 4 ชม. (ปัจจุบัน {duration:.1f})")
            if flow and flow < 300:
                lines.append(f"  • เพิ่ม blood flow เป็น 300+ mL/min (ปัจจุบัน {flow:.0f})")
            lines.append("  • ตรวจ vascular access: recirculation, stenosis")
            lines.append("  • เปลี่ยน dialyzer (high-flux, larger surface)")
            lines.append("  • ตรวจ compliance: ผู้ป่วยมาครบตามนัดหรือไม่")

        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
# 6.3 Nutrition & Fluid Advisor
# ═══════════════════════════════════════════════════════════

@dataclass
class NutritionAdvice:
    """Personalized nutrition advice for HD patient."""
    potassium_status: str = ""  # "normal", "high", "low"
    phosphate_status: str = ""
    calcium_status: str = ""
    albumin_status: str = ""
    fluid_gain_status: str = ""
    diet_recommendations: list = field(default_factory=list)
    fluid_recommendation: str = ""
    overall_th: str = ""


class NutritionFluidAdvisor:
    """Personalized nutrition & fluid management for HD patients.

    KDIGO/KDOQI targets for HD patients:
    - K+: 3.5-5.5 mEq/L
    - PO4: 3.5-5.5 mg/dL
    - Ca: 8.4-9.5 mg/dL (corrected)
    - Ca×PO4: < 55
    - Albumin: ≥ 3.5 g/dL
    - IDWG: < 4% of dry weight
    """

    K_LOW = 3.5
    K_HIGH = 5.5
    K_CRITICAL = 6.5
    PO4_LOW = 3.5
    PO4_HIGH = 5.5
    CA_LOW = 8.4
    CA_HIGH = 9.5
    ALB_LOW = 3.5
    IDWG_MAX_PCT = 4.0

    def advise(
        self,
        potassium: Optional[float] = None,
        phosphate: Optional[float] = None,
        calcium: Optional[float] = None,
        albumin: Optional[float] = None,
        pre_weight: Optional[float] = None,
        dry_weight: Optional[float] = None,
        patient_name: str = "",
    ) -> NutritionAdvice:
        """Generate personalized nutrition advice."""
        advice = NutritionAdvice()
        recs = []

        # Potassium
        if potassium is not None:
            if potassium > self.K_CRITICAL:
                advice.potassium_status = "critical_high"
                recs.append("🔴 K+ สูงวิกฤต ({:.1f}) — ลดอาหาร K+ สูงทันที:".format(potassium))
                recs.append("  ❌ หลีกเลี่ยง: กล้วย, ส้ม, มะเขือเทศ, มันฝรั่ง, ผักใบเขียวเข้ม")
                recs.append("  ❌ หลีกเลี่ยง: น้ำมะพร้าว, น้ำผลไม้, ช็อกโกแลต")
                recs.append("  ✅ แนะนำ: แอปเปิ้ล, สับปะรด, แตงโม (ปริมาณน้อย)")
                recs.append("  💡 แช่ผักในน้ำ 2 ชม. ก่อนปรุง ลด K+ 30-50%")
            elif potassium > self.K_HIGH:
                advice.potassium_status = "high"
                recs.append("⚠️ K+ สูง ({:.1f}) — จำกัดอาหาร K+ สูง:".format(potassium))
                recs.append("  • ลดผลไม้ K+ สูง: กล้วย, ส้ม, มะม่วง")
                recs.append("  • ลดผัก K+ สูง: มะเขือเทศ, มันฝรั่ง")
                recs.append("  💡 แช่ผักในน้ำก่อนปรุง")
            elif potassium < self.K_LOW:
                advice.potassium_status = "low"
                recs.append("⚠️ K+ ต่ำ ({:.1f}) — เพิ่มอาหาร K+:".format(potassium))
                recs.append("  • กล้วย, ส้ม, น้ำมะพร้าว (ปริมาณพอเหมาะ)")
            else:
                advice.potassium_status = "normal"

        # Phosphate
        if phosphate is not None:
            if phosphate > self.PO4_HIGH:
                advice.phosphate_status = "high"
                recs.append("⚠️ PO4 สูง ({:.1f}) — จำกัดฟอสเฟต:".format(phosphate))
                recs.append("  ❌ ลด: นม, ชีส, ถั่ว, น้ำอัดลมสีเข้ม, เครื่องในสัตว์")
                recs.append("  ❌ ลด: อาหารแปรรูป (มี phosphate additives)")
                recs.append("  💊 กิน phosphate binder พร้อมอาหารทุกมื้อ")
            elif phosphate < self.PO4_LOW:
                advice.phosphate_status = "low"
                recs.append("ℹ️ PO4 ต่ำ ({:.1f}) — ปรึกษาแพทย์เรื่อง binder".format(phosphate))
            else:
                advice.phosphate_status = "normal"

        # Calcium
        if calcium is not None:
            ca_po4 = calcium * phosphate if phosphate else 0
            if calcium > self.CA_HIGH:
                advice.calcium_status = "high"
                recs.append("⚠️ Ca สูง ({:.1f}) — ลดแคลเซียม:".format(calcium))
                recs.append("  • พิจารณาเปลี่ยน binder เป็น non-calcium based")
                if ca_po4 > 55:
                    recs.append(f"  🔴 Ca×PO4 = {ca_po4:.0f} (>55) — เสี่ยง calcification")
            elif calcium < self.CA_LOW:
                advice.calcium_status = "low"
                recs.append("⚠️ Ca ต่ำ ({:.1f}) — ตรวจ Vitamin D + PTH".format(calcium))

        # Albumin (nutrition marker)
        if albumin is not None:
            if albumin < self.ALB_LOW:
                advice.albumin_status = "low"
                recs.append("⚠️ Albumin ต่ำ ({:.1f}) — ภาวะขาดโปรตีน:".format(albumin))
                recs.append("  • เพิ่มโปรตีนคุณภาพ: ไข่ขาว, ปลา, อกไก่")
                recs.append("  • เป้าหมาย: โปรตีน 1.0-1.2 g/kg/วัน (HD)")
                recs.append("  • พิจารณา oral nutritional supplements")

        # Fluid gain (IDWG)
        if pre_weight and dry_weight and dry_weight > 0:
            idwg = pre_weight - dry_weight
            idwg_pct = (idwg / dry_weight) * 100
            if idwg_pct > self.IDWG_MAX_PCT:
                advice.fluid_gain_status = "excess"
                advice.fluid_recommendation = (
                    f"🔴 น้ำหนักเพิ่ม {idwg:.1f} kg ({idwg_pct:.1f}% ของ DW)\n"
                    f"  • จำกัดน้ำ: 500-750 mL + ปัสสาวะ/วัน\n"
                    f"  • ลดเกลือ: < 5 g/วัน\n"
                    f"  • อมน้ำแข็ง แทนดื่มน้ำ\n"
                    f"  • หลีกเลี่ยง: ซุป, อาหารน้ำมาก, ผลไม้ฉ่ำน้ำ"
                )
            elif idwg_pct > 2:
                advice.fluid_gain_status = "moderate"
                advice.fluid_recommendation = (
                    f"⚠️ น้ำหนักเพิ่ม {idwg:.1f} kg ({idwg_pct:.1f}%)\n"
                    f"  • จำกัดน้ำ + ลดเกลือ"
                )
            else:
                advice.fluid_gain_status = "normal"
                advice.fluid_recommendation = f"✅ น้ำหนักเพิ่ม {idwg:.1f} kg ({idwg_pct:.1f}%) — ปกติ"

        advice.diet_recommendations = recs

        # Build overall summary
        all_lines = []
        if patient_name:
            all_lines.append(f"🥗 คำแนะนำโภชนาการ — {patient_name}")
            all_lines.append("━━━━━━━━━━━━━━━━")
        if recs:
            all_lines.extend(recs)
        else:
            all_lines.append("✅ ค่าเลือดอยู่ในเกณฑ์ปกติ")
        if advice.fluid_recommendation:
            all_lines.append("")
            all_lines.append("💧 น้ำและเกลือ:")
            all_lines.append(advice.fluid_recommendation)
        all_lines.append("\n⚕️ AI แนะนำเท่านั้น — ปรึกษาแพทย์และนักกำหนดอาหาร")
        advice.overall_th = "\n".join(all_lines)

        return advice


# ═══════════════════════════════════════════════════════════
# LINE Formatting
# ═══════════════════════════════════════════════════════════

def format_ckd_prediction_for_line(pred: CkdPrediction, patient_name: str = "") -> str:
    """Format CKD prediction as LINE text."""
    risk_icons = {
        "rapid_decline": "🔴 ลดเร็ว",
        "moderate_decline": "🟡 ลดปานกลาง",
        "stable": "🟢 คงที่",
        "improving": "📈 ดีขึ้น",
    }
    risk_label = risk_icons.get(pred.risk_level, pred.risk_level)

    lines = [
        f"📊 พยากรณ์ CKD Progression",
    ]
    if patient_name:
        lines[0] += f" — {patient_name}"
    lines.append("━━━━━━━━━━━━━━━━")
    lines.append(f"🔬 eGFR ปัจจุบัน: {pred.current_egfr} mL/min")
    lines.append(f"🏷️ CKD Stage: {pred.current_stage} ({pred.stage_description})")
    lines.append(f"📉 อัตราลดลง: {pred.decline_rate:+.1f} mL/min/ปี")
    lines.append(f"🔮 คาด 6 เดือน: {pred.predicted_egfr_6mo} mL/min")
    lines.append(f"🔮 คาด 12 เดือน: {pred.predicted_egfr_12mo} mL/min")
    if pred.months_to_eskd:
        lines.append(f"⏰ ถึง ESKD ใน: ~{pred.months_to_eskd:.0f} เดือน")
    lines.append(f"⚡ ระดับความเสี่ยง: {risk_label}")
    lines.append(f"📊 ความเชื่อมั่น: {pred.confidence.upper()}")
    lines.append("")
    lines.append("💡 คำแนะนำ:")
    lines.append(pred.recommendation_th)
    lines.append("\n⚕️ AI แนะนำเท่านั้น — แพทย์ตัดสินใจ")

    return "\n".join(lines)


def format_adequacy_for_line(result: AdequacyResult) -> str:
    """Format adequacy assessment as LINE text."""
    return result.recommendation_th
