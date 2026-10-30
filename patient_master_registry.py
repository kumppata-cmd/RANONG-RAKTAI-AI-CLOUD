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
import os
import re
import time
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
    GCS_VERSIONED_PREFIX = "_patient_master/registry_v"

    # อายุแคชของ "เส้นทางอ่าน" (วินาที) — ดู _refresh_if_stale()
    # ตั้ง 0 เพื่อกลับไปโหลดใหม่ทุกครั้งเหมือนพฤติกรรมเดิม
    CACHE_TTL_SECONDS = float(
        os.getenv("REGISTRY_CACHE_TTL", "5").strip() or "5"
    )

    def __init__(self, storage=None):
        self._storage = storage  # GCS bucket object
        self._patients: list[MasterPatient] = []
        self._clinic_hn_index: dict[str, int] = {}   # clinic_hn -> list index
        self._hospital_hn_index: dict[str, int] = {}  # hospital_hn -> list index
        # HN แบบตัด 0 นำหน้า -> list index (fallback matching)
        self._normalized_hn_index: dict[str, int] = {}
        self._loaded = False
        # เวลาที่โหลดสำเร็จล่าสุด (monotonic) — ใช้คู่กับ TTL ด้านล่าง
        self._loaded_at = 0.0
        self._cache_ttl = self.CACHE_TTL_SECONDS

    def set_storage(self, storage) -> None:
        """ตั้งค่า GCS bucket สำหรับเก็บ registry"""
        self._storage = storage

    # ── Load / Save ──────────────────────────────────────────

    def _find_latest_versioned(self):
        """ค้นหา versioned registry ล่าสุด"""
        try:
            blobs = list(self._storage.list_blobs(
                prefix=self.GCS_VERSIONED_PREFIX
            ))
            if not blobs:
                return None
            # เรียงตามชื่อ — v{timestamp} จะเรียงตาม time
            blobs.sort(key=lambda b: b.name, reverse=True)
            return blobs[0]
        except Exception as e:
            logger.warning("MasterPatientRegistry: list versioned failed: %s", e)
            return None

    def _load(self) -> None:
        """โหลด registry จาก GCS — ลอง versioned ก่อน, fallback legacy"""
        if self._loaded or not self._storage:
            return
        try:
            # ลอง versioned file ก่อน (ใหม่)
            latest = self._find_latest_versioned()
            if latest:
                data = json.loads(latest.download_as_text())
                patients_data = data.get("patients", [])
                self._patients = [
                    MasterPatient.from_dict(p) for p in patients_data
                ]
                self._rebuild_index()
                logger.info(
                    "MasterPatientRegistry: loaded %d patients from %s",
                    len(self._patients), latest.name,
                )
                self._loaded = True
                self._loaded_at = time.monotonic()
                return

            # fallback: legacy path
            blob = self._storage.blob(self.GCS_FILE)
            if blob.exists():
                data = json.loads(blob.download_as_text())
                patients_data = data.get("patients", [])
                self._patients = [
                    MasterPatient.from_dict(p) for p in patients_data
                ]
                self._rebuild_index()
                logger.info(
                    "MasterPatientRegistry: loaded %d patients (legacy)",
                    len(self._patients),
                )
            else:
                logger.info("MasterPatientRegistry: no registry file — starting empty")
            self._loaded = True
            self._loaded_at = time.monotonic()
        except Exception as e:
            logger.error("MasterPatientRegistry: load failed: %s", e)

    def _refresh_if_stale(self) -> None:
        """โหลดใหม่เฉพาะเมื่อแคชเก่าเกิน TTL — ใช้กับ "เส้นทางอ่าน" เท่านั้น

        ทำไมถึงต้องมี:
          เดิมทุกเมธอดอ่าน (match_hn, count, list_all, search) สั่ง
          _loaded = False ทุกครั้ง ทำให้เรียก GCS ใหม่ทั้ง list + download
          ทุกการเรียก การประมวลผลเอกสารหนึ่งใบเรียกเมธอดเหล่านี้หลายครั้ง
          (นับได้จริง 10 GCS round-trips ต่อเอกสาร 1 ใบ) ทั้งที่ข้อมูล
          ชุดเดียวกันทั้งหมด และทั้งหมดอยู่ในเส้นทางประมวลผล webhook
          จึงหน่วงทุกเอกสารที่คลินิกส่ง

        ทำไมยังปลอดภัยกับ multi-worker:
          - TTL สั้น (ค่าเริ่มต้น 5 วินาที) ความไม่สดจึงถูกจำกัดเวลาไว้
          - "เส้นทางเขียน" (register/update/delete) ยังบังคับ reload
            เต็มรูปแบบเหมือนเดิม เพื่อความถูกต้องของ read-modify-write
          - หลังเขียนเสร็จ worker ที่เขียนจะเห็นข้อมูลของตัวเองทันที
          - worker อื่นเห็นภายใน TTL ผลของความช้าช่วงสั้นนี้คือเอกสาร
            อาจไม่ได้แฟ้มรายผู้ป่วยชั่วคราว ซึ่งแก้ย้อนหลังได้ด้วย
            retroactive routing — ไม่ใช่การสูญหายหรือเข้าแฟ้มผิดคน
        """
        if not self._loaded:
            self._load()
            return
        if (time.monotonic() - self._loaded_at) >= self._cache_ttl:
            self._loaded = False
            self._load()

    def invalidate_cache(self) -> None:
        """บังคับให้โหลดใหม่ในครั้งถัดไป (ใช้หลังเขียน หรือเมื่อต้องการสด)"""
        self._loaded = False
        self._loaded_at = 0.0

    def _save(self) -> tuple:
        """บันทึก registry ลง GCS — ใช้ versioned path (ไม่ต้อง delete permission)
        Returns: (success: bool, error_detail: str)
        """
        if not self._storage:
            logger.warning("MasterPatientRegistry: no storage — cannot save")
            return False, "no storage backend"
        try:
            # ใช้ timestamp เป็น version — เขียนไฟล์ใหม่ทุกครั้ง ไม่ overwrite
            ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
            versioned_path = f"{self.GCS_VERSIONED_PREFIX}{ts}.json"
            blob = self._storage.blob(versioned_path)
            data = {
                "version": 1,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "patient_count": len(self._patients),
                "patients": [p.to_dict() for p in self._patients],
            }
            payload = json.dumps(data, ensure_ascii=False, indent=2)
            logger.info(
                "MasterPatientRegistry: saving %d patients (%d bytes) to %s",
                len(self._patients), len(payload), versioned_path,
            )
            blob.upload_from_string(
                payload,
                content_type="application/json",
            )
            logger.info(
                "MasterPatientRegistry: saved %d patients OK → %s",
                len(self._patients), versioned_path,
            )

            # ลบ version เก่า (best-effort — อาจล้มเหลวถ้าไม่มี delete permission)
            self._cleanup_old_versions(keep=3)

            # ข้อมูลใน memory ตรงกับที่เพิ่งเขียนลง GCS แล้ว จึงถือว่า
            # แคชสดใหม่ ณ จุดนี้ — worker ที่เขียนเห็นผลของตัวเองทันที
            # โดยไม่ต้องดาวน์โหลดกลับมาอ่านซ้ำ
            self._loaded = True
            self._loaded_at = time.monotonic()

            return True, ""
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            logger.error("MasterPatientRegistry: save failed: %s", err)
            return False, err

    def _cleanup_old_versions(self, keep: int = 3) -> None:
        """ลบ version เก่า — best effort"""
        try:
            blobs = list(self._storage.list_blobs(
                prefix=self.GCS_VERSIONED_PREFIX
            ))
            if len(blobs) <= keep:
                return
            blobs.sort(key=lambda b: b.name, reverse=True)
            for old_blob in blobs[keep:]:
                try:
                    old_blob.delete()
                except Exception:
                    pass  # ไม่มี delete permission — ปล่อยไป
        except Exception:
            pass

    def _rebuild_index(self) -> None:
        """สร้าง index ใหม่จาก patient list"""
        self._clinic_hn_index.clear()
        self._hospital_hn_index.clear()
        self._normalized_hn_index = {}
        for i, p in enumerate(self._patients):
            if p.clinic_hn:
                self._clinic_hn_index[p.clinic_hn] = i
                norm = _normalize_hn(p.clinic_hn)
                if norm:
                    self._normalized_hn_index.setdefault(norm, i)
            if p.hospital_hn:
                self._hospital_hn_index[p.hospital_hn] = i
                norm = _normalize_hn(p.hospital_hn)
                if norm:
                    self._normalized_hn_index.setdefault(norm, i)

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
        # Force reload จาก GCS ทุกครั้ง — ป้องกัน Gunicorn multi-worker data overwrite
        self._loaded = False
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
            ok, err = self._save()
            if ok:
                return True, f"อัปเดต: {p.hn_display} ({p.patient_name})"
            return False, f"❌ บันทึกไม่สำเร็จ: {err}"

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
        ok, err = self._save()
        if ok:
            return True, f"✅ ลงทะเบียน: {patient.hn_display} ({patient.patient_name})"
        # rollback — ลบ patient ที่เพิ่งเพิ่มเข้าไป
        self._patients.pop()
        self._rebuild_index()
        return False, f"❌ บันทึกไม่สำเร็จ: {err}"

    def register_batch(
        self,
        patients_data: list[dict],
        registered_by: str = "admin_ocr",
    ) -> tuple[int, int, list[str]]:
        """ลงทะเบียนหลายคนพร้อมกัน (จาก OCR รายชื่อ)

        Returns: (new_count, updated_count, messages)
        """
        # Force reload จาก GCS ทุกครั้ง — ป้องกัน Gunicorn multi-worker data overwrite
        self._loaded = False
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
        ok, err = self._save()
        if ok:
            messages.append(
                f"✅ ลงทะเบียนใหม่ {new_count} คน, อัปเดต {updated_count} คน"
            )
        else:
            messages.append(f"❌ บันทึกลง GCS ไม่สำเร็จ: {err}")
        return new_count, updated_count, messages

    def match_hn(self, hn: str) -> Optional[MasterPatient]:
        """ค้นหาผู้ป่วยจาก HN (ทั้ง clinic_hn และ hospital_hn)

        ลำดับการค้นหา:
          1. clinic_hn ตรงตัว
          2. hospital_hn ตรงตัว
          3. เทียบแบบตัด 0 นำหน้า — เอกสารจริงพิมพ์ HN ไม่สม่ำเสมอ
             เช่น ทะเบียนเก็บ "00007567" แต่ใบยา/ใบฟอกเลือดพิมพ์ "7567"
             (fallback ใช้เฉพาะเมื่อค้นแบบตรงตัวไม่พบ)
        """
        # [perf] โหลดใหม่เฉพาะเมื่อแคชเก่าเกิน TTL — ดู _refresh_if_stale()
        self._refresh_if_stale()
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
        # Fallback: เทียบแบบตัด 0 นำหน้า
        norm = _normalize_hn(hn)
        if norm:
            idx = getattr(self, "_normalized_hn_index", {}).get(norm)
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
        # [perf] โหลดใหม่เฉพาะเมื่อแคชเก่าเกิน TTL — ดู _refresh_if_stale()
        self._refresh_if_stale()
        q = query.strip().upper()
        results = []
        for p in self._patients:
            if (q in p.clinic_hn
                    or q in p.hospital_hn
                    or q in p.patient_name.upper()):
                results.append(p)
        return results

    def search_by_name(self, name: str) -> MasterPatient | None:
        """ค้นหาผู้ป่วยจากชื่อ — ใช้เมื่อ OCR หา HN ไม่ได้

        ลำดับการค้น:
          1. ตรงทั้งชื่อ (exact, case-insensitive)
          2. ชื่อ OCR เป็น substring ของชื่อใน registry
          3. ชื่อใน registry เป็น substring ของชื่อ OCR
          4. ตรงทั้งชื่อตัว + นามสกุล (แยก token)

        คืน MasterPatient ถ้าพบ **ผู้ป่วยเพียง 1 คน** เท่านั้น
        ถ้าพบหลายคน → คืน None (กันจัดเข้าแฟ้มผิดคน)
        """
        if not name or len(name.strip()) < 3:
            return None

        self._refresh_if_stale()
        name_clean = name.strip()
        name_upper = name_clean.upper()

        # ─── Pass 1: exact match (case-insensitive) ───
        exact = [p for p in self._patients if p.patient_name.upper() == name_upper]
        if len(exact) == 1:
            return exact[0]

        # ─── ตัด prefix (นาย/นาง/นางสาว) ก่อน fuzzy ───
        import re as _re
        _prefix_re = _re.compile(
            r'^(?:นาย|นาง(?:สาว)?|เด็กชาย|เด็กหญิง|ด\.ช\.|ด\.ญ\.)\s*',
        )
        name_no_prefix = _prefix_re.sub('', name_clean).strip()
        name_no_prefix_upper = name_no_prefix.upper()

        def _strip_prefix(n: str) -> str:
            return _prefix_re.sub('', n).strip()

        # ─── Pass 2: OCR name is substring of registry name ───
        if len(name_no_prefix) >= 4:
            matches = [
                p for p in self._patients
                if name_no_prefix_upper in _strip_prefix(p.patient_name).upper()
            ]
            if len(matches) == 1:
                return matches[0]

        # ─── Pass 3: registry name is substring of OCR name ───
        if len(name_no_prefix) >= 4:
            matches = [
                p for p in self._patients
                if len(_strip_prefix(p.patient_name)) >= 4
                and _strip_prefix(p.patient_name).upper() in name_no_prefix_upper
            ]
            if len(matches) == 1:
                return matches[0]

        # ─── Pass 4: token match (ชื่อ + นามสกุล ตรงกัน) ───
        name_tokens = set(name_no_prefix_upper.split())
        if len(name_tokens) >= 2:
            matches = []
            for p in self._patients:
                p_tokens = set(_strip_prefix(p.patient_name).upper().split())
                if len(p_tokens) >= 2 and name_tokens == p_tokens:
                    matches.append(p)
            if len(matches) == 1:
                return matches[0]

        return None

    def list_all(self) -> list[MasterPatient]:
        """แสดงรายชื่อทั้งหมด"""
        # [perf] โหลดใหม่เฉพาะเมื่อแคชเก่าเกิน TTL — ดู _refresh_if_stale()
        self._refresh_if_stale()
        return sorted(self._patients, key=lambda p: p.clinic_hn or p.hospital_hn)

    def count(self) -> int:
        """จำนวนผู้ป่วยทั้งหมด"""
        # [perf] โหลดใหม่เฉพาะเมื่อแคชเก่าเกิน TTL — ดู _refresh_if_stale()
        self._refresh_if_stale()
        return len(self._patients)

    def update_patient(
        self,
        hn: str,
        new_name: str = "",
        new_clinic_hn: str = "",
        new_hospital_hn: str = "",
    ) -> tuple[bool, str]:
        """แก้ไขข้อมูลผู้ป่วย — ค้นจาก HN แล้วอัปเดตชื่อหรือ HN

        Args:
            hn: HN ที่ใช้ค้นหา (clinic หรือ hospital)
            new_name: ชื่อใหม่ (ถ้าต้องการแก้)
            new_clinic_hn: HN คลินิกใหม่ (ถ้าต้องการแก้)
            new_hospital_hn: HN โรงพยาบาลใหม่ (ถ้าต้องการแก้)

        Returns: (success, message)
        """
        # Force reload จาก GCS ทุกครั้ง — ป้องกัน Gunicorn multi-worker data overwrite
        self._loaded = False
        self._load()
        patient = self.match_hn(hn.strip().upper())
        if not patient:
            return False, f"ไม่พบผู้ป่วย HN: {hn}"

        old_display = f"{patient.hn_display} ({patient.patient_name})"
        changes = []

        if new_name and new_name.strip() != patient.patient_name:
            patient.patient_name = new_name.strip()
            changes.append(f"ชื่อ → {patient.patient_name}")

        if new_clinic_hn:
            new_chn = new_clinic_hn.strip().upper()
            if new_chn != patient.clinic_hn:
                if not _validate_hn(new_chn):
                    return False, f"รูปแบบ HN คลินิกไม่ถูกต้อง: {new_chn}"
                # ตรวจว่า HN ใหม่ไม่ซ้ำกับคนอื่น
                if new_chn in self._clinic_hn_index:
                    existing = self._patients[self._clinic_hn_index[new_chn]]
                    return False, f"HN {new_chn} ใช้แล้วโดย: {existing.patient_name}"
                patient.clinic_hn = new_chn
                changes.append(f"HN คลินิก → {new_chn}")

        if new_hospital_hn:
            new_hhn = new_hospital_hn.strip().upper()
            if new_hhn != patient.hospital_hn:
                if not _validate_hn(new_hhn):
                    return False, f"รูปแบบ HN โรงพยาบาลไม่ถูกต้อง: {new_hhn}"
                if new_hhn in self._hospital_hn_index:
                    existing = self._patients[self._hospital_hn_index[new_hhn]]
                    return False, f"HN {new_hhn} ใช้แล้วโดย: {existing.patient_name}"
                patient.hospital_hn = new_hhn
                changes.append(f"HN รพ → {new_hhn}")

        if not changes:
            return False, f"ไม่มีอะไรเปลี่ยนแปลง — ข้อมูลเดิม: {old_display}"

        patient.updated_at = datetime.now(timezone.utc).isoformat()
        self._rebuild_index()
        ok, err = self._save()
        if ok:
            return True, (
                f"✅ แก้ไขสำเร็จ\n"
                f"เดิม: {old_display}\n"
                f"แก้ไข: {', '.join(changes)}"
            )
        return False, f"❌ บันทึกไม่สำเร็จ: {err}"

    def delete(self, clinic_hn: str) -> tuple[bool, str]:
        """ลบผู้ป่วยจาก registry"""
        # Force reload จาก GCS ทุกครั้ง — ป้องกัน Gunicorn multi-worker data overwrite
        self._loaded = False
        self._load()
        clinic_hn = clinic_hn.strip().upper()
        idx = self._clinic_hn_index.get(clinic_hn)
        if idx is None:
            return False, f"ไม่พบ HN: {clinic_hn}"
        patient = self._patients.pop(idx)
        self._rebuild_index()
        ok, err = self._save()
        if ok:
            return True, f"✅ ลบสำเร็จ: {patient.hn_display} ({patient.patient_name})"
        # rollback — ใส่กลับ
        self._patients.insert(idx, patient)
        self._rebuild_index()
        return False, f"❌ บันทึกไม่สำเร็จ: {err}"


