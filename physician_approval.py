#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Physician Approval Workflow — RANONG-RAKTAI-AI 2.0

ระบบตรวจสอบและยืนยันผลแล็บโดยแพทย์

กฎความปลอดภัย:
  - เฉพาะ PHYSICIAN_USERS เท่านั้นที่ approve/reject/correct ได้
  - ADMIN_USERS ไม่ใช่ Physician โดยอัตโนมัติ
  - LINE command ไม่ให้สิทธิ์ Physician
  - ต้องมี review notes สำหรับ rejection หรือ material corrections
  - เก็บ immutable review history ทุก action
  - ใช้ optimistic concurrency (version check) ป้องกัน concurrent conflicts
  - Low-confidence results ห้าม auto-approve
  - Preserve original extracted results เสมอ
"""

from __future__ import annotations

import json
import hashlib
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Optional, Any

from lab_models import LabReport, ReviewStatus, LabResult
from audit_trail import AuditPersistenceError


# ═══════════════════════════════════════════════════════════
# Physician Role — แยกจาก ADMIN_USERS
# ═══════════════════════════════════════════════════════════

class PhysicianRole(str, Enum):
    """บทบาทของ Physician."""
    PHYSICIAN = "physician"      # แพทย์ — approve/reject/correct ได้
    ADMIN = "admin"              # ผู้ดูแลระบบ — จัดการ enrollment, ดูรายงาน
    CLINIC_STAFF = "clinic_staff"  # เจ้าหน้าที่คลินิก — ส่งเอกสาร


class ReviewAction(str, Enum):
    """ประเภท action ใน review history."""
    SUBMITTED = "submitted"       # ผลแล็บถูกส่งเข้ามา
    APPROVED = "approved"         # แพทย์ยืนยันผลแล็บ
    REJECTED = "rejected"         # แพทย์ปฏิเสธผลแล็บ
    CORRECTED = "corrected"       # แพทย์แก้ไขค่า OCR
    CORRECTION_APPROVED = "correction_approved"  # แพทย์ approve หลังแก้ไข


@dataclass
class ReviewHistoryEntry:
    """รายการประวัติการตรวจสอบ — immutable.

    แต่ละ entry เป็น append-only, ไม่สามารถแก้ไขหรือลบได้
    """
    event_id: str                   # UUID ของ event
    action: str                     # ReviewAction value
    actor_hash: str                 # hash ของผู้ทำ action (8 chars)
    timestamp: str                  # ISO timestamp
    notes: str = ""                 # หมายเหตุ
    corrections: dict = field(default_factory=dict)  # ค่าที่แก้ไข (ถ้ามี)
    previous_status: str = ""       # สถานะก่อนหน้า
    new_status: str = ""            # สถานะใหม่

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> ReviewHistoryEntry:
        return cls(
            event_id=data.get("event_id", ""),
            action=data.get("action", ""),
            actor_hash=data.get("actor_hash", ""),
            timestamp=data.get("timestamp", ""),
            notes=data.get("notes", ""),
            corrections=data.get("corrections", {}),
            previous_status=data.get("previous_status", ""),
            new_status=data.get("new_status", ""),
        )


@dataclass
class EnhancedLabReport:
    """LabReport ที่มี review history และ original results.

    เพิ่มจาก LabReport เดิม:
      - review_history: ประวัติการตรวจสอบ immutable
      - original_results: ผลตรวจต้นฉบับ (ก่อน correction)
      - version: สำหรับ optimistic concurrency
      - corrected_results: ผลตรวจที่แก้ไขแล้ว (ถ้ามี)
      - confidence_flags: สรุป flags จาก extraction
    """
    report: LabReport
    review_history: list[ReviewHistoryEntry] = field(default_factory=list)
    original_results: list[dict] = field(default_factory=list)
    corrected_results: list[dict] = field(default_factory=list)
    version: int = 1  # increment ทุกครั้งที่มีการเปลี่ยนแปลง
    has_corrections: bool = False
    confidence_flags: list[str] = field(default_factory=list)

    def __post_init__(self):
        # ถ้ายังไม่มี original_results ให้เก็บจาก report.results
        if not self.original_results and self.report.results:
            self.original_results = [r.to_dict() for r in self.report.results]
        # สรุป confidence flags
        if not self.confidence_flags:
            self._compute_confidence_flags()

    def _compute_confidence_flags(self):
        """สรุป flag ที่เกี่ยวกับ confidence จากผลตรวจ."""
        flags = set()
        for r in self.report.results:
            if r.confidence < 0.7:
                flags.add("low_confidence")
            if r.confidence < 0.5:
                flags.add("very_low_confidence")
            for f in r.flags:
                if f in ("low_confidence", "unit_mismatch",
                         "physiologic_impossible"):
                    flags.add(f)
        self.confidence_flags = sorted(flags)

    def has_low_confidence(self) -> bool:
        """มีผลตรวจ confidence ต่ำหรือไม่."""
        return any(
            f in ("low_confidence", "very_low_confidence")
            for f in self.confidence_flags
        )

    def to_dict(self) -> dict:
        return {
            "report": self.report.to_dict(),
            "review_history": [h.to_dict() for h in self.review_history],
            "original_results": self.original_results,
            "corrected_results": self.corrected_results,
            "version": self.version,
            "has_corrections": self.has_corrections,
            "confidence_flags": self.confidence_flags,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: dict) -> EnhancedLabReport:
        """สร้างจาก dict — รองรับทั้ง format เก่าและใหม่."""
        # ถ้าเป็น format เก่า (LabReport ตรง)
        if "source_document_id" in data and "report" not in data:
            report = LabReport.from_dict(data)
            return cls(report=report)

        # Format ใหม่
        report = LabReport.from_dict(data.get("report", data))
        history = [
            ReviewHistoryEntry.from_dict(h)
            for h in data.get("review_history", [])
        ]
        return cls(
            report=report,
            review_history=history,
            original_results=data.get("original_results", []),
            corrected_results=data.get("corrected_results", []),
            version=data.get("version", 1),
            has_corrections=data.get("has_corrections", False),
            confidence_flags=data.get("confidence_flags", []),
        )

    def add_history(self, entry: ReviewHistoryEntry):
        """เพิ่ม entry ลง review history — append-only."""
        self.review_history.append(entry)
        self.version += 1


# ═══════════════════════════════════════════════════════════
# Physician Approval Service
# ═══════════════════════════════════════════════════════════

class PhysicianApprovalError(Exception):
    """Base exception สำหรับ physician approval."""
    pass


class UnauthorizedPhysicianError(PhysicianApprovalError):
    """ผู้ใช้ไม่ใช่ Physician."""
    pass


class StaleVersionError(PhysicianApprovalError):
    """Version ไม่ตรง — มีคนอื่นแก้ไขแล้ว."""
    pass


class InvalidStateTransitionError(PhysicianApprovalError):
    """State transition ไม่ถูกต้อง."""
    pass


class ReviewNotesRequiredError(PhysicianApprovalError):
    """ต้องมี review notes."""
    pass


class DuplicateApprovalError(PhysicianApprovalError):
    """เอกสารได้รับการ approve แล้ว."""
    pass


# Valid state transitions
VALID_TRANSITIONS = {
    ReviewStatus.PENDING_REVIEW.value: {
        ReviewAction.APPROVED.value,
        ReviewAction.REJECTED.value,
        ReviewAction.CORRECTED.value,
    },
    "corrected": {
        ReviewAction.CORRECTION_APPROVED.value,
        ReviewAction.REJECTED.value,
        ReviewAction.CORRECTED.value,  # แก้ไขซ้ำ
    },
}


class PhysicianApprovalService:
    """Service จัดการ physician approval workflow.

    Dependencies:
      - storage: StorageLocal/StorageGCS instance
      - audit_logger: AuditLogger instance
      - physician_users: set ของ physician user hashes
    """

    def __init__(self, storage, audit_logger, physician_users: set[str],
                 admin_users: set[str]):
        self._storage = storage
        self._audit = audit_logger
        self._physician_users = physician_users
        self._admin_users = admin_users

    def is_physician(self, user_hash: str) -> bool:
        """ตรวจสอบว่าเป็น Physician หรือไม่."""
        return user_hash in self._physician_users

    def is_admin(self, user_hash: str) -> bool:
        """ตรวจสอบว่าเป็น Admin หรือไม่."""
        return user_hash in self._admin_users

    def _load_enhanced_report(self, lab_path: str) -> EnhancedLabReport | None:
        """โหลด EnhancedLabReport จาก storage."""
        data = self._storage.get(lab_path)
        if data is None:
            return None
        parsed = json.loads(data)
        return EnhancedLabReport.from_dict(parsed)

    def _load_enhanced_report_with_generation(
        self, lab_path: str
    ) -> tuple:
        """โหลด EnhancedLabReport พร้อม GCS generation สำหรับ CAS.

        Returns:
            (EnhancedLabReport, generation: int) ถ้าพบ
            (None, None) ถ้าไม่พบ
        """
        if hasattr(self._storage, "get_with_generation"):
            data, generation = self._storage.get_with_generation(lab_path)
        else:
            # fallback สำหรับ storage ที่ไม่รองรับ CAS
            data = self._storage.get(lab_path)
            generation = None
        if data is None:
            return None, None
        parsed = json.loads(data)
        return EnhancedLabReport.from_dict(parsed), generation

    def _save_enhanced_report(self, lab_path: str,
                               enhanced: EnhancedLabReport,
                               expected_generation: int = None) -> bool:
        """บันทึก EnhancedLabReport กลับ storage ด้วย CAS.

        ใช้ GCS if_generation_match เพื่อป้องกัน lost-update race
        ระหว่าง workers ที่ทำงานพร้อมกัน.

        Args:
            lab_path: path ใน storage
            enhanced: report ที่แก้ไขแล้ว
            expected_generation: GCS generation ที่คาดหวัง (จาก load)
                ถ้า None → fallback ไป blind put (backward compat)

        Returns:
            True ถ้าเขียนสำเร็จ

        Raises:
            StaleVersionError: ถ้า worker อื่นเขียนไปก่อนแล้ว (CAS conflict)
        """
        encoded = enhanced.to_json().encode("utf-8")

        if expected_generation is not None and hasattr(
            self._storage, "put_conditional"
        ):
            success, _new_gen = self._storage.put_conditional(
                lab_path,
                encoded,
                if_generation_match=expected_generation,
                content_type="application/json",
            )
            if not success:
                raise StaleVersionError(
                    "ข้อมูลถูกแก้ไขโดย worker อื่น "
                    "(GCS generation conflict) — กรุณาโหลดใหม่แล้วลองอีกครั้ง"
                )
            return True

        # fallback: blind put (storage ไม่รองรับ CAS)
        try:
            self._storage.put(
                lab_path,
                encoded,
                content_type="application/json",
            )
            return True
        except Exception:
            return False

    def _validate_physician(self, user_hash: str):
        """ตรวจสอบ physician authorization."""
        if not self.is_physician(user_hash):
            raise UnauthorizedPhysicianError(
                "เฉพาะแพทย์ที่ได้รับอนุญาตเท่านั้นที่สามารถดำเนินการนี้ได้"
            )

    def _validate_state_transition(self, current_status: str,
                                    action: str):
        """ตรวจสอบว่า state transition ถูกต้อง."""
        valid_actions = VALID_TRANSITIONS.get(current_status, set())
        if action not in valid_actions:
            raise InvalidStateTransitionError(
                f"ไม่สามารถ {action} จากสถานะ {current_status} ได้"
            )

    def _check_version(self, enhanced: EnhancedLabReport,
                       expected_version: int):
        """ตรวจสอบ optimistic concurrency version."""
        if enhanced.version != expected_version:
            raise StaleVersionError(
                f"ข้อมูลได้รับการแก้ไขแล้ว (version ปัจจุบัน: "
                f"{enhanced.version}, ส่งมา: {expected_version})"
            )

    def list_pending_reports(self, doc_index,
                              user_hash: str = "",
                              limit: int = 20) -> list[dict]:
        """แสดงรายการผลแล็บที่รอตรวจสอบ.

        Args:
            doc_index: document index instance
            user_hash: hash ของผู้ใช้ — ต้องเป็น physician
            limit: จำนวนสูงสุดที่แสดง

        Returns list of dicts with doc_id, specimen_date, result_count,
        confidence_flags, status.

        Raises:
            UnauthorizedPhysicianError: ผู้ใช้ไม่ใช่ physician
        """
        if not user_hash or not self.is_physician(user_hash):
            raise UnauthorizedPhysicianError(
                "เฉพาะแพทย์ที่ได้รับอนุญาตเท่านั้นที่สามารถดูรายการรอตรวจสอบได้"
            )

        pending = []
        from server import LAB_RESULTS_PREFIX
        all_docs = doc_index.all() if hasattr(doc_index, 'all') else []

        for doc in all_docs:
            doc_id = doc.get("document_id", "")
            if not doc_id:
                continue
            lab_path = f"{LAB_RESULTS_PREFIX}{doc_id}.json"
            enhanced = self._load_enhanced_report(lab_path)
            if enhanced is None:
                continue
            status = enhanced.report.status
            if status == ReviewStatus.PENDING_REVIEW.value or status == "corrected":
                pending.append({
                    "doc_id": doc_id,
                    "specimen_date": enhanced.report.specimen_date,
                    "result_count": len(enhanced.report.results),
                    "confidence_flags": enhanced.confidence_flags,
                    "status": status,
                    "version": enhanced.version,
                    "has_low_confidence": enhanced.has_low_confidence(),
                })
                if len(pending) >= limit:
                    break

        return pending

    def get_report_for_review(self, lab_path: str,
                               user_hash: str) -> dict:
        """แสดงผลแล็บสำหรับ review โดยแพทย์.

        Returns dict with report details, original/current values,
        confidence info, and version for concurrency control.
        """
        self._validate_physician(user_hash)

        enhanced = self._load_enhanced_report(lab_path)
        if enhanced is None:
            return {"error": "ไม่พบผลแล็บ"}

        # สร้าง review view
        results_view = []
        for i, r in enumerate(enhanced.report.results):
            result_view = {
                "index": i,
                "analyte": r.analyte,
                "value": r.value,
                "unit": r.unit,
                "reference_range": r.reference_range,
                "confidence": r.confidence,
                "flags": r.flags,
                "raw_text": r.raw_text,
            }
            # ถ้ามี correction ให้แสดง original ด้วย
            if enhanced.has_corrections and enhanced.original_results:
                if i < len(enhanced.original_results):
                    orig = enhanced.original_results[i]
                    if orig.get("value") != r.value or orig.get("unit") != r.unit:
                        result_view["original_value"] = orig.get("value")
                        result_view["original_unit"] = orig.get("unit")
            results_view.append(result_view)

        return {
            "doc_id": enhanced.report.source_document_id,
            "specimen_date": enhanced.report.specimen_date,
            "status": enhanced.report.status,
            "version": enhanced.version,
            "extraction_method": enhanced.report.extraction_method,
            "extracted_at": enhanced.report.extracted_at,
            "results": results_view,
            "warnings": enhanced.report.warnings,
            "confidence_flags": enhanced.confidence_flags,
            "has_low_confidence": enhanced.has_low_confidence(),
            "has_corrections": enhanced.has_corrections,
            "review_history": [h.to_dict() for h in enhanced.review_history],
        }

    def approve_report(self, lab_path: str, user_hash: str,
                        expected_version: int,
                        notes: str = "") -> dict:
        """Physician approve ผลแล็บ.

        Args:
            lab_path: path ของ lab report ใน storage
            user_hash: hash ของ physician
            expected_version: version สำหรับ optimistic concurrency
            notes: หมายเหตุ (optional สำหรับ approve)

        Returns dict with status and message.

        Raises:
            UnauthorizedPhysicianError: ไม่ใช่ physician
            StaleVersionError: version ไม่ตรง
            InvalidStateTransitionError: state ไม่ถูกต้อง
            DuplicateApprovalError: ได้รับ approve แล้ว
        """
        self._validate_physician(user_hash)

        enhanced, gcs_generation = self._load_enhanced_report_with_generation(
            lab_path
        )
        if enhanced is None:
            return {"error": "ไม่พบผลแล็บ"}

        current_status = enhanced.report.status

        # ตรวจ duplicate approval
        if current_status == ReviewStatus.APPROVED.value:
            raise DuplicateApprovalError(
                "ผลแล็บได้รับการยืนยันแล้ว"
            )

        # ตรวจ version
        self._check_version(enhanced, expected_version)

        # กำหนด action ตาม current status
        if current_status == "corrected":
            action = ReviewAction.CORRECTION_APPROVED.value
            new_status = ReviewStatus.APPROVED.value
        else:
            action = ReviewAction.APPROVED.value
            new_status = ReviewStatus.APPROVED.value

        self._validate_state_transition(current_status, action)

        # สร้าง history entry
        entry = ReviewHistoryEntry(
            event_id=uuid.uuid4().hex[:16],
            action=action,
            actor_hash=user_hash[:8],
            timestamp=datetime.now().isoformat(timespec="seconds"),
            notes=notes,
            previous_status=current_status,
            new_status=new_status,
        )

        # อัพเดทสถานะ
        enhanced.report.status = new_status
        enhanced.report.reviewed_by = user_hash[:8]
        enhanced.report.reviewed_at = entry.timestamp
        enhanced.report.review_notes = notes
        enhanced.add_history(entry)

        # Audit BEFORE save — security-critical: abort if audit fails
        try:
            self._audit.log("physician_approval", user_hash, {
                "doc_id": enhanced.report.source_document_id[:8],
                "result_count": len(enhanced.report.results),
                "action": action,
                "version": enhanced.version,
            })
        except AuditPersistenceError:
            raise  # ห้าม approve โดยไม่มี audit trail

        # บันทึกด้วย CAS — ป้องกัน lost-update race ข้าม workers
        self._save_enhanced_report(
            lab_path, enhanced, expected_generation=gcs_generation
        )

        return {
            "status": "success",
            "new_status": new_status,
            "version": enhanced.version,
            "message": (
                f"✅ แพทย์ยืนยันผลแล็บ "
                f"{enhanced.report.source_document_id[:8]} สำเร็จ\n"
                f"จำนวนผลตรวจ: {len(enhanced.report.results)} รายการ\n"
                f"สถานะ: {new_status}"
            ),
        }

    def reject_report(self, lab_path: str, user_hash: str,
                       expected_version: int,
                       notes: str) -> dict:
        """Physician reject ผลแล็บ.

        notes เป็น required สำหรับ rejection.
        """
        self._validate_physician(user_hash)

        if not notes or not notes.strip():
            raise ReviewNotesRequiredError(
                "ต้องระบุเหตุผลในการปฏิเสธผลแล็บ"
            )

        enhanced, gcs_generation = self._load_enhanced_report_with_generation(
            lab_path
        )
        if enhanced is None:
            return {"error": "ไม่พบผลแล็บ"}

        current_status = enhanced.report.status
        if current_status == ReviewStatus.APPROVED.value:
            raise InvalidStateTransitionError(
                "ไม่สามารถปฏิเสธผลแล็บที่ได้รับการยืนยันแล้ว"
            )

        self._check_version(enhanced, expected_version)
        self._validate_state_transition(current_status,
                                         ReviewAction.REJECTED.value)

        entry = ReviewHistoryEntry(
            event_id=uuid.uuid4().hex[:16],
            action=ReviewAction.REJECTED.value,
            actor_hash=user_hash[:8],
            timestamp=datetime.now().isoformat(timespec="seconds"),
            notes=notes,
            previous_status=current_status,
            new_status=ReviewStatus.REJECTED.value,
        )

        enhanced.report.status = ReviewStatus.REJECTED.value
        enhanced.report.reviewed_by = user_hash[:8]
        enhanced.report.reviewed_at = entry.timestamp
        enhanced.report.review_notes = notes
        enhanced.add_history(entry)

        # Audit BEFORE save — security-critical: abort if audit fails
        try:
            self._audit.log("physician_rejection", user_hash, {
                "doc_id": enhanced.report.source_document_id[:8],
                "action": ReviewAction.REJECTED.value,
                "version": enhanced.version,
            })
        except AuditPersistenceError:
            raise  # ห้าม reject โดยไม่มี audit trail

        # บันทึกด้วย CAS
        self._save_enhanced_report(
            lab_path, enhanced, expected_generation=gcs_generation
        )

        return {
            "status": "success",
            "new_status": ReviewStatus.REJECTED.value,
            "version": enhanced.version,
            "message": (
                f"❌ ปฏิเสธผลแล็บ "
                f"{enhanced.report.source_document_id[:8]}\n"
                f"เหตุผล: {notes}\n"
                f"สถานะ: rejected"
            ),
        }

    def correct_and_approve(self, lab_path: str, user_hash: str,
                             expected_version: int,
                             corrections: dict,
                             notes: str) -> dict:
        """Physician แก้ไข OCR errors แล้ว approve.

        corrections: dict mapping result index to corrected values
            e.g. {"0": {"value": 1.5}, "2": {"value": 4.2, "unit": "mg/dL"}}

        notes เป็น required สำหรับ material corrections.
        """
        self._validate_physician(user_hash)

        if not corrections:
            return {"error": "ไม่มีค่าที่ต้องแก้ไข"}

        if not notes or not notes.strip():
            raise ReviewNotesRequiredError(
                "ต้องระบุหมายเหตุเมื่อมีการแก้ไขค่า"
            )

        enhanced, gcs_generation = self._load_enhanced_report_with_generation(
            lab_path
        )
        if enhanced is None:
            return {"error": "ไม่พบผลแล็บ"}

        current_status = enhanced.report.status
        if current_status == ReviewStatus.APPROVED.value:
            raise InvalidStateTransitionError(
                "ไม่สามารถแก้ไขผลแล็บที่ได้รับการยืนยันแล้ว"
            )

        self._check_version(enhanced, expected_version)
        self._validate_state_transition(current_status,
                                         ReviewAction.CORRECTED.value)

        # เก็บ original results ก่อนแก้ไข (ถ้ายังไม่มี)
        if not enhanced.original_results:
            enhanced.original_results = [
                r.to_dict() for r in enhanced.report.results
            ]

        # Apply corrections
        correction_details = {}
        for idx_str, changes in corrections.items():
            try:
                idx = int(idx_str)
            except (ValueError, TypeError):
                continue
            if idx < 0 or idx >= len(enhanced.report.results):
                continue

            result = enhanced.report.results[idx]
            old_values = {}

            if "value" in changes:
                old_values["value"] = result.value
                try:
                    result.value = float(changes["value"])
                except (ValueError, TypeError):
                    continue

            if "unit" in changes:
                old_values["unit"] = result.unit
                result.unit = str(changes["unit"])

            if old_values:
                correction_details[idx_str] = {
                    "analyte": result.analyte,
                    "old": old_values,
                    "new": {k: getattr(result, k) for k in old_values},
                }

        if not correction_details:
            return {"error": "ไม่สามารถใช้การแก้ไขได้"}

        # บันทึก corrected results
        enhanced.corrected_results = [
            r.to_dict() for r in enhanced.report.results
        ]
        enhanced.has_corrections = True

        # สร้าง correction history
        correction_entry = ReviewHistoryEntry(
            event_id=uuid.uuid4().hex[:16],
            action=ReviewAction.CORRECTED.value,
            actor_hash=user_hash[:8],
            timestamp=datetime.now().isoformat(timespec="seconds"),
            notes=notes,
            corrections=correction_details,
            previous_status=current_status,
            new_status="corrected",
        )
        enhanced.add_history(correction_entry)

        # Auto-approve after correction
        approve_entry = ReviewHistoryEntry(
            event_id=uuid.uuid4().hex[:16],
            action=ReviewAction.CORRECTION_APPROVED.value,
            actor_hash=user_hash[:8],
            timestamp=datetime.now().isoformat(timespec="seconds"),
            notes=f"ยืนยันหลังแก้ไข: {notes}",
            previous_status="corrected",
            new_status=ReviewStatus.APPROVED.value,
        )

        enhanced.report.status = ReviewStatus.APPROVED.value
        enhanced.report.reviewed_by = user_hash[:8]
        enhanced.report.reviewed_at = approve_entry.timestamp
        enhanced.report.review_notes = notes
        enhanced.add_history(approve_entry)

        # Audit BEFORE save — security-critical: abort if audit fails
        try:
            self._audit.log("physician_correction", user_hash, {
                "doc_id": enhanced.report.source_document_id[:8],
                "corrections": list(correction_details.keys()),
                "version": enhanced.version,
            })
        except AuditPersistenceError:
            raise  # ห้าม correct โดยไม่มี audit trail

        # บันทึกด้วย CAS
        self._save_enhanced_report(
            lab_path, enhanced, expected_generation=gcs_generation
        )

        # สร้าง summary ของการแก้ไข
        correction_summary = []
        for idx_str, detail in correction_details.items():
            old = detail["old"]
            new = detail["new"]
            parts = []
            if "value" in old:
                parts.append(f"{old['value']}→{new['value']}")
            if "unit" in old:
                parts.append(f"{old['unit']}→{new['unit']}")
            correction_summary.append(
                f"  {detail['analyte']}: {', '.join(parts)}"
            )

        return {
            "status": "success",
            "new_status": ReviewStatus.APPROVED.value,
            "version": enhanced.version,
            "corrections_applied": len(correction_details),
            "message": (
                f"✅ แพทย์แก้ไขและยืนยันผลแล็บ "
                f"{enhanced.report.source_document_id[:8]} สำเร็จ\n"
                f"แก้ไข {len(correction_details)} รายการ:\n"
                + "\n".join(correction_summary) + "\n"
                f"หมายเหตุ: {notes}\n"
                f"สถานะ: approved (หลังแก้ไข)"
            ),
        }

    def get_review_history(self, lab_path: str,
                            user_hash: str) -> list[dict]:
        """ดูประวัติการตรวจสอบ (physician/admin only)."""
        if not self.is_physician(user_hash) and not self.is_admin(user_hash):
            return []

        enhanced = self._load_enhanced_report(lab_path)
        if enhanced is None:
            return []

        return [h.to_dict() for h in enhanced.review_history]


def wrap_existing_report(lab_data: bytes) -> EnhancedLabReport:
    """แปลง LabReport format เก่า เป็น EnhancedLabReport.

    ใช้สำหรับ backward compatibility กับข้อมูลที่มีอยู่
    """
    parsed = json.loads(lab_data)
    return EnhancedLabReport.from_dict(parsed)


def format_pending_list(pending_reports: list[dict]) -> str:
    """สร้างข้อความแสดง pending reports สำหรับ LINE."""
    if not pending_reports:
        return "✅ ไม่มีผลแล็บที่รอตรวจสอบ"

    lines = [f"📋 ผลแล็บรอตรวจสอบ ({len(pending_reports)} รายการ)\n"]
    for i, r in enumerate(pending_reports, 1):
        doc_short = r["doc_id"][:8]
        confidence_warning = " ⚠️ OCR ต่ำ" if r.get("has_low_confidence") else ""
        status_label = "แก้ไขแล้ว" if r["status"] == "corrected" else "รอตรวจ"
        lines.append(
            f"{i}. [{doc_short}] {r['specimen_date']} "
            f"({r['result_count']} รายการ) "
            f"[{status_label}]{confidence_warning}"
        )

    lines.append("\nใช้คำสั่ง: ตรวจสอบแล็บ [รหัสเอกสาร]")
    return "\n".join(lines)


def format_review_detail(review_data: dict) -> str:
    """สร้างข้อความแสดงรายละเอียดผลแล็บสำหรับ review."""
    if "error" in review_data:
        return review_data["error"]

    lines = []
    doc_short = review_data["doc_id"][:8]
    lines.append(f"🔬 ผลแล็บ [{doc_short}]")
    lines.append(f"📅 วันที่: {review_data['specimen_date']}")
    lines.append(f"🔍 สถานะ: {review_data['status']}")
    lines.append(f"📝 Version: {review_data['version']}")

    if review_data.get("has_low_confidence"):
        lines.append("⚠️ มีผลตรวจ OCR ความมั่นใจต่ำ — ต้องตรวจสอบเอกสารต้นฉบับ")

    lines.append("")

    for r in review_data.get("results", []):
        flag_str = ""
        if r.get("flags"):
            flag_icons = []
            for f in r["flags"]:
                if "critical" in f:
                    flag_icons.append("🔴")
                elif "abnormal" in f:
                    flag_icons.append("🟡")
                elif "impossible" in f or "mismatch" in f:
                    flag_icons.append("⚠️")
                elif "low_confidence" in f:
                    flag_icons.append("❓")
                else:
                    flag_icons.append("ℹ️")
            flag_str = " " + "".join(flag_icons)

        confidence_str = f" [conf:{r['confidence']:.0%}]" if r.get("confidence", 1.0) < 0.9 else ""

        line = (
            f"  {r['analyte']}: {r['value']} {r['unit']} "
            f"(ref: {r['reference_range']}){flag_str}{confidence_str}"
        )

        # แสดง original value ถ้ามีการแก้ไข
        if "original_value" in r:
            line += f"\n    ↳ ค่าเดิม: {r['original_value']}"
            if "original_unit" in r:
                line += f" {r['original_unit']}"

        lines.append(line)

    if review_data.get("warnings"):
        lines.append("\n⚠️ คำเตือน:")
        for w in review_data["warnings"]:
            lines.append(f"  - {w}")

    lines.append("")
    lines.append("คำสั่งที่ใช้ได้:")
    lines.append(f"  ยืนยันแล็บ {doc_short}")
    lines.append(f"  ปฏิเสธแล็บ {doc_short} [เหตุผล]")
    lines.append(f"  แก้ไขแล็บ {doc_short} [index] [ค่าใหม่]")

    return "\n".join(lines)
