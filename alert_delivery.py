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
import re
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

# รูปแบบปลายทางที่ LINE push ได้: user (U), group (C), room (R)
# ตามด้วย hex 32 ตัว
_LINE_ID_RE = re.compile(r"^[UCR][0-9a-fA-F]{32}$")

# รูปแบบ owner hash ของระบบนี้ — hash_user_id() คืน SHA-256 ตัด 32 ตัวแรก
# (ไม่ใช่ 64 ตัวเต็ม) ตรงกับ _OWNER_HASH_RE ใน server.py
#
# สองรูปแบบข้างบนแยกกันได้ด้วยตัวนำ: LINE ID ขึ้นต้น U/C/R เสมอ
# ส่วน hash เป็น hex ล้วน ความยาวเท่ากัน (32) จึงใช้ความยาวแยกไม่ได้
_OWNER_HASH_RE = re.compile(r"^[0-9a-f]{32}$")


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

    def set_recipients(self, recipients: dict) -> None:
        """เปลี่ยนรายชื่อผู้รับโดยไม่ต้อง restart

        จำเป็นเพราะ service ถูกสร้างครั้งเดียวตอน import ถ้าไม่มีตัวนี้
        การเพิ่มพยาบาลจะไม่มีผลจนกว่าจะ redeploy — ซึ่งคนเพิ่มจะไม่รู้
        และคิดว่าเพิ่มสำเร็จแล้ว
        """
        self._recipients = recipients or {}

    def recipient_count(self, role: str = "physician") -> int:
        """จำนวนผู้รับปัจจุบัน — ไม่คืนตัว ID"""
        return len(self._recipients.get(role, []))

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
    source = "ALERT_RECIPIENTS"
    if not alert_ids:
        alert_ids = os.getenv("PHYSICIAN_USERS", "").strip()
        source = "PHYSICIAN_USERS"

    if not alert_ids:
        logger.error(
            "ALERT_RECIPIENTS และ PHYSICIAN_USERS ไม่ได้ตั้งค่า — "
            "การแจ้งเตือนค่าวิกฤตจะไม่ถึงใครเลย"
        )
        return recipients

    raw = [uid.strip() for uid in alert_ids.split(",") if uid.strip()]

    # กรองให้เหลือเฉพาะ LINE ID จริง
    #
    # ทำไมต้องกรอง: PHYSICIAN_USERS เก็บ "SHA-256 hash" ของ LINE userId
    # (ดู server.py — ตั้งใจให้เป็น hash เพื่อไม่เก็บ ID ดิบไว้ตรวจสิทธิ์)
    # แต่ LINE push API ต้องใช้ ID ดิบ การ fallback ไปใช้ PHYSICIAN_USERS
    # จึงได้ค่าที่ push ไม่ได้เลย ทุก alert ถูกตีตรา delivery_failed
    # แบบเงียบ ๆ และไม่มีใครรู้ว่าค่าวิกฤตไม่ถึงมือแพทย์
    # (เกิดขึ้นจริงแล้ว — ตรวจพบ 26 ต.ค. 2026)
    #
    # รูปแบบ ID ของ LINE: U/C/R ตามด้วย hex 32 ตัว
    # ส่วน SHA-256 hex ยาว 64 ตัวและไม่มีตัวนำ จึงแยกออกจากกันได้ชัด
    valid = [uid for uid in raw if _LINE_ID_RE.match(uid)]
    rejected = len(raw) - len(valid)

    if valid:
        recipients["physician"] = valid
        logger.info("ผู้รับการแจ้งเตือน: %d คน (จาก %s)", len(valid), source)
    if rejected:
        # ไม่ log ตัว ID — บอกแค่จำนวนและสาเหตุ
        #
        # แยก hash ด้วย "ตัวนำ" ไม่ใช่ความยาว เพราะ hash_user_id คืน
        # hex 32 ตัว เท่ากับความยาวส่วน hex ของ LINE ID พอดี
        looks_hashed = any(_OWNER_HASH_RE.match(uid.lower()) for uid in raw)
        logger.error(
            "ผู้รับการแจ้งเตือน %d ค่าใช้ไม่ได้ (ไม่ใช่รูปแบบ LINE ID)%s",
            rejected,
            " — ค่าที่ให้มาดูเป็น hash: ALERT_RECIPIENTS ต้องใช้ LINE "
            "user ID ดิบ ไม่ใช่ค่าเดียวกับ PHYSICIAN_USERS"
            if looks_hashed else "",
        )
    if not valid:
        logger.error(
            "ไม่มีผู้รับการแจ้งเตือนที่ใช้ได้เลย — "
            "การแจ้งเตือนค่าวิกฤตจะไม่ถึงใคร"
        )

    return recipients