# ─────────────────────────────────────────────────────────────
# OCR Patient List Parser — ดึง HN + ชื่อจากรูปถ่ายรายชื่อ
# ─────────────────────────────────────────────────────────────

def parse_patient_list_ocr(text: str) -> list[dict]:
    """แยก HN + ชื่อผู้ป่วยจากข้อความ OCR หรือข้อความพิมพ์

    รองรับรูปแบบ:
    1. "1234 สมชาย ใจดี"           → HN=1234
    2. "HN 1234 สมชาย"             → HN=1234
    3. "HN:1234 สมชาย ใจดี"        → HN=1234
    4. "1. 1234 สมชาย ใจดี"        → HN=1234 (ลำดับนำหน้า)
    5. "1234/56789 สมชาย"          → clinic=1234, hospital=56789
    6. "นาย สมชาย ใจดี 00002567 56001"  → ตาราง: ชื่อ + HN รพ + HN clinic
    7. "นาย สมชาย 56001"           → ชื่อ + HN ท้ายบรรทัด
    8. แต่ละบรรทัดมี 1 ผู้ป่วย

    Returns: list of {"clinic_hn": str, "hospital_hn": str, "patient_name": str}
    """
    results = []
    seen_hns: set[str] = set()  # ป้องกัน duplicate
    lines = text.strip().split("\n")

    for line in lines:
        line = line.strip()
        if not line or len(line) < 3:
            continue

        parsed = _parse_patient_line(line)
        if parsed:
            # ป้องกัน duplicate — ตรวจ HN ซ้ำ
            key = parsed.get("clinic_hn", "") or parsed.get("hospital_hn", "")
            if key and key not in seen_hns:
                seen_hns.add(key)
                results.append(parsed)

    logger.info("parse_patient_list_ocr: %d patients from %d lines",
                len(results), len(lines))
    return results


