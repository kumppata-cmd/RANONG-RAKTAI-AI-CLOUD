"""RBAC v2 — Role-Based Access Control — V4.0 Phase 3.2

Fine-grained permission system แทนที่ 3-tier hash check เดิม

Roles:
- patient: ดูเอกสารตัวเอง, ลงทะเบียน HN
- nurse: ดูเอกสารผู้ป่วย, ส่งเอกสาร, เลือก HN
- physician: ทั้งหมดของ nurse + อนุมัติผลแล็บ + เขียน notes + ค้นหา HN
- admin: ทั้งหมด + จัดการระบบ + vendor registry + alert config

Permissions:
- document:read, document:write, document:delete
- lab:read, lab:approve, lab:analyze
- patient:read, patient:register
- system:config, system:admin, system:backup
- approval:approve, approval:reject
- google:sync, google:backup

ใช้ ENV-based hash sets เดิมเป็น seed → migration ไม่กระทบระบบเดิม
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


# ─── Permissions ─────────────────────────────────────────


class Permission(str, Enum):
    # Document
    DOC_READ = "document:read"
    DOC_WRITE = "document:write"
    DOC_DELETE = "document:delete"
    DOC_SEARCH = "document:search"

    # Lab
    LAB_READ = "lab:read"
    LAB_APPROVE = "lab:approve"
    LAB_ANALYZE = "lab:analyze"

    # Patient
    PATIENT_READ = "patient:read"
    PATIENT_REGISTER = "patient:register"
    PATIENT_SELECT = "patient:select"

    # Clinical
    CLINICAL_NOTES = "clinical:notes"
    CLINICAL_KPI = "clinical:kpi"
    MEDICATION_REVIEW = "medication:review"

    # System
    SYSTEM_CONFIG = "system:config"
    SYSTEM_ADMIN = "system:admin"
    SYSTEM_BACKUP = "system:backup"
    SYSTEM_MONITOR = "system:monitor"

    # Approval
    APPROVAL_REQUEST = "approval:request"
    APPROVAL_APPROVE = "approval:approve"

    # Google
    GOOGLE_SYNC = "google:sync"
    GOOGLE_BACKUP = "google:backup"

    # Finance
    FINANCE_VIEW = "finance:view"
    FINANCE_MANAGE = "finance:manage"


# ─── Role Definitions ───────────────────────────────────


class Role(str, Enum):
    PATIENT = "patient"
    NURSE = "nurse"
    PHYSICIAN = "physician"
    ADMIN = "admin"


ROLE_PERMISSIONS: dict[Role, set[Permission]] = {
    Role.PATIENT: {
        Permission.DOC_READ,
        Permission.LAB_READ,
        Permission.PATIENT_REGISTER,
    },
    Role.NURSE: {
        Permission.DOC_READ,
        Permission.DOC_WRITE,
        Permission.DOC_SEARCH,
        Permission.LAB_READ,
        Permission.PATIENT_READ,
        Permission.PATIENT_REGISTER,
        Permission.PATIENT_SELECT,
        Permission.FINANCE_VIEW,
        Permission.APPROVAL_REQUEST,
    },
    Role.PHYSICIAN: {
        Permission.DOC_READ,
        Permission.DOC_WRITE,
        Permission.DOC_SEARCH,
        Permission.LAB_READ,
        Permission.LAB_APPROVE,
        Permission.LAB_ANALYZE,
        Permission.PATIENT_READ,
        Permission.PATIENT_REGISTER,
        Permission.PATIENT_SELECT,
        Permission.CLINICAL_NOTES,
        Permission.CLINICAL_KPI,
        Permission.MEDICATION_REVIEW,
        Permission.APPROVAL_REQUEST,
        Permission.APPROVAL_APPROVE,
        Permission.GOOGLE_SYNC,
        Permission.GOOGLE_BACKUP,
        Permission.FINANCE_VIEW,
        Permission.SYSTEM_MONITOR,
    },
    Role.ADMIN: set(Permission),  # ทุก permission
}


# ─── RBAC Engine ─────────────────────────────────────────


class RBACEngine:
    """Role-Based Access Control Engine.

    Backward compatible กับ ENV-based hash sets เดิม:
    - AUTHORIZED_USERS → Role.NURSE
    - PHYSICIAN_USERS → Role.PHYSICIAN
    - ADMIN_USERS → Role.ADMIN
    - ไม่อยู่ใน set ไหน → Role.PATIENT
    """

    def __init__(
        self,
        authorized_users: set[str] | None = None,
        physician_users: set[str] | None = None,
        admin_users: set[str] | None = None,
    ):
        self._authorized = authorized_users or set()
        self._physicians = physician_users or set()
        self._admins = admin_users or set()

        # Custom role overrides (ถ้าต้องการกำหนดเอง)
        self._custom_roles: dict[str, Role] = {}
        # Custom permission overrides
        self._extra_permissions: dict[str, set[Permission]] = {}

    def get_role(self, user_hash: str) -> Role:
        """ดึง role ของผู้ใช้."""
        # Custom override first
        if user_hash in self._custom_roles:
            return self._custom_roles[user_hash]

        # ENV-based (backward compatible)
        if user_hash in self._admins:
            return Role.ADMIN
        if user_hash in self._physicians:
            return Role.PHYSICIAN
        if user_hash in self._authorized:
            return Role.NURSE
        return Role.PATIENT

    def get_permissions(self, user_hash: str) -> set[Permission]:
        """ดึง permissions ทั้งหมดของผู้ใช้."""
        role = self.get_role(user_hash)
        perms = set(ROLE_PERMISSIONS.get(role, set()))

        # เพิ่ม custom permissions
        if user_hash in self._extra_permissions:
            perms |= self._extra_permissions[user_hash]

        return perms

    def has_permission(
        self, user_hash: str, permission: Permission
    ) -> bool:
        """ตรวจว่าผู้ใช้มี permission หรือไม่."""
        return permission in self.get_permissions(user_hash)

    def check_permission(
        self, user_hash: str, permission: Permission
    ) -> tuple[bool, str]:
        """ตรวจ permission พร้อมข้อความ error.

        Returns:
            (allowed, error_message)
        """
        if self.has_permission(user_hash, permission):
            return True, ""
        role = self.get_role(user_hash)
        return False, (
            f"ไม่มีสิทธิ์ ({permission.value}) — "
            f"role ปัจจุบัน: {role.value}"
        )

    def set_custom_role(self, user_hash: str, role: Role) -> None:
        """กำหนด role แบบ custom."""
        self._custom_roles[user_hash] = role
        logger.info(
            "RBAC: custom role set %s → %s",
            user_hash[:8], role.value,
        )

    def grant_permission(
        self, user_hash: str, permission: Permission
    ) -> None:
        """เพิ่ม permission พิเศษ."""
        if user_hash not in self._extra_permissions:
            self._extra_permissions[user_hash] = set()
        self._extra_permissions[user_hash].add(permission)

    def get_user_info(self, user_hash: str) -> dict:
        """ข้อมูล RBAC ของผู้ใช้."""
        role = self.get_role(user_hash)
        perms = self.get_permissions(user_hash)
        return {
            "user_hash": user_hash[:8],
            "role": role.value,
            "permissions": sorted(p.value for p in perms),
            "permission_count": len(perms),
            "has_custom_role": user_hash in self._custom_roles,
            "has_extra_permissions": user_hash in self._extra_permissions,
        }

    # ── Backward-compatible helpers ──────────────────────

    def is_authorized(self, user_hash: str) -> bool:
        """เทียบเท่า is_authorized_user เดิม."""
        return self.get_role(user_hash) in (
            Role.NURSE, Role.PHYSICIAN, Role.ADMIN,
        )

    def is_physician(self, user_hash: str) -> bool:
        """เทียบเท่า is_physician_user เดิม."""
        return self.get_role(user_hash) in (
            Role.PHYSICIAN, Role.ADMIN,
        )

    def is_admin(self, user_hash: str) -> bool:
        """เทียบเท่า is_admin_user เดิม."""
        return self.get_role(user_hash) == Role.ADMIN
