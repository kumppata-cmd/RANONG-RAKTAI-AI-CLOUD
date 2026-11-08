"""HD Smart Monitoring — Hemoglobin Predictor & ESA Optimizer.

Predicts Hb level 2 weeks ahead based on:
- Hb trend (last 3-6 months)
- ESA type and dose
- Iron status (Ferritin + TSAT)

KDIGO Anemia Guidelines:
- Target Hb: 10-12 g/dL (individualized)
- ESA dose adjustment based on Hb response
- Iron repletion before ESA dose increase

Safety: AI แนะนำเท่านั้น — แพทย์ตัดสินใจทุกครั้ง
"""

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class HbDataPoint:
    """A single Hb measurement."""
    date: datetime
    hb: float  # g/dL
    esa_drug: str = ""
    esa_dose: float = 0  # units
    ferritin: Optional[float] = None
    tsat: Optional[float] = None


@dataclass
class HbPrediction:
    """Result of Hb prediction."""
    current_hb: float
    predicted_hb_2wk: float
    trend: str  # "rising", "stable", "falling"
    trend_rate: float  # g/dL per month
    target_min: float = 10.0
    target_max: float = 12.0
    in_target: bool = False
    predicted_in_target: bool = False
    esa_recommendation: str = ""
    esa_recommendation_th: str = ""
    confidence: str = "low"  # "low", "medium", "high"
    iron_status: str = ""  # "adequate", "low", "deficient"
    iron_recommendation_th: str = ""
    details: dict = field(default_factory=dict)