# Header/noise patterns ที่ต้อง skip
_SKIP_PATTERNS = re.compile(
    r'ลำดับ|ชื่อ.*สกุล|HN\s*รพ|HN\s*clinic|รหัส.*ผู้ป่วย|'
    r'ทะเบียน|หมายเหตุ|วันที่|หน้า\s*\d|page\s*\d|'
    r'โรงพยาบาล.*ระนอง|คลินิก.*รักษ์|total|รวม',
    re.IGNORECASE,
)


def _parse_patient_line(line: str) -> Optional[dict]:
    """แยก HN + ชื่อจากบรรทัดเดียว — รองรับทั้ง OCR ตาราง และพิมพ์เอง"""

    # ลบหมายเลขลำดับ (1. , 2. , 3) , #1, ฯลฯ)
    line = re.sub(r'^[\d]{1,3}[\.\)\s]+', '', line).strip()
    line = re.sub(r'^#\d+\s*', '', line).strip()

    if not line or len(line) < 3:
        return None

    # Skip header/noise lines
    if _SKIP_PATTERNS.search(line):
        return None

    clinic_hn = ""
    hospital_hn = ""
    patient_name = ""

    # ── Pattern 1: Explicit HN label ──
    # "HN:1234", "HN 1234", "HN1234", "HN 1234/56789 สมชาย"
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

    # ── Pattern 2: HN pair at start "1234/56789 ชื่อ" ──
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

    # ── Pattern 3: HN at start + Thai/English name ──
    # "56001 นาย สมชาย ใจดี"
    m = re.match(
        r'(\d{2,10})\s+[,\-\s]*([ก-๙A-Za-z].+)',
        line,
    )
    if m:
        # ตรวจว่ามี HN ตัวที่สองท้ายบรรทัดหรือไม่
        rest = m.group(2)
        m2 = re.search(r'\s+(\d{3,10})\s*$', rest)
        if m2:
            # มี 2 HN: ตัวแรก + ชื่อ + ตัวที่สอง
            name_part = rest[:m2.start()].strip().rstrip(',.-')
            return {
                "clinic_hn": m.group(1),
                "hospital_hn": m2.group(1),
                "patient_name": name_part,
            }
        return {
            "clinic_hn": m.group(1),
            "hospital_hn": "",
            "patient_name": rest.strip().rstrip(',.-'),
        }

    # ── Pattern 4: Table format — ชื่อไทยอยู่ตรงกลาง, HN อยู่ท้าย ──
    # "นาย สมชาย ใจดี 00002567 56001"
    # "นส. สมหญิง รักดี 00005318 56034"
    # Strategy: หาตัวเลข 3+ หลักทั้งหมด + หาชื่อไทย
    digit_groups = re.findall(r'\d{3,}', line)
    thai_match = re.search(r'[ก-๙][ก-๙\s\.A-Za-z]*[ก-๙]', line)

    if digit_groups and thai_match:
        name = thai_match.group().strip()
        # ลบคำนำหน้า "นาย" "นาง" "นส" ที่ซ้ำ ไม่ต้อง — เก็บไว้
        if len(digit_groups) >= 2:
            # 2+ ตัวเลข: ตัวยาวกว่า = hospital HN, ตัวสั้นกว่า = clinic HN
            # ถ้ายาวเท่ากัน: ตัวแรก = hospital, ตัวหลัง = clinic
            nums = list(digit_groups)
            if len(nums[0]) > len(nums[-1]):
                hospital_hn = nums[0]
                clinic_hn = nums[-1]
            elif len(nums[0]) < len(nums[-1]):
                clinic_hn = nums[0]
                hospital_hn = nums[-1]
            else:
                # ยาวเท่ากัน → ตัวแรก hospital, ตัวหลัง clinic
                hospital_hn = nums[0]
                clinic_hn = nums[-1]
        else:
            clinic_hn = digit_groups[0]

        if name and len(name) >= 2 and (clinic_hn or hospital_hn):
            return {
                "clinic_hn": clinic_hn,
                "hospital_hn": hospital_hn,
                "patient_name": name,
            }

    # ── Pattern 5: ชื่อไทย + HN เดี่ยวท้ายบรรทัด ──
    # "นาย สมชาย ใจดี 56001"
    m = re.match(
        r'([ก-๙].+?)\s+(?:HN|hn)?\s*[:=]?\s*(\d{2,10})\s*$',
        line,
    )
    if m:
        return {
            "clinic_hn": m.group(2),
            "hospital_hn": "",
            "patient_name": m.group(1).strip().rstrip(',.-'),
        }

    # ── Pattern 6: Fallback — หาชื่อไทยและตัวเลขอะไรก็ได้ ──
    if thai_match:
        any_nums = re.findall(r'\d{2,}', line)
        if any_nums:
            name = thai_match.group().strip()
            if len(name) >= 2:
                return {
                    "clinic_hn": any_nums[-1],  # ตัวเลขสุดท้าย
                    "hospital_hn": any_nums[0] if len(any_nums) > 1 else "",
                    "patient_name": name,
                }

    # ── Pattern 7: OCR Table Fallback — ไม่ต้องพึ่งตัวอักษรไทย ──
    # สำหรับ OCR ที่อ่านภาษาไทยเป็นอักษรละตินแต่ตัวเลข HN ถูกต้อง
    # เช่น "P35 | Sarr [situ annues | 00063895 | 63203"
    # ต้องมีตัวเลข ≥5 หลักอย่างน้อย 2 กลุ่ม (hospital_hn + clinic_hn)
    # ลบ pipe separators, วงเล็บ, เครื่องหมายพิเศษก่อน
    cleaned = re.sub(r'[|"\[\](){}«»~=*;:,_]+', ' ', line)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    # หาเลข ≥5 หลัก (HN จริงๆ) — ไม่นับเลข 1-4 หลักซึ่งเป็นลำดับแถว
    long_nums = re.findall(r'\d{5,}', cleaned)
    if len(long_nums) >= 2:
        # แยก hospital_hn (ตัวยาวกว่า) กับ clinic_hn (ตัวสั้นกว่า)
        # ถ้ายาวเท่ากัน: ตัวแรก = hospital, ตัวหลัง = clinic
        nums_sorted = sorted(long_nums, key=len, reverse=True)
        hospital_hn = nums_sorted[0]
        clinic_hn = nums_sorted[-1]
        if hospital_hn == clinic_hn and len(long_nums) > 1:
            hospital_hn = long_nums[0]
            clinic_hn = long_nums[-1]
        # ดึงชื่อ: ข้อความระหว่างตัวเลข HN (แม้เป็นอักษรละติน)
        # ลบตัวเลข HN และเลขลำดับแถว ออกจากบรรทัด
        name_part = cleaned
        for num in long_nums:
            name_part = name_part.replace(num, ' ', 1)
        # ลบเลขลำดับแถว (1-3 หลักที่ขึ้นต้น) รวมถึง hex-like (a1, 4a)
        name_part = re.sub(r'^\s*[0-9a-fA-F]{1,3}\s+', '', name_part)
        # ลบตัวอักษรเดี่ยว/noise ที่ไม่ใช่คำ
        name_part = re.sub(r'\b[A-Z]{1,2}\b', '', name_part)
        # ลบ prefix แปลกๆ จาก OCR (เช่น P, EP, CAD, QD, Tag)
        name_part = re.sub(r'^\s*[A-Z][a-z]{0,2}\d*\s*', '', name_part)
        # ลบ non-alpha noise (เช่น ——, —+, +)
        name_part = re.sub(r'[—–+]+', ' ', name_part)
        name_part = re.sub(r'\s+', ' ', name_part).strip()
        # ลบ trailing/leading punctuation
        name_part = name_part.strip(' .,;:-_|"\'')
        # ถ้าชื่อว่างหรือสั้นเกินให้ใช้ placeholder
        if not name_part or len(name_part) < 2:
            name_part = f"ผู้ป่วย-{clinic_hn}"
        return {
            "clinic_hn": clinic_hn,
            "hospital_hn": hospital_hn,
            "patient_name": name_part,
        }
    elif len(long_nums) == 1:
        # มี HN เดียว — ยังลงทะเบียนได้
        hn = long_nums[0]
        name_part = cleaned
        name_part = name_part.replace(hn, ' ', 1)
        name_part = re.sub(r'^\s*[0-9a-fA-F]{1,3}\s+', '', name_part)
        name_part = re.sub(r'[—–+]+', ' ', name_part)
        name_part = re.sub(r'\s+', ' ', name_part).strip()
        name_part = name_part.strip(' .,;:-_|"\'')
        if not name_part or len(name_part) < 2:
            name_part = f"ผู้ป่วย-{hn}"
        return {
            "clinic_hn": hn if len(hn) <= 6 else "",
            "hospital_hn": hn if len(hn) > 6 else "",
            "patient_name": name_part,
        }

    return None


