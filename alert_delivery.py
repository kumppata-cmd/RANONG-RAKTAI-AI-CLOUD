#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Critical Lab Alert Delivery — RANONG-RAKTAI-AI 2.0

ระบบส่ง alert วิกฤตให้แพทย์ผ่าน LINE Push Message

กฎความปลอดภัย:
  - ไม่มี PHI ใน notification preview
  - API acceptance ≠ clinician acknowledgement
  - Low-confidence OCR → urgent source verification, NOT auto-accept
  - eGFR<15 ในผู้ป่วย maintenance dialysis ไม่ใช่ acute emergency อัตโนมัติ
  - Retry with exponential backoff
  - Failed delivery escalation
  - Persistent alert state
  - Audit logging ทุก event
"""

from __future__ import annotations

import json
import os
import uuid
import time
import logging
import hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Optional, Any

logger = logging.getLogger(__name__)

# LINE Push Messaging API endpoint (ไม่ใช่ Secrets)
LINE_PUSH_API_URL = "https://api.line.me/v2/bot/message/push"


# ═══════════════════════════════════════════════════════════
# Alert State Machine
# ═══════════════════════════════════════════════════════════

class AlertState(str, Enum):
    """สถานะของ critical alert."""
    DETECTED = "detected"
    QUEUED = "queued"
    SENT = "sent"
    DELIVERY_FAILED = "delivery_failed"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    ESCALATED = "escalated"


class AlertSeverity(str, Enum):
    """ระดับความรุนแรง."""
    CRITICAL = "critical"       # ต้องแจ้งทันที
    URGENT = "urgent"           # ต้องแจ้งเร็ว
    SOURCE_VERIFY = "source_verify"  # OCR confidence ต่ำ ต้องตรวจสอบต้นฉบับ


# Valid state transitions
ALERT_TRANSITIONS = {
    AlertState.DETECTED.value: {AlertState.QUEUED.value},
    AlertState.QUEUED.value: {AlertState.SENT.value, AlertState.DELIVERY_FAILED.value},
    AlertState.SENT.value: {AlertState.ACKNOWLEDGED.value, AlertState.ESCALATED.value},
    AlertState.DELIVERY_FAILED.value: {AlertState.QUEUED.value, AlertState.ESCALATED.value},
    AlertState.ACKNOWLEDGED.value: {AlertState.RESOLVED.value},
    AlertState.ESCALATED.value: {AlertState.QUEUED.value, AlertState.RESOLVED.value},
}


@dataclass
class AlertEvent:
    """Audit event สำหรับ alert lifecycle."""
    event_id: str
    action: str
    timestamp: str
    actor: str = ""
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CriticalAlert:
    """Critical lab alert record.

    ไม่เก็บข้อมูลผู้ป่วยโดยตรง — ใช้ doc_id อ้างอิง
    """
    alert_id: str                    # UUID
    doc_id: str                      # document ID ที่พบค่าผิดปกติ
    owner_hash: str                  # owner ของเอกสาร (hashed)
    analyte: str                     # ชื่อ analyte ที่ผิดปกติ
    value: float                     # ค่าที่ตรวจพบ
    unit: str                        # หน่วย
    alert_level: str                 # critical_high/critical_low/etc
    severity: str = AlertSeverity.CRITICAL.value
    state: str = AlertState.DETECTED.value
    ocr_confidence: float = 1.0      # confidence ของ OCR
    created_at: str = ""
    sent_at: str = ""
    acknowledged_at: str = ""
    acknowledged_by: str = ""
    resolved_at: str = ""
    delivery_attempts: int = 0
    max_delivery_attempts: int = 5
    last_delivery_error: str = ""
    escalation_target: str = ""
    notification_text: str = ""      # ข้อความที่ส่ง (ไม่มี PHI)
    events: list[dict] = field(default_factory=list)
    dedup_key: str = ""              # สำหรับ deduplication
    # [CS-3] เวลารอการรับทราบจากแพทย์ (วินาที) — ส่งถึงแล้วแต่ไม่มีใคร
    # ตอบรับภายในเวลานี้ ต้อง escalate ไม่ใช่ปล่อยเงียบ
    ack_timeout_sec: int = 900       # 15 นาที
    escalation_reason: str = ""
    # จำนวนผู้รับที่ส่งถึงจริง / ผู้รับที่ตั้งไว้ทั้งหมด
    # ใช้ตรวจว่าการแจ้งเตือนมีสำรองจริงหรือถึงแค่คนเดียว
    delivered_to_count: int = 0
    recipient_count: int = 0

    def __post_init__(self):
        if not self.alert_id:
            self.alert_id = uuid.uuid4().hex[:16]
        if not self.created_at:
            self.created_at = datetime.now().isoformat(timespec="seconds")
        if not self.dedup_key:
            self.dedup_key = f"{self.doc_id}:{self.analyte}:{self.alert_level}"
        # Low OCR confidence → source verification severity
        if self.ocr_confidence < 0.7:
            self.severity = AlertSeverity.SOURCE_VERIFY.value

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: dict) -> CriticalAlert:
        events = data.pop("events", [])
        alert = cls(**{k: v for k, v in data.items()
                       if k in cls.__dataclass_fields__})
        alert.events = events
        return alert

    def add_event(self, action: str, actor: str = "",
                  details: dict | None = None):
        """เพิ่ม audit event."""
        event = AlertEvent(
            event_id=uuid.uuid4().hex[:8],
            action=action,
            timestamp=datetime.now().isoformat(timespec="seconds"),
            actor=actor,
            details=details or {},
        )
        self.events.append(event.to_dict())

    def can_transition_to(self, new_state: str) -> bool:
        valid = ALERT_TRANSITIONS.get(self.state, set())
        return new_state in valid

    def should_retry(self) -> bool:
        return (
            self.state == AlertState.DELIVERY_FAILED.value
            and self.delivery_attempts < self.max_delivery_attempts
        )

    def should_escalate(self) -> bool:
        return (
            self.state == AlertState.DELIVERY_FAILED.value
            and self.delivery_attempts >= self.max_delivery_attempts
        )

    def seconds_awaiting_ack(self, now: float | None = None) -> float:
        """เวลาที่ผ่านไปตั้งแต่ส่งถึง โดยยังไม่มีใครรับทราบ (วินาที)

        คืน 0.0 ถ้าไม่ได้อยู่ในสถานะรอการรับทราบ หรืออ่านเวลาไม่ได้
        """
        if self.state != AlertState.SENT.value or not self.sent_at:
            return 0.0
        try:
            sent_ts = datetime.fromisoformat(self.sent_at).timestamp()
        except (ValueError, TypeError):
            return 0.0
        return max(0.0, (now if now is not None else time.time()) - sent_ts)

    def should_escalate_unacknowledged(self, now: float | None = None) -> bool:
        """[CS-3] ส่งถึงแล้วแต่ไม่มีแพทย์รับทราบภายในเวลาที่กำหนด

        ก่อนหน้านี้ should_escalate() ผูกกับ DELIVERY_FAILED เท่านั้น
        ทำให้ alert ที่ "ส่งถึงแต่ไม่มีใครดู" แยกไม่ออกจาก alert ที่
        จัดการแล้ว — ซึ่งเป็นสถานการณ์ที่อันตรายที่สุดทางคลินิก
        """
        if self.ack_timeout_sec <= 0:
            return False
        return self.seconds_awaiting_ack(now) >= self.ack_timeout_sec

    def get_retry_delay(self) -> int:
        """Exponential backoff delay in seconds."""
        base = 30  # 30 seconds base
        return min(base * (2 ** (self.delivery_attempts - 1)), 3600)


# ═══════════════════════════════════════════════════════════
# Alert Delivery Service
# ═══════════════════════════════════════════════════════════

ALERTS_PREFIX = "_alerts/"  # GCS path


class AlertDeliveryService:
    """จัดการ critical alert delivery.

    Dependencies:
      - storage: StorageLocal/StorageGCS
      - audit_logger: AuditLogger
      - clinician_recipients: dict mapping role → LINE user_id list
    """

    def __init__(self, storage, audit_logger,
                 clinician_recipients: dict[str, list[str]] | None = None,
                 allow_simulated_delivery: bool = False):
        """
        allow_simulated_delivery: อนุญาตให้ "ถือว่าส่งสำเร็จ" เมื่อไม่มี
            send_push_fn — ใช้ได้เฉพาะในการทดสอบเท่านั้น

            ค่าเริ่มต้นเป็น False (fail-closed) โดยเจตนา: ถ้า production
            ไม่มีช่องทางส่ง (เช่น LINE_CHANNEL_ACCESS_TOKEN ไม่ได้ตั้ง)
            alert ต้องถูกบันทึกเป็น delivery_failed เพื่อให้เข้า retry
            และ escalation ไม่ใช่บันทึกว่า SENT ทั้งที่ไม่ได้ส่งอะไรเลย
            (ดู CLINICAL_SAFETY_GAP_ANALYSIS CS-4)
        """
        self._storage = storage
        self._audit = audit_logger
        self._recipients = clinician_recipients or {}
        self._allow_simulated_delivery = allow_simulated_delivery
        self._dedup_cache: dict[str, str] = {}  # dedup_key → alert_id

    def _alert_path(self, alert_id: str) -> str:
        return f"{ALERTS_PREFIX}{alert_id}.json"

    def _save_alert(self, alert: CriticalAlert) -> bool:
        try:
            self._storage.put(
                self._alert_path(alert.alert_id),
                alert.to_json().encode("utf-8"),
                content_type="application/json",
            )
            return True
        except Exception as e:
            logger.error(f"Alert save failed: {type(e).__name__}")
            return False

    def _load_alert(self, alert_id: str) -> CriticalAlert | None:
        data = self._storage.get(self._alert_path(alert_id))
        if data is None:
            return None
        return CriticalAlert.from_dict(json.loads(data))

    def create_alert(self, doc_id: str, owner_hash: str,
                      analyte: str, value: float, unit: str,
                      alert_level: str,
                      ocr_confidence: float = 1.0,
                      patient_context: dict | None = None) -> CriticalAlert | None:
        """สร้าง alert ใหม่จากผลตรวจที่ผิดปกติ.

        patient_context: ข้อมูลบริบทผู้ป่วย เช่น
            {"is_dialysis_patient": True} — ไม่ auto-classify eGFR<15

        Returns None ถ้า deduplicated.
        """
        # สร้าง dedup key
        dedup_key = f"{doc_id}:{analyte}:{alert_level}"

        # Check dedup
        if dedup_key in self._dedup_cache:
            existing_id = self._dedup_cache[dedup_key]
            existing = self._load_alert(existing_id)
            if existing and existing.state not in (
                AlertState.RESOLVED.value,
            ):
                logger.info(f"Alert deduplicated: {dedup_key}")
                return None

        # Patient context interpretation
        severity = AlertSeverity.CRITICAL.value
        if ocr_confidence < 0.7:
            severity = AlertSeverity.SOURCE_VERIFY.value
        elif patient_context:
            # eGFR<15 ในผู้ป่วย maintenance dialysis ไม่ใช่ acute emergency
            if (analyte.lower() in ("egfr", "gfr")
                    and value < 15
                    and patient_context.get("is_dialysis_patient")):
                severity = AlertSeverity.URGENT.value

        alert = CriticalAlert(
            alert_id="",  # auto-generated
            doc_id=doc_id,
            owner_hash=owner_hash[:8],
            analyte=analyte,
            value=value,
            unit=unit,
            alert_level=alert_level,
            severity=severity,
            ocr_confidence=ocr_confidence,
        )

        # Build notification text (NO PHI)
        alert.notification_text = self._build_notification(alert)

        alert.add_event("detected", details={
            "severity": severity,
            "ocr_confidence": ocr_confidence,
        })

        # Transition to queued
        alert.state = AlertState.QUEUED.value
        alert.add_event("queued")

        if self._save_alert(alert):
            self._dedup_cache[dedup_key] = alert.alert_id
            self._audit.log("alert_created", owner_hash, {
                "alert_id": alert.alert_id[:8],
                "analyte": analyte,
                "alert_level": alert_level,
                "severity": severity,
            })
            return alert

        return None

    def _build_notification(self, alert: CriticalAlert) -> str:
        """สร้าง notification text — ไม่มี PHI.

        ข้อความจะไม่มี: ชื่อผู้ป่วย, วันเกิด, หมายเลขบัตร,
        ที่อยู่ หรือข้อมูลที่ระบุตัวบุคคลได้
        """
        severity_icon = {
            AlertSeverity.CRITICAL.value: "🔴",
            AlertSeverity.URGENT.value: "🟠",
            AlertSeverity.SOURCE_VERIFY.value: "⚠️",
        }.get(alert.severity, "🔴")

        text = (
            f"{severity_icon} แจ้งเตือนผลตรวจ\n"
            f"รายการ: {alert.analyte}\n"
            f"ระดับ: {alert.alert_level}\n"
            f"เอกสาร: {alert.doc_id[:8]}\n"
        )

        if alert.severity == AlertSeverity.SOURCE_VERIFY.value:
            text += (
                f"\n⚠️ OCR confidence ต่ำ ({alert.ocr_confidence:.0%})\n"
                f"กรุณาตรวจสอบเอกสารต้นฉบับ"
            )

        text += f"\n\nตอบ 'รับทราบ {alert.alert_id[:8]}' เพื่อยืนยัน"
        return text

    def attempt_delivery(self, alert_id: str,
                          send_push_fn=None) -> dict:
        """พยายามส่ง alert ผ่าน LINE push.

        send_push_fn: function(user_id: str, message: str) -> bool
            ถ้าไม่มี — จำลอง delivery (testing mode)
        """
        alert = self._load_alert(alert_id)
        if alert is None:
            return {"error": "ไม่พบ alert"}

        if alert.state not in (AlertState.QUEUED.value,
                                AlertState.DELIVERY_FAILED.value):
            return {"error": f"สถานะ {alert.state} ไม่สามารถส่งได้"}

        alert.delivery_attempts += 1
        # ล้างเหตุผลของครั้งก่อน เพื่อให้ค่าที่บันทึกตรงกับความพยายามครั้งนี้
        alert.last_delivery_error = ""

        # ส่ง LINE push
        recipients = self._recipients.get("physician", [])
        if not recipients:
            logger.warning("No physician recipients configured for alerts")
            alert.state = AlertState.DELIVERY_FAILED.value
            alert.last_delivery_error = "ไม่มี physician recipient"
            alert.add_event("delivery_failed", details={
                "reason": "no_recipients",
                "attempt": alert.delivery_attempts,
            })
            self._save_alert(alert)
            return {"error": "ไม่มี physician recipient"}

        delivery_success = False
        delivered_count = 0
        failed_count = 0
        if send_push_fn:
            # ส่งให้ผู้รับ "ทุกคน" ที่ตั้งไว้ ไม่หยุดที่คนแรกที่สำเร็จ
            #
            # เดิม break ทันทีเมื่อส่งสำเร็จคนแรก → ผลวิกฤตถึงแพทย์
            # คนเดียวโดยไม่มีสำรอง ถ้าแพทย์คนนั้นไม่ว่าง ปิดเสียง หรือ
            # ไม่ได้เปิด LINE ก็ไม่มีใครรู้เลย ทั้งที่ตั้งผู้รับไว้หลายคน
            # เพื่อการนี้โดยเฉพาะ
            for recipient_id in recipients:
                try:
                    if send_push_fn(recipient_id, alert.notification_text):
                        delivered_count += 1
                    else:
                        failed_count += 1
                except Exception as e:
                    failed_count += 1
                    logger.warning(
                        f"Alert push failed: {type(e).__name__}"
                    )
            # ถึงอย่างน้อยหนึ่งคน = ส่งสำเร็จ (ไม่ให้ retry ทั้งชุดซ้ำ
            # ซึ่งจะทำให้คนที่ได้รับแล้วโดนแจ้งซ้ำ และอาจเข้าใจผิดว่า
            # เป็นเหตุการณ์ใหม่)
            delivery_success = delivered_count > 0
            if delivered_count and failed_count:
                logger.warning(
                    "Alert partial delivery: %d สำเร็จ %d ล้มเหลว",
                    delivered_count, failed_count,
                )
        elif self._allow_simulated_delivery:
            # โหมดทดสอบเท่านั้น — ต้อง opt-in ที่ constructor
            delivery_success = True
        else:
            # [CS-4] ไม่มีช่องทางส่งจริง → ต้องถือว่าส่งไม่สำเร็จ
            # ห้ามบันทึกว่า SENT เพราะไม่มีใครได้รับอะไรเลย
            logger.error(
                "Alert delivery: ไม่มี send_push_fn และไม่ได้อนุญาต "
                "simulated delivery — ถือว่าส่งไม่สำเร็จ (fail-closed)"
            )
            delivery_success = False
            alert.last_delivery_error = "no_sender_configured"

        if delivery_success:
            alert.state = AlertState.SENT.value
            alert.sent_at = datetime.now().isoformat(timespec="seconds")
            alert.delivered_to_count = delivered_count
            alert.recipient_count = len(recipients)
            alert.add_event("sent", details={
                "attempt": alert.delivery_attempts,
                # บันทึกว่าถึงกี่คนจากกี่คน เพื่อให้เห็นการส่งถึงบางส่วน
                "delivered": delivered_count,
                "failed": failed_count,
                "recipients": len(recipients),
            })
            self._audit.log("alert_sent", alert.owner_hash, {
                "alert_id": alert.alert_id[:8],
                "analyte": alert.analyte,
                "attempt": alert.delivery_attempts,
                "delivered": delivered_count,
                "recipients": len(recipients),
            })
        else:
            alert.state = AlertState.DELIVERY_FAILED.value
            # ไม่เขียนทับเหตุผลที่เจาะจงกว่า (เช่น no_sender_configured)
            if not alert.last_delivery_error:
                alert.last_delivery_error = "LINE push failed"
            alert.add_event("delivery_failed", details={
                "attempt": alert.delivery_attempts,
                "reason": alert.last_delivery_error,
            })

            # Check escalation
            if alert.should_escalate():
                alert.state = AlertState.ESCALATED.value
                alert.add_event("escalated", details={
                    "reason": "max_delivery_attempts_reached",
                })
                self._audit.log("alert_escalated", alert.owner_hash, {
                    "alert_id": alert.alert_id[:8],
                    "analyte": alert.analyte,
                    "attempts": alert.delivery_attempts,
                })

        self._save_alert(alert)
        return {
            "alert_id": alert.alert_id,
            "state": alert.state,
            "delivery_attempts": alert.delivery_attempts,
            "success": delivery_success,
        }

    def find_alert_id_by_prefix(self, prefix: str) -> str | None:
        """หา alert_id เต็มจากคำนำหน้า 8 ตัวที่แสดงในข้อความแจ้งเตือน

        [CS-2] ข้อความแจ้งเตือนบอกให้แพทย์ตอบ 'รับทราบ <8 ตัวแรก>'
        จึงต้องแปลงกลับเป็น alert_id เต็มเพื่อเรียก acknowledge_alert

        คืน None ถ้าไม่พบ หรือถ้าคำนำหน้าตรงกับหลายรายการ (กำกวม —
        ปลอดภัยกว่าที่จะไม่เดา เพราะนี่คือการรับทราบผลวิกฤต)
        """
        prefix = (prefix or "").strip().lower()
        if len(prefix) < 6:
            return None
        try:
            files = self._storage.list_files(ALERTS_PREFIX)
        except Exception as e:
            logger.warning(f"find_alert_id_by_prefix failed: {type(e).__name__}")
            return None

        matches = []
        for f in files:
            name = f.rsplit("/", 1)[-1]
            if name.endswith(".json"):
                aid = name[:-5]
                if aid.lower().startswith(prefix):
                    matches.append(aid)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            logger.warning(
                "find_alert_id_by_prefix: คำนำหน้ากำกวม พบ %d รายการ",
                len(matches),
            )
        return None

    def acknowledge_alert(self, alert_id: str,
                           physician_hash: str) -> dict:
        """แพทย์รับทราบ alert.

        API acceptance ≠ clinician acknowledgement
        ต้องมี physician ตอบกลับจริง
        """
        alert = self._load_alert(alert_id)
        if alert is None:
            return {"error": "ไม่พบ alert"}

        if alert.state not in (AlertState.SENT.value,
                                AlertState.ESCALATED.value):
            return {"error": f"ไม่สามารถรับทราบ alert สถานะ {alert.state}"}

        alert.state = AlertState.ACKNOWLEDGED.value
        alert.acknowledged_at = datetime.now().isoformat(timespec="seconds")
        alert.acknowledged_by = physician_hash[:8]
        alert.add_event("acknowledged", actor=physician_hash[:8])

        self._save_alert(alert)
        self._audit.log("alert_acknowledged", physician_hash, {
            "alert_id": alert.alert_id[:8],
            "analyte": alert.analyte,
            "response_time_sec": self._calc_response_time(alert),
        })

        return {
            "status": "acknowledged",
            "alert_id": alert.alert_id,
            "message": f"✅ รับทราบการแจ้งเตือน {alert.analyte} แล้ว",
        }

    def resolve_alert(self, alert_id: str,
                       physician_hash: str,
                       resolution_notes: str = "") -> dict:
        """แพทย์ resolve alert."""
        alert = self._load_alert(alert_id)
        if alert is None:
            return {"error": "ไม่พบ alert"}

        if alert.state != AlertState.ACKNOWLEDGED.value:
            return {"error": "ต้อง acknowledge ก่อน resolve"}

        alert.state = AlertState.RESOLVED.value
        alert.resolved_at = datetime.now().isoformat(timespec="seconds")
        alert.add_event("resolved", actor=physician_hash[:8],
                        details={"notes": resolution_notes})

        self._save_alert(alert)
        self._audit.log("alert_resolved", physician_hash, {
            "alert_id": alert.alert_id[:8],
            "analyte": alert.analyte,
        })

        return {
            "status": "resolved",
            "alert_id": alert.alert_id,
            "message": f"✅ แจ้งเตือน {alert.analyte} ได้รับการแก้ไขแล้ว",
        }

    def get_active_alerts(self) -> list[dict]:
        """แสดง alerts ที่ยังไม่ resolved — ดึงจาก GCS + dedup cache."""
        active = []
        seen_ids = set()

        # 1. ดึงจาก GCS storage (persistent)
        try:
            alert_files = self._storage.list_files(ALERTS_PREFIX)
            for f in alert_files:
                try:
                    data = self._storage.get(f)
                    if data is None:
                        continue
                    alert = CriticalAlert.from_dict(json.loads(data))
                    if alert.state != AlertState.RESOLVED.value:
                        seen_ids.add(alert.alert_id)
                        active.append({
                            "alert_id": alert.alert_id,
                            "analyte": alert.analyte,
                            "severity": alert.severity,
                            "state": alert.state,
                            "created_at": alert.created_at,
                            "doc_id": alert.doc_id[:8],
                        })
                except (json.JSONDecodeError, KeyError):
                    continue
        except Exception as e:
            logger.warning(f"GCS alert listing failed: {type(e).__name__}")

        # 2. Fallback: dedup cache (in case GCS listing missed some)
        for dedup_key, alert_id in self._dedup_cache.items():
            if alert_id in seen_ids:
                continue
            alert = self._load_alert(alert_id)
            if alert and alert.state != AlertState.RESOLVED.value:
                active.append({
                    "alert_id": alert.alert_id,
                    "analyte": alert.analyte,
                    "severity": alert.severity,
                    "state": alert.state,
                    "created_at": alert.created_at,
                    "doc_id": alert.doc_id[:8],
                })
        return active

    def _calc_response_time(self, alert: CriticalAlert) -> int:
        """คำนวณ response time เป็นวินาที."""
        try:
            if alert.sent_at and alert.acknowledged_at:
                sent = datetime.fromisoformat(alert.sent_at)
                acked = datetime.fromisoformat(alert.acknowledged_at)
                return int((acked - sent).total_seconds())
        except (ValueError, TypeError):
            pass
        return -1


# ═══════════════════════════════════════════════════════════
# LINE Push Delivery — Real Integration
# ═══════════════════════════════════════════════════════════

def create_line_push_fn(channel_access_token: str):
    """สร้าง push function สำหรับ LINE Messaging API.

    ใช้ requests.post โดยตรง (ไม่ต้อง linebot SDK สำหรับ push)
    ห้ามส่ง PHI — notification_text ผ่าน _build_notification ที่กรอง PHI แล้ว
    ห้ามบันทึก token, user_id หรือเนื้อหาข้อความลง log

    Args:
        channel_access_token: LINE Channel Access Token จาก env var

    Returns:
        function(user_id, message) -> bool
    """
    import urllib.request
    import urllib.error

    def _push(user_id: str, message: str) -> bool:
        """ส่ง push message ไปยัง LINE user.

        ไม่ log user_id, token หรือเนื้อหาข้อความ
        """
        if not user_id or not message:
            return False

        payload = json.dumps({
            "to": user_id,
            "messages": [{
                "type": "text",
                "text": message,
            }],
        }).encode("utf-8")

        req = urllib.request.Request(
            LINE_PUSH_API_URL,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {channel_access_token}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = resp.getcode()
                if status == 200:
                    logger.info("LINE push delivered successfully")
                    return True
                else:
                    logger.warning(f"LINE push returned status {status}")
                    return False
        except urllib.error.HTTPError as e:
            logger.warning(f"LINE push HTTP error: {e.code}")
            return False
        except urllib.error.URLError as e:
            logger.warning(f"LINE push URL error: {type(e).__name__}")
            return False
        except Exception as e:
            logger.warning(f"LINE push failed: {type(e).__name__}")
            return False

    return _push


def build_alert_recipients_from_env() -> dict[str, list[str]]:
    """สร้าง recipient mapping จาก environment variables.

    PHYSICIAN_USERS: comma-separated hashed LINE user IDs ที่มีสิทธิ์แพทย์
    ALERT_RECIPIENTS: comma-separated LINE user IDs ที่จะได้รับแจ้งเตือน
    (ถ้าไม่ตั้ง ALERT_RECIPIENTS ใช้ PHYSICIAN_USERS แทน)

    ไม่ log user IDs
    """
    recipients = {}

    # ลำดับ: ALERT_RECIPIENTS → PHYSICIAN_USERS
    alert_ids = os.getenv("ALERT_RECIPIENTS", "").strip()
    if not alert_ids:
        alert_ids = os.getenv("PHYSICIAN_USERS", "").strip()

    if alert_ids:
        ids = [uid.strip() for uid in alert_ids.split(",") if uid.strip()]
        if ids:
            recipients["physician"] = ids
            logger.info(f"Alert recipients configured: {len(ids)} physician(s)")
        else:
            logger.warning("No valid alert recipients found in env")
    else:
        logger.warning("ALERT_RECIPIENTS and PHYSICIAN_USERS not configured")

    return recipients


class AlertRetryManager:
    """จัดการ retry สำหรับ failed deliveries.

    เรียกจาก periodic check (ไม่ใช่ background thread)
    เพื่อความ simple บน single-instance Render
    """

    # สถานะที่ driver ไม่ต้องแตะอีกแล้ว — อ้างอิงจาก ALERT_TRANSITIONS
    #   ACKNOWLEDGED -> {RESOLVED}   เปลี่ยนได้ด้วยคนเท่านั้น
    #   RESOLVED     -> {}           สิ้นสุด ไม่มีทางออก
    # ไม่รวม ESCALATED เพราะยังกลับไป QUEUED ได้ จึงต้องสแกนต่อ
    _DRIVER_INERT_STATES = frozenset({
        AlertState.ACKNOWLEDGED.value,
        AlertState.RESOLVED.value,
    })

    def __init__(self, alert_service: AlertDeliveryService):
        self._service = alert_service
        self._last_retry_check = 0.0
        self._retry_interval = 60  # ตรวจทุก 60 วินาที
        # alert ที่จบแล้ว — ข้ามโดยไม่ต้องดาวน์โหลดซ้ำทุกรอบ
        self._inert_alert_ids: set[str] = set()

    def check_and_retry(self, send_push_fn=None) -> list[dict]:
        """ตรวจสอบ alerts ที่ต้อง retry.

        เรียกจาก health check หรือ periodic task
        Returns list of retry results
        """
        now = time.time()
        if now - self._last_retry_check < self._retry_interval:
            return []

        self._last_retry_check = now
        results = []

        try:
            # List alerts from storage
            alert_files = self._service._storage.list_files(ALERTS_PREFIX)
            for f in alert_files:
                try:
                    # [perf] ข้าม alert ที่จบแล้วโดยไม่ดาวน์โหลดซ้ำ
                    #
                    # driver รันทุก 2 นาทีตลอดเวลา เดิมดาวน์โหลด alert
                    # ทุกไฟล์ที่เคยสร้างมาทุกรอบ แม้รายการที่แพทย์รับทราบ
                    # หรือปิดเคสไปแล้วซึ่งไม่ต้องทำอะไรอีก ภาระจึงโตขึ้น
                    # เรื่อย ๆ ตามจำนวน alert สะสม ไม่ใช่ตามงานที่ค้างจริง
                    _name = f.rsplit("/", 1)[-1]
                    _aid = _name[:-5] if _name.endswith(".json") else _name
                    if _aid in self._inert_alert_ids:
                        continue

                    data = self._service._storage.get(f)
                    if data is None:
                        continue
                    alert = CriticalAlert.from_dict(json.loads(data))

                    if alert.state in self._DRIVER_INERT_STATES:
                        self._inert_alert_ids.add(alert.alert_id)
                        continue

                    # Check if needs retry
                    if alert.should_retry():
                        delay = alert.get_retry_delay()
                        # Check if enough time passed since last attempt
                        if alert.sent_at:
                            last_attempt_time = datetime.fromisoformat(
                                alert.sent_at
                            ).timestamp()
                        else:
                            last_attempt_time = datetime.fromisoformat(
                                alert.created_at
                            ).timestamp()

                        if now - last_attempt_time >= delay:
                            result = self._service.attempt_delivery(
                                alert.alert_id, send_push_fn
                            )
                            results.append(result)

                    # Check if needs escalation (ส่งไม่สำเร็จจนครบจำนวนครั้ง)
                    elif alert.should_escalate():
                        self._escalate(
                            alert, send_push_fn,
                            reason="max_delivery_attempts_reached",
                        )
                        results.append({
                            "alert_id": alert.alert_id,
                            "state": "escalated",
                            "reason": "max_delivery_attempts_reached",
                        })

                    # [CS-3] ส่งถึงแล้วแต่ไม่มีแพทย์รับทราบในเวลาที่กำหนด
                    elif alert.should_escalate_unacknowledged(now):
                        waited = int(alert.seconds_awaiting_ack(now))
                        self._escalate(
                            alert, send_push_fn,
                            reason="acknowledgement_overdue",
                            waited_sec=waited,
                        )
                        results.append({
                            "alert_id": alert.alert_id,
                            "state": "escalated",
                            "reason": "acknowledgement_overdue",
                            "waited_sec": waited,
                        })
                except (json.JSONDecodeError, KeyError):
                    continue
        except Exception as e:
            logger.warning(f"Retry check failed: {type(e).__name__}")

        return results

    def _escalate(self, alert: CriticalAlert,
                  send_push_fn=None,
                  reason: str = "max_delivery_attempts_reached",
                  waited_sec: int = 0):
        """Escalation — ส่ง admin notification.

        ไม่มี PHI ใน escalation message

        reason:
          max_delivery_attempts_reached — ส่งไม่สำเร็จจนครบจำนวนครั้ง
          acknowledgement_overdue       — ส่งถึงแล้วแต่ไม่มีใครรับทราบ
        """
        if reason == "acknowledgement_overdue":
            headline = (
                f"🆘 ESCALATION: แจ้งเตือน {alert.analyte} "
                f"ส่งถึงแล้วแต่ยังไม่มีแพทย์รับทราบ"
            )
            detail = f"รอมาแล้ว {waited_sec // 60} นาที"
        else:
            headline = (
                f"🆘 ESCALATION: แจ้งเตือน {alert.analyte} "
                f"ส่งไม่สำเร็จ"
            )
            detail = f"ส่งแล้ว {alert.delivery_attempts} ครั้ง"

        message = (
            f"{headline}\n"
            f"{detail}\n"
            f"เอกสาร: {alert.doc_id[:8]}\n"
            f"รับทราบ: พิมพ์ 'รับทราบ {alert.alert_id[:8]}'"
        )

        # ส่งให้ admin ก่อน ถ้าไม่มีจึงส่งซ้ำให้ physician
        admin_recipients = self._service._recipients.get("admin", [])
        targets = admin_recipients or self._service._recipients.get(
            "physician", []
        )
        if send_push_fn and targets:
            for recipient in targets:
                try:
                    send_push_fn(recipient, message)
                except Exception:
                    pass

        alert.state = AlertState.ESCALATED.value
        alert.escalation_reason = reason
        alert.escalation_target = "admin" if admin_recipients else "physician"
        alert.add_event("escalated", details={
            "reason": reason,
            "total_attempts": alert.delivery_attempts,
            "waited_sec": waited_sec,
        })
        self._service._save_alert(alert)
        self._service._audit.log("alert_escalated", alert.owner_hash, {
            "alert_id": alert.alert_id[:8],
            "analyte": alert.analyte,
            "attempts": alert.delivery_attempts,
            "reason": reason,
        })


def rebuild_dedup_cache(service: AlertDeliveryService) -> int:
    """Rebuild dedup cache จาก GCS หลัง restart.

    เรียกตอน startup เพื่อกู้คืน state
    Returns จำนวน active alerts ที่โหลดได้
    """
    count = 0
    try:
        files = service._storage.list_files(ALERTS_PREFIX)
        for f in files:
            try:
                data = service._storage.get(f)
                if data is None:
                    continue
                alert = CriticalAlert.from_dict(json.loads(data))
                if alert.state != AlertState.RESOLVED.value:
                    service._dedup_cache[alert.dedup_key] = alert.alert_id
                    count += 1
            except (json.JSONDecodeError, KeyError):
                continue
    except Exception as e:
        logger.warning(f"Dedup cache rebuild failed: {type(e).__name__}")
    logger.info(f"Rebuilt dedup cache: {count} active alerts")
    return count


def format_active_alerts(alerts: list[dict]) -> str:
    """สร้างข้อความแสดง active alerts สำหรับ LINE."""
    if not alerts:
        return "✅ ไม่มีการแจ้งเตือนที่ค้างอยู่"

    lines = [f"🔔 การแจ้งเตือน ({len(alerts)} รายการ)\n"]
    for a in alerts:
        severity_icon = {
            AlertSeverity.CRITICAL.value: "🔴",
            AlertSeverity.URGENT.value: "🟠",
            AlertSeverity.SOURCE_VERIFY.value: "⚠️",
        }.get(a["severity"], "🔴")

        state_icon = {
            AlertState.QUEUED.value: "⏳",
            AlertState.SENT.value: "📤",
            AlertState.DELIVERY_FAILED.value: "❌",
            AlertState.ESCALATED.value: "🆘",
            AlertState.ACKNOWLEDGED.value: "✅",
        }.get(a["state"], "")

        # ต้องแสดงรหัส alert (ไม่ใช่ doc_id) เพราะคำสั่งรับทราบใช้รหัสนี้
        # ก่อนหน้านี้แสดงแต่ doc_id ทำให้แพทย์ไม่มีรหัสที่ใช้ตอบกลับได้
        _ref = (a.get("alert_id") or "")[:8]
        lines.append(
            f"{severity_icon} {a['analyte']} {state_icon} {a['state']}\n"
            f"   รหัส: {_ref} · เอกสาร: {a['doc_id']}"
        )

    return "\n".join(lines)
