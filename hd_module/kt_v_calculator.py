"""F3: Kt/V Calculator — คำนวณ Dialysis Adequacy

KDOQI Clinical Practice Guidelines:
- Kt/V ≥ 1.2 (adequate) — single-pool
- URR ≥ 65% (adequate)
- Kt/V 1.0-1.19 (borderline)
- Kt/V < 1.0 (inadequate)

Formulas:
  Kt/V (Daugirdas 2nd generation):
    Kt/V = -ln(R - 0.008*t) + (4 - 3.5*R) * UF/W
    where R = post_BUN/pre_BUN, t = duration(hr), UF = weight loss(kg), W = post_weight(kg)

  URR:
    URR = (1 - post_BUN/pre_BUN) * 100
"""

import logging
import math

logger = logging.getLogger(__name__)


def calculate_kt_v(pre_bun: float, post_bun: float,
                    duration_hours: float,
                    pre_weight: float, post_weight: float) -> dict:
    """คำนวณ Kt/V + URR + assessment

    Returns: {
        "kt_v": 1.35,
        "urr": 72.5,
        "assessment": "adequate",
        "assessment_th": "เพียงพอ",
        "kdoqi_target": "≥1.2",
        "details": {...}
    }
    """
    if pre_bun <= 0 or post_bun <= 0 or duration_hours <= 0:
        return {"error": "ค่า BUN หรือ duration ไม่ถูกต้อง"}
    if pre_weight <= 0 or post_weight <= 0:
        return {"error": "ค่าน้ำหนักไม่ถูกต้อง"}
    if post_bun >= pre_bun:
        return {"error": "Post-BUN ต้องน้อยกว่า Pre-BUN"}

    R = post_bun / pre_bun
    t = duration_hours
    uf = pre_weight - post_weight  # kg
    W = post_weight

    # Daugirdas 2nd generation formula
    try:
        kt_v = -math.log(R - 0.008 * t) + (4 - 3.5 * R) * (uf / W)
    except (ValueError, ZeroDivisionError):
        return {"error": "คำนวณ Kt/V ไม่ได้ — ตรวจค่า input"}

    urr = (1 - R) * 100

    # Assessment
    if kt_v >= 1.4:
        assessment = "excellent"
        assessment_th = "ดีมาก"
        emoji = "🟢"
    elif kt_v >= 1.2:
        assessment = "adequate"
        assessment_th = "เพียงพอ"
        emoji = "🟢"
    elif kt_v >= 1.0:
        assessment = "borderline"
        assessment_th = "พอใช้ — ควรปรับปรุง"
        emoji = "🟡"
    else:
        assessment = "inadequate"
        assessment_th = "ไม่เพียงพอ — ต้องปรับการฟอก"
        emoji = "🔴"

    urr_ok = urr >= 65

    return {
        "kt_v": round(kt_v, 2),
        "urr": round(urr, 1),
        "assessment": assessment,
        "assessment_th": assessment_th,
        "emoji": emoji,
        "kdoqi_target_kt_v": "≥1.2",
        "kdoqi_target_urr": "≥65%",
        "urr_adequate": urr_ok,
        "details": {
            "pre_bun": pre_bun,
            "post_bun": post_bun,
            "R": round(R, 3),
            "duration_hours": t,
            "uf_kg": round(uf, 1),
            "uf_ml": round(uf * 1000),
            "pre_weight": pre_weight,
            "post_weight": post_weight,
            "formula": "Daugirdas 2nd generation",
        },
        "line_summary": (
            f"{emoji} Kt/V = {kt_v:.2f} ({assessment_th})\n"
            f"   URR = {urr:.1f}% ({'✅' if urr_ok else '❌'} target ≥65%)\n"
            f"   BUN: {pre_bun}→{post_bun} mg/dL\n"
            f"   UF: {uf:.1f} kg ({uf*1000:.0f} mL)\n"
            f"   Duration: {t:.1f} hr"
        ),
    }
