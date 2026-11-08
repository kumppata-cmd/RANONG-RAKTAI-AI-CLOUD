"""HD Smart Monitoring — LINE Push Alert Integration.

When AlertEngine triggers an alert, push LINE notification to
configured HD staff (physicians + nurses).

Safety: AI ห้ามตัดสินใจรักษา — แจ้งเตือนเท่านั้น
"""

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

from .clinical_alerts import AlertResult, format_alert_for_line

logger = logging.getLogger(__name__)

# Bangkok timezone offset
_BKK = timezone(timedelta(hours=7))


class HdAlertPushService:
    """Push HD clinical alerts to LINE recipients.

    Integrates with server.py's push_message() function.
    Recipients come from ENV or database.
    """

    def __init__(self, push_fn=None, db_url: str = ""):
        """
        Args:
            push_fn: server.py's push_message(user_id, text) function
            db_url: PostgreSQL connection string for logging alerts
        """
        self._push_fn = push_fn
        self._db_url = db_url
        self._recipients: list[dict] = []
        self._load_recipients()

    def _load_recipients(self):
        """Load alert recipients from ENV or database."""
        # ENV-based recipients (quick setup)
        env_ids = os.environ.get("HD_ALERT_RECIPIENTS", "").strip()
        if env_ids:
            for entry in env_ids.split(","):
                entry = entry.strip()
                if not entry:
                    continue
                # Format: line_user_id or line_user_id:role:name
                parts = entry.split(":")
                self._recipients.append({
                    "line_user_id": parts[0],
                    "role": parts[1] if len(parts) > 1 else "nurse",
                    "name": parts[2] if len(parts) > 2 else "",
                })

        # Also load from existing alert_recipients ENV (server.py compatibility)
        for env_key in ["ALERT_RECIPIENT_1", "ALERT_RECIPIENT_2",
                        "ALERT_RECIPIENT_3"]:
            uid = os.environ.get(env_key, "").strip()
            if uid and not any(r["line_user_id"] == uid for r in self._recipients):
                self._recipients.append({
                    "line_user_id": uid,
                    "role": "physician",
                    "name": env_key,
                })

        if self._recipients:
            logger.info("HD Alert Push: %d recipients configured",
                        len(self._recipients))
        else:
            logger.warning("HD Alert Push: no recipients configured — "
                           "set HD_ALERT_RECIPIENTS or ALERT_RECIPIENT_1 ENV")

    def push_alert(
        self,
        alert: AlertResult,
        patient_name: str = "",
        hn: str = "",
        bed: Optional[int] = None,
        session_id: Optional[int] = None,
    ) -> dict:
        """Push a single alert to all configured recipients.

        Args:
            alert: AlertResult from AlertEngine
            patient_name: ชื่อผู้ป่วย
            hn: HN
            bed: เตียง
            session_id: HD session ID

        Returns:
            dict with sent/failed counts
        """
        if not self._push_fn:
            logger.warning("HD Alert Push: no push function — alert not sent")
            return {"sent": 0, "failed": 0, "reason": "no_push_fn"}

        if not self._recipients:
            logger.warning("HD Alert Push: no recipients — alert not sent")
            return {"sent": 0, "failed": 0, "reason": "no_recipients"}

        # Format alert message
        msg = format_alert_for_line(alert, patient_name, hn, bed)

        # Add timestamp in Bangkok time
        now_bkk = datetime.now(_BKK)
        msg += f"\n🕐 {now_bkk.strftime('%d/%m/%Y %H:%M')}"

        # Determine which recipients get this alert
        targets = self._filter_recipients(alert.severity)

        sent = 0
        failed = 0
        for recipient in targets:
            try:
                success = self._push_fn(recipient["line_user_id"], msg)
                if success:
                    sent += 1
                else:
                    failed += 1
            except Exception as e:
                logger.error("HD Alert Push failed for %s: %s",
                             recipient.get("name", "unknown"), e)
                failed += 1

        # Log to database if available
        self._log_alert_to_db(alert, hn, bed, session_id, sent, failed)

        result = {"sent": sent, "failed": failed, "total_recipients": len(targets)}
        if sent > 0:
            logger.info("HD Alert Push: %s → sent to %d/%d recipients",
                        alert.rule_id, sent, len(targets))
        return result

    def push_alerts_batch(
        self,
        alerts: list[AlertResult],
        patient_name: str = "",
        hn: str = "",
        bed: Optional[int] = None,
        session_id: Optional[int] = None,
    ) -> dict:
        """Push multiple alerts (from evaluate_vitals) as one combined message.

        Combines all alerts into a single LINE message to avoid spam.
        """
        if not alerts:
            return {"sent": 0, "failed": 0}

        if not self._push_fn or not self._recipients:
            return {"sent": 0, "failed": 0}

        # Build combined message
        now_bkk = datetime.now(_BKK)
        lines = [
            "🏥 HD Smart Monitoring — แจ้งเตือน",
            "━━━━━━━━━━━━━━",
        ]
        if patient_name or hn:
            lines.append(f"👤 {patient_name} (HN {hn})")
        if bed:
            lines.append(f"🛏️ เตียง {bed}")
        lines.append("")

        has_critical = False
        for alert in alerts:
            if alert.severity == "critical":
                has_critical = True
            lines.append(alert.message_th)

        lines.append(f"\n🕐 {now_bkk.strftime('%d/%m/%Y %H:%M')}")
        if has_critical:
            lines.append("\n⚡ มีค่าวิกฤต — กรุณาตรวจสอบทันที")
        else:
            lines.append("\n⚡ กรุณาตรวจสอบและดำเนินการ")

        msg = "\n".join(lines)

        # Determine highest severity for recipient filtering
        max_severity = "info"
        for a in alerts:
            if a.severity == "critical":
                max_severity = "critical"
                break
            if a.severity == "warning":
                max_severity = "warning"

        targets = self._filter_recipients(max_severity)

        sent = 0
        failed = 0
        for recipient in targets:
            try:
                success = self._push_fn(recipient["line_user_id"], msg)
                if success:
                    sent += 1
                else:
                    failed += 1
            except Exception:
                failed += 1

        return {"sent": sent, "failed": failed, "alerts_count": len(alerts)}

    def _filter_recipients(self, severity: str) -> list[dict]:
        """Filter recipients by alert severity.

        - critical: notify ALL (physicians + nurses + admin)
        - warning: notify physicians + nurses
        - info: notify physicians only
        """
        if severity == "critical":
            return self._recipients  # everyone
        elif severity == "warning":
            return [r for r in self._recipients
                    if r["role"] in ("physician", "nurse", "admin")]
        else:  # info
            return [r for r in self._recipients
                    if r["role"] == "physician"]

    def _log_alert_to_db(
        self,
        alert: AlertResult,
        hn: str,
        bed: Optional[int],
        session_id: Optional[int],
        sent: int,
        failed: int,
    ):
        """Log pushed alert to hd_clinical_alerts table."""
        if not self._db_url:
            return
        try:
            import psycopg2
            conn = psycopg2.connect(self._db_url, connect_timeout=5)
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO hd_clinical_alerts
                        (session_id, rule_id, severity, message, details,
                         triggered_at, is_acknowledged)
                    VALUES (%s, %s, %s::hd_alert_severity, %s, %s, NOW(), FALSE)
                """, (
                    session_id,
                    alert.rule_id,
                    alert.severity,
                    alert.message_th,
                    json.dumps(alert.details, ensure_ascii=False),
                ))
                conn.commit()
            conn.close()
        except Exception as e:
            logger.warning("HD Alert DB log failed: %s", e)

    @property
    def recipient_count(self) -> int:
        return len(self._recipients)

    @property
    def is_configured(self) -> bool:
        return bool(self._push_fn and self._recipients)
