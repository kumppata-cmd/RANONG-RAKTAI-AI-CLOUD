#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Persistent Audit Trail — RANONG-RAKTAI-AI 2.0

Durable audit event storage บน GCS (ไม่ใช่ Render ephemeral filesystem)

กฎความปลอดภัย:
  - Append-only / immutable records
  - Event IDs (UUID)
  - Actor identity (pseudonymous hash)
  - Persistent storage outside ephemeral Render filesystem
  - Fail-closed: security-critical actions fail when audit can't persist
  - ไม่เก็บ PHI, credentials, tokens ใน audit log
  - Concurrency safety (UUID filenames)
"""

from __future__ import annotations

import json
import uuid
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional, Any

logger = logging.getLogger(__name__)

AUDIT_PREFIX = "_audit/"


class AuditCategory(str):
    """หมวดหมู่ audit events."""
    SECURITY = "security"           # auth, authz, access control
    CLINICAL = "clinical"           # lab approval, alert, analysis
    DOCUMENT = "document"           # upload, association, quarantine
    PATIENT = "patient"             # registration, merge, reassign
    SYSTEM = "system"               # health, startup, config
    ADMIN = "admin"                 # admin actions


# Security-critical actions ที่ต้อง fail-closed
SECURITY_CRITICAL_ACTIONS = {
    "physician_approval",
    "physician_rejection",
    "physician_correction",
    "alert_acknowledged",
    "alert_resolved",
    "patient_registered",
    "patients_merged",
    "document_reassigned",
    "admin_access",
    "auth_failure",
    "enrollment_created",
    "enrollment_used",
}


@dataclass
class AuditEvent:
    """Immutable audit event record.

    เมื่อสร้างแล้ว ห้ามแก้ไข
    """
    event_id: str                    # UUID
    action: str                      # เหตุการณ์ที่เกิดขึ้น
    actor_hash: str                  # ผู้กระทำ (pseudonymous)
    timestamp: str = ""              # ISO 8601 (auto-filled)
    category: str = AuditCategory.SYSTEM
    document_ref: str = ""           # doc_id ที่เกี่ยวข้อง
    outcome: str = "success"         # success/failure/error
    details: dict = field(default_factory=dict)
    session_id: str = ""             # request session ID
    ip_hash: str = ""                # hashed IP (ไม่เก็บ IP จริง)

    def __post_init__(self):
        if not self.event_id:
            self.event_id = uuid.uuid4().hex
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat(timespec="seconds")

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict) -> AuditEvent:
        return cls(**{k: v for k, v in data.items()
                      if k in cls.__dataclass_fields__})


class PersistentAuditTrail:
    """Durable audit trail บน GCS.

    Features:
      - Append-only: ไม่มี update หรือ delete
      - UUID filenames: concurrency safe
      - Monthly partitioning
      - Fail-closed สำหรับ security-critical actions
      - ไม่เก็บ PHI หรือ credentials

    Storage layout:
      _audit/YYYY-MM/{uuid}.json    — per-event files
      _audit/YYYY-MM/batch_{uuid}.jsonl — batch files (append mode)
    """

    def __init__(self, storage, fail_closed: bool = True):
        """
        Args:
            storage: StorageLocal/StorageGCS instance
            fail_closed: ถ้า True, security-critical actions จะ fail
                         เมื่อ audit persistence ล้มเหลว
        """
        self._storage = storage
        self._fail_closed = fail_closed
        self._write_failures = 0

    def log(self, action: str, actor_hash: str,
            details: dict | None = None,
            document_ref: str = "",
            category: str = "",
            outcome: str = "success") -> AuditEvent | None:
        """บันทึก audit event.

        สำหรับ security-critical actions:
        - ถ้า fail_closed=True และเขียนไม่ได้ → raise exception
        - caller ต้อง handle exception และหยุดการทำงาน

        Returns AuditEvent ถ้าบันทึกสำเร็จ, None ถ้าล้มเหลว (non-critical)
        """
        # Auto-detect category
        if not category:
            category = self._detect_category(action)

        # Sanitize — ไม่เก็บ PHI
        safe_details = self._sanitize_details(details or {})

        event = AuditEvent(
            event_id="",
            action=action,
            actor_hash=actor_hash[:8] if actor_hash else "",
            category=category,
            document_ref=document_ref[:8] if document_ref else "",
            outcome=outcome,
            details=safe_details,
        )

        # Persist
        persisted = self._persist_event(event)

        if not persisted:
            self._write_failures += 1
            is_critical = action in SECURITY_CRITICAL_ACTIONS

            if is_critical and self._fail_closed:
                logger.error(
                    f"FAIL-CLOSED: Cannot persist audit for "
                    f"security-critical action '{action}'"
                )
                raise AuditPersistenceError(
                    f"ไม่สามารถบันทึก audit สำหรับ {action} — "
                    f"หยุดการทำงานเพื่อความปลอดภัย"
                )

            logger.warning(
                f"Audit persistence failed for '{action}' "
                f"(total failures: {self._write_failures})"
            )
            return None

        return event

    def log_failure(self, action: str, actor_hash: str,
                     error_type: str,
                     details: dict | None = None,
                     document_ref: str = "") -> AuditEvent | None:
        """บันทึก failed action."""
        all_details = {**(details or {}), "error_type": error_type}
        return self.log(
            action=action,
            actor_hash=actor_hash,
            details=all_details,
            document_ref=document_ref,
            outcome="failure",
        )

    def _persist_event(self, event: AuditEvent) -> bool:
        """เขียน event ลง persistent storage.

        ใช้ UUID filename — concurrency safe
        Monthly partition — retention policy support
        """
        try:
            # Monthly partition
            month = event.timestamp[:7]  # YYYY-MM
            path = (
                f"{AUDIT_PREFIX}{month}/{event.event_id}.json"
            )

            self._storage.put(
                path,
                event.to_json().encode("utf-8"),
                content_type="application/json",
            )
            return True

        except Exception as e:
            # Fallback: try per-event file at root audit level
            try:
                fallback_path = (
                    f"{AUDIT_PREFIX}fallback_{event.event_id}.json"
                )
                self._storage.put(
                    fallback_path,
                    event.to_json().encode("utf-8"),
                    content_type="application/json",
                )
                return True
            except Exception:
                logger.error(
                    f"Audit persistence completely failed: "
                    f"{type(e).__name__}"
                )
                return False

    def query_events(self, month: str | None = None,
                      action: str | None = None,
                      limit: int = 100) -> list[dict]:
        """ดึง audit events (read-only query).

        Args:
            month: YYYY-MM format
            action: filter by action type
            limit: max results
        """
        if month is None:
            month = datetime.now().strftime("%Y-%m")

        prefix = f"{AUDIT_PREFIX}{month}/"
        events = []

        try:
            files = self._storage.list_files(prefix)
            for f in files[:limit]:
                data = self._storage.get(f)
                if data:
                    event = json.loads(data)
                    if action is None or event.get("action") == action:
                        events.append(event)
        except Exception as e:
            logger.warning(f"Audit query failed: {type(e).__name__}")

        return events

    def get_write_failure_count(self) -> int:
        """จำนวนครั้งที่เขียน audit ไม่สำเร็จ."""
        return self._write_failures

    def _detect_category(self, action: str) -> str:
        """Auto-detect category from action name."""
        action_lower = action.lower()

        if any(k in action_lower for k in (
            "auth", "login", "access", "permission", "enrollment"
        )):
            return AuditCategory.SECURITY

        if any(k in action_lower for k in (
            "approv", "reject", "correct", "alert", "clinical", "lab"
        )):
            return AuditCategory.CLINICAL

        if any(k in action_lower for k in (
            "document", "upload", "quarantine", "association"
        )):
            return AuditCategory.DOCUMENT

        if any(k in action_lower for k in (
            "patient", "merge", "reassign", "register"
        )):
            return AuditCategory.PATIENT

        if any(k in action_lower for k in ("admin",)):
            return AuditCategory.ADMIN

        return AuditCategory.SYSTEM

    def _sanitize_details(self, details: dict) -> dict:
        """ลบข้อมูลที่ไม่ควรอยู่ใน audit log.

        ไม่เก็บ: credentials, tokens, PHI, full paths
        """
        FORBIDDEN_KEYS = {
            "password", "secret", "token", "credential", "api_key",
            "access_token", "reply_token", "channel_secret",
            "patient_name", "date_of_birth", "id_card", "phone",
            "address", "email",
        }

        safe = {}
        for key, value in details.items():
            key_lower = key.lower()
            if any(fk in key_lower for fk in FORBIDDEN_KEYS):
                safe[key] = "[REDACTED]"
            elif isinstance(value, str) and len(value) > 200:
                safe[key] = value[:100] + "...[truncated]"
            else:
                safe[key] = value
        return safe


class AuditPersistenceError(Exception):
    """Raised when audit persistence fails for security-critical action.

    Caller ต้อง handle และหยุดการทำงาน
    """
    pass


# ═══════════════════════════════════════════════════════════
# Enhanced AuditLogger — drop-in replacement
# ═══════════════════════════════════════════════════════════

class EnhancedAuditLogger:
    """Wrapper ที่ backward-compatible กับ AuditLogger เดิม
    แต่ใช้ PersistentAuditTrail ข้างใน."""

    def __init__(self, storage, fail_closed: bool = True):
        self._trail = PersistentAuditTrail(storage, fail_closed)

    def log(self, action: str, actor_hash: str,
            details: dict | None = None, **kwargs) -> bool:
        """Backward-compatible log method.

        Returns True ถ้าบันทึกสำเร็จ
        """
        try:
            event = self._trail.log(
                action=action,
                actor_hash=actor_hash,
                details=details,
                document_ref=kwargs.get("document_ref", ""),
            )
            return event is not None
        except AuditPersistenceError:
            raise
        except Exception:
            return False

    def query(self, month: str | None = None,
              action: str | None = None,
              limit: int = 100) -> list[dict]:
        return self._trail.query_events(month, action, limit)

    @property
    def write_failures(self) -> int:
        return self._trail.get_write_failure_count()
