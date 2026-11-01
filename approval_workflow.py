"""Approval Workflow Engine — V4.0 Phase 3.1

ระบบอนุมัติสำหรับ actions ที่มีความเสี่ยง:
- Financial changes (ค่าใช้จ่าย, บิล)
- File deletion
- Configuration changes
- Sensitive operations

Workflow: Request → Pending → Approved/Rejected
ส่ง approval request ผ่าน LINE push notification

ไม่เก็บข้อมูลผู้ป่วยใน approval records (PDPA)

Usage:
    from approval_workflow import ApprovalEngine, ApprovalRequest

    engine = ApprovalEngine(storage=storage, push_fn=push_message)
    req = engine.request_approval(
        action_type="delete_document",
        requester_hash="abc123",
        description="ลบเอกสาร doc-xxx",
        approver_hashes=["admin_hash"],
    )
    # Admin อนุมัติผ่าน LINE command: "อนุมัติ <approval_id>"
    engine.approve(req.approval_id, approver_hash="admin_hash")
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable

logger = logging.getLogger(__name__)


# ─── Approval Status ────────────────────────────────────


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


# ─── Action Types ────────────────────────────────────────

ACTION_TYPES = {
    "delete_document": {
        "description": "ลบเอกสาร",
        "requires_approval": True,
        "expiry_minutes": 60,
    },
    "financial_change": {
        "description": "เปลี่ยนแปลงข้อมูลการเงิน",
        "requires_approval": True,
        "expiry_minutes": 120,
    },
    "config_change": {
        "description": "เปลี่ยนการตั้งค่าระบบ",
        "requires_approval": True,
        "expiry_minutes": 60,
    },
    "bulk_operation": {
        "description": "ดำเนินการกับเอกสารจำนวนมาก",
        "requires_approval": True,
        "expiry_minutes": 30,
    },
    "export_data": {
        "description": "ส่งออกข้อมูลผู้ป่วย",
        "requires_approval": True,
        "expiry_minutes": 60,
    },
}


# ─── Approval Request ───────────────────────────────────


@dataclass
class ApprovalRequest:
    """คำขออนุมัติ."""
    approval_id: str = field(
        default_factory=lambda: f"APR-{uuid.uuid4().hex[:8]}"
    )
    action_type: str = ""
    description: str = ""
    requester_hash: str = ""
    approver_hashes: list[str] = field(default_factory=list)
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    decided_at: str = ""
    decided_by: str = ""
    reason: str = ""
    expiry_minutes: int = 60
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "approval_id": self.approval_id,
            "action_type": self.action_type,
            "description": self.description,
            "requester_hash": self.requester_hash[:8],
            "status": self.status.value,
            "created_at": self.created_at,
            "decided_at": self.decided_at,
            "decided_by": self.decided_by[:8] if self.decided_by else "",
            "reason": self.reason,
            "expiry_minutes": self.expiry_minutes,
            "metadata": dict(self.metadata or {}),
        }

    @classmethod
    def from_dict(cls, d: dict) -> ApprovalRequest:
        r = cls()
        r.approval_id = d.get("approval_id", r.approval_id)
        r.action_type = d.get("action_type", "")
        r.description = d.get("description", "")
        r.requester_hash = d.get("requester_hash", "")
        r.approver_hashes = d.get("approver_hashes", [])
        r.status = ApprovalStatus(d.get("status", "pending"))
        r.metadata = dict(d.get("metadata") or {})
        r.created_at = d.get("created_at", r.created_at)
        r.decided_at = d.get("decided_at", "")
        r.decided_by = d.get("decided_by", "")
        r.reason = d.get("reason", "")
        r.expiry_minutes = d.get("expiry_minutes", 60)
        r.metadata = d.get("metadata", {})
        return r

    def is_expired(self) -> bool:
        try:
            created = datetime.fromisoformat(self.created_at)
            now = datetime.now(timezone.utc)
            elapsed = (now - created).total_seconds() / 60
            return elapsed > self.expiry_minutes
        except Exception:
            return False


# ─── Approval Engine ─────────────────────────────────────


class ApprovalEngine:
    """จัดการ approval workflow."""

    GCS_PREFIX = "_approvals/"

    def __init__(
        self,
        storage=None,
        push_fn: Callable | None = None,
    ):
        self._storage = storage
        self._push_fn = push_fn  # push_message(line_user_id, text)
        self._pending: dict[str, ApprovalRequest] = {}

    def set_storage(self, storage) -> None:
        self._storage = storage

    def set_push_fn(self, fn: Callable) -> None:
        self._push_fn = fn

    def requires_approval(self, action_type: str) -> bool:
        """ตรวจว่า action นี้ต้องอนุมัติหรือไม่."""
        config = ACTION_TYPES.get(action_type, {})
        return config.get("requires_approval", False)

    def request_approval(
        self,
        action_type: str,
        requester_hash: str,
        description: str,
        approver_hashes: list[str] | None = None,
        metadata: dict | None = None,
    ) -> ApprovalRequest:
        """สร้างคำขออนุมัติ.

        Args:
            action_type: ประเภท action
            requester_hash: hash ของผู้ขอ
            description: คำอธิบาย action (ห้ามมี PHI)
            approver_hashes: hash ของผู้มีสิทธิ์อนุมัติ
            metadata: ข้อมูลเพิ่มเติม

        Returns:
            ApprovalRequest
        """
        config = ACTION_TYPES.get(action_type, {})

        req = ApprovalRequest(
            action_type=action_type,
            description=description,
            requester_hash=requester_hash,
            approver_hashes=approver_hashes or [],
            expiry_minutes=config.get("expiry_minutes", 60),
            metadata=metadata or {},
        )

        self._pending[req.approval_id] = req
        self._persist(req)

        logger.info(
            "Approval requested: %s (type=%s, by=%s)",
            req.approval_id, action_type, requester_hash[:8],
        )

        return req

    def approve(
        self,
        approval_id: str,
        approver_hash: str,
        reason: str = "",
    ) -> tuple[bool, str]:
        """อนุมัติคำขอ.

        Returns:
            (success, message)
        """
        req = self._get_request(approval_id)
        if not req:
            return False, "ไม่พบคำขออนุมัติ"

        if req.status != ApprovalStatus.PENDING:
            return False, f"คำขอนี้ {req.status.value} แล้ว"

        if req.is_expired():
            req.status = ApprovalStatus.EXPIRED
            self._persist(req)
            return False, "คำขอหมดอายุแล้ว"

        if req.approver_hashes and approver_hash not in req.approver_hashes:
            return False, "คุณไม่มีสิทธิ์อนุมัติคำขอนี้"

        req.status = ApprovalStatus.APPROVED
        req.decided_at = datetime.now(timezone.utc).isoformat()
        req.decided_by = approver_hash
        req.reason = reason
        self._pending.pop(approval_id, None)
        self._persist(req)

        logger.info(
            "Approval APPROVED: %s by %s",
            approval_id, approver_hash[:8],
        )
        return True, "อนุมัติสำเร็จ"

    def reject(
        self,
        approval_id: str,
        approver_hash: str,
        reason: str = "",
    ) -> tuple[bool, str]:
        """ปฏิเสธคำขอ."""
        req = self._get_request(approval_id)
        if not req:
            return False, "ไม่พบคำขออนุมัติ"

        if req.status != ApprovalStatus.PENDING:
            return False, f"คำขอนี้ {req.status.value} แล้ว"

        req.status = ApprovalStatus.REJECTED
        req.decided_at = datetime.now(timezone.utc).isoformat()
        req.decided_by = approver_hash
        req.reason = reason
        self._pending.pop(approval_id, None)
        self._persist(req)

        logger.info(
            "Approval REJECTED: %s by %s",
            approval_id, approver_hash[:8],
        )
        return True, "ปฏิเสธแล้ว"

    def get_pending(self) -> list[dict]:
        """ดูคำขอที่รออนุมัติ."""
        # ตรวจและ expire คำขอที่หมดอายุ
        expired = []
        for aid, req in list(self._pending.items()):
            if req.is_expired():
                req.status = ApprovalStatus.EXPIRED
                self._persist(req)
                expired.append(aid)
        for aid in expired:
            self._pending.pop(aid, None)

        return [r.to_dict() for r in self._pending.values()]

    def get_request(self, approval_id: str) -> dict | None:
        """ดูคำขอเดียว."""
        req = self._get_request(approval_id)
        return req.to_dict() if req else None

    def _get_request(self, approval_id: str) -> ApprovalRequest | None:
        """ดึง request จาก memory หรือ GCS."""
        if approval_id in self._pending:
            return self._pending[approval_id]
        # ลองดึงจาก GCS
        if self._storage:
            try:
                path = f"{self.GCS_PREFIX}{approval_id}.json"
                raw = self._storage.get(path)
                if raw:
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    return ApprovalRequest.from_dict(json.loads(raw))
            except Exception:
                pass
        return None

    def _persist(self, req: ApprovalRequest) -> None:
        """บันทึก request ลง GCS."""
        if not self._storage:
            return
        try:
            path = f"{self.GCS_PREFIX}{req.approval_id}.json"
            data = json.dumps(req.to_dict(), ensure_ascii=False).encode("utf-8")
            self._storage.put(path, data, content_type="application/json")
        except Exception as e:
            logger.warning(
                "Approval persist failed: %s", type(e).__name__,
            )