def parse_patient_text_input(text: str) -> list[dict]:
    """แยก HN + ชื่อจากข้อความที่พิมพ์เอง (ไม่ใช่ OCR)

    รองรับรูปแบบ:
    - "HN 12345 สมชาย ใจดี"
    - "12345 สมชาย ใจดี"
    - "12345/67890 สมชาย"
    - "สมชาย ใจดี HN12345"
    - หลายบรรทัด (1 คนต่อบรรทัด)
    - คั่นด้วย comma: "12345 สมชาย, 12346 สมหญิง"

    Returns: list of {"clinic_hn": str, "hospital_hn": str, "patient_name": str}
    """
    results = []
    seen_hns: set[str] = set()

    # แยกบรรทัด และแยก comma
    raw_lines = text.strip().split("\n")
    lines: list[str] = []
    for raw in raw_lines:
        # แยก comma-separated entries ถ้ามี pattern HN/ชื่อ
        parts = re.split(r',\s*(?=\d|HN|hn|[ก-๙])', raw)
        lines.extend(parts)

    for line in lines:
        line = line.strip()
        if not line or len(line) < 3:
            continue
        parsed = _parse_patient_line(line)
        if parsed:
            key = parsed.get("clinic_hn", "") or parsed.get("hospital_hn", "")
            if key and key not in seen_hns:
                seen_hns.add(key)
                results.append(parsed)

    logger.info("parse_patient_text_input: %d patients from text", len(results))
    return results