# ═══════════════════════════════════════════════════════════
# ทะเบียนผู้รับการแจ้งเตือน — เพิ่มคนได้โดยไม่เปิดเผย LINE user ID
#
# ปัญหาที่ต้องแก้: ALERT_RECIPIENTS ต้องใช้ LINE user ID ดิบ แต่คำสั่ง
# ลงทะเบียนของระบบตั้งใจไว้ว่า "ไม่แสดง LINE userId จริง" (มาตรการลด
# การเปิดเผยตัวระบุตัวตนตาม PDPA) สองอย่างนี้ขัดกันตรง ๆ และเป็นเหตุ
# ที่การแจ้งเตือนค่าวิกฤตไปถึงแพทย์คนเดียว เพิ่มพยาบาลไม่ได้เลย
#
# ทางออกที่เลือก — ไม่ลดมาตรการเดิมลง:
#   ตอนลงทะเบียน  ระบบเก็บ hash -> LINE user ID ไว้เองที่ _enrolled_ids/
#                  ไม่แสดงให้ใครเห็น ไม่ใส่ log ไม่คืนทาง API ใด
#   เพิ่มผู้รับ    ผู้ดูแลส่ง "hash" ซึ่งเจ้าตัวได้รับอยู่แล้วตอน
#                  ลงทะเบียน ระบบไปหา ID ดิบเองตอนส่ง
#   ผลลัพธ์        ไม่มีใครเห็น ID ดิบของใคร และ ID ไม่ต้องเดินทาง
#                  ผ่านแชทเลย
# ═══════════════════════════════════════════════════════════

ENROLLED_IDS_PREFIX = "_enrolled_ids/"
RECIPIENT_REGISTRY_PATH = "_alert_recipients/registry.json"

# บทบาทที่รับได้ — ทุกบทบาทส่งเข้าช่อง "physician" ของ service
# เพราะเป็นช่องเดียวที่ใช้ส่ง alert วิกฤต บทบาทเก็บไว้เพื่อให้ผู้ดูแล
# รู้ว่าใครเป็นใครเวลาดูรายการ
ALERT_ROLES = ("physician", "nurse", "admin")


