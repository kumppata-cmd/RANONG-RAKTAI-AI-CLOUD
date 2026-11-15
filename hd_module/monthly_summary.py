"""
Monthly HD Lab Summary — push สรุปผลเลือดรายเดือนให้ผู้ป่วยทาง LINE
เดือนละ 1 ครั้ง (วันที่ 1 ของเดือน, เวลา 09:00)

ใช้ข้อมูลจาก hd_lab_results, hd_sessions, hd_patient_mapping
ส่งเฉพาะผู้ป่วย active ที่ผูก LINE แล้ว + มีผล lab เดือนที่แล้ว
"""
import logging
import os
from datetime import date, timedelta

logger = logging.getLogger(__name__)


def _f(v):
    """Format number or return None."""
    if v is None:
        return None
    try:
        return round(float(v), 1)
    except (TypeError, ValueError):
        return None


def _interpret_hb(v):
    if v is None:
        return ""
    if v < 7:
        return "⚠️ ต่ำมาก — ควรปรึกษาแพทย์"
    if v < 10:
        return "🟡 ต่ำกว่าเป้า (เป้า 10-12)"
    if v <= 12:
        return "✅ อยู่ในเป้า (10-12)"
    return "🟡 สูงกว่าเป้า"


def _interpret_ktv(v):
    if v is None:
        return ""
    if v >= 1.4:
        return "✅ ดีมาก"
    if v >= 1.2:
        return "✅ ผ่านเกณฑ์ (≥1.2)"
    return "⚠️ ต่ำกว่าเกณฑ์ — ควรเพิ่มเวลาฟอกหรือปรับ flow"


def _trend_arrow(current, previous):
    """Return ↑ ↓ → based on change."""
    if current is None or previous is None:
        return ""
    diff = current - previous
    if abs(diff) < 0.2:
        return "→"
    return "↑" if diff > 0 else "↓"


def collect_monthly_data(conn, year, month):
    """Collect summary data for all active patients for a given month.

    Returns list of dicts with patient info + lab summary.
    """
    # Calculate date range for the target month
    first_day = date(year, month, 1)
    if month == 12:
        last_day = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(year, month + 1, 1) - timedelta(days=1)

    # Previous month for trend comparison
    if month == 1:
        prev_first = date(year - 1, 12, 1)
        prev_last = date(year - 1, 12, 31)
    else:
        prev_first = date(year, month - 1, 1)
        prev_last = first_day - timedelta(days=1)

    results = []

    with conn.cursor() as cur:
        # Get all active patients
        cur.execute("""
            SELECT id, hn, patient_name, dry_weight, patient_goal
            FROM hd_patient_mapping WHERE is_active = TRUE
            ORDER BY patient_name
        """)
        patients = cur.fetchall()

        for pid, hn, name, dw, goal in patients:
            # This month's labs (latest per field)
            cur.execute("""
                SELECT hemoglobin, hematocrit, ferritin, ipth, kt_v, lab_date
                FROM hd_lab_results
                WHERE patient_id = %s AND lab_date BETWEEN %s AND %s
                ORDER BY lab_date DESC, id DESC LIMIT 5
            """, (pid, first_day, last_day))
            rows = cur.fetchall()
            if not rows:
                continue  # No labs this month — skip

            # Latest values this month
            this = {}
            for r in rows:
                for i, k in enumerate(("hb", "hct", "ferritin", "ipth", "ktv")):
                    if r[i] is not None and k not in this:
                        this[k] = _f(r[i])
            this["date"] = rows[0][5].isoformat() if rows[0][5] else None

            # Previous month's labs for trend
            cur.execute("""
                SELECT hemoglobin, hematocrit, ferritin, ipth, kt_v
                FROM hd_lab_results
                WHERE patient_id = %s AND lab_date BETWEEN %s AND %s
                ORDER BY lab_date DESC, id DESC LIMIT 5
            """, (pid, prev_first, prev_last))
            prev_rows = cur.fetchall()
            prev = {}
            for r in prev_rows:
                for i, k in enumerate(("hb", "hct", "ferritin", "ipth", "ktv")):
                    if r[i] is not None and k not in prev:
                        prev[k] = _f(r[i])

            # Sessions this month
            cur.execute("""
                SELECT COUNT(*), AVG(actual_uf)
                FROM hd_sessions
                WHERE patient_id = %s AND session_date BETWEEN %s AND %s
                  AND status = 'completed'
            """, (pid, first_day, last_day))
            sess = cur.fetchone()
            session_count = sess[0] or 0
            avg_uf = _f(sess[1])

            # IDWG: sessions with pre_weight this month
            idwg_over = 0
            if dw and float(dw) > 0:
                cur.execute("""
                    SELECT pre_weight FROM hd_sessions
                    WHERE patient_id = %s AND session_date BETWEEN %s AND %s
                      AND status = 'completed' AND pre_weight IS NOT NULL
                    ORDER BY session_date
                """, (pid, first_day, last_day))
                for (pw,) in cur.fetchall():
                    if pw and float(pw) > 0:
                        pct = (float(pw) - float(dw)) / float(dw) * 100
                        if pct > 5:
                            idwg_over += 1

            results.append({
                "pid": pid,
                "hn": hn,
                "name": name,
                "dw": _f(dw),
                "goal": goal,
                "this": this,
                "prev": prev,
                "sessions": session_count,
                "avg_uf": avg_uf,
                "idwg_over": idwg_over,
            })

    return results