# ─────────────────────────────────────────────────────────────
# HN Extractor — ดึง HN จากเอกสารทางการแพทย์ (OCR text)
# ─────────────────────────────────────────────────────────────

# Regex patterns สำหรับ HN ในเอกสารทางการแพทย์ไทย
def _normalize_hn(hn: str) -> str:
    """ทำ HN ให้เป็นรูปแบบเทียบได้ — ตัด 0 นำหน้าและอักขระที่ไม่ใช่ตัวเลข/ตัวอักษร

    เอกสารจริงพิมพ์ HN ไม่สม่ำเสมอ: ทะเบียนเก็บ "00007567"
    แต่ใบยา/ใบฟอกเลือดอาจพิมพ์ "7567" หรือ "HN-7567"

    คืน "" ถ้าผลลัพธ์สั้นกว่า 3 อักขระ — กัน false positive
    จากตัวเลขสั้นที่ไม่ใช่ HN
    """
    if not hn:
        return ""
    cleaned = re.sub(r"[^0-9A-Za-z]", "", hn).upper()
    if not cleaned:
        return ""
    # ตัด label นำหน้า เช่น "HN7567", "AN7567", "VN7567", "PID7567"
    cleaned = re.sub(r"^(?:HN|AN|VN|PID)(?=\d)", "", cleaned)
    # ตัด 0 นำหน้าเฉพาะเมื่อเป็นตัวเลขล้วน
    if cleaned.isdigit():
        cleaned = cleaned.lstrip("0")
    if len(cleaned) < 3:
        return ""
    return cleaned