def store_enrolled_line_id(storage, user_hash: str, line_user_id: str) -> bool:
    """เก็บ hash -> LINE user ID ไว้ใช้ส่งการแจ้งเตือน

    ค่านี้ไม่ถูกแสดงที่ใดเลย ไม่ใส่ log และไม่มี API ใดคืนออกไป
    ใช้เฉพาะตอน resolve ผู้รับก่อน push
    """
    if not _LINE_ID_RE.match(line_user_id or ""):
        return False
    if not _OWNER_HASH_RE.match((user_hash or "").lower()):
        return False
    user_hash = user_hash.lower()
    try:
        # เขียนเป็นไฟล์เวอร์ชันใหม่ — SA เขียนทับไม่ได้ (create-only) ถ้าใช้ชื่อ
        # เดิม การผูกบัญชีซ้ำ (เช่น ผู้ป่วยเปลี่ยนโทรศัพท์) จะล้มเหลวเงียบ ๆ
        # และการแจ้งเตือนไปที่เครื่องเก่า  ตัวอ่านเลือกไฟล์ล่าสุด
        _payload = json.dumps({
            "line_user_id": line_user_id,
            "enrolled_at": datetime.now().isoformat(),
        }, ensure_ascii=False).encode("utf-8")
        _ts = datetime.now().strftime("%Y%m%d%H%M%S%f")
        storage.put(
            f"{ENROLLED_IDS_PREFIX}{user_hash}/v{_ts}.json",
            _payload, content_type="application/json",
        )
        # ไฟล์เดี่ยวแบบเดิมสำหรับผู้อ่านที่ไม่มี bucket (ครั้งแรกเท่านั้น —
        # ครั้งถัดไปเขียนทับไม่ได้ ซึ่งไม่เป็นไร เพราะตัวอ่านดูเวอร์ชันก่อน)
        try:
            storage.put(
                f"{ENROLLED_IDS_PREFIX}{user_hash}.json",
                _payload, content_type="application/json",
            )
        except Exception:
            pass
        return True
    except Exception as e:
        # ไม่ log ตัว ID — แค่ชนิดข้อผิดพลาด
        logger.warning(
            "เก็บ LINE ID ของผู้ลงทะเบียนไม่สำเร็จ: %s", type(e).__name__
        )
        return False


def _lookup_enrolled_line_id(storage, user_hash: str) -> str:
    """หา LINE user ID จาก hash — คืน "" ถ้าไม่พบ

    ปรับเป็นตัวพิมพ์เล็กก่อนค้น ให้ตรงกับที่ store เขียนไว้ ไม่งั้น hash
    เดียวกันที่พิมพ์ต่างกันจะกลายเป็นคนละคน
    """
    user_hash = (user_hash or "").strip().lower()
    if not _OWNER_HASH_RE.match(user_hash):
        return ""
    try:
        raw = None
        bucket = getattr(storage, "_bucket", None)
        if bucket is not None:
            # ไฟล์เวอร์ชันล่าสุด (ผูกใหม่ชนะ)
            latest = None
            for b in bucket.list_blobs(prefix=f"{ENROLLED_IDS_PREFIX}{user_hash}/v"):
                if latest is None or b.name > latest.name:
                    latest = b
            if latest is not None:
                raw = latest.download_as_text().encode("utf-8")
        if raw is None:
            raw = storage.get(f"{ENROLLED_IDS_PREFIX}{user_hash}.json")
        if not raw:
            return ""
        uid = (json.loads(raw.decode("utf-8")) or {}).get("line_user_id", "")
        return uid if _LINE_ID_RE.match(uid or "") else ""
    except Exception as e:
        logger.warning("อ่าน LINE ID ไม่สำเร็จ: %s", type(e).__name__)
        return ""


def load_recipient_registry(storage) -> dict:
    """อ่านทะเบียนผู้รับ — คืนโครงว่างถ้ายังไม่มีไฟล์"""
    try:
        raw = storage.get(RECIPIENT_REGISTRY_PATH)
        if raw:
            data = json.loads(raw.decode("utf-8")) or {}
            if isinstance(data.get("recipients"), list):
                return data
    except Exception as e:
        logger.warning("อ่านทะเบียนผู้รับไม่สำเร็จ: %s", type(e).__name__)
    return {"recipients": []}


def save_recipient_registry(storage, data: dict) -> bool:
    try:
        storage.put(
            RECIPIENT_REGISTRY_PATH,
            json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"),
            content_type="application/json",
        )
        return True
    except Exception as e:
        logger.error("เขียนทะเบียนผู้รับไม่สำเร็จ: %s", type(e).__name__)
        return False