class HbPredictor:
    """Predict Hb and recommend ESA dose adjustments.

    Uses linear regression on Hb history weighted by recency.
    Not a deep learning model — a clinically-guided heuristic
    that follows KDIGO anemia guidelines.
    """

    # KDIGO Anemia targets
    HB_TARGET_MIN = 10.0  # g/dL
    HB_TARGET_MAX = 12.0  # g/dL
    HB_CRITICAL_LOW = 7.0
    HB_CRITICAL_HIGH = 13.0

    # ESA dose adjustment rules (KDIGO 2012)
    ESA_DOSE_INCREASE_PCT = 25  # increase by 25% if Hb below target
    ESA_DOSE_DECREASE_PCT = 25  # decrease by 25% if Hb above target

    def predict(self, data_points: list[HbDataPoint]) -> Optional[HbPrediction]:
        """Predict Hb 2 weeks ahead from historical data.

        Requires at least 2 data points for trend.
        More points = higher confidence.
        """
        if not data_points or len(data_points) < 2:
            if data_points:
                return HbPrediction(
                    current_hb=data_points[0].hb,
                    predicted_hb_2wk=data_points[0].hb,
                    trend="unknown",
                    trend_rate=0,
                    in_target=self.HB_TARGET_MIN <= data_points[0].hb <= self.HB_TARGET_MAX,
                    predicted_in_target=self.HB_TARGET_MIN <= data_points[0].hb <= self.HB_TARGET_MAX,
                    confidence="low",
                    esa_recommendation="insufficient_data",
                    esa_recommendation_th="ข้อมูลไม่เพียงพอ — ต้องมีผล Hb อย่างน้อย 2 ครั้ง",
                )
            return None

        # Sort by date ascending
        sorted_pts = sorted(data_points, key=lambda p: p.date)
        latest = sorted_pts[-1]
        current_hb = latest.hb

        # Calculate trend using weighted linear regression
        trend_rate, r_squared = self._calc_trend(sorted_pts)

        # Predict 2 weeks ahead
        predicted_hb = current_hb + (trend_rate * 0.5)  # 0.5 months ≈ 2 weeks
        predicted_hb = max(3.0, min(18.0, predicted_hb))  # clamp to realistic range

        # Determine trend label
        if abs(trend_rate) < 0.3:
            trend = "stable"
        elif trend_rate > 0:
            trend = "rising"
        else:
            trend = "falling"

        # Confidence based on data quantity and consistency
        n_points = len(sorted_pts)
        if n_points >= 6 and r_squared > 0.5:
            confidence = "high"
        elif n_points >= 3:
            confidence = "medium"
        else:
            confidence = "low"

        # Check target ranges
        in_target = self.HB_TARGET_MIN <= current_hb <= self.HB_TARGET_MAX
        predicted_in_target = self.HB_TARGET_MIN <= predicted_hb <= self.HB_TARGET_MAX

        # Iron status assessment
        iron_status, iron_rec_th = self._assess_iron(latest)

        # ESA recommendation
        esa_rec, esa_rec_th = self._recommend_esa(
            current_hb, predicted_hb, trend, trend_rate,
            latest.esa_drug, latest.esa_dose,
            iron_status,
        )

        return HbPrediction(
            current_hb=round(current_hb, 1),
            predicted_hb_2wk=round(predicted_hb, 1),
            trend=trend,
            trend_rate=round(trend_rate, 2),
            target_min=self.HB_TARGET_MIN,
            target_max=self.HB_TARGET_MAX,
            in_target=in_target,
            predicted_in_target=predicted_in_target,
            esa_recommendation=esa_rec,
            esa_recommendation_th=esa_rec_th,
            confidence=confidence,
            iron_status=iron_status,
            iron_recommendation_th=iron_rec_th,
            details={
                "n_data_points": n_points,
                "r_squared": round(r_squared, 3),
                "date_range_days": (sorted_pts[-1].date - sorted_pts[0].date).days,
                "esa_drug": latest.esa_drug,
                "esa_dose": latest.esa_dose,
                "ferritin": latest.ferritin,
                "tsat": latest.tsat,
            },
        )

    def _calc_trend(self, sorted_pts: list[HbDataPoint]) -> tuple[float, float]:
        """Calculate Hb trend rate (g/dL per month) using weighted linear regression.

        Recent points weighted more heavily.
        Returns (slope_per_month, r_squared)
        """
        if len(sorted_pts) < 2:
            return 0.0, 0.0

        base_date = sorted_pts[0].date
        n = len(sorted_pts)

        # Convert to months from first point, with recency weights
        xs = []  # months
        ys = []  # Hb values
        ws = []  # weights (more recent = higher)
        for i, pt in enumerate(sorted_pts):
            days = (pt.date - base_date).days
            months = days / 30.0
            xs.append(months)
            ys.append(pt.hb)
            # Exponential recency weight
            ws.append(math.exp(0.5 * i / max(n - 1, 1)))

        # Weighted linear regression
        sum_w = sum(ws)
        sum_wx = sum(w * x for w, x in zip(ws, xs))
        sum_wy = sum(w * y for w, y in zip(ws, ys))
        sum_wxx = sum(w * x * x for w, x in zip(ws, xs))
        sum_wxy = sum(w * x * y for w, x, y in zip(ws, xs, ys))

        denom = sum_w * sum_wxx - sum_wx * sum_wx
        if abs(denom) < 1e-10:
            return 0.0, 0.0

        slope = (sum_w * sum_wxy - sum_wx * sum_wy) / denom
        intercept = (sum_wy - slope * sum_wx) / sum_w

        # R-squared
        mean_y = sum_wy / sum_w
        ss_res = sum(w * (y - (slope * x + intercept)) ** 2
                     for w, x, y in zip(ws, xs, ys))
        ss_tot = sum(w * (y - mean_y) ** 2
                     for w, y in zip(ws, ys))
        r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

        return slope, max(0, r_squared)

    def _assess_iron(self, latest: HbDataPoint) -> tuple[str, str]:
        """Assess iron status based on Ferritin + TSAT (KDIGO).

        KDIGO 2012:
        - Ferritin < 100 ng/mL → iron deficient (HD patients: < 200)
        - TSAT < 20% → functional iron deficiency
        - Ferritin > 500 → iron overload risk
        """
        if latest.ferritin is None and latest.tsat is None:
            return "unknown", "ไม่มีข้อมูล Ferritin/TSAT — แนะนำตรวจ"

        status = "adequate"
        rec_th = ""

        if latest.ferritin is not None:
            if latest.ferritin < 200:
                status = "deficient"
                rec_th = f"Ferritin {latest.ferritin:.0f} ต่ำ (<200) — พิจารณาให้ IV iron"
            elif latest.ferritin > 500:
                status = "overload_risk"
                rec_th = f"Ferritin {latest.ferritin:.0f} สูง (>500) — หยุด iron ชั่วคราว"

        if latest.tsat is not None and latest.tsat < 20:
            if status != "deficient":
                status = "functional_deficiency"
            rec_th += f"\nTSAT {latest.tsat:.0f}% ต่ำ (<20%) — functional iron deficiency"

        if status == "adequate":
            rec_th = "สถานะเหล็กปกติ"

        return status, rec_th.strip()

    def _recommend_esa(
        self,
        current_hb: float,
        predicted_hb: float,
        trend: str,
        trend_rate: float,
        esa_drug: str,
        esa_dose: float,
        iron_status: str,
    ) -> tuple[str, str]:
        """Recommend ESA dose adjustment based on KDIGO guidelines.

        Returns (recommendation_code, recommendation_thai)
        """
        # Critical Hb — urgent
        if current_hb < self.HB_CRITICAL_LOW:
            return ("urgent_low", f"🚨 Hb {current_hb} วิกฤต (<{self.HB_CRITICAL_LOW}) "
                    f"— พิจารณาให้เลือดและทบทวนสาเหตุ")

        if current_hb > self.HB_CRITICAL_HIGH:
            return ("urgent_high", f"🚨 Hb {current_hb} สูงเกิน (>{self.HB_CRITICAL_HIGH}) "
                    f"— พิจารณาลด/หยุด ESA ชั่วคราว")

        # Iron deficient — fix iron before adjusting ESA
        if iron_status in ("deficient", "functional_deficiency"):
            return ("iron_first", "💉 แก้ไขภาวะขาดเหล็กก่อนปรับ ESA "
                    "— KDIGO แนะนำ IV iron trial")

        esa_name = esa_drug or "ESA"

        # Below target
        if current_hb < self.HB_TARGET_MIN:
            if trend == "rising" and predicted_hb >= self.HB_TARGET_MIN:
                return ("maintain", f"📈 Hb กำลังขึ้น ({trend_rate:+.1f}/เดือน) "
                        f"คาดว่า 2 wk → {predicted_hb:.1f} — คง {esa_name} ขนาดเดิม")

            new_dose = esa_dose * (1 + self.ESA_DOSE_INCREASE_PCT / 100)
            return ("increase", f"⬆️ Hb {current_hb} ต่ำกว่าเป้า (<{self.HB_TARGET_MIN}) "
                    f"— พิจารณาเพิ่ม {esa_name} เป็น {new_dose:.0f}u "
                    f"(+{self.ESA_DOSE_INCREASE_PCT}%)")

        # Above target
        if current_hb > self.HB_TARGET_MAX:
            if trend == "falling":
                return ("monitor", f"📉 Hb กำลังลง ({trend_rate:+.1f}/เดือน) "
                        f"— ติดตาม 2 wk ก่อน")

            new_dose = esa_dose * (1 - self.ESA_DOSE_DECREASE_PCT / 100)
            return ("decrease", f"⬇️ Hb {current_hb} สูงกว่าเป้า (>{self.HB_TARGET_MAX}) "
                    f"— พิจารณาลด {esa_name} เป็น {new_dose:.0f}u "
                    f"(-{self.ESA_DOSE_DECREASE_PCT}%)")

        # In target
        if trend == "falling" and predicted_hb < self.HB_TARGET_MIN:
            return ("watch", f"⚠️ Hb ในเป้าแต่กำลังลง ({trend_rate:+.1f}/เดือน) "
                    f"คาดว่า 2 wk → {predicted_hb:.1f} — ติดตามใกล้ชิด")

        return ("on_target", f"✅ Hb {current_hb} อยู่ในเป้า ({self.HB_TARGET_MIN}-{self.HB_TARGET_MAX}) "
                f"— คง {esa_name} ขนาดเดิม")


