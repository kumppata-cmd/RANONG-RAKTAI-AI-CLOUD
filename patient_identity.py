#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Patient Identity Verification — RANONG-RAKTAI-AI 2.0

แยก LINE account ออกจาก patient identity
ห้าม infer patient identity จาก LINE sender เพียงอย่างเดียว

กฎความปลอดภัย:
  - Internal patient ID แยกจาก LINE userId
  - Explicit document-to-patient association
  - Verification ก่อน longitudinal aggregation
  - ป้องกัน cross-patient mixing
  - Multi-patient submissions by authorized clinic staff
  - Duplicate patient detection
  - Unmatched-document quarantine
  - Manual reconciliation workflow
  - RBAC
  - Audit history for reassignment
"""

from __future__ import annotations

import json
import uuid
import hashlib
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Optional, Any

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# Patient Identity Types
# ═══════════════════════════════════════════════════════════

class PatientStatus(str, Enum):
    """สถานะของ patient record."""
    ACTIVE = "active"
    INACTIVE = "inactive"
    MERGED = "merged"  # ถูก merge กับ record อื่น


class DocumentAssociationStatus(str, Enum):
    """สถานะความเชื่อมโยง document กับ patient."""
    VERIFIED = "verified"        # ยืนยันแล้วว่าเป็นของ patient นี้
    UNVERIFIED = "unverified"    # ยังไม่ยืนยัน
    QUARANTINED = "quarantined"  # ไม่แน่ใจ รอ manual reconciliation
    REASSIGNED = "reassigned"    # ถูกย้ายไปยัง patient อื่น


class SubmitterRole(str, Enum):
    """บทบาทของผู้ส่งเอกสาร."""
    SELF = "self"              # ผู้ป่วยส่งเอง
    CLINIC_STAFF = "clinic_staff"  # เจ้าหน้าที่คลินิกส่งให้
    PHYSICIAN = "physician"    # แพทย์ส่งให้
    CAREGIVER = "caregiver"    # ผู้ดูแลส่งให้


@dataclass
class PatientRecord:
    """ข้อมูล patient สำหรับ identity management.

    ไม่เก็บข้อมูลทางการแพทย์ — เฉพาะ identity metadata
    ไม่เก็บ PII โดยตรง — ใช้ hash
    """
    patient_id: str                  # Internal UUID-based ID
    owner_hash: str                  # SHA-256 hash ของ LINE userId
    display_code: str = ""           # รหัสอ้างอิงสั้น (แสดงให้ผู้ใช้เห็น)
    status: str = PatientStatus.ACTIVE.value
    created_at: str = ""
    created_by: str = ""             # ผู้สร้าง record (owner_hash)
    submitter_role: str = SubmitterRole.SELF.value
    verified: bool = False           # ยืนยัน identity แล้วหรือยัง
    verified_at: str = ""
    verified_by: str = ""
    document_ids: list[str] = field(default_factory=list)
    merged_into: str = ""            # patient_id ที่ถูก merge เข้า
    notes: str = ""
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.patient_id:
            self.patient_id = uuid.uuid4().hex[:16]
        if not self.created_at:
            self.created_at = datetime.now().isoformat(timespec="seconds")
        if not self.display_code:
            self.display_code = f"P-{self.patient_id[:6].upper()}"

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: dict) -> PatientRecord:
        return cls(**{k: v for k, v in data.items()
                      if k in cls.__dataclass_fields__})


@dataclass
class DocumentAssociation:
    """ความเชื่อมโยงระหว่าง document กับ patient.

    ต้อง explicit — ไม่ใช้ LINE sender เป็น patient identity
    """
    association_id: str
    doc_id: str
    patient_id: str
    owner_hash: str                  # ผู้ส่งเอกสาร (อาจเป็น clinic staff)
    status: str = DocumentAssociationStatus.UNVERIFIED.value
    submitter_role: str = SubmitterRole.SELF.value
    created_at: str = ""
    verified_at: str = ""
    verified_by: str = ""
    reassigned_from: str = ""        # patient_id เดิม (ถ้า reassign)
    reassignment_reason: str = ""
    history: list[dict] = field(default_factory=list)

    def __post_init__(self):
        if not self.association_id:
            self.association_id = uuid.uuid4().hex[:12]
        if not self.created_at:
            self.created_at = datetime.now().isoformat(timespec="seconds")

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: dict) -> DocumentAssociation:
        history = data.pop("history", [])
        assoc = cls(**{k: v for k, v in data.items()
                       if k in cls.__dataclass_fields__})
        assoc.history = history
        return assoc


# ═══════════════════════════════════════════════════════════
# Patient Identity Service
# ═══════════════════════════════════════════════════════════

PATIENTS_PREFIX = "_patients/"
DOC_ASSOC_PREFIX = "_doc_associations/"
QUARANTINE_PREFIX = "_quarantine/"


class PatientIdentityService:
    """จัดการ patient identity แยกจาก LINE account.

    Dependencies:
      - storage: StorageLocal/StorageGCS
      - audit_logger: AuditLogger
      - clinic_staff_users: set ของ authorized clinic staff hashes
    """

    def __init__(self, storage, audit_logger,
                 clinic_staff_users: set[str] | None = None,
                 admin_users: set[str] | None = None):
        self._storage = storage
        self._audit = audit_logger
        self._clinic_staff = clinic_staff_users or set()
        self._admin_users = admin_users or set()
        self._patient_cache: dict[str, PatientRecord] = {}

    def _is_admin(self, user_hash: str) -> bool:
        """ตรวจสอบว่าเป็น admin user หรือไม่."""
        return user_hash in self._admin_users or user_hash[:8] in self._admin_users

    def _patient_path(self, patient_id: str) -> str:
        return f"{PATIENTS_PREFIX}{patient_id}.json"

    def _assoc_path(self, doc_id: str) -> str:
        return f"{DOC_ASSOC_PREFIX}{doc_id}.json"

    def _quarantine_path(self, doc_id: str) -> str:
        return f"{QUARANTINE_PREFIX}{doc_id}.json"

    def _save_patient(self, patient: PatientRecord) -> bool:
        try:
            self._storage.put(
                self._patient_path(patient.patient_id),
                patient.to_json().encode("utf-8"),
                content_type="application/json",
            )
            self._patient_cache[patient.patient_id] = patient
            return True
        except Exception as e:
            logger.error(f"Patient save failed: {type(e).__name__}")
            return False

    def _load_patient(self, patient_id: str) -> PatientRecord | None:
        if patient_id in self._patient_cache:
            return self._patient_cache[patient_id]
        data = self._storage.get(self._patient_path(patient_id))
        if data is None:
            return None
        patient = PatientRecord.from_dict(json.loads(data))
        self._patient_cache[patient_id] = patient
        return patient

    def _save_association(self, assoc: DocumentAssociation) -> bool:
        try:
            self._storage.put(
                self._assoc_path(assoc.doc_id),
                assoc.to_json().encode("utf-8"),
                content_type="application/json",
            )
            return True
        except Exception as e:
            logger.error(f"Association save failed: {type(e).__name__}")
            return False

    def _load_association(self, doc_id: str) -> DocumentAssociation | None:
        data = self._storage.get(self._assoc_path(doc_id))
        if data is None:
            return None
        return DocumentAssociation.from_dict(json.loads(data))

    def is_clinic_staff(self, user_hash: str) -> bool:
        return user_hash in self._clinic_staff or user_hash[:8] in self._clinic_staff

    # ─── Patient Registration ─────────────────────────

    def register_patient(self, owner_hash: str,
                          submitter_hash: str,
                          submitter_role: str = SubmitterRole.SELF.value,
                          notes: str = "") -> PatientRecord:
        """ลงทะเบียน patient ใหม่.

        ผู้ป่วยสามารถลงทะเบียนตนเอง หรือ clinic staff ลงทะเบียนให้
        """
        # ตรวจสอบสิทธิ์ clinic staff
        if submitter_role == SubmitterRole.CLINIC_STAFF.value:
            if not self.is_clinic_staff(submitter_hash):
                raise PermissionError("ไม่มีสิทธิ์ลงทะเบียนผู้ป่วย")

        patient = PatientRecord(
            patient_id="",  # auto-generated
            owner_hash=owner_hash[:8],
            created_by=submitter_hash[:8],
            submitter_role=submitter_role,
            notes=notes,
        )

        if self._save_patient(patient):
            self._audit.log("patient_registered", submitter_hash, {
                "patient_id": patient.patient_id[:8],
                "submitter_role": submitter_role,
            })
            return patient

        raise RuntimeError("ไม่สามารถบันทึกข้อมูลผู้ป่วยได้")

    def find_patients_by_owner(self, owner_hash: str) -> list[PatientRecord]:
        """ค้นหา patients ที่เป็นของ owner.

        owner อาจมีหลาย patient records (เช่น clinic staff ส่งให้หลายคน)
        Falls back to GCS scan if cache is empty (e.g. after restart).
        """
        results = []
        owner_short = owner_hash[:8]

        # Try cache first
        for pid, patient in self._patient_cache.items():
            if (patient.owner_hash == owner_short
                    and patient.status == PatientStatus.ACTIVE.value):
                results.append(patient)

        # GCS fallback: if cache had no results, scan storage
        if not results:
            try:
                files = self._storage.list_files(PATIENTS_PREFIX)
                for f in files:
                    data = self._storage.get(f)
                    if data is None:
                        continue
                    try:
                        patient = PatientRecord.from_dict(json.loads(data))
                    except (json.JSONDecodeError, TypeError):
                        logger.warning("Corrupt patient file skipped: %s", f)
                        continue
                    # Populate cache for future lookups
                    self._patient_cache[patient.patient_id] = patient
                    if (patient.owner_hash == owner_short
                            and patient.status == PatientStatus.ACTIVE.value):
                        results.append(patient)
            except Exception as e:
                logger.warning("GCS patient scan failed: %s", type(e).__name__)

        return results

    def check_duplicate(self, owner_hash: str) -> list[PatientRecord]:
        """ตรวจสอบ duplicate patient records."""
        existing = self.find_patients_by_owner(owner_hash)
        return existing

    # ─── Document Association ─────────────────────────

    def associate_document(self, doc_id: str, patient_id: str,
                            submitter_hash: str,
                            submitter_role: str = SubmitterRole.SELF.value,
                            auto_verify: bool = False) -> DocumentAssociation:
        """เชื่อมโยงเอกสารกับ patient.

        ต้อง explicit — ไม่ใช้ LINE sender เป็น patient identity
        auto_verify=True เฉพาะกรณีที่ผู้ป่วยส่งเอกสารของตัวเอง
        """
        # ตรวจสอบว่า patient มีอยู่จริง
        patient = self._load_patient(patient_id)
        if patient is None:
            raise ValueError(f"ไม่พบผู้ป่วย: {patient_id[:8]}")

        # ตรวจสอบสิทธิ์
        if submitter_role == SubmitterRole.CLINIC_STAFF.value:
            if not self.is_clinic_staff(submitter_hash):
                raise PermissionError("ไม่มีสิทธิ์เชื่อมโยงเอกสาร")

        # ตรวจสอบ existing association
        existing = self._load_association(doc_id)
        if existing and existing.status == DocumentAssociationStatus.VERIFIED.value:
            raise ValueError(
                f"เอกสาร {doc_id[:8]} ถูกเชื่อมโยงกับผู้ป่วยอื่นแล้ว"
            )

        status = DocumentAssociationStatus.UNVERIFIED.value
        if auto_verify and submitter_role == SubmitterRole.SELF.value:
            # ผู้ป่วยส่งเอกสารตัวเอง — auto verify
            # (แต่ยังต้อง physician review ผล lab)
            status = DocumentAssociationStatus.VERIFIED.value

        assoc = DocumentAssociation(
            association_id="",
            doc_id=doc_id,
            patient_id=patient_id,
            owner_hash=submitter_hash[:8],
            status=status,
            submitter_role=submitter_role,
        )

        if auto_verify:
            assoc.verified_at = datetime.now().isoformat(timespec="seconds")
            assoc.verified_by = submitter_hash[:8]

        assoc.history.append({
            "action": "created",
            "timestamp": assoc.created_at,
            "actor": submitter_hash[:8],
            "status": status,
        })

        if self._save_association(assoc):
            # เพิ่ม doc_id ลงใน patient record
            if doc_id not in patient.document_ids:
                patient.document_ids.append(doc_id)
                self._save_patient(patient)

            self._audit.log("document_associated", submitter_hash, {
                "doc_id": doc_id[:8],
                "patient_id": patient_id[:8],
                "status": status,
                "submitter_role": submitter_role,
            })
            return assoc

        raise RuntimeError("ไม่สามารถบันทึกความเชื่อมโยงได้")

    def get_association(self, doc_id: str) -> DocumentAssociation | None:
        """ดึงข้อมูล document association."""
        return self._load_association(doc_id)

    def verify_association(self, doc_id: str,
                            verifier_hash: str) -> DocumentAssociation:
        """ยืนยัน document-patient association.

        ต้องทำก่อน longitudinal aggregation
        """
        assoc = self._load_association(doc_id)
        if assoc is None:
            raise ValueError(f"ไม่พบความเชื่อมโยงสำหรับเอกสาร {doc_id[:8]}")

        if assoc.status == DocumentAssociationStatus.VERIFIED.value:
            return assoc  # ยืนยันแล้ว

        assoc.status = DocumentAssociationStatus.VERIFIED.value
        assoc.verified_at = datetime.now().isoformat(timespec="seconds")
        assoc.verified_by = verifier_hash[:8]
        assoc.history.append({
            "action": "verified",
            "timestamp": assoc.verified_at,
            "actor": verifier_hash[:8],
        })

        if self._save_association(assoc):
            self._audit.log("association_verified", verifier_hash, {
                "doc_id": doc_id[:8],
                "patient_id": assoc.patient_id[:8],
            })
            return assoc

        raise RuntimeError("ไม่สามารถบันทึกการยืนยันได้")

    def is_verified_for_aggregation(self, doc_id: str) -> bool:
        """ตรวจสอบว่าเอกสารได้รับการยืนยัน identity แล้ว
        จึงสามารถนำไปรวมกับข้อมูลอื่นได้."""
        assoc = self._load_association(doc_id)
        return (assoc is not None
                and assoc.status == DocumentAssociationStatus.VERIFIED.value)

    # ─── Quarantine ───────────────────────────────────

    def quarantine_document(self, doc_id: str,
                             reason: str,
                             actor_hash: str) -> DocumentAssociation | None:
        """กักเอกสารที่ไม่แน่ใจว่าเป็นของ patient ใด."""
        assoc = self._load_association(doc_id)
        if assoc is None:
            # สร้าง association ใหม่เป็น quarantined
            assoc = DocumentAssociation(
                association_id="",
                doc_id=doc_id,
                patient_id="",  # ยังไม่ทราบ
                owner_hash=actor_hash[:8],
                status=DocumentAssociationStatus.QUARANTINED.value,
            )
        else:
            assoc.status = DocumentAssociationStatus.QUARANTINED.value

        assoc.history.append({
            "action": "quarantined",
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "actor": actor_hash[:8],
            "reason": reason,
        })

        if self._save_association(assoc):
            # Save to quarantine index
            self._storage.put(
                self._quarantine_path(doc_id),
                json.dumps({
                    "doc_id": doc_id,
                    "reason": reason,
                    "quarantined_at": datetime.now().isoformat(timespec="seconds"),
                    "quarantined_by": actor_hash[:8],
                }, ensure_ascii=False).encode("utf-8"),
                content_type="application/json",
            )

            self._audit.log("document_quarantined", actor_hash, {
                "doc_id": doc_id[:8],
                "reason": reason,
            })
            return assoc

        return None

    # ─── Reconciliation ──────────────────────────────

    def reassign_document(self, doc_id: str,
                           new_patient_id: str,
                           actor_hash: str,
                           reason: str,
                           requesting_user_hash: str | None = None) -> DocumentAssociation:
        """ย้ายเอกสารไปยัง patient อื่น (manual reconciliation).

        ต้องมี reason — audit trail
        ต้องเป็น admin เท่านั้น (security-critical)
        """
        # RBAC: admin-only operation
        rbac_hash = requesting_user_hash or actor_hash
        if not self._is_admin(rbac_hash):
            raise PermissionError("เฉพาะ admin เท่านั้นที่สามารถย้ายเอกสารได้")

        if not reason:
            raise ValueError("ต้องระบุเหตุผลในการย้ายเอกสาร")

        # ตรวจสอบ new patient
        new_patient = self._load_patient(new_patient_id)
        if new_patient is None:
            raise ValueError(f"ไม่พบผู้ป่วยปลายทาง: {new_patient_id[:8]}")

        assoc = self._load_association(doc_id)
        if assoc is None:
            raise ValueError(f"ไม่พบความเชื่อมโยงสำหรับเอกสาร {doc_id[:8]}")

        # Block unverified doc re-association — quarantine instead
        if assoc.status != DocumentAssociationStatus.VERIFIED.value:
            logger.warning(
                "Blocked reassignment of unverified document — quarantining instead"
            )
            self.quarantine_document(
                doc_id,
                reason=f"Blocked reassignment (unverified): {reason}",
                actor_hash=actor_hash,
            )
            raise ValueError(
                "ไม่สามารถย้ายเอกสารที่ยังไม่ได้ยืนยัน — เอกสารถูกกักไว้แล้ว"
            )

        old_patient_id = assoc.patient_id

        # Remove from old patient
        if old_patient_id:
            old_patient = self._load_patient(old_patient_id)
            if old_patient and doc_id in old_patient.document_ids:
                old_patient.document_ids.remove(doc_id)
                self._save_patient(old_patient)

        # Update association
        assoc.reassigned_from = old_patient_id
        assoc.reassignment_reason = reason
        assoc.patient_id = new_patient_id
        assoc.status = DocumentAssociationStatus.REASSIGNED.value
        assoc.history.append({
            "action": "reassigned",
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "actor": actor_hash[:8],
            "from_patient": old_patient_id[:8] if old_patient_id else "",
            "to_patient": new_patient_id[:8],
            "reason": reason,
        })

        # Add to new patient
        if doc_id not in new_patient.document_ids:
            new_patient.document_ids.append(doc_id)
            self._save_patient(new_patient)

        if self._save_association(assoc):
            self._audit.log("document_reassigned", actor_hash, {
                "doc_id": doc_id[:8],
                "from_patient": old_patient_id[:8] if old_patient_id else "",
                "to_patient": new_patient_id[:8],
                "reason": reason,
            })
            return assoc

        raise RuntimeError("ไม่สามารถบันทึกการย้ายเอกสารได้")

    def merge_patients(self, source_id: str, target_id: str,
                        actor_hash: str,
                        reason: str,
                        requesting_user_hash: str | None = None) -> PatientRecord:
        """Merge patient record เข้าด้วยกัน.

        ย้ายเอกสารทั้งหมดจาก source ไป target
        Atomic: save target ก่อน แล้วค่อย clear source
        ต้องเป็น admin เท่านั้น (security-critical)
        """
        # RBAC: admin-only operation
        rbac_hash = requesting_user_hash or actor_hash
        if not self._is_admin(rbac_hash):
            raise PermissionError("เฉพาะ admin เท่านั้นที่สามารถ merge ผู้ป่วยได้")

        if not reason:
            raise ValueError("ต้องระบุเหตุผลในการ merge")

        source = self._load_patient(source_id)
        target = self._load_patient(target_id)
        if source is None:
            raise ValueError(f"ไม่พบ patient ต้นทาง: {source_id[:8]}")
        if target is None:
            raise ValueError(f"ไม่พบ patient ปลายทาง: {target_id[:8]}")

        # Move all documents
        for doc_id in source.document_ids:
            if doc_id not in target.document_ids:
                target.document_ids.append(doc_id)
                # Update association
                assoc = self._load_association(doc_id)
                if assoc:
                    assoc.patient_id = target_id
                    assoc.history.append({
                        "action": "merged",
                        "timestamp": datetime.now().isoformat(timespec="seconds"),
                        "actor": actor_hash[:8],
                        "from_patient": source_id[:8],
                        "to_patient": target_id[:8],
                    })
                    self._save_association(assoc)

        # Atomic: save target FIRST, then clear source only on success
        if not self._save_patient(target):
            raise RuntimeError("ไม่สามารถบันทึก patient ปลายทางได้ — merge ถูกยกเลิก")

        # Target saved successfully — now safe to clear source
        source.status = PatientStatus.MERGED.value
        source.merged_into = target_id
        source.document_ids = []  # cleared
        if not self._save_patient(source):
            logger.error(
                "Source patient save failed after target save succeeded — "
                "source may still appear active"
            )

        self._audit.log("patients_merged", actor_hash, {
            "source_patient": source_id[:8],
            "target_patient": target_id[:8],
            "documents_moved": len(target.document_ids),
            "reason": reason,
        })

        return target

    # ─── Query ────────────────────────────────────────

    def get_patient(self, patient_id: str) -> PatientRecord | None:
        return self._load_patient(patient_id)

    def get_patient_documents(self, patient_id: str) -> list[str]:
        """ดึง document IDs ที่เชื่อมโยงกับ patient.

        เฉพาะที่ verified แล้วเท่านั้น สำหรับ longitudinal aggregation
        """
        patient = self._load_patient(patient_id)
        if patient is None:
            return []

        verified_docs = []
        for doc_id in patient.document_ids:
            if self.is_verified_for_aggregation(doc_id):
                verified_docs.append(doc_id)
        return verified_docs

    def get_quarantined_documents(self) -> list[dict]:
        """แสดงเอกสารที่ถูก quarantine."""
        quarantined = []
        try:
            files = self._storage.list_files(QUARANTINE_PREFIX)
            for f in files:
                try:
                    data = self._storage.get(f)
                    if data:
                        quarantined.append(json.loads(data))
                except json.JSONDecodeError:
                    logger.warning("Corrupt quarantine file skipped: %s", f)
                except Exception as e:
                    logger.warning(
                        "Failed to read quarantine file %s: %s", f, type(e).__name__
                    )
        except Exception as e:
            logger.warning("Failed to list quarantine files: %s", type(e).__name__)
        return quarantined


def format_patient_info(patient: PatientRecord) -> str:
    """สร้างข้อความแสดงข้อมูล patient สำหรับ LINE."""
    status_icon = "🟢" if patient.status == PatientStatus.ACTIVE.value else "⚪"
    verified_icon = "✅" if patient.verified else "⏳"

    return (
        f"{status_icon} ผู้ป่วย: {patient.display_code}\n"
        f"สถานะ: {patient.status}\n"
        f"Identity: {verified_icon}\n"
        f"เอกสาร: {len(patient.document_ids)} รายการ\n"
        f"ลงทะเบียน: {patient.created_at[:10]}"
    )


def format_quarantine_list(docs: list[dict]) -> str:
    """สร้างข้อความแสดงเอกสารที่ถูก quarantine."""
    if not docs:
        return "✅ ไม่มีเอกสารที่รอการตรวจสอบ"

    lines = [f"⚠️ เอกสาร Quarantine ({len(docs)} รายการ)\n"]
    for d in docs:
        lines.append(
            f"📄 {d.get('doc_id', '')[:8]} — "
            f"{d.get('reason', 'ไม่ทราบเหตุผล')}"
        )
    return "\n".join(lines)