def add_recipient(
    storage, user_hash: str, role: str, added_by: str, note: str = "",
) -> dict:
    """เพิ่มผู้รับด้วย hash — ระบบไปหา LINE ID ดิบเอง

    คืน dict ที่ไม่มี LINE user ID อยู่ในนั้นเลย
    """
    user_hash = (user_hash or "").strip().lower()
    if not _OWNER_HASH_RE.match(user_hash):
        return {"error": (
            "hash ไม่ถูกต้อง — ต้องเป็น hex ตัวพิมพ์เล็ก 32 ตัว "
            "(ค่าที่ได้จากคำสั่ง 'ยืนยัน' ใน LINE)"
        )}
    if role not in ALERT_ROLES:
        return {"error": f"บทบาทต้องเป็นหนึ่งใน {', '.join(ALERT_ROLES)}"}

    # ต้องลงทะเบียนมาก่อน ไม่งั้นเพิ่มแล้วส่งไม่ได้และไม่มีใครรู้
    if not _lookup_enrolled_line_id(storage, user_hash):
        return {"error": (
            "ยังไม่พบการลงทะเบียนของ hash นี้ — "
            "ให้เจ้าตัวพิมพ์ 'ยืนยัน <รหัส>' ใน LINE ก่อน "
            "แล้วค่อยเพิ่มเป็นผู้รับ"
        )}

    data = load_recipient_registry(storage)
    for r in data["recipients"]:
        if r.get("user_hash") == user_hash:
            r["role"] = role
            r["active"] = True
            r["updated_at"] = datetime.now().isoformat()
            r["updated_by"] = (added_by or "")[:8]
            if note:
                r["note"] = note[:120]
            if not save_recipient_registry(storage, data):
                return {"error": "บันทึกทะเบียนไม่สำเร็จ"}
            return {"status": "updated", "role": role}

    data["recipients"].append({
        "user_hash": user_hash,
        "role": role,
        "active": True,
        "added_at": datetime.now().isoformat(),
        "added_by": (added_by or "")[:8],
        "note": note[:120] if note else "",
    })
    if not save_recipient_registry(storage, data):
        return {"error": "บันทึกทะเบียนไม่สำเร็จ"}
    return {"status": "added", "role": role}


def remove_recipient(storage, user_hash: str, removed_by: str) -> dict:
    """ปิดการใช้งานผู้รับ — ไม่ลบประวัติ"""
    user_hash = (user_hash or "").strip().lower()
    data = load_recipient_registry(storage)
    for r in data["recipients"]:
        if r.get("user_hash") == user_hash:
            if not r.get("active", True):
                return {"status": "already_inactive"}
            r["active"] = False
            r["updated_at"] = datetime.now().isoformat()
            r["updated_by"] = (removed_by or "")[:8]
            if not save_recipient_registry(storage, data):
                return {"error": "บันทึกทะเบียนไม่สำเร็จ"}
            return {"status": "removed"}
    return {"error": "ไม่พบ hash นี้ในทะเบียนผู้รับ"}


def list_recipients(storage) -> list:
    """รายการผู้รับ — ไม่มี LINE user ID และบอก hash เพียง 12 ตัวแรก

    hash เต็มคือสิ่งที่ใช้เพิ่ม/ลบ เจ้าตัวมีอยู่แล้ว การคืนเต็มทาง API
    จึงไม่จำเป็น และ 12 ตัวพอให้ผู้ดูแลแยกแยะได้
    """
    out = []
    for r in load_recipient_registry(storage).get("recipients", []):
        out.append({
            "user_hash_prefix": (r.get("user_hash") or "")[:12],
            "role": r.get("role", ""),
            "active": bool(r.get("active", True)),
            "added_at": r.get("added_at", ""),
            "note": r.get("note", ""),
        })
    return out


