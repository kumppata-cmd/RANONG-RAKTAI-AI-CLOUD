"""F2: Nurse Shift Handover — สรุปส่งต่อเวรอัตโนมัติ

เมื่อเปลี่ยนเวร → สรุปสถานะทุกเตียง + alerts + vitals ล่าสุด
ส่งผ่าน LINE push ให้พยาบาลเวรถัดไป
"""

import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def generate_handover_summary(db_url: str, shift: str = "morning") -> dict:
    """สร้างสรุปส่งต่อเวร

    Returns: {
        "shift": "morning",
        "generated_at": "...",
        "active_beds": [...],
        "completed_beds": [...],
        "pending_alerts": [...],
        "notes": "...",
        "line_message": "..." (formatted for LINE push)
    }
    """
    if not db_url:
        return {"error": "No database", "active_beds": [], "line_message": ""}

    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        result = {
            "shift": shift,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "active_beds": [],
            "completed_beds": [],
            "pending_alerts": [],
        }

        # Active sessions
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.bed_number, p.patient_name, p.hn, s.status,
                       s.started_at, s.uf_goal,
                       (SELECT sbp||'/'||dbp FROM hd_vital_signs
                        WHERE session_id=s.id AND sbp>0
                        ORDER BY recorded_at DESC LIMIT 1) as last_bp,
                       (SELECT heart_rate FROM hd_vital_signs
                        WHERE session_id=s.id AND heart_rate>0
                        ORDER BY recorded_at DESC LIMIT 1) as last_hr
                FROM hd_sessions s
                JOIN hd_patient_mapping p ON p.id = s.patient_id
                WHERE s.status = 'in_progress'
                ORDER BY s.bed_number
            """)
            for r in cur.fetchall():
                elapsed = ""
                if r[4]:
                    mins = (datetime.now(timezone.utc) - r[4].replace(
                        tzinfo=timezone.utc)).total_seconds() / 60
                    elapsed = f"{int(mins//60)}:{int(mins%60):02d}"
                result["active_beds"].append({
                    "bed": r[0], "name": r[1], "hn": r[2],
                    "elapsed": elapsed, "uf_goal": r[5],
                    "last_bp": r[6] or "-", "last_hr": r[7] or "-",
                })

        # Completed today
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.bed_number, p.patient_name, p.hn,
                       s.actual_uf, s.notes
                FROM hd_sessions s
                JOIN hd_patient_mapping p ON p.id = s.patient_id
                WHERE s.status = 'completed'
                  AND s.ended_at > NOW() - INTERVAL '12 hours'
                ORDER BY s.ended_at DESC
            """)
            for r in cur.fetchall():
                result["completed_beds"].append({
                    "bed": r[0], "name": r[1], "hn": r[2],
                    "actual_uf": r[3], "notes": r[4] or "",
                })

        # Unacknowledged alerts
        with conn.cursor() as cur:
            cur.execute("""
                SELECT a.severity, a.title, p.patient_name, s.bed_number
                FROM hd_clinical_alerts a
                JOIN hd_sessions s ON s.id = a.session_id
                JOIN hd_patient_mapping p ON p.id = s.patient_id
                WHERE a.acknowledged = FALSE
                ORDER BY a.created_at DESC LIMIT 10
            """)
            for r in cur.fetchall():
                result["pending_alerts"].append({
                    "severity": r[0], "title": r[1],
                    "patient": r[2], "bed": r[3],
                })

        conn.close()

        # Build LINE message
        shift_names = {"morning": "เช้า", "afternoon": "บ่าย", "evening": "ค่ำ"}
        msg_lines = [
            f"📋 สรุปส่งต่อเวร{shift_names.get(shift, shift)}",
            f"⏰ {datetime.now(timezone.utc).strftime('%H:%M')} UTC",
            "",
        ]

        if result["active_beds"]:
            msg_lines.append(f"🟢 กำลังฟอก {len(result['active_beds'])} เตียง:")
            for b in result["active_beds"]:
                msg_lines.append(
                    f"  เตียง {b['bed']}: {b['name']} ({b['hn']})"
                    f" BP {b['last_bp']} HR {b['last_hr']}"
                    f" ⏱{b['elapsed']}"
                )
        else:
            msg_lines.append("🟢 ไม่มีเตียงกำลังฟอก")

        if result["completed_beds"]:
            msg_lines.append(f"\n✅ เสร็จแล้ว {len(result['completed_beds'])} เตียง")

        if result["pending_alerts"]:
            msg_lines.append(f"\n🚨 Alert ยังไม่ ack: {len(result['pending_alerts'])} รายการ")
            for a in result["pending_alerts"][:3]:
                emoji = "🔴" if a["severity"] == "critical" else "⚠️"
                msg_lines.append(f"  {emoji} {a['title']} — {a['patient']} เตียง {a['bed']}")

        result["line_message"] = "\n".join(msg_lines)
        return result

    except Exception as e:
        logger.error("handover summary error: %s", e)
        return {"error": str(e), "active_beds": [], "line_message": f"❌ Error: {e}"}
