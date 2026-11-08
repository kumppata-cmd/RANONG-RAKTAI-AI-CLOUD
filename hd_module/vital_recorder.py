"""HD Smart Monitoring — Real-time Vital Signs Recorder.

Records vital signs to PostgreSQL, evaluates clinical alerts,
and triggers LINE push notifications when thresholds are exceeded.

Safety: AI ห้ามตัดสินใจรักษา — บันทึกและแจ้งเตือนเท่านั้น
"""

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

from .clinical_alerts import AlertEngine, AlertResult
from .config import HdConfig

logger = logging.getLogger(__name__)


class VitalRecorder:
    """Record vital signs → evaluate alerts → push notifications."""

    def __init__(self, db_url: str = "", alert_push_service=None):
        self._db_url = db_url or os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
        self._alert_push = alert_push_service
        self._alert_engine = AlertEngine()

    def record_vitals(
        self,
        session_id: int,
        patient_id: int,
        phase: str,  # 'pre', 'intra', 'post'
        sbp: Optional[int] = None,
        dbp: Optional[int] = None,
        hr: Optional[int] = None,
        temp: Optional[float] = None,
        spo2: Optional[int] = None,
        uf_rate: Optional[float] = None,
        weight: Optional[float] = None,
        notes: str = "",
        recorded_by: str = "",
    ) -> dict:
        """Record vital signs and evaluate alerts.

        Returns: {
            "vital_id": int,
            "alerts": [...],
            "push_result": {...},
        }
        """
        if not self._db_url:
            return {"error": "database not configured"}

        try:
            import psycopg2
            conn = psycopg2.connect(self._db_url, connect_timeout=5)

            # 1. Insert vital signs
            vital_id = None
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO hd_vital_signs
                        (session_id, phase, systolic_bp, diastolic_bp,
                         heart_rate, temperature, spo2, notes, recorded_by,
                         recorded_at)
                    VALUES (%s, %s::hd_vital_phase, %s, %s, %s, %s, %s, %s, %s, NOW())
                    RETURNING id
                """, (session_id, phase, sbp, dbp, hr, temp, spo2, notes, recorded_by))
                vital_id = cur.fetchone()[0]
                conn.commit()

            # 2. Get previous SBPs for IDH check (last 30 min)
            previous_sbps = []
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT recorded_at, systolic_bp FROM hd_vital_signs
                    WHERE session_id = %s AND systolic_bp IS NOT NULL
                      AND recorded_at > NOW() - INTERVAL '%s minutes'
                      AND id != %s
                    ORDER BY recorded_at DESC
                    LIMIT 10
                """, (session_id, HdConfig.ALERT_IDH_WINDOW_MINUTES, vital_id))
                for row in cur.fetchall():
                    previous_sbps.append((row[0], row[1]))

            # 3. Get patient info for alerts
            patient_info = self._get_patient_info(conn, patient_id)
            dry_weight = patient_info.get("dry_weight")
            patient_name = patient_info.get("patient_name", "")
            hn = patient_info.get("hn", "")
            bed = self._get_session_bed(conn, session_id)

            # 4. Evaluate alerts
            alerts = self._alert_engine.evaluate_vitals(
                current_sbp=sbp,
                current_dbp=dbp,
                current_hr=hr,
                previous_sbps=previous_sbps if previous_sbps else None,
                uf_rate_ml_hr=uf_rate,
                patient_weight_kg=dry_weight,
                pre_weight=weight,
                dry_weight=dry_weight,
                last_vital_time=datetime.now(timezone.utc),  # just recorded
            )

            conn.close()

            # 5. Push LINE alerts if any triggered
            push_result = {"sent": 0, "failed": 0}
            triggered_alerts = [a for a in alerts if a.triggered]
            if triggered_alerts and self._alert_push:
                push_result = self._alert_push.push_alerts_batch(
                    triggered_alerts,
                    patient_name=patient_name,
                    hn=hn,
                    bed=bed,
                    session_id=session_id,
                )

            return {
                "vital_id": vital_id,
                "alerts": [
                    {
                        "rule_id": a.rule_id,
                        "severity": a.severity,
                        "message": a.message_th,
                        "triggered": a.triggered,
                    }
                    for a in triggered_alerts
                ],
                "alert_count": len(triggered_alerts),
                "push_result": push_result,
            }

        except Exception as e:
            logger.error("VitalRecorder error: %s", e)
            return {"error": str(e)}

    def record_uf(
        self,
        session_id: int,
        uf_volume_ml: float,
        uf_rate_ml_hr: float,
        total_uf_ml: float = 0,
        notes: str = "",
    ) -> dict:
        """Record UF (ultrafiltration) data."""
        if not self._db_url:
            return {"error": "database not configured"}

        try:
            import psycopg2
            conn = psycopg2.connect(self._db_url, connect_timeout=5)
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO hd_uf_records
                        (session_id, uf_volume_ml, uf_rate_ml_hr,
                         total_uf_ml, recorded_at)
                    VALUES (%s, %s, %s, %s, NOW())
                    RETURNING id
                """, (session_id, uf_volume_ml, uf_rate_ml_hr, total_uf_ml))
                uf_id = cur.fetchone()[0]
                conn.commit()
            conn.close()
            return {"uf_id": uf_id}
        except Exception as e:
            logger.error("UF record error: %s", e)
            return {"error": str(e)}

    def _get_patient_info(self, conn, patient_id: int) -> dict:
        """Get patient info from hd_patient_mapping."""
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT hn, patient_name, dry_weight, vascular_access
                    FROM hd_patient_mapping WHERE id = %s
                """, (patient_id,))
                row = cur.fetchone()
                if row:
                    return {
                        "hn": row[0],
                        "patient_name": row[1],
                        "dry_weight": float(row[2]) if row[2] else None,
                        "vascular_access": row[3],
                    }
        except Exception:
            pass
        return {}

    def _get_session_bed(self, conn, session_id: int) -> Optional[int]:
        """Get bed number from session."""
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT bed_number FROM hd_sessions WHERE id = %s",
                    (session_id,),
                )
                row = cur.fetchone()
                return row[0] if row else None
        except Exception:
            return None