def format_hb_prediction_flex(prediction: HbPrediction) -> dict:
    """Format HbPrediction as LINE Flex Message."""
    trend_icon = {"rising": "📈", "falling": "📉", "stable": "➡️"}.get(
        prediction.trend, "❓"
    )
    status_color = "#00C853" if prediction.in_target else "#FF6D00"
    predicted_color = "#00C853" if prediction.predicted_in_target else "#FF6D00"

    body_contents = [
        {"type": "text", "text": "🔬 พยากรณ์ Hb & ESA", "weight": "bold",
         "size": "lg", "color": "#1a1a2e"},
        {"type": "separator", "margin": "md"},
        {"type": "box", "layout": "horizontal", "margin": "md", "contents": [
            {"type": "text", "text": "Hb ปัจจุบัน", "size": "sm", "color": "#888", "flex": 4},
            {"type": "text", "text": f"{prediction.current_hb} g/dL",
             "size": "sm", "weight": "bold", "color": status_color, "flex": 3,
             "align": "end"},
        ]},
        {"type": "box", "layout": "horizontal", "contents": [
            {"type": "text", "text": f"คาดการณ์ 2 wk {trend_icon}",
             "size": "sm", "color": "#888", "flex": 4},
            {"type": "text", "text": f"{prediction.predicted_hb_2wk} g/dL",
             "size": "sm", "weight": "bold", "color": predicted_color, "flex": 3,
             "align": "end"},
        ]},
        {"type": "box", "layout": "horizontal", "contents": [
            {"type": "text", "text": "Trend", "size": "sm", "color": "#888", "flex": 4},
            {"type": "text", "text": f"{prediction.trend_rate:+.1f} g/dL/เดือน",
             "size": "sm", "flex": 3, "align": "end"},
        ]},
        {"type": "box", "layout": "horizontal", "contents": [
            {"type": "text", "text": "เป้าหมาย", "size": "sm", "color": "#888", "flex": 4},
            {"type": "text", "text": f"{prediction.target_min}-{prediction.target_max} g/dL",
             "size": "sm", "flex": 3, "align": "end"},
        ]},
        {"type": "box", "layout": "horizontal", "contents": [
            {"type": "text", "text": "ความเชื่อมั่น", "size": "sm", "color": "#888", "flex": 4},
            {"type": "text", "text": prediction.confidence.upper(),
             "size": "sm", "flex": 3, "align": "end"},
        ]},
        {"type": "separator", "margin": "md"},
        {"type": "text", "text": "💊 คำแนะนำ ESA", "weight": "bold",
         "size": "sm", "margin": "md"},
        {"type": "text", "text": prediction.esa_recommendation_th,
         "size": "sm", "wrap": True, "margin": "sm"},
    ]

    if prediction.iron_recommendation_th:
        body_contents.extend([
            {"type": "separator", "margin": "md"},
            {"type": "text", "text": "💉 สถานะเหล็ก", "weight": "bold",
             "size": "sm", "margin": "md"},
            {"type": "text", "text": prediction.iron_recommendation_th,
             "size": "sm", "wrap": True, "margin": "sm"},
        ])

    body_contents.append(
        {"type": "text", "text": "⚕️ AI แนะนำเท่านั้น — แพทย์ตัดสินใจ",
         "size": "xs", "color": "#999", "margin": "lg", "align": "center"}
    )

    return {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": body_contents,
        },
    }