MONTH_TH = {
    1: "ม.ค.", 2: "ก.พ.", 3: "มี.ค.", 4: "เม.ย.",
    5: "พ.ค.", 6: "มิ.ย.", 7: "ก.ค.", 8: "ส.ค.",
    9: "ก.ย.", 10: "ต.ค.", 11: "พ.ย.", 12: "ธ.ค.",
}


def build_message(rec, year, month):
    """Build a LINE push message text for one patient."""
    m = MONTH_TH.get(month, str(month))
    yr = year + 543  # Buddhist era

    lines = [
        f"📊 สรุปผลเลือดประจำเดือน {m} {yr}",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"👤 {rec['name']}",
        "",
    ]

    this = rec["this"]
    prev = rec["prev"]

    # Hb
    if this.get("hb") is not None:
        arrow = _trend_arrow(this["hb"], prev.get("hb"))
        interp = _interpret_hb(this["hb"])
        lines.append(f"🩸 Hb: {this['hb']} g/dL {arrow}")
        lines.append(f"   {interp}")

    # Hct
    if this.get("hct") is not None:
        arrow = _trend_arrow(this["hct"], prev.get("hct"))
        lines.append(f"   Hct: {this['hct']}% {arrow}")

    # Kt/V
    if this.get("ktv") is not None:
        arrow = _trend_arrow(this["ktv"], prev.get("ktv"))
        interp = _interpret_ktv(this["ktv"])
        lines.append(f"💧 Kt/V: {this['ktv']} {arrow}")
        lines.append(f"   {interp}")

    # Ferritin
    if this.get("ferritin") is not None:
        lines.append(f"🔬 Ferritin: {this['ferritin']} ng/mL")

    # iPTH
    if this.get("ipth") is not None:
        lines.append(f"🦴 iPTH: {this['ipth']} pg/mL")

    lines.append("")

    # Sessions
    lines.append(f"🏥 ฟอกเลือดเดือนนี้: {rec['sessions']} ครั้ง")
    if rec["avg_uf"]:
        lines.append(f"   UF เฉลี่ย: {rec['avg_uf']} L")
    if rec["idwg_over"] > 0:
        lines.append(f"   ⚠️ น้ำหนักขึ้น >5% DW: {rec['idwg_over']} ครั้ง")

    # Goal
    if rec.get("goal"):
        lines.append("")
        lines.append(f"🎯 เป้าหมาย: {rec['goal']}")

    lines.append("")
    lines.append("พิมพ์ 'ผลเลือด' เพื่อดูกราฟแนวโน้ม 📈")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("คลินิกระนอง รักษ์ไต 🏥")

    return "\n".join(lines)


def run_monthly_push(conn, push_fn, resolve_line_id_fn,
                     get_setting_fn, log_action_fn=None):
    """Run the monthly summary push for all eligible patients.

    Args:
        conn: database connection
        push_fn: function(line_id, message) to push LINE message
        resolve_line_id_fn: function(hn) → LINE user ID or None
        get_setting_fn: function(key, default) → value
        log_action_fn: optional function to log actions

    Returns dict with stats.
    """
    # Check if feature is enabled
    enabled = (get_setting_fn("monthly_lab_summary", "on") or "on").lower()
    if enabled == "off":
        return {"skipped": "feature disabled via setting monthly_lab_summary=off"}

    # Determine target month (previous month)
    today = date.today()
    if today.month == 1:
        target_year, target_month = today.year - 1, 12
    else:
        target_year, target_month = today.year, today.month - 1

    data = collect_monthly_data(conn, target_year, target_month)

    sent = 0
    skipped_no_line = 0
    skipped_consent = 0
    errors = 0

    for rec in data:
        hn = rec["hn"]
        line_id = resolve_line_id_fn(hn)
        if not line_id:
            skipped_no_line += 1
            continue

        msg = build_message(rec, target_year, target_month)
        try:
            push_fn(line_id, msg)
            sent += 1
            if log_action_fn:
                try:
                    log_action_fn(
                        user_id="system",
                        action="monthly_summary.push",
                        resource_type="patient",
                        resource_id=hn,
                        new_values={"month": f"{target_year}-{target_month:02d}",
                                    "hb": rec["this"].get("hb"),
                                    "ktv": rec["this"].get("ktv")},
                        ip_address="cron",
                    )
                except Exception:
                    pass
        except Exception as e:
            errors += 1
            logger.warning("monthly_summary push %s: %s", hn, e)

    return {
        "month": f"{target_year}-{target_month:02d}",
        "eligible": len(data),
        "sent": sent,
        "skipped_no_line": skipped_no_line,
        "skipped_consent": skipped_consent,
        "errors": errors,
    }