_HN_PATTERNS = [
    # "HN: 12345" or "HN:12345" or "H.N. 12345" or "HN 12345"
    re.compile(r'(?:H\.?N\.?|HN)\s*[:=\-]?\s*(\d{2,10})', re.IGNORECASE),
    # OCR อาจอ่าน "HN" เป็น "H N" (มีเว้นวรรค) — เช่น "H N 56001"
    re.compile(r'(?:H\s+N)\s*[:=\-]?\s*(\d{2,10})', re.IGNORECASE),
    # OCR อาจอ่านเป็น "H.N" หรือ "H .N." (มี dot/space ปน)
    re.compile(r'H\s*\.\s*N\s*\.?\s*[:=\-]?\s*(\d{2,10})', re.IGNORECASE),
    # OCR อาจอ่าน "HN" เป็น "HIN" หรือ "HM" (OCR misread N)
    re.compile(r'(?:HIN|HM)\s*[:=\-]?\s*(\d{2,10})', re.IGNORECASE),
    # "เลขที่ 12345" or "เลขที่ผู้ป่วย 12345"
    re.compile(r'เลข(?:ที่)?(?:\s*ผู้ป่วย)?\s*[:=]?\s*(\d{2,10})'),
    # "รหัสผู้ป่วย 12345" or "รหัส HN 12345"
    re.compile(r'รหัส\s*(?:ผู้ป่วย|HN|hn)?\s*[:=]?\s*(\d{2,10})'),
    # "Patient ID: 12345" or "PID: 12345"
    re.compile(r'(?:Patient\s*ID|PID|Pat\.?\s*ID)\s*[:=\-]?\s*(\d{2,10})', re.IGNORECASE),
    # "AN: 12345" (Admission Number) — ใช้ lookbehind กัน match กลางคำ เช่น Losart"an"
    re.compile(r'(?<![a-zA-Zก-๙])(?:AN|A\.N\.)\s*[:=\-]?\s*(\d{2,10})', re.IGNORECASE),
    # "VN: 12345" (Visit Number) — ใช้ lookbehind เช่นกัน
    re.compile(r'(?<![a-zA-Zก-๙])(?:VN|V\.N\.)\s*[:=\-]?\s*(\d{2,10})', re.IGNORECASE),
    # OCR อาจอ่าน separator เป็น "I" หรือ "|" — เช่น "HN I 00290382"
    re.compile(r'(?:H\.?N\.?)\s*[I|l]\s*(\d{2,10})', re.IGNORECASE),
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

    # ตัวเลขยาวเกินไป (> 8 หลัก) — ไม่น่าจะเป็น HN
    # HN คลินิก/โรงพยาบาลทั่วไปยาว 2-8 หลัก
    # ตัวเลข 9-10 หลักมักเป็น เลขบัตรประชาชน, วันที่, หรือเบอร์โทร
    if len(hn) > 8:
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


# ─────────────────────────────────────────────────────────────
# Patient Name Extraction from OCR Text
# ─────────────────────────────────────────────────────────────

# รูปแบบชื่อคนไข้ที่อาจพบในเอกสารทางการแพทย์
_NAME_PATTERNS = [
    # "ชื่อ-สกุล: สมชาย ใจดี" หรือ "ชื่อ-นามสกุล สมชาย ใจดี"
    re.compile(
        r'ชื่อ\s*[-–]?\s*(?:นาม)?สกุล\s*[:=]?\s*'
        r'((?:นาย|นาง(?:สาว)?|เด็กชาย|เด็กหญิง|ด\.ช\.|ด\.ญ\.|Mr\.?|Mrs\.?|Ms\.?|Miss)?\s*'
        r'[ก-๙a-zA-ZÀ-ɏ][ก-๙a-zA-ZÀ-ɏ ]{2,40})',
        re.IGNORECASE,
    ),
    # "ชื่อผู้ป่วย: สมชาย ใจดี" หรือ "ชื่อผู้ป่วย สมชาย"
    re.compile(
        r'ชื่อ\s*(?:ผู้ป่วย|คนไข้)\s*[:=]?\s*'
        r'((?:นาย|นาง(?:สาว)?|เด็กชาย|เด็กหญิง|ด\.ช\.|ด\.ญ\.|Mr\.?|Mrs\.?|Ms\.?|Miss)?\s*'
        r'[ก-๙a-zA-ZÀ-ɏ][ก-๙a-zA-ZÀ-ɏ ]{2,40})',
        re.IGNORECASE,
    ),
    # "ชื่อ: สมชาย ใจดี" (standalone ชื่อ)
    re.compile(
        r'(?:^|\n)\s*ชื่อ\s*[:=]\s*'
        r'((?:นาย|นาง(?:สาว)?|เด็กชาย|เด็กหญิง|ด\.ช\.|ด\.ญ\.|Mr\.?|Mrs\.?|Ms\.?|Miss)?\s*'
        r'[ก-๙a-zA-ZÀ-ɏ][ก-๙a-zA-ZÀ-ɏ ]{2,40})',
        re.IGNORECASE,
    ),
    # "Patient: Somchai Jaidee" หรือ "Patient Name: ..."
    re.compile(
        r'(?:Patient|Patient\s*Name)\s*[:=]\s*'
        r'((?:Mr\.?|Mrs\.?|Ms\.?|Miss)?\s*[A-Za-zก-๙À-ɏ][A-Za-zก-๙À-ɏ ]{2,40})',
        re.IGNORECASE,
    ),
    # "นาย/นาง/นางสาว + ชื่อ + สกุล" ที่ขึ้นต้นบรรทัดหรือหลัง prefix
    re.compile(
        r'(?:^|\n)\s*((?:นาย|นาง(?:สาว)?)\s+[ก-๙]{2,}(?:\s+[ก-๙]{2,}){1,2})',
    ),
]

# คำที่ไม่ใช่ชื่อคนจริง — กรองออก
_NAME_STOP_WORDS = {
    "แพทย์ผู้สั่ง", "แพทย์", "เภสัชกร", "พยาบาล", "ผู้สั่ง",
    "หน่วยไตเทียม", "ระนอง", "คลินิก", "โรงพยาบาล",
    "medication", "record", "unit", "oral", "hospital",
}


def extract_patient_name_from_document(text: str) -> str:
    """ดึงชื่อผู้ป่วยจาก OCR text ของเอกสารทางการแพทย์

    Returns: ชื่อผู้ป่วย (cleaned) หรือ "" ถ้าไม่พบ
    """
    if not text or len(text.strip()) < 5:
        return ""

    for pattern in _NAME_PATTERNS:
        match = pattern.search(text)
        if match:
            raw_name = match.group(1).strip()
            cleaned = _clean_patient_name(raw_name)
            if cleaned and len(cleaned) >= 3:
                # ตรวจว่าไม่ใช่ stop word
                name_lower = cleaned.lower()
                if not any(sw in name_lower for sw in _NAME_STOP_WORDS):
                    return cleaned

    return ""


def _clean_patient_name(raw: str) -> str:
    """ทำความสะอาดชื่อ — ตัดคำนำหน้า, whitespace, ตัวเลข, HN/VN ฯลฯ"""
    if not raw:
        return ""
    # ตัดบรรทัดใหม่ — เอาแค่บรรทัดแรก
    if '\n' in raw:
        raw = raw.split('\n')[0].strip()
    # ตัด HN, VN, AN, PID labels ที่ติดมาท้ายชื่อ
    cleaned = re.sub(
        r'\s*(?:H\.?N\.?|HN|VN|AN|PID|Patient\s*ID)\s*[:=]?\s*\d*\s*$',
        '', raw, flags=re.IGNORECASE,
    )
    # ตัด "H N" (OCR อ่านผิด) ที่ติดมาท้ายชื่อ
    cleaned = re.sub(r'\s+H\s+N\s*$', '', cleaned)
    # ตัด title prefix ก่อน clean (เก็บชื่อจริง)
    title_stripped = re.sub(
        r'^(?:นาย|นาง(?:สาว)?|เด็กชาย|เด็กหญิง|ด\.ช\.|ด\.ญ\.|Mr\.?\s*|Mrs\.?\s*|Ms\.?\s*|Miss\s+)',
        '', cleaned, flags=re.IGNORECASE,
    ).strip()
    # ตัดตัวเลขและอักขระพิเศษท้ายชื่อ
    title_stripped = re.sub(r'[\d\.\,\;\:\!\?\@\#\$\%\^\&\*\(\)]+.*$', '', title_stripped).strip()
    # ถ้ายังมีตัวเลขเกาะท้าย ตัดออก
    title_stripped = re.sub(r'\s*\d+\s*$', '', title_stripped).strip()
    # เอา prefix กลับมาถ้ามี
    if title_stripped and len(title_stripped) >= 2:
        # ใช้ title_stripped เป็นชื่อจริง (ไม่มี prefix)
        cleaned = title_stripped
    else:
        # fallback: ใช้ cleaned เดิม ตัดอักขระพิเศษท้าย
        cleaned = re.sub(r'\s*[\d\.\,\;\:]+\s*$', '', cleaned)
    # ตัด whitespace ซ้ำ
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    # จำกัดความยาว
    if len(cleaned) > 50:
        cleaned = cleaned[:50].strip()
    return cleaned


# ─────────────────────────────────────────────────────────────
# Document Date Extraction from OCR Text
# ─────────────────────────────────────────────────────────────

# เดือนไทย → เลขเดือน
_THAI_MONTHS = {
    "มกราคม": 1, "ม.ค.": 1, "ม.ค": 1, "มค": 1,
    "กุมภาพันธ์": 2, "ก.พ.": 2, "ก.พ": 2, "กพ": 2,
    "มีนาคม": 3, "มี.ค.": 3, "มี.ค": 3, "มีค": 3,
    "เมษายน": 4, "เม.ย.": 4, "เม.ย": 4, "เมย": 4,
    "พฤษภาคม": 5, "พ.ค.": 5, "พ.ค": 5, "พค": 5,
    "มิถุนายน": 6, "มิ.ย.": 6, "มิ.ย": 6, "มิย": 6,
    "กรกฎาคม": 7, "ก.ค.": 7, "ก.ค": 7, "กค": 7,
    "สิงหาคม": 8, "ส.ค.": 8, "ส.ค": 8, "สค": 8,
    "กันยายน": 9, "ก.ย.": 9, "ก.ย": 9, "กย": 9,
    "ตุลาคม": 10, "ต.ค.": 10, "ต.ค": 10, "ตค": 10,
    "พฤศจิกายน": 11, "พ.ย.": 11, "พ.ย": 11, "พย": 11,
    "ธันวาคม": 12, "ธ.ค.": 12, "ธ.ค": 12, "ธค": 12,
}

# สร้าง regex alternatives สำหรับชื่อเดือนไทย — เรียงยาว→สั้น เพื่อให้ match ชื่อเต็มก่อน
_THAI_MONTH_ALT = "|".join(
    sorted(_THAI_MONTHS.keys(), key=len, reverse=True)
)

# ── Date patterns — จากเอกสารจริงของคลินิก ──
_DATE_PATTERNS = [
    # "วันที่สั่ง 2 ต.ค. 2569" หรือ "วันที่สั่ง: 2 ต.ค. 69"
    # "วันที่ 15 ตุลาคม 2569"
    re.compile(
        r'วัน(?:ที่|ที่สั่ง|ที่ตรวจ|ที่ทำ|ที่รับ|ที่บันทึก)\s*[:=]?\s*'
        r'(\d{1,2})\s*[-/\.]?\s*(' + _THAI_MONTH_ALT + r')\s*'
        r'(\d{2,4})',
    ),
    # "วันที่สั่ง 2/10/2569" หรือ "วันที่ 02/10/69"
    re.compile(
        r'วัน(?:ที่|ที่สั่ง|ที่ตรวจ|ที่ทำ|ที่รับ|ที่บันทึก)\s*[:=]?\s*'
        r'(\d{1,2})\s*[/\-\.]\s*(\d{1,2})\s*[/\-\.]\s*(\d{2,4})',
    ),
    # "Date: 2/10/2569" หรือ "Date 02-10-2026"
    re.compile(
        r'[Dd]ate\s*[:=]?\s*(\d{1,2})\s*[/\-\.]\s*(\d{1,2})\s*[/\-\.]\s*(\d{2,4})',
    ),
    # "วันที่ 2 ต.ค. 69" (ท้ายบรรทัด — ไม่มี prefix เฉพาะ)
    re.compile(
        r'(?:^|\n)\s*(\d{1,2})\s*[-/\.]?\s*(' + _THAI_MONTH_ALT + r')\s*'
        r'(\d{2,4})\s*(?:$|\n)',
    ),
]


def extract_document_date(text: str) -> Optional[datetime]:
    """ดึงวันที่จากเอกสาร OCR text — ใช้วันที่สั่ง/วันที่ตรวจ/วันที่ทำ

    รองรับทั้ง:
    - ปี พ.ศ. (2566-2600 → แปลงเป็น ค.ศ.)
    - ปี ค.ศ. (2020-2030)
    - ปี 2 หลัก (67-70 → 2567-2570 → 2024-2027 CE)
    - เดือนไทย (ม.ค., มกราคม ฯลฯ)
    - เดือนตัวเลข (01-12)

    Returns: datetime (UTC) หรือ None ถ้าไม่พบ/parse ไม่ได้
    """
    if not text or len(text.strip()) < 5:
        return None

    for pattern in _DATE_PATTERNS:
        match = pattern.search(text)
        if match:
            groups = match.groups()
            parsed = _parse_date_groups(groups)
            if parsed:
                return parsed

    return None


def _parse_date_groups(groups: tuple) -> Optional[datetime]:
    """Parse date components จาก regex groups

    groups อาจเป็น:
    - (day, thai_month_name, year) — "2 ต.ค. 2569"
    - (day, month_number, year) — "2/10/2569"
    """
    if len(groups) < 3:
        return None

    try:
        day_str, month_part, year_str = groups[0], groups[1], groups[2]

        # ── Day ──
        day = int(day_str)
        if day < 1 or day > 31:
            return None

        # ── Month ──
        month: int
        if month_part in _THAI_MONTHS:
            month = _THAI_MONTHS[month_part]
        else:
            try:
                month = int(month_part)
            except ValueError:
                return None
        if month < 1 or month > 12:
            return None

        # ── Year ──
        year = int(year_str)
        if year < 100:
            # ปี 2 หลัก → ถ้า 40-99 → สมมติ พ.ศ. 25xx
            # ถ้า 20-39 → สมมติ ค.ศ. 20xx
            if year >= 40:
                year = 2500 + year  # เช่น 69 → 2569 BE
            else:
                year = 2000 + year  # เช่น 26 → 2026 CE
        # ── แปลง พ.ศ. → ค.ศ. ──
        if year >= 2500:
            year = year - 543
        # ── ตรวจว่าปีสมเหตุสมผล ──
        if year < 2020 or year > 2030:
            return None

        return datetime(year, month, day, tzinfo=timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None
