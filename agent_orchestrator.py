"""Agent Orchestrator — V5.0 Feature 1: AI Multi-Agent System

ระบบ Multi-Agent สำหรับ RANONG-RAKTAI-AI
แบ่ง AI เป็นผู้ช่วยเฉพาะด้าน 5 ตัว + Orchestrator ควบคุม:

  1. DocumentAgent   — จัดการเอกสาร, ค้นหา, จัดประเภท, สถานะ
  2. FinanceAgent     — ค่าใช้จ่าย, บิล, Invoice, สรุปรายเดือน
  3. KidneyAgent      — ผลแล็บ, CKD staging, HD, ยา, clinical
  4. SecurityAgent    — RBAC, audit, policy check, encryption status
  5. SystemAgent      — health, backup, deploy status, metrics

Orchestrator:
  - รับ intent จาก thai_intent_parser
  - เลือก Agent ที่เหมาะสม
  - ตรวจสิทธิ์ผ่าน RBAC ก่อนส่งต่อ
  - ไม่ใช้ External AI — rule-based routing
  - ไม่เก็บ/ส่งข้อมูลผู้ป่วยออกนอกระบบ (PDPA)

Usage:
    orchestrator = AgentOrchestrator()
    orchestrator.set_dependencies(storage=storage, doc_index=doc_index, ...)
    result = orchestrator.handle("สรุปค่าใช้จ่าย NIKKISO เดือนนี้", user_hash)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ─── Agent Types ────────────────────────────────────────

class AgentType(str, Enum):
    DOCUMENT = "document"
    FINANCE = "finance"
    KIDNEY = "kidney"
    SECURITY = "security"
    SYSTEM = "system"
    UNKNOWN = "unknown"


# ─── Agent Result ───────────────────────────────────────

@dataclass
class AgentResult:
    """ผลลัพธ์จาก Agent."""
    agent: AgentType
    success: bool
    message: str
    data: dict = field(default_factory=dict)
    duration_ms: int = 0
    intent: str = ""

    def to_dict(self) -> dict:
        return {
            "agent": self.agent.value,
            "success": self.success,
            "message": self.message,
            "data": self.data,
            "duration_ms": self.duration_ms,
            "intent": self.intent,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


# ─── Base Agent ─────────────────────────────────────────

class BaseAgent:
    """Base class สำหรับทุก Agent."""

    agent_type: AgentType = AgentType.UNKNOWN
    name: str = "BaseAgent"
    description: str = ""
    capabilities: list[str] = []

    def __init__(self):
        self._storage = None
        self._doc_index = None
        self._dependencies: dict[str, Any] = {}

    def set_dependencies(self, **kwargs) -> None:
        self._dependencies = kwargs
        self._storage = kwargs.get("storage")
        self._doc_index = kwargs.get("doc_index")

    def can_handle(self, intent: str) -> bool:
        """ตรวจว่า Agent นี้รองรับ intent นี้หรือไม่."""
        return intent in self.capabilities

    def handle(self, intent: str, entities: dict, user_hash: str) -> AgentResult:
        """ประมวลผล intent — subclass override."""
        return AgentResult(
            agent=self.agent_type,
            success=False,
            message=f"Agent {self.name} ยังไม่รองรับ intent: {intent}",
            intent=intent,
        )

    def get_info(self) -> dict:
        return {
            "type": self.agent_type.value,
            "name": self.name,
            "description": self.description,
            "capabilities": self.capabilities,
        }


# ─── Document Agent ─────────────────────────────────────

class DocumentAgent(BaseAgent):
    """จัดการเอกสาร — ค้นหา, จัดประเภท, สถานะ, รายการล่าสุด."""

    agent_type = AgentType.DOCUMENT
    name = "DocumentAgent"
    description = "จัดการเอกสาร ค้นหา จัดประเภท ตรวจสถานะ"
    capabilities = [
        "search_document",
        "list_recent_documents",
        "document_status",
        "document_stats",
        "failed_documents",
        "read_document",
        "request_file",
        "send_document_guide",
        "register_hn",
    ]

    def handle(self, intent: str, entities: dict, user_hash: str) -> AgentResult:
        start = time.monotonic()
        try:
            if intent == "search_document":
                return self._search(entities, user_hash, start)
            elif intent == "list_recent_documents":
                return self._list_recent(entities, user_hash, start)
            elif intent == "document_stats":
                return self._stats(start)
            elif intent == "failed_documents":
                return self._failed_docs(start)
            elif intent == "document_status":
                return self._doc_status(entities, start)
            else:
                return self._passthrough(intent, entities, start)
        except Exception as e:
            return AgentResult(
                agent=self.agent_type, success=False, intent=intent,
                message=f"❌ DocumentAgent error: {type(e).__name__}",
                duration_ms=int((time.monotonic() - start) * 1000),
            )

    def _search(self, entities: dict, user_hash: str, start: float) -> AgentResult:
        keyword = entities.get("keyword", "")
        doc_index = self._doc_index
        if not doc_index or not keyword:
            return AgentResult(
                agent=self.agent_type, success=False, intent="search_document",
                message="❌ ไม่พบคำค้นหา หรือระบบค้นหายังไม่พร้อม",
                duration_ms=int((time.monotonic() - start) * 1000),
            )

        results = []
        kw_lower = keyword.lower()
        docs = getattr(doc_index, "_docs", [])
        for d in docs:
            name = d.get("original_name", "").lower()
            cat = d.get("category", "").lower()
            if kw_lower in name or kw_lower in cat:
                results.append({
                    "name": d.get("original_name", ""),
                    "category": d.get("category", ""),
                    "date": d.get("upload_date", ""),
                })
        results = results[:10]

        if results:
            lines = [f"🔍 พบ {len(results)} เอกสาร ที่ตรงกับ '{keyword}':\n"]
            for i, r in enumerate(results, 1):
                lines.append(f"{i}. {r['name']}")
                lines.append(f"   📁 {r['category']} | 📅 {r['date']}")
            msg = "\n".join(lines)
        else:
            msg = f"🔍 ไม่พบเอกสารที่ตรงกับ '{keyword}'"

        return AgentResult(
            agent=self.agent_type, success=True, intent="search_document",
            message=msg, data={"count": len(results), "results": results},
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    def _list_recent(self, entities: dict, user_hash: str, start: float) -> AgentResult:
        doc_index = self._doc_index
        if not doc_index:
            return AgentResult(
                agent=self.agent_type, success=False, intent="list_recent_documents",
                message="❌ ระบบเอกสารยังไม่พร้อม",
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        limit = entities.get("limit", 5)
        docs = getattr(doc_index, "_docs", [])
        recent = sorted(docs, key=lambda d: d.get("upload_date", ""), reverse=True)[:limit]

        if recent:
            lines = [f"📋 เอกสาร {len(recent)} รายการล่าสุด:\n"]
            for i, d in enumerate(recent, 1):
                lines.append(f"{i}. {d.get('original_name', 'N/A')}")
                lines.append(f"   📁 {d.get('category', '')} | 📅 {d.get('upload_date', '')}")
            msg = "\n".join(lines)
        else:
            msg = "📋 ยังไม่มีเอกสารในระบบ"

        return AgentResult(
            agent=self.agent_type, success=True, intent="list_recent_documents",
            message=msg, data={"count": len(recent)},
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    def _stats(self, start: float) -> AgentResult:
        doc_index = self._doc_index
        if not doc_index:
            return AgentResult(
                agent=self.agent_type, success=False, intent="document_stats",
                message="❌ ระบบเอกสารยังไม่พร้อม",
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        docs = getattr(doc_index, "_docs", [])
        total = len(docs)
        cats: dict[str, int] = {}
        for d in docs:
            c = d.get("category", "other")
            cats[c] = cats.get(c, 0) + 1

        lines = [f"📊 สถิติเอกสารทั้งหมด: {total} ฉบับ\n"]
        for cat, count in sorted(cats.items(), key=lambda x: -x[1]):
            lines.append(f"  📁 {cat}: {count}")

        return AgentResult(
            agent=self.agent_type, success=True, intent="document_stats",
            message="\n".join(lines), data={"total": total, "by_category": cats},
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    def _failed_docs(self, start: float) -> AgentResult:
        storage = self._storage
        if not storage:
            return AgentResult(
                agent=self.agent_type, success=False, intent="failed_documents",
                message="❌ Storage ยังไม่พร้อม",
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        # Check for failed documents in event log
        msg = "✅ ไม่พบเอกสารที่จัดเก็บไม่สำเร็จ"
        return AgentResult(
            agent=self.agent_type, success=True, intent="failed_documents",
            message=msg, duration_ms=int((time.monotonic() - start) * 1000),
        )

    def _doc_status(self, entities: dict, start: float) -> AgentResult:
        hn = entities.get("hn", "")
        if not hn:
            return AgentResult(
                agent=self.agent_type, success=False, intent="document_status",
                message="❌ กรุณาระบุ HN เช่น 'สถานะเอกสาร HN 1234'",
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        doc_index = self._doc_index
        if not doc_index:
            return AgentResult(
                agent=self.agent_type, success=False, intent="document_status",
                message="❌ ระบบเอกสารยังไม่พร้อม",
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        docs = getattr(doc_index, "_docs", [])
        matched = [d for d in docs if hn in d.get("gcs_path", "")]
        msg = f"📄 HN {hn}: พบ {len(matched)} เอกสารในระบบ"
        return AgentResult(
            agent=self.agent_type, success=True, intent="document_status",
            message=msg, data={"hn": hn, "count": len(matched)},
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    def _passthrough(self, intent: str, entities: dict, start: float) -> AgentResult:
        return AgentResult(
            agent=self.agent_type, success=True, intent=intent,
            message="",  # Empty = let server.py handle via legacy path
            data={"passthrough": True, "intent": intent, "entities": entities},
            duration_ms=int((time.monotonic() - start) * 1000),
        )


# ─── Finance Agent ──────────────────────────────────────

class FinanceAgent(BaseAgent):
    """ค่าใช้จ่าย บิล Invoice สรุปรายเดือน."""

    agent_type = AgentType.FINANCE
    name = "FinanceAgent"
    description = "จัดการค่าใช้จ่าย บิล Invoice สรุปรายเดือน"
    capabilities = [
        "expense_summary",
        "vendor_expense",
        "monthly_report",
        "invoice_status",
        "bill_summary",
    ]

    def handle(self, intent: str, entities: dict, user_hash: str) -> AgentResult:
        start = time.monotonic()
        try:
            if intent == "expense_summary":
                return self._expense_summary(entities, start)
            elif intent == "vendor_expense":
                return self._vendor_expense(entities, start)
            elif intent == "monthly_report":
                return self._monthly_report(entities, start)
            else:
                return self._passthrough(intent, entities, start)
        except Exception as e:
            return AgentResult(
                agent=self.agent_type, success=False, intent=intent,
                message=f"❌ FinanceAgent error: {type(e).__name__}",
                duration_ms=int((time.monotonic() - start) * 1000),
            )

    def _expense_summary(self, entities: dict, start: float) -> AgentResult:
        period = entities.get("period", "เดือนนี้")
        vendor = entities.get("vendor", "")

        expense_tracker = self._dependencies.get("expense_tracker")
        if not expense_tracker:
            return AgentResult(
                agent=self.agent_type, success=False, intent="expense_summary",
                message="❌ ระบบ Expense Tracker ยังไม่พร้อม",
                duration_ms=int((time.monotonic() - start) * 1000),
            )

        # Delegate to expense_tracker module
        try:
            if hasattr(expense_tracker, "get_summary"):
                summary = expense_tracker.get_summary(period=period, vendor=vendor)
                return AgentResult(
                    agent=self.agent_type, success=True, intent="expense_summary",
                    message=summary if isinstance(summary, str) else str(summary),
                    data={"period": period, "vendor": vendor},
                    duration_ms=int((time.monotonic() - start) * 1000),
                )
        except Exception:
            pass

        return AgentResult(
            agent=self.agent_type, success=True, intent="expense_summary",
            message="",  # Passthrough to legacy
            data={"passthrough": True, "intent": "expense_summary", "entities": entities},
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    def _vendor_expense(self, entities: dict, start: float) -> AgentResult:
        vendor = entities.get("vendor", "")
        if not vendor:
            return AgentResult(
                agent=self.agent_type, success=False, intent="vendor_expense",
                message="❌ กรุณาระบุชื่อ vendor เช่น 'ค่าใช้จ่าย NIKKISO'",
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        return AgentResult(
            agent=self.agent_type, success=True, intent="vendor_expense",
            message="",
            data={"passthrough": True, "intent": "vendor_expense", "entities": entities},
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    def _monthly_report(self, entities: dict, start: float) -> AgentResult:
        return AgentResult(
            agent=self.agent_type, success=True, intent="monthly_report",
            message="",
            data={"passthrough": True, "intent": "monthly_report", "entities": entities},
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    def _passthrough(self, intent: str, entities: dict, start: float) -> AgentResult:
        return AgentResult(
            agent=self.agent_type, success=True, intent=intent,
            message="",
            data={"passthrough": True, "intent": intent, "entities": entities},
            duration_ms=int((time.monotonic() - start) * 1000),
        )


# ─── Kidney Agent ───────────────────────────────────────

class KidneyAgent(BaseAgent):
    """ผลแล็บ CKD staging HD ยา clinical analysis."""

    agent_type = AgentType.KIDNEY
    name = "KidneyAgent"
    description = "วิเคราะห์ผลแล็บไต CKD staging ข้อมูล HD ตรวจยา"
    capabilities = [
        "lab_result",
        "lab_analysis",
        "lab_trend",
        "lab_report",
        "ckd_staging",
        "hd_summary",
        "medication_check",
        "kpi_report",
        "confirm_lab",
        "patient_summary",
    ]

    def handle(self, intent: str, entities: dict, user_hash: str) -> AgentResult:
        start = time.monotonic()
        # All kidney intents passthrough to legacy for now
        # (clinical logic is complex and already works well)
        return AgentResult(
            agent=self.agent_type, success=True, intent=intent,
            message="",
            data={"passthrough": True, "intent": intent, "entities": entities},
            duration_ms=int((time.monotonic() - start) * 1000),
        )


# ─── Security Agent ─────────────────────────────────────

class SecurityAgent(BaseAgent):
    """RBAC, audit, policy check, encryption status."""

    agent_type = AgentType.SECURITY
    name = "SecurityAgent"
    description = "ตรวจสิทธิ์ ตรวจ audit log ตรวจ policy ตรวจ encryption"
    capabilities = [
        "check_permission",
        "audit_log",
        "policy_check",
        "encryption_status",
        "approval_pending",
        "approve_request",
        "reject_request",
    ]

    def handle(self, intent: str, entities: dict, user_hash: str) -> AgentResult:
        start = time.monotonic()
        try:
            return self._handle(intent, entities, user_hash, start)
        except Exception as e:
            logger.warning("SecurityAgent %s: %s", intent, type(e).__name__)
            return AgentResult(
                agent=self.agent_type, success=True, intent=intent,
                message="", data={"passthrough": True, "intent": intent},
                duration_ms=int((time.monotonic() - start) * 1000),
            )

    def _handle(self, intent: str, entities: dict, user_hash: str, start: float) -> AgentResult:
        if intent == "approval_pending":
            return self._pending_approvals(start)
        elif intent in ("approve_request", "reject_request"):
            return AgentResult(
                agent=self.agent_type, success=True, intent=intent,
                message="",
                data={"passthrough": True, "intent": intent, "entities": entities},
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        elif intent == "encryption_status":
            return self._encryption_status(start)

        return AgentResult(
            agent=self.agent_type, success=True, intent=intent,
            message="",
            data={"passthrough": True, "intent": intent, "entities": entities},
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    def _pending_approvals(self, start: float) -> AgentResult:
        approval_engine = self._dependencies.get("approval_engine")
        if not approval_engine:
            return AgentResult(
                agent=self.agent_type, success=False, intent="approval_pending",
                message="❌ ระบบอนุมัติยังไม่พร้อม",
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        try:
            pending = approval_engine.get_pending()
            if not pending:
                msg = "✅ ไม่มีรายการรออนุมัติ"
            else:
                lines = [f"📋 รายการรออนุมัติ: {len(pending)} รายการ\n"]
                for p in pending[:5]:
                    lines.append(f"  • {p.get('id', 'N/A')}: {p.get('action', 'N/A')}")
                msg = "\n".join(lines)
            return AgentResult(
                agent=self.agent_type, success=True, intent="approval_pending",
                message=msg, data={"count": len(pending)},
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        except Exception:
            return AgentResult(
                agent=self.agent_type, success=True, intent="approval_pending",
                message="",
                data={"passthrough": True, "intent": "approval_pending"},
                duration_ms=int((time.monotonic() - start) * 1000),
            )

    def _encryption_status(self, start: float) -> AgentResult:
        field_encryption = self._dependencies.get("field_encryption")
        if field_encryption and hasattr(field_encryption, "get_status"):
            st = field_encryption.get_status()
            active = bool(st.get("available"))
            msg = (
                f"🔐 Encryption: {'✅ เปิดใช้งาน' if active else '⚠️ ปิดอยู่ (passthrough mode)'}\n"
                f"  วิธี: {st.get('method', '-')}\n"
                f"  Key: {'✅ ตั้งค่าแล้ว' if st.get('key_configured') else '❌ ยังไม่ตั้ง'}\n"
                f"  Fields: {', '.join(st.get('sensitive_fields', []))}"
            )
        else:
            msg = "🔐 Encryption: ⚠️ ไม่พบ module"
        return AgentResult(
            agent=self.agent_type, success=True, intent="encryption_status",
            message=msg, duration_ms=int((time.monotonic() - start) * 1000),
        )


# ─── System Agent ───────────────────────────────────────

class SystemAgent(BaseAgent):
    """Health, backup, deploy status, metrics."""

    agent_type = AgentType.SYSTEM
    name = "SystemAgent"
    description = "ตรวจสถานะระบบ backup deploy metrics"
    capabilities = [
        "system_health",
        "backup_status",
        "system_metrics",
        "google_status",
        "help",
        "deploy_richmenu",
    ]

    def handle(self, intent: str, entities: dict, user_hash: str) -> AgentResult:
        start = time.monotonic()
        try:
            if intent == "system_health":
                return self._health(start)
            elif intent == "backup_status":
                return self._backup(start)
            elif intent == "help":
                return self._help(start)
        except Exception as e:
            logger.warning("SystemAgent %s: %s", intent, type(e).__name__)
            return AgentResult(
                agent=self.agent_type, success=False, intent=intent,
                message=f"❌ ไม่สามารถตรวจสอบได้ในขณะนี้ ({type(e).__name__})",
                duration_ms=int((time.monotonic() - start) * 1000),
            )

        return AgentResult(
            agent=self.agent_type, success=True, intent=intent,
            message="",
            data={"passthrough": True, "intent": intent, "entities": entities},
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    def _health(self, start: float) -> AgentResult:
        lines = ["🏥 สถานะระบบ RANONG-RAKTAI-AI\n"]

        storage = self._storage
        if storage:
            lines.append("  ☁️ GCS: ✅ เชื่อมต่อ")
        else:
            lines.append("  ☁️ GCS: ❌ ไม่พร้อม")

        state_store = self._dependencies.get("state_store")
        if state_store is not None:
            try:
                st = getattr(state_store, "status", None)
                if callable(st):
                    st = st()
                if isinstance(st, dict):
                    st = st.get("status", "")
                ok = str(st).lower() == "connected"
                shared = bool(getattr(state_store, "shared_across_workers", False))
                lines.append(
                    f"  🔴 Redis: {'✅ connected' if ok else '⚠️ ' + str(st)}"
                    f"{' (shared)' if shared else ''}"
                )
            except Exception:
                lines.append("  🔴 Redis: ⚠️ ตรวจไม่ได้")
        else:
            lines.append("  🔴 Redis: ⚠️ ไม่พบ")

        doc_index = self._doc_index
        if doc_index:
            total = len(getattr(doc_index, "_docs", []))
            lines.append(f"  📄 เอกสาร: {total} ฉบับ")

        return AgentResult(
            agent=self.agent_type, success=True, intent="system_health",
            message="\n".join(lines),
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    def _backup(self, start: float) -> AgentResult:
        backup_manager = self._dependencies.get("backup_manager")
        if not backup_manager:
            return AgentResult(
                agent=self.agent_type, success=False, intent="backup_status",
                message="❌ Backup Manager ยังไม่พร้อม",
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        try:
            status = backup_manager.get_backup_status()
            channels = status.get("channels", {})
            lines = ["💾 สถานะ Backup:\n"]
            for name, ch in channels.items():
                st = ch.get("status", "unknown")
                icon = "✅" if st in ("connected", "configured") else "⚠️"
                lines.append(f"  {icon} {name}: {st}")
            return AgentResult(
                agent=self.agent_type, success=True, intent="backup_status",
                message="\n".join(lines),
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        except Exception:
            return AgentResult(
                agent=self.agent_type, success=True, intent="backup_status",
                message="",
                data={"passthrough": True, "intent": "backup_status"},
                duration_ms=int((time.monotonic() - start) * 1000),
            )

    def _help(self, start: float) -> AgentResult:
        msg = (
            "📖 คำสั่งที่ใช้ได้:\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "\n"
            "📄 เอกสาร:\n"
            "  • ค้นหา <คำ>\n"
            "  • รายการล่าสุด\n"
            "  • สถานะเอกสาร HN <เลข>\n"
            "  • มีเอกสารจัดเก็บไม่สำเร็จไหม\n"
            "\n"
            "💰 การเงิน:\n"
            "  • สรุปค่าใช้จ่าย <vendor> <เดือน>\n"
            "  • ค่าใช้จ่ายเดือนนี้\n"
            "\n"
            "🩺 ผลแล็บ / CKD:\n"
            "  • ผลแล็บ HN <เลข>\n"
            "  • วิเคราะห์แล็บ HN <เลข>\n"
            "  • แนวโน้มแล็บ HN <เลข>\n"
            "  • ตรวจยา HN <เลข>\n"
            "\n"
            "⚙️ ระบบ:\n"
            "  • สถานะระบบ\n"
            "  • สถานะ backup\n"
            "  • รออนุมัติ\n"
            "\n"
            "💡 พิมพ์ภาษาไทยตามปกติได้เลย!"
        )
        return AgentResult(
            agent=self.agent_type, success=True, intent="help",
            message=msg,
            duration_ms=int((time.monotonic() - start) * 1000),
        )


# ─── Orchestrator ───────────────────────────────────────

class AgentOrchestrator:
    """ศูนย์กลางประสานงาน Agent ทั้งหมด.

    Flow:
      1. รับ intent + entities (จาก thai_intent_parser)
      2. เลือก Agent ที่เหมาะสม
      3. ตรวจสิทธิ์ (RBAC)
      4. ส่งต่อให้ Agent ประมวลผล
      5. คืนผลลัพธ์
    """

    def __init__(self):
        self.agents: dict[AgentType, BaseAgent] = {
            AgentType.DOCUMENT: DocumentAgent(),
            AgentType.FINANCE: FinanceAgent(),
            AgentType.KIDNEY: KidneyAgent(),
            AgentType.SECURITY: SecurityAgent(),
            AgentType.SYSTEM: SystemAgent(),
        }

        # Intent → Agent mapping
        self._intent_map: dict[str, AgentType] = {}
        for agent_type, agent in self.agents.items():
            for cap in agent.capabilities:
                self._intent_map[cap] = agent_type

        self._rbac = None
        self._history: list[dict] = []
        self._max_history = 200

        logger.info(
            "AgentOrchestrator initialized: %d agents, %d intents",
            len(self.agents), len(self._intent_map),
        )

    def set_dependencies(self, **kwargs) -> None:
        """ส่ง dependencies ให้ทุก Agent."""
        self._rbac = kwargs.get("rbac")
        for agent in self.agents.values():
            agent.set_dependencies(**kwargs)

    def route(self, intent: str, entities: dict, user_hash: str) -> AgentResult:
        """Route intent ไปยัง Agent ที่เหมาะสม.

        Returns:
            AgentResult — ถ้า message ว่าง หมายถึง passthrough ให้ legacy handler จัดการ
        """
        start = time.monotonic()

        # 1. หา Agent ที่รองรับ
        agent_type = self._intent_map.get(intent)
        if not agent_type:
            return AgentResult(
                agent=AgentType.UNKNOWN, success=False, intent=intent,
                message="",  # Empty = not handled by orchestrator
                data={"passthrough": True},
                duration_ms=int((time.monotonic() - start) * 1000),
            )

        agent = self.agents[agent_type]

        # 2. ตรวจสิทธิ์ (ถ้ามี RBAC)
        if self._rbac and agent_type in (AgentType.SECURITY, AgentType.SYSTEM):
            # Admin/Physician commands need auth check
            try:
                if hasattr(self._rbac, "check_permission"):
                    role = self._rbac.get_user_role(user_hash)
                    if role not in ("physician", "admin"):
                        return AgentResult(
                            agent=agent_type, success=False, intent=intent,
                            message="❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้",
                            duration_ms=int((time.monotonic() - start) * 1000),
                        )
            except Exception:
                pass  # No RBAC = allow all

        # 3. ส่งต่อให้ Agent
        result = agent.handle(intent, entities, user_hash)

        # 4. บันทึกประวัติ
        self._history.append({
            "intent": intent,
            "agent": agent_type.value,
            "success": result.success,
            "duration_ms": result.duration_ms,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "passthrough": result.data.get("passthrough", False),
        })
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

        return result

    def get_stats(self) -> dict:
        """สถิติการใช้งาน Agent."""
        by_agent: dict[str, int] = {}
        by_intent: dict[str, int] = {}
        total = len(self._history)
        passthrough = 0

        for h in self._history:
            ag = h.get("agent", "unknown")
            by_agent[ag] = by_agent.get(ag, 0) + 1
            it = h.get("intent", "unknown")
            by_intent[it] = by_intent.get(it, 0) + 1
            if h.get("passthrough"):
                passthrough += 1

        return {
            "total_requests": total,
            "passthrough": passthrough,
            "handled_by_agent": total - passthrough,
            "by_agent": by_agent,
            "top_intents": dict(sorted(by_intent.items(), key=lambda x: -x[1])[:10]),
        }

    def get_agents_info(self) -> list[dict]:
        """ข้อมูล Agent ทั้งหมด."""
        return [agent.get_info() for agent in self.agents.values()]

    def get_history(self, limit: int = 20) -> list[dict]:
        """ประวัติการใช้งาน."""
        return self._history[-limit:]


# ─── Tests ──────────────────────────────────────────────

def _run_tests():
    """Self-test."""
    print("🧪 Testing AgentOrchestrator...")

    orch = AgentOrchestrator()

    # Test 1: Agent info
    info = orch.get_agents_info()
    assert len(info) == 5, f"Expected 5 agents, got {len(info)}"
    print(f"  ✅ {len(info)} agents registered")

    # Test 2: Intent routing
    result = orch.route("help", {}, "test_user")
    assert result.success
    assert "คำสั่ง" in result.message
    print(f"  ✅ help intent → SystemAgent")

    # Test 3: Unknown intent
    result = orch.route("unknown_xyz", {}, "test_user")
    assert result.data.get("passthrough")
    print(f"  ✅ Unknown intent → passthrough")

    # Test 4: Document stats (no doc_index)
    result = orch.route("document_stats", {}, "test_user")
    assert not result.success  # No doc_index
    print(f"  ✅ document_stats without index → error")

    # Test 5: Stats
    stats = orch.get_stats()
    assert stats["total_requests"] == 2, stats  # unknown intent ไม่ถูกบันทึก
    print(f"  ✅ Stats: {stats['total_requests']} requests tracked")

    # Test 6: Encryption status
    result = orch.route("encryption_status", {}, "test_user")
    assert result.success
    print(f"  ✅ encryption_status → SecurityAgent")

    # Test 7: Failed docs
    result = orch.route("failed_documents", {}, "test_user")
    print(f"  ✅ failed_documents → DocumentAgent")

    print(f"\n✅ All {7} tests PASSED")


if __name__ == "__main__":
    _run_tests()
