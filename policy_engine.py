"""Policy Engine — V4.0 Phase 4.3

Multi-agent policy enforcement — ทุก action ต้องผ่าน policy check
AI agents ไม่สามารถทำงาน high-risk โดยไม่ผ่าน policy

Policies:
- no_external_ai: ห้ามส่งข้อมูลไป External AI
- require_approval: actions ที่ต้องอนุมัติก่อน
- rate_limit: จำกัดจำนวน actions
- data_isolation: ข้อมูลผู้ป่วยแยก owner
- phi_protection: ห้าม log ข้อมูลผู้ป่วย
- no_auto_delete: ห้ามลบเอกสารอัตโนมัติ

Usage:
    from policy_engine import PolicyEngine

    policy = PolicyEngine()
    allowed, reason = policy.check("delete_document", context={...})
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

logger = logging.getLogger(__name__)


class PolicyDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass
class PolicyResult:
    decision: PolicyDecision
    reason: str = ""
    policy_name: str = ""
    requires_approval: bool = False

    def to_dict(self) -> dict:
        return {
            "decision": self.decision.value,
            "reason": self.reason,
            "policy": self.policy_name,
            "requires_approval": self.requires_approval,
        }


# ─── Policy Definitions ─────────────────────────────────

POLICIES = {
    "no_external_ai": {
        "description": "ห้ามส่งข้อมูลผู้ป่วยไป External AI",
        "actions": ["ai_classify", "ai_analyze", "ai_summarize"],
        "decision": PolicyDecision.DENY,
        "hardcoded": True,
    },
    "require_approval_delete": {
        "description": "ลบเอกสารต้องอนุมัติ",
        "actions": ["delete_document", "bulk_delete"],
        "decision": PolicyDecision.REQUIRE_APPROVAL,
    },
    "require_approval_financial": {
        "description": "การเงินต้องอนุมัติ",
        "actions": ["financial_change", "vendor_payment"],
        "decision": PolicyDecision.REQUIRE_APPROVAL,
    },
    "require_approval_config": {
        "description": "เปลี่ยน config ต้องอนุมัติ",
        "actions": ["config_change", "role_change"],
        "decision": PolicyDecision.REQUIRE_APPROVAL,
    },
    "require_approval_export": {
        "description": "ส่งออกข้อมูลต้องอนุมัติ",
        "actions": ["export_data", "bulk_export"],
        "decision": PolicyDecision.REQUIRE_APPROVAL,
    },
    "no_auto_delete": {
        "description": "ห้ามลบเอกสารอัตโนมัติ (ต้องมีคนสั่ง)",
        "actions": ["auto_delete", "scheduled_delete"],
        "decision": PolicyDecision.DENY,
    },
    "phi_protection": {
        "description": "ห้าม log/ส่ง PHI",
        "actions": ["log_phi", "send_phi_external"],
        "decision": PolicyDecision.DENY,
        "hardcoded": True,
    },
}


# ─── Policy Engine ───────────────────────────────────────


class PolicyEngine:
    """Policy enforcement สำหรับทุก action.

    ทุก action ต้องผ่าน policy.check() ก่อนดำเนินการ
    """

    def __init__(self):
        self._policies = dict(POLICIES)
        self._audit_log: list[dict] = []
        self._max_audit = 500

    def check(
        self,
        action: str,
        context: dict | None = None,
    ) -> PolicyResult:
        """ตรวจสอบว่า action ถูกอนุญาตหรือไม่.

        Args:
            action: ชื่อ action ที่จะทำ
            context: ข้อมูลเพิ่มเติม (user_hash, target, etc.)

        Returns:
            PolicyResult
        """
        ctx = context or {}

        for name, policy in self._policies.items():
            if action in policy.get("actions", []):
                decision = policy["decision"]

                result = PolicyResult(
                    decision=decision,
                    reason=policy.get("description", ""),
                    policy_name=name,
                    requires_approval=(
                        decision == PolicyDecision.REQUIRE_APPROVAL
                    ),
                )

                # Audit log
                self._log_check(action, result, ctx)

                if decision == PolicyDecision.DENY:
                    logger.warning(
                        "Policy DENY: %s (policy=%s, user=%s)",
                        action, name, ctx.get("user_hash", "")[:8],
                    )
                elif decision == PolicyDecision.REQUIRE_APPROVAL:
                    logger.info(
                        "Policy REQUIRE_APPROVAL: %s (policy=%s)",
                        action, name,
                    )

                return result

        # ไม่มี policy match → ALLOW
        result = PolicyResult(
            decision=PolicyDecision.ALLOW,
            reason="ไม่มี policy ที่ตรงกัน",
        )
        self._log_check(action, result, ctx)
        return result

    def check_and_enforce(
        self,
        action: str,
        context: dict | None = None,
    ) -> tuple[bool, str]:
        """ตรวจและบังคับใช้ policy.

        Returns:
            (allowed, message)
            allowed=True → ทำได้เลย
            allowed=False → ห้ามทำ (DENY หรือต้อง approval)
        """
        result = self.check(action, context)

        if result.decision == PolicyDecision.ALLOW:
            return True, ""
        if result.decision == PolicyDecision.DENY:
            return False, f"🚫 {result.reason}"
        if result.decision == PolicyDecision.REQUIRE_APPROVAL:
            return False, f"⏳ ต้องอนุมัติก่อน: {result.reason}"

        return True, ""

    def _log_check(
        self, action: str, result: PolicyResult, context: dict,
    ) -> None:
        """บันทึก policy check."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "decision": result.decision.value,
            "policy": result.policy_name,
            "user": context.get("user_hash", "")[:8],
        }
        self._audit_log.append(entry)
        if len(self._audit_log) > self._max_audit:
            self._audit_log = self._audit_log[-self._max_audit:]

    def get_policies(self) -> list[dict]:
        """ดู policies ทั้งหมด."""
        return [
            {
                "name": name,
                "description": p.get("description", ""),
                "actions": p.get("actions", []),
                "decision": p.get("decision", PolicyDecision.ALLOW).value,
                "hardcoded": p.get("hardcoded", False),
            }
            for name, p in self._policies.items()
        ]

    def get_audit_log(self, limit: int = 50) -> list[dict]:
        """ดู policy audit log."""
        return self._audit_log[-limit:]

    def get_stats(self) -> dict:
        """สถิติ policy checks."""
        by_decision: dict[str, int] = {}
        for entry in self._audit_log:
            d = entry.get("decision", "unknown")
            by_decision[d] = by_decision.get(d, 0) + 1
        return {
            "total_checks": len(self._audit_log),
            "by_decision": by_decision,
            "policy_count": len(self._policies),
        }
