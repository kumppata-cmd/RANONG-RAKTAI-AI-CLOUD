# =============================================================================
# PATIENT HN REGISTRY — ระบบจัดการรหัส HN ผู้ป่วย
# คลินิกระนอง รักษ์ไต × RANONG RAKTAI AI 2.0
#
# จับคู่รหัส HN คลินิก + HN โรงพยาบาล กับ LINE User (owner_hash)
# เพื่อใช้ในการติดตามผลแล็บ ใบยา และฟอกไต รายบุคคล
#
# Safety: ไม่เก็บ LINE userId ตรง — ใช้ owner_hash (SHA-256 truncated)
# =============================================================================

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Data Model
# ─────────────────────────────────────────────────────────────

class PatientHN:
    """ข้อมูล HN ผู้ป่วย — จับคู่ HN คลินิก + HN โรงพยาบาล"""

    def __init__(
        self,
        owner_hash: str,
        clinic_hn: str = "",
        hospital_hn: str = "",
        display_name: str = "",
        registered_at: str = "",
        updated_at: str = "",
        registered_by: str = "patient",
        notes: str = "",
    ):
        self.owner_hash = owner_hash
        self.clinic_hn = clinic_hn.strip().upper()
        self.hospital_hn = hospital_hn.strip().upper()
        self.display_name = display_name.strip()
        self.registered_at = registered_at or datetime.now(
            timezone.utc
        ).isoformat()
        self.updated_at = updated_at or self.registered_at
        self.registered_by = registered_by
        self.notes = notes

    def to_dict(self) -> dict:
        return {
            "owner_hash": self.owner_hash,
            "clinic_hn": self.clinic_hn,
            "hospital_hn": self.hospital_hn,
            "display_name": self.display_name,
            "registered_at": self.registered_at,
            "updated_at": self.updated_at,
            "registered_by": self.registered_by,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PatientHN":
        return cls(**{k: v for k, v in data.items() if k in {
            "owner_hash", "clinic_hn", "hospital_hn", "display_name",
            "registered_at", "updated_at", "registered_by", "notes",
        }})

    @property
    def hn_pair(self) -> str:
        """รหัส HN คู่ สำหรับแสดงผล เช่น 'C-1234/H-56789'"""
        parts = []
        if self.clinic_hn:
            parts.append(f"C-{self.clinic_hn}")
        if self.hospital_hn:
            parts.append(f"H-{self.hospital_hn}")
        return "/".join(parts) if parts else f"ID-{self.owner_hash[:6]}"

    @property
    def folder_name(self) -> str:
        """ชื่อ folder สำหรับจัดเก็บเอกสารรายคน"""
        if self.clinic_hn and self.hospital_hn:
            return f"{self.clinic_hn}_{self.hospital_hn}"
        if self.clinic_hn:
            return self.clinic_hn
        if self.hospital_hn:
            return self.hospital_hn
        return self.owner_hash[:8]


# ─────────────────────────────────────────────────────────────
# HN Registry — GCS-backed patient registry
# ─────────────────────────────────────────────────────────────

class HNRegistry:
    """จัดการทะเบียน HN ผู้ป่วย — เก็บใน GCS

    GCS path: _hn_registry/{owner_hash}.json
    """

    GCS_PREFIX = "_hn_registry"

    def __init__(self, storage=None):
        self._storage = storage
        self._cache: dict[str, PatientHN] = {}
        self._hn_index: dict[str, str] = {}  # clinic_hn -> owner_hash
        self._hospital_index: dict[str, str] = {}  # hospital_hn -> owner_hash
        self._loaded = False

    def set_storage(self, storage) -> None:
        self._storage = storage

    # ── Load / Save ──────────────────────────────────────────

    def _gcs_path(self, owner_hash: str) -> str:
        return f"{self.GCS_PREFIX}/{owner_hash}.json"

    def _load_all(self) -> None:
        """โหลด registry ทั้งหมดจาก GCS"""
        if self._loaded or not self._storage:
            return
        try:
            # รองรับทั้ง storage wrapper และ bucket โดยตรง
            bucket = (self._storage._bucket
                      if hasattr(self._storage, '_bucket')
                      else self._storage)
            if not bucket or not hasattr(bucket, 'list_blobs'):
                logger.warning("HNRegistry: bucket not available for listing")
                return
            blobs = bucket.list_blobs(prefix=f"{self.GCS_PREFIX}/")
            for blob in blobs:
                try:
                    data = json.loads(blob.download_as_text())
                    patient = PatientHN.from_dict(data)
                    self._cache[patient.owner_hash] = patient
                    if patient.clinic_hn:
                        self._hn_index[patient.clinic_hn] = patient.owner_hash
                    if patient.hospital_hn:
                        self._hospital_index[patient.hospital_hn] = patient.owner_hash
                except Exception as e:
                    logger.warning("HNRegistry: skip corrupt blob %s: %s",
                                   blob.name, e)
            self._loaded = True
            logger.info("HNRegistry: loaded %d patients", len(self._cache))
        except Exception as e:
            logger.error("HNRegistry: load failed: %s", e)

    def _save_patient(self, patient: PatientHN) -> tuple[bool, str]:
        """บันทึก HN ผู้ป่วยลง GCS — คืน (success, error_detail)

        SA อาจมีแค่ storage.objects.create (Object Creator role)
        ซึ่ง upload_from_string ปกติจะ "overwrite" → ต้อง objects.delete ด้วย
        วิธีแก้: ใช้ if_generation_match=0 (create-only) เหมือน save()
        ถ้า object มีอยู่แล้ว (412) → ลองลบก่อนแล้วสร้างใหม่
        """
        if not self._storage:
            logger.warning("HNRegistry: no storage — cannot save")
            return False, "storage not configured"
        try:
            path = self._gcs_path(patient.owner_hash)
            data_bytes = json.dumps(
                patient.to_dict(), ensure_ascii=False, indent=2
            ).encode("utf-8")

            # หา bucket — รองรับทั้ง storage wrapper และ bucket โดยตรง
            bucket = (self._storage._bucket
                      if hasattr(self._storage, '_bucket')
                      else self._storage)
            if not bucket or not hasattr(bucket, 'blob'):
                return False, "no bucket"

            blob = bucket.blob(path)
            try:
                # create-only: ใช้แค่ objects.create permission
                blob.upload_from_string(
                    data_bytes,
                    content_type="application/json",
                    if_generation_match=0,
                )
            except Exception as create_err:
                err_code = getattr(create_err, "code", None)
                if err_code == 412:
                    # 412 = object มีอยู่แล้ว (ลงทะเบียนไปแล้วก่อนหน้า)
                    # SA ไม่มี objects.delete → ไม่สามารถ overwrite ได้
                    # อ่านข้อมูลเดิมแล้ว return สำเร็จ
                    try:
                        existing = json.loads(blob.download_as_text())
                        patient_from_gcs = PatientHN.from_dict(existing)
                        # อัปเดต cache ให้ตรงกับ GCS
                        self._cache[patient.owner_hash] = patient_from_gcs
                        if patient_from_gcs.clinic_hn:
                            self._hn_index[patient_from_gcs.clinic_hn] = patient.owner_hash
                        logger.info("HNRegistry: patient already registered %s",
                                    patient_from_gcs.hn_pair)
                        return True, ""
                    except Exception:
                        pass
                    # ถ้าอ่านไม่ได้ ก็ถือว่าลงทะเบียนไว้แล้ว
                    logger.info("HNRegistry: patient exists (412), cannot read back")
                    return True, ""
                else:
                    raise

            logger.info("HNRegistry: saved patient %s", patient.hn_pair)
            return True, ""
        except Exception as e:
            logger.error("HNRegistry: save failed for %s: %s",
                         patient.owner_hash[:6], e)
            return False, type(e).__name__

    # ── Public API ──────────────────────────────────────────

    def register(
        self,
        owner_hash: str,
        clinic_hn: str = "",
        hospital_hn: str = "",
        display_name: str = "",
        registered_by: str = "patient",
        notes: str = "",
    ) -> tuple[bool, str]:
        """ลงทะเบียน HN ใหม่หรืออัปเดต HN เดิม

        Returns: (success, message)
        """
        self._load_all()

        # ตรวจสอบรูปแบบ HN
        if clinic_hn and not _validate_hn(clinic_hn):
            return False, "รูปแบบ HN คลินิกไม่ถูกต้อง (ใช้ตัวเลขหรือตัวอักษร)"
        if hospital_hn and not _validate_hn(hospital_hn):
            return False, "รูปแบบ HN โรงพยาบาลไม่ถูกต้อง"

        # ตรวจว่า HN ซ้ำกับคนอื่นไหม
        if clinic_hn:
            existing = self._hn_index.get(clinic_hn.strip().upper())
            if existing and existing != owner_hash:
                return False, f"HN คลินิก {clinic_hn} ถูกใช้แล้ว"
        if hospital_hn:
            existing = self._hospital_index.get(hospital_hn.strip().upper())
            if existing and existing != owner_hash:
                return False, f"HN โรงพยาบาล {hospital_hn} ถูกใช้แล้ว"

        # สร้างหรืออัปเดต
        existing_patient = self._cache.get(owner_hash)
        if existing_patient:
            # อัปเดต — เก็บค่าเดิมถ้าไม่ได้ส่งมาใหม่
            if clinic_hn:
                # ลบ index เก่า
                if existing_patient.clinic_hn:
                    self._hn_index.pop(existing_patient.clinic_hn, None)
                existing_patient.clinic_hn = clinic_hn.strip().upper()
            if hospital_hn:
                if existing_patient.hospital_hn:
                    self._hospital_index.pop(existing_patient.hospital_hn, None)
                existing_patient.hospital_hn = hospital_hn.strip().upper()
            if display_name:
                existing_patient.display_name = display_name.strip()
            if notes:
                existing_patient.notes = notes
            existing_patient.updated_at = datetime.now(timezone.utc).isoformat()
            patient = existing_patient
            action = "อัปเดต"
        else:
            patient = PatientHN(
                owner_hash=owner_hash,
                clinic_hn=clinic_hn,
                hospital_hn=hospital_hn,
                display_name=display_name,
                registered_by=registered_by,
                notes=notes,
            )
            action = "ลงทะเบียน"

        # บันทึก
        saved, err_detail = self._save_patient(patient)
        if saved:
            self._cache[owner_hash] = patient
            if patient.clinic_hn:
                self._hn_index[patient.clinic_hn] = owner_hash
            if patient.hospital_hn:
                self._hospital_index[patient.hospital_hn] = owner_hash
            return True, f"✅ {action}สำเร็จ: {patient.hn_pair}"
        return False, f"❌ บันทึกไม่สำเร็จ ({err_detail}) — กรุณาลองใหม่"

    def get_by_owner(self, owner_hash: str) -> Optional[PatientHN]:
        """ค้นหาด้วย owner_hash"""
        self._load_all()
        return self._cache.get(owner_hash)

    def get_by_clinic_hn(self, clinic_hn: str) -> Optional[PatientHN]:
        """ค้นหาด้วย HN คลินิก"""
        self._load_all()
        owner = self._hn_index.get(clinic_hn.strip().upper())
        return self._cache.get(owner) if owner else None

    def get_by_hospital_hn(self, hospital_hn: str) -> Optional[PatientHN]:
        """ค้นหาด้วย HN โรงพยาบาล"""
        self._load_all()
        owner = self._hospital_index.get(hospital_hn.strip().upper())
        return self._cache.get(owner) if owner else None

    def search_hn(self, query: str) -> list[PatientHN]:
        """ค้นหา HN (clinic หรือ hospital)"""
        self._load_all()
        q = query.strip().upper()
        results = []
        for p in self._cache.values():
            if (q in p.clinic_hn or q in p.hospital_hn
                    or q in p.display_name.upper()
                    or q in p.owner_hash[:8].upper()):
                results.append(p)
        return results

    def list_all(self) -> list[PatientHN]:
        """แสดงรายชื่อทั้งหมด (admin only)"""
        self._load_all()
        return sorted(self._cache.values(),
                      key=lambda p: p.clinic_hn or p.owner_hash)

    def get_folder_name(self, owner_hash: str) -> str:
        """ดึงชื่อ folder สำหรับผู้ป่วย"""
        patient = self.get_by_owner(owner_hash)
        if patient:
            return patient.folder_name
        return owner_hash[:8]


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _validate_hn(hn: str) -> bool:
    """ตรวจรูปแบบ HN — ยอมรับตัวเลข ตัวอักษร ขีด"""
    return bool(re.match(r'^[A-Za-z0-9\-/]{1,20}$', hn.strip()))


def resolve_patient_folder(
    owner_hash_full: str,
    hn_reg=None,
    master_reg=None,
    is_staff_account: bool = False,
) -> str:
    """หาชื่อแฟ้มผู้ป่วยจากบัญชี LINE ของผู้ส่ง (ไม่พึ่ง OCR)

    ใช้เมื่อ OCR อ่าน HN จากเอกสารไม่ได้ — ผู้ป่วยพิมพ์
    "ลงทะเบียนHN <HN>" ใน LINE ไว้ล่วงหน้า

    ลำดับ:
      1. ทะเบียน LINE→HN หา HN ของผู้ส่ง
      2. แปลง HN → ชื่อแฟ้มของ master registry (มีชื่อผู้ป่วย)
         เพื่อให้เอกสารของคนเดียวกันอยู่แฟ้มเดียว ไม่แตกเป็นสองแฟ้ม
      3. ถ้าไม่มีในทะเบียนกลาง ใช้ชื่อแฟ้มจากทะเบียน LINE

    คืน "" เมื่อหาไม่ได้ — เอกสารจะอยู่ระดับไตรมาส/เดือนเท่านั้น
    ไม่ใช้ owner_hash เป็นชื่อแฟ้ม เพราะอ่านไม่ออกและไม่สื่อความหมาย

    is_staff_account: บัญชีเจ้าหน้าที่/ผู้ดูแล ซึ่งส่งเอกสารแทนผู้ป่วย
        หลายคนจากบัญชีเดียว — ห้ามใช้การผูก HN ของบัญชีนี้มาจัดแฟ้ม
        เพราะจะทำให้เอกสารของผู้ป่วยทุกคนไปรวมอยู่แฟ้มคนเดียว
        บัญชีนี้ให้อาศัย HN ในเอกสาร (OCR) เท่านั้น
    """
    if not owner_hash_full or not hn_reg:
        return ""
    if is_staff_account:
        return ""

    try:
        linked = hn_reg.get_by_owner(owner_hash_full)
    except Exception:
        return ""
    if not linked:
        return ""

    # แปลง HN → แฟ้มมาตรฐานของ master registry
    for hn in (
        getattr(linked, "clinic_hn", ""),
        getattr(linked, "hospital_hn", ""),
    ):
        if not hn:
            continue
        try:
            folder = master_reg.get_folder_for_hn(hn) if master_reg else ""
        except Exception:
            folder = ""
        if folder:
            return folder

    folder = getattr(linked, "folder_name", "") or ""
    # กัน fallback ที่คืน owner_hash มาเป็นชื่อแฟ้ม
    if folder and folder == owner_hash_full[:8]:
        return ""
    return folder


def get_quarter(date: datetime) -> str:
    """คืนค่าไตรมาส เช่น '2026-Q4'"""
    q = (date.month - 1) // 3 + 1
    return f"{date.year}-Q{q}"


def get_quarter_range(quarter_str: str) -> tuple[datetime, datetime]:
    """คืนช่วงวันที่ของไตรมาส เช่น '2026-Q4' -> (Oct 1, Dec 31)"""
    year, q_num = quarter_str.split("-Q")
    year = int(year)
    q = int(q_num)
    start_month = (q - 1) * 3 + 1
    end_month = q * 3
    start = datetime(year, start_month, 1, tzinfo=timezone.utc)
    if end_month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, end_month + 1, 1, tzinfo=timezone.utc)
    return start, end


