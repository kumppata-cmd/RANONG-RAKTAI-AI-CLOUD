# =============================================================================
# PATIENT MASTER REGISTRY — ทะเบียนรายชื่อผู้ป่วย (Admin / Clinic-wide)
# คลินิกระนอง รักษ์ไต × RANONG RAKTAI AI 2.0
#
# ออกแบบสำหรับ:
#   1. Admin ถ่ายรูปรายชื่อผู้ป่วย → OCR → ลงทะเบียนอัตโนมัติ
#   2. เอกสารที่ส่งเข้ามา → OCR → ดึง HN → จับคู่กับทะเบียน → จัดเก็บตามรายคน
#
# GCS path: _patient_master/registry.json
#
# Safety:
#   - ไม่เก็บ LINE userId ตรง
#   - ข้อมูลผู้ป่วยเก็บเฉพาะ HN + ชื่อ (สำหรับการจัดเก็บเอกสาร)
#   - ห้ามส่ง PHI ไป External AI
# =============================================================================

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Data Model — Patient Entry in Master Registry
# ─────────────────────────────────────────────────────────────

class MasterPatient:
    """ข้อมูลผู้ป่วยในทะเบียนรวม — ใช้สำหรับจับคู่ HN กับเอกสาร"""

    def __init__(
        self,
        clinic_hn: str = "",
        hospital_hn: str = "",
        patient_name: str = "",
        registered_at: str = "",
        updated_at: str = "",
        registered_by: str = "admin",
        notes: str = "",
    ):
        self.clinic_hn = clinic_hn.strip().upper() if clinic_hn else ""
        self.hospital_hn = hospital_hn.strip().upper() if hospital_hn else ""
        self.patient_name = patient_name.strip()
        self.registered_at = registered_at or datetime.now(
            timezone.utc
        ).isoformat()
        self.updated_at = updated_at or self.registered_at
        self.registered_by = registered_by
        self.notes = notes

    def to_dict(self) -> dict:
        return {
            "clinic_hn": self.clinic_hn,
            "hospital_hn": self.hospital_hn,
            "patient_name": self.patient_name,
            "registered_at": self.registered_at,
            "updated_at": self.updated_at,
            "registered_by": self.registered_by,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MasterPatient":
        return cls(**{k: v for k, v in data.items() if k in {
            "clinic_hn", "hospital_hn", "patient_name",
            "registered_at", "updated_at", "registered_by", "notes",
        }})

    @property
    def hn_display(self) -> str:
        """รหัส HN สำหรับแสดงผล เช่น '1234/H-56789' หรือ '1234'"""
        parts = []
        if self.clinic_hn:
            parts.append(self.clinic_hn)
        if self.hospital_hn:
            parts.append(f"H-{self.hospital_hn}")
        return "/".join(parts) if parts else "N/A"

    @property
    def folder_name(self) -> str:
        """ชื่อ folder สำหรับจัดเก็บเอกสารรายคน

        Format: {clinic_hn}_{patient_name} หรือ {clinic_hn} ถ้าไม่มีชื่อ
        ชื่อจะถูก sanitize ให้ปลอดภัยสำหรับ path
        """
        safe_name = _sanitize_folder_name(self.patient_name)
        if self.clinic_hn and safe_name:
            return f"{self.clinic_hn}_{safe_name}"
        if self.clinic_hn:
            return self.clinic_hn
        if self.hospital_hn:
            return self.hospital_hn
        return "unknown"


# ─────────────────────────────────────────────────────────────
# Master Patient Registry — GCS-backed, Clinic-wide
# ─────────────────────────────────────────────────────────────

class MasterPatientRegistry:
    """ทะเบียนผู้ป่วยกลาง — เก็บใน GCS

    ต่างจาก HNRegistry ตรงที่:
    - HNRegistry: จับคู่ LINE user กับ HN (per-user)
    - MasterPatientRegistry: ทะเบียนผู้ป่วยทั้งหมดของคลินิก (clinic-wide)
      ใช้สำหรับ auto-route เอกสารไปยัง folder ตาม HN

    GCS path: _patient_master/registry.json
    """

    GCS_PREFIX = "_patient_master"
    GCS_FILE = "_patient_master/registry.json"

    def __init__(self, storage=None):
        self._storage = storage  # GCS bucket object
        self._patients: list[MasterPatient] = []
        self._clinic_hn_index: dict[str, int] = {}   # clinic_hn -> list index
        self._hospital_hn_index: dict[str, int] = {}  # hospital_hn -> list index
        self._loaded = False

    def set_storage(self, storage) -> None:
        """ตั้งค่า GCS bucket สำหรับเก็บ registry"""
        self._storage = storage

    # ── Load / Save ──────────────────────────────────────────

    def _load(self) -> None:
        """โหลด registry จาก GCS"""
        if self._loaded or not self._storage:
            return
        try:
            blob = self._storage.blob(self.GCS_FILE)
            if blob.exists():
                data = json.loads(blob.download_as_text())
                patients_data = data.get("patients", [])
                self._patients = [
                    MasterPatient.from_dict(p) for p in patients_data
                ]
                self._rebuild_index()
                logger.info(
                    "MasterPatientRegistry: loaded %d patients",
                    len(self._patients),
                )
            else:
                logger.info("MasterPatientRegistry: no registry file — starting empty")
            self._loaded = True
        except Exception as e:
            logger.error("MasterPatientRegistry: load failed: %s", e)

    def _save(self) -> bool:
        """บันทึก registry ทั้งหมดลง GCS"""
        if not self._storage:
            logger.warning("MasterPatientRegistry: no storage — cannot save")
            return False
        try:
            blob = self._storage.blob(self.GCS_FILE)
            data = {
                "version": 1,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "patient_count": len(self._patients),
                "patients": [p.to_dict() for p in self._patients],
            }
            blob.upload_from_string(
                json.dumps(data, ensure_ascii=False, indent=2),
                content_type="application/json",
            )
            logger.info(
                "MasterPatientRegistry: saved %d patients",
                len(self._patients),
            )
            return True
        except Exception as e:
            logger.error("MasterPatientRegistry: save failed: %s", e)
            return False

    def _rebuild_index(self) -> None:
        """สร้าง index ใหม่จาก patient list"""
        self._clinic_hn_index.clear()
        self._hospital_hn_index.clear()
        for i, p in enumerate(self._patients):
            if p.clinic_hn:
                self._clinic_hn_index[p.clinic_hn] = i
            if p.hospital_hn:
                self._hospital_hn_index[p.hospital_hn] = i

    # ── Public API ──────────────────────────────────────────

    def register(
        self,
        clinic_hn: str = "",
        hospital_hn: str = "",
        patient_name: str = "",
        registered_by: str = "admin",
        notes: str = "",
    ) -> tuple[bool, str]:
        """ลงทะเบียนผู้ป่วยใหม่หรืออัปเดตข้อมูล

        Returns: (success, message)
        """
        self._load()

        clinic_hn = clinic_hn.strip().upper() if clinic_hn else ""
        hospital_hn = hospital_hn.strip().upper() if hospital_hn else ""

        if not clinic_hn and not hospital_hn:
            return False, "ต้องระบุ HN คลินิก หรือ HN โรงพยาบาล อย่างน้อย 1 อย่าง"

        # ตรวจสอบรูปแบบ HN
        if clinic_hn and not _validate_hn(clinic_hn):
            return False, f"รูปแบบ HN คลินิกไม่ถูกต้อง: {clinic_hn}"
        if hospital_hn and not _validate_hn(hospital_hn):
            return False, f"รูปแบบ HN โรงพยาบาลไม่ถูกต้อง: {hospital_hn}"

        # ตรวจว่ามีอยู่แล้วหรือไม่ — อัปเดตถ้ามี
        existing_idx = None
        if clinic_hn and clinic_hn in self._clinic_hn_index:
            existing_idx = self._clinic_hn_index[clinic_hn]
        elif hospital_hn and hospital_hn in self._hospital_hn_index:
            existing_idx = self._hospital_hn_index[hospital_hn]

        if existing_idx is not None:
            # อัปเดตข้อมูล
            p = self._patients[existing_idx]
            if patient_name:
                p.patient_name = patient_name.strip()
            if hospital_hn and not p.hospital_hn:
                p.hospital_hn = hospital_hn
            if clinic_hn and not p.clinic_hn:
                p.clinic_hn = clinic_hn
            if notes:
                p.notes = notes
            p.updated_at = datetime.now(timezone.utc).isoformat()
            self._rebuild_index()
            if self._save():
                return True, f"อัปเดต: {p.hn_display} ({p.patient_name})"
            return False, "❌ บันทึกไม่สำเร็จ"

        # ลงทะเบียนใหม่
        patient = MasterPatient(
            clinic_hn=clinic_hn,
            hospital_hn=hospital_hn,
            patient_name=patient_name,
            registered_by=registered_by,
            notes=notes,
        )
        self._patients.append(patient)
        self._rebuild_index()
        if self._save():
            return True, f"✅ ลงทะเบียน: {patient.hn_display} ({patient.patient_name})"
        return False, "❌ บันทึกไม่สำเร็จ"

    def register_batch(
        self,
        patients_data: list[dict],
        registered_by: str = "admin_ocr",
    ) -> tuple[int, int, list[str]]:
        """ลงทะเบียนหลายคนพร้อมกัน (จาก OCR รายชื่อ)

        Returns: (new_count, updated_count, messages)
        """
        self._load()
        new_count = 0
        updated_count = 0
        messages = []

        for pdata in patients_data:
            clinic_hn = pdata.get("clinic_hn", "").strip().upper()
            hospital_hn = pdata.get("hospital_hn", "").strip().upper()
            name = pdata.get("patient_name", "").strip()

            if not clinic_hn and not hospital_hn:
                continue

            # ตรวจ HN format
            if clinic_hn and not _validate_hn(clinic_hn):
                messages.append(f"⚠️ HN ไม่ถูกต้อง: {clinic_hn}")
                continue

            # ตรวจว่ามีอยู่แล้ว
            existing_idx = None
            if clinic_hn and clinic_hn in self._clinic_hn_index:
                existing_idx = self._clinic_hn_index[clinic_hn]
            elif hospital_hn and hospital_hn in self._hospital_hn_index:
                existing_idx = self._hospital_hn_index[hospital_hn]

            if existing_idx is not None:
                p = self._patients[existing_idx]
                changed = False
                if name and name != p.patient_name:
                    p.patient_name = name
                    changed = True
                if hospital_hn and not p.hospital_hn:
                    p.hospital_hn = hospital_hn
                    changed = True
                if changed:
                    p.updated_at = datetime.now(timezone.utc).isoformat()
                    updated_count += 1
            else:
                patient = MasterPatient(
                    clinic_hn=clinic_hn,
                    hospital_hn=hospital_hn,
                    patient_name=name,
                    registered_by=registered_by,
                )
                self._patients.append(patient)
                new_count += 1

        self._rebuild_index()
        if self._save():
            messages.append(
                f"✅ ลงทะเบียนใหม่ {new_count} คน, อัปเดต {updated_count} คน"
            )
        else:
            messages.append("❌ บันทึกลง GCS ไม่สำเร็จ")
        return new_count, updated_count, messages

    def match_hn(self, hn: str) -> Optional[MasterPatient]:
        """ค้นหาผู้ป่วยจาก HN (ทั้ง clinic_hn และ hospital_hn)"""
        self._load()
        hn = hn.strip().upper()
        if not hn:
            return None
        # ค้นหา clinic_hn ก่อน
        idx = self._clinic_hn_index.get(hn)
        if idx is not None:
            return self._patients[idx]
        # ค้นหา hospital_hn
        idx = self._hospital_hn_index.get(hn)
        if idx is not None:
            return self._patients[idx]
        return None

    def get_folder_for_hn(self, hn: str) -> str:
        """ดึงชื่อ folder สำหรับ HN — ใช้ใน GCS path building"""
        patient = self.match_hn(hn)
        if patient:
            return patient.folder_name
        return ""

    def search(self, query: str) -> list[MasterPatient]:
        """ค้นหาผู้ป่วย (HN หรือชื่อ)"""
        self._load()
        q = query.strip().upper()
        results = []
        for p in self._patients:
            if (q in p.clinic_hn
                    or q in p.hospital_hn
                    or q in p.patient_name.upper()):
                results.append(p)
        return results

    def list_all(self) -> list[MasterPatient]:
        """แสดงรายชื่อทั้งหมด"""
        self._load()
        return sorted(self._patients, key=lambda p: p.clinic_hn or p.hospital_hn)

    def count(self) -> int:
        """จำนวนผู้ป่วยทั้งหมด"""
        self._load()
        return len(self._patients)

    def delete(self, clinic_hn: str) -> tuple[bool, str]:
        """ลบผู้ป่วยจาก registry"""
        self._load()
        clinic_hn = clinic_hn.strip().upper()
        idx = self._clinic_hn_index.get(clinic_hn)
        if idx is None:
            return False, f"ไม่พบ HN: {clinic_hn}"
        patient = self._patients.pop(idx)
        self._rebuild_index()
        if self._save():
            return True, f"✅ ลบสำเร็จ: {patient.hn_display} ({patient.patient_name})"
        return False, "❌ บันทึกไม่สำเร็จ"


# ─────────────────────────────────────────────────────────────
# OCR Patient List Parser — ดึง HN + ชื่อจากรูปถ่ายรายชื่อ
# ─────────────────────────────────────────────────────────────

def parse_patient_list_ocr(text: str) -> list[dict]:
    """แยก HN + ชื่อผู้ป่วยจากข้อความ OCR ของรายชื่อผู้ป่วย

    รองรับรูปแบบ:
    1. "1234 สมชาย ใจดี"          → HN=1234, ชื่อ=สมชาย ใจดี
    2. "HN 1234 สมชาย"            → HN=1234, ชื่อ=สมชาย
    3. "HN:1234 สมชาย ใจดี"       → HN=1234, ชื่อ=สมชาย ใจดี
    4. "1. 1234 สมชาย ใจดี"       → HN=1234, ชื่อ=สมชาย ใจดี
    5. "1234/56789 สมชาย"         → clinic=1234, hospital=56789
    6. "1234 - สมชาย ใจดี"        → HN=1234, ชื่อ=สมชาย ใจดี
    7. แต่ละบรรทัดมี 1 ผู้ป่วย

    Returns: list of {"clinic_hn": str, "hospital_hn": str, "patient_name": str}
    """
    results = []
    lines = text.strip().split("\n")

    for line in lines:
        line = line.strip()
        if not line or len(line) < 3:
            continue

        parsed = _parse_patient_line(line)
        if parsed:
            results.append(parsed)

    logger.info("parse_patient_list_ocr: %d patients from %d lines",
                len(results), len(lines))
    return results


def _parse_patient_line(line: str) -> Optional[dict]:
    """แยก HN + ชื่อจากบรรทัดเดียว"""

    # ลบหมายเลขลำดับ (1. , 2. , 3) , #1, ฯลฯ)
    line = re.sub(r'^[\d]+[\.\)\s]+', '', line).strip()
    line = re.sub(r'^#\d+\s*', '', line).strip()

    if not line:
        return None

    clinic_hn = ""
    hospital_hn = ""
    patient_name = ""

    # Pattern 1: "HN:1234" หรือ "HN 1234" หรือ "HN1234"
    m = re.match(
        r'(?:HN|hn|H\.?N\.?)\s*[:=\-]?\s*(\d{1,10})\s*[/\-]?\s*(\d{1,10})?\s*[,\-\s]*(.+)?',
        line,
    )
    if m:
        clinic_hn = m.group(1)
        if m.group(2):
            hospital_hn = m.group(2)
        if m.group(3):
            patient_name = m.group(3).strip().rstrip(',.-')
        if clinic_hn:
            return {
                "clinic_hn": clinic_hn,
                "hospital_hn": hospital_hn,
                "patient_name": patient_name,
            }

    # Pattern 2: ตัวเลข HN นำหน้า เช่น "1234 สมชาย ใจดี"
    #   หรือ "1234/56789 สมชาย ใจดี" (clinic/hospital pair)
    m = re.match(
        r'(\d{1,10})\s*[/\-]\s*(\d{1,10})\s+(.+)',
        line,
    )
    if m:
        return {
            "clinic_hn": m.group(1),
            "hospital_hn": m.group(2),
            "patient_name": m.group(3).strip().rstrip(',.-'),
        }

    # Pattern 3: ตัวเลข HN เดี่ยว + ชื่อไทย/อังกฤษ
    m = re.match(
        r'(\d{1,10})\s+[,\-\s]*([ก-๙A-Za-z].+)',
        line,
    )
    if m:
        return {
            "clinic_hn": m.group(1),
            "hospital_hn": "",
            "patient_name": m.group(2).strip().rstrip(',.-'),
        }

    # Pattern 4: ชื่อไทย + HN (กลับด้าน)
    m = re.match(
        r'([ก-๙].+?)\s+(?:HN|hn)?\s*[:=]?\s*(\d{2,10})',
        line,
    )
    if m:
        return {
            "clinic_hn": m.group(2),
            "hospital_hn": "",
            "patient_name": m.group(1).strip().rstrip(',.-'),
        }

    return None


# ─────────────────────────────────────────────────────────────
# HN Extractor — ดึง HN จากเอกสารทางการแพทย์ (OCR text)
# ─────────────────────────────────────────────────────────────

# Regex patterns สำหรับ HN ในเอกสารทางการแพทย์ไทย
_HN_PATTERNS = [
    # "HN: 12345" or "HN:12345" or "H.N. 12345" or "HN 12345"
    re.compile(r'(?:H\.?N\.?|HN)\s*[:=\-]?\s*(\d{2,10})', re.IGNORECASE),
    # "เลขที่ 12345" or "เลขที่ผู้ป่วย 12345"
    re.compile(r'เลข(?:ที่)?(?:\s*ผู้ป่วย)?\s*[:=]?\s*(\d{2,10})'),
    # "รหัสผู้ป่วย 12345" or "รหัส HN 12345"
    re.compile(r'รหัส\s*(?:ผู้ป่วย|HN|hn)?\s*[:=]?\s*(\d{2,10})'),
    # "Patient ID: 12345" or "PID: 12345"
    re.compile(r'(?:Patient\s*ID|PID|Pat\.?\s*ID)\s*[:=\-]?\s*(\d{2,10})', re.IGNORECASE),
    # "AN: 12345" (Admission Number)
    re.compile(r'(?:AN|A\.N\.)\s*[:=\-]?\s*(\d{2,10})', re.IGNORECASE),
    # "VN: 12345" (Visit Number)
    re.compile(r'(?:VN|V\.N\.)\s*[:=\-]?\s*(\d{2,10})', re.IGNORECASE),
]


def extract_hn_from_document(text: str) -> list[str]:
    """ดึง HN ทั้งหมดจากข้อความ OCR ของเอกสารทางการแพทย์

    Returns: list of HN strings (unique, sorted)
    """
    if not text:
        return []

    found: set[str] = set()
    for pattern in _HN_PATTERNS:
        for match in pattern.finditer(text):
            hn = match.group(1).strip()
            # กรองตัวเลขที่ไม่น่าจะเป็น HN (เช่น ปี พ.ศ., ค่าแล็บ)
            if _is_likely_hn(hn, text, match.start()):
                found.add(hn)

    return sorted(found)


def _is_likely_hn(hn: str, text: str, pos: int) -> bool:
    """ตรวจสอบว่าตัวเลขที่พบน่าจะเป็น HN จริง

    กรองออก:
    - ปี พ.ศ. (2566-2570)
    - ปี ค.ศ. (2023-2027)
    - ค่าแล็บ (ตัวเลข + mg/dL, mmol/L, ฯลฯ)
    - เบอร์โทร (10 หลัก ขึ้นต้นด้วย 0)
    - จำนวนเงิน (บาท)
    """
    hn_int = int(hn) if hn.isdigit() else 0

    # ปี พ.ศ.
    if 2500 <= hn_int <= 2600:
        # ตรวจว่ามี พ.ศ. หรือ วันที่ ใกล้ๆ
        context = text[max(0, pos - 30):pos + len(hn) + 30]
        if re.search(r'พ\.?ศ\.?|วันที่|date|ปี', context, re.IGNORECASE):
            return False

    # ปี ค.ศ.
    if 2020 <= hn_int <= 2030:
        context = text[max(0, pos - 30):pos + len(hn) + 30]
        if re.search(r'ค\.?ศ\.?|year|ปี|วันที่', context, re.IGNORECASE):
            return False

    # ค่าแล็บ — ตัวเลข + หน่วย
    after = text[pos + len(hn):pos + len(hn) + 20]
    if re.match(r'\s*(?:mg|mmol|umol|g/|mL|IU|U/L|%|เม็ด|tab|cap)', after, re.IGNORECASE):
        return False

    # เบอร์โทร (ขึ้นต้นด้วย 0 และยาว 9-10 หลัก)
    if hn.startswith("0") and len(hn) >= 9:
        return False

    # สั้นเกินไป (1 หลัก)
    if len(hn) < 2:
        return False

    return True


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _validate_hn(hn: str) -> bool:
    """ตรวจรูปแบบ HN — ยอมรับตัวเลข ตัวอักษร ขีด (ใจกว้างกว่า HNRegistry)"""
    return bool(re.match(r'^[A-Za-z0-9\-/]{1,20}$', hn.strip()))


def _sanitize_folder_name(name: str) -> str:
    """ทำให้ชื่อปลอดภัยสำหรับใช้เป็น folder name

    - เก็บเฉพาะตัวอักษรไทย อังกฤษ ตัวเลข ขีด
    - แทนช่องว่างด้วย _
    - ตัดให้ไม่เกิน 50 ตัวอักษร
    """
    if not name:
        return ""
    # เก็บ Thai, ASCII alphanumeric, space, dash
    safe = re.sub(r'[^฀-๿a-zA-Z0-9\s\-]', '', name)
    safe = re.sub(r'\s+', '_', safe.strip())
    return safe[:50]