def resolve_alert_recipients(storage) -> dict:
    """รวมผู้รับจาก ENV และจากทะเบียน แล้วคืน LINE user ID ดิบสำหรับ push

    ENV มาก่อนเพื่อไม่ให้การเพิ่มทะเบียนทำให้ผู้รับเดิมหลุดหาย
    ไม่ log ตัว ID
    """
    recipients = build_alert_recipients_from_env()
    ids = list(recipients.get("physician", []))
    seen = set(ids)

    resolved = 0
    unresolved = 0
    for r in load_recipient_registry(storage).get("recipients", []):
        if not r.get("active", True):
            continue
        uid = _lookup_enrolled_line_id(storage, r.get("user_hash", ""))
        if uid:
            if uid not in seen:
                ids.append(uid)
                seen.add(uid)
            resolved += 1
        else:
            unresolved += 1

    if unresolved:
        logger.error(
            "ผู้รับการแจ้งเตือน %d รายในทะเบียนหา LINE ID ไม่ได้ — "
            "เจ้าตัวอาจยังไม่ได้ลงทะเบียน", unresolved,
        )
    if resolved:
        logger.info("ผู้รับจากทะเบียน: %d ราย", resolved)

    if ids:
        recipients["physician"] = ids
    return recipients


def describe_alert_recipients(storage=None) -> dict:
    """สรุปความพร้อมของเส้นทางแจ้งเตือน สำหรับ /health

    คืนเฉพาะ "จำนวน" และ "สาเหตุ" ไม่คืน LINE user ID ใด ๆ
    เพราะ /health เปิดอ่านได้โดยไม่ต้องยืนยันตัวตน

    มีไว้เพราะเดิมไม่มีทางรู้เลยว่าการแจ้งเตือนค่าวิกฤตส่งถึงใครได้
    หรือไม่ นอกจากไปอ่าน log — ซึ่งไม่มีใครอ่านจนกระทั่งตรวจเจอว่า
    alert ทุกใบ delivery_failed
    """
    alert_ids = os.getenv("ALERT_RECIPIENTS", "").strip()
    source = "ALERT_RECIPIENTS"
    if not alert_ids:
        alert_ids = os.getenv("PHYSICIAN_USERS", "").strip()
        source = "PHYSICIAN_USERS"

    raw = [uid.strip() for uid in alert_ids.split(",") if uid.strip()]
    valid = [uid for uid in raw if _LINE_ID_RE.match(uid)]

    # ผู้รับจากทะเบียน (เพิ่มด้วย hash — ไม่ต้องแก้ ENV และไม่ต้อง restart)
    from_registry = 0
    registry_unresolved = 0
    if storage is not None:
        try:
            for r in load_recipient_registry(storage).get("recipients", []):
                if not r.get("active", True):
                    continue
                if _lookup_enrolled_line_id(storage, r.get("user_hash", "")):
                    from_registry += 1
                else:
                    registry_unresolved += 1
        except Exception as e:
            logger.warning(
                "อ่านทะเบียนผู้รับสำหรับ /health ไม่สำเร็จ: %s",
                type(e).__name__,
            )

    total = len(valid) + from_registry

    if not raw and not from_registry and not registry_unresolved:
        reason = (
            "ยังไม่มีผู้รับเลย — ตั้ง ALERT_RECIPIENTS ใน Render "
            "หรือเพิ่มด้วย /admin/api/v2/alerts/recipients"
        )
    elif not total:
        if raw and not valid:
            reason = (
                "ค่าที่ตั้งไว้ไม่ใช่รูปแบบ LINE user ID "
                "(ALERT_RECIPIENTS ต้องใช้ ID ดิบ ไม่ใช่ hash)"
            )
        else:
            reason = (
                f"ผู้รับในทะเบียน {registry_unresolved} ราย "
                "ยังไม่ได้ลงทะเบียนใน LINE จึงส่งไม่ได้"
            )
    else:
        bits = []
        if raw and len(valid) < len(raw):
            bits.append(f"ค่าใน ENV ใช้ได้ {len(valid)} จาก {len(raw)}")
        if registry_unresolved:
            bits.append(
                f"ในทะเบียน {registry_unresolved} ราย"
                "ยังไม่ได้ลงทะเบียนใน LINE"
            )
        reason = " · ".join(bits)

    return {
        "can_deliver": total > 0,
        "recipients": total,
        "from_env": len(valid),
        "from_registry": from_registry,
        "registry_unresolved": registry_unresolved,
        "configured_values": len(raw),
        "source": source if raw else "",
        "problem": reason,
    }


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