def get_month_range(year: int, month: int) -> tuple[datetime, datetime]:
    """คืนช่วงวันที่ของเดือน"""
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return start, end


# ─────────────────────────────────────────────────────────────
# Quarter/Month label helpers (Thai)
# ─────────────────────────────────────────────────────────────

THAI_MONTHS = [
    "", "ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
    "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค.",
]

THAI_QUARTER_NAMES = {
    1: "ไตรมาส 1 (ม.ค.–มี.ค.)",
    2: "ไตรมาส 2 (เม.ย.–มิ.ย.)",
    3: "ไตรมาส 3 (ก.ค.–ก.ย.)",
    4: "ไตรมาส 4 (ต.ค.–ธ.ค.)",
}


def quarter_label_thai(quarter_str: str) -> str:
    """เช่น '2026-Q4' -> 'ไตรมาส 4/2569 (ต.ค.–ธ.ค.)'"""
    year, q_num = quarter_str.split("-Q")
    q = int(q_num)
    thai_year = int(year) + 543
    name = THAI_QUARTER_NAMES.get(q, f"ไตรมาส {q}")
    return f"{name} ปี {thai_year}"


def month_label_thai(year: int, month: int) -> str:
    """เช่น (2026, 10) -> 'ต.ค. 2569'"""
    thai_year = year + 543
    return f"{THAI_MONTHS[month]} {thai_year}"
