# =============================================================================
# QUARTERLY COMPARE — ระบบเปรียบเทียบผลแล็บและใบยารายไตรมาส
# คลินิกระนอง รักษ์ไต × RANONG RAKTAI AI 2.0
#
# ฟีเจอร์:
#   1. จัดเก็บเอกสารแบบแบ่งไตรมาส (lab, rx) / เดือน (dialysis)
#   2. เปรียบเทียบผลแล็บ เก่า vs ใหม่ — แสดงการเปลี่ยนแปลง
#   3. เปรียบเทียบใบยา เก่า vs ใหม่ — แสดงยาที่เพิ่ม/ลด/เปลี่ยน
#   4. สร้าง GCS path ที่รองรับ quarterly/monthly sub-folders
#
# Safety: ไม่ส่งข้อมูลผู้ป่วยออกนอกระบบ, ใช้ owner_hash เท่านั้น
# =============================================================================

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from patient_hn_registry import get_quarter, get_quarter_range, get_month_range

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# GCS Path Builders — Quarterly / Monthly
# ─────────────────────────────────────────────────────────────

try:
    from zoneinfo import ZoneInfo
    BANGKOK_TZ = ZoneInfo("Asia/Bangkok")
except Exception:  # pragma: no cover - ถ้าไม่มีฐานข้อมูลเขตเวลาในระบบ
    from datetime import timedelta as _timedelta
    BANGKOK_TZ = timezone(_timedelta(hours=7), "Asia/Bangkok")


def to_bangkok(date: datetime | None = None) -> datetime:
    """แปลงเวลาเป็นเขตเวลาไทย — ใช้ตัดสินว่าเอกสารอยู่เดือน/ไตรมาสใด

    ทำไมต้องมี: คลินิกอยู่ประเทศไทย (UTC+7) แต่เดิมระบบคิดเดือนจาก UTC
    เอกสารที่ส่งเข้ามาช่วง 00:00–07:00 ตามเวลาไทย จึงถูกนับเป็นวันก่อนหน้า
    ตามเวลา UTC และถ้าคาบเกี่ยวสิ้นเดือนก็จะลงแฟ้ม "เดือนก่อน"
    เช่น ส่ง 1 พ.ย. 01:30 น. (ไทย) = 31 ต.ค. 18:30 UTC → เคยลง 2026-10

    datetime ที่ไม่มีเขตเวลา (naive) ถือเป็น UTC ตามที่ระบบใช้มาเดิม
    """
    dt = date or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BANGKOK_TZ)


def bangkok_month(date: datetime | None = None) -> str:
    """เดือนตามปฏิทินไทย เช่น '2026-10'"""
    return to_bangkok(date).strftime("%Y-%m")


def build_quarterly_path(folder: str, filename: str, date: datetime | None = None) -> str:
    """สร้าง GCS path สำหรับเอกสารแบ่งไตรมาส.

    เช่น: 04_LAB_RESULTS/2026-Q4/uuid.pdf
    เก็บ YYYY-MM ไว้เป็น subfolder ย่อยเพื่อ backward compatibility
    """
    dt = to_bangkok(date)
    quarter = get_quarter(dt)
    monthly = dt.strftime("%Y-%m")
    return f"{folder}/{quarter}/{monthly}/{filename}"


def build_monthly_patient_path(
    folder: str, filename: str,
    patient_folder: str = "",
    date: datetime | None = None,
) -> str:
    """สร้าง GCS path สำหรับเอกสารฟอกไตราย 3 เดือน/รายคน.

    เช่น: 05_DIALYSIS_RECORDS/2026-Q4/HN58072_ชื่อ/uuid.pdf
    ถ้าไม่มี patient_folder จะใช้ quarterly path (backward compatible)
    เปลี่ยนจาก monthly เป็น quarterly ตามที่ผู้ใช้ต้องการ
    """
    dt = to_bangkok(date)
    quarter = get_quarter(dt)
    if patient_folder:
        return f"{folder}/{quarter}/{patient_folder}/{filename}"
    return f"{folder}/{quarter}/{filename}"


def build_legacy_path(
    folder: str, filename: str, subfolder: str = "",
    date: datetime | None = None,
) -> str:
    """สร้าง GCS path แบบ folder/[subfolder/]YYYY-MM/filename

    เช่น: 02_BILLS_RECEIPTS/NIKKISO/2026-10/uuid.pdf

    date: เวลาที่ผู้ส่งส่งเอกสารเข้ามา (ควรเป็น timestamp จาก LINE)
          ถ้าไม่ส่งมาจะใช้เวลาปัจจุบัน เดือนคิดตามปฏิทินไทยทุกกรณี
          (ดู to_bangkok)

    เหตุที่ต้องรับ date: เดิมฟังก์ชันนี้ใช้ datetime.now() ตายตัว ทำให้
    การประมวลผลซ้ำ (retry, รวมหลายหน้า, จัดประเภทใหม่) ได้เดือนตามเวลา
    ที่ประมวลผล ไม่ใช่เดือนที่ผู้ส่งส่งเข้ามา
    """
    monthly = bangkok_month(date)
    if subfolder:
        safe_sub = _safe_folder_component(subfolder)
        if safe_sub:
            return f"{folder}/{safe_sub}/{monthly}/{filename}"
    return f"{folder}/{monthly}/{filename}"


# หมวดเอกสารทางคลินิกที่ต้องแยก sub-folder รายไตรมาส/รายเดือน/รายผู้ป่วย
CLINICAL_CATEGORIES = ("lab", "rx", "dialysis")


# อักขระที่ Windows ห้ามใช้ในชื่อไฟล์/โฟลเดอร์ — เอกสารต้องซิงก์ลง Windows
# จึงต้องตัดออก ไม่งั้นไฟล์จะสร้างบนเครื่องปลายทางไม่ได้
_WINDOWS_ILLEGAL = '<>:"|?*'

# ชื่อที่ Windows สงวนไว้ — ใช้เป็นชื่อโฟลเดอร์ไม่ได้
_WINDOWS_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)


def _safe_folder_component(name: str) -> str:
    """ทำความสะอาดชื่อ folder — กัน path traversal และชื่อที่ Windows ใช้ไม่ได้

    รับชื่อที่มีภาษาไทยได้ (ใช้กับแฟ้มรายผู้ป่วย เช่น '57042_สมชาย')
    คืนค่าว่างถ้าทำความสะอาดแล้วไม่เหลืออะไรที่ใช้เป็นชื่อแฟ้มได้
    """
    if not name:
        return ""
    cleaned = name.replace("..", "_").replace("/", "_").replace("\\", "_")
    # อักขระควบคุมและอักขระที่ Windows ห้าม
    cleaned = "".join(
        "_" if (ch in _WINDOWS_ILLEGAL or ord(ch) < 32) else ch
        for ch in cleaned
    )
    # Windows ตัดจุดและช่องว่างท้ายชื่อทิ้งเอง — ตัดให้ตรงกันไว้ก่อน
    cleaned = cleaned.strip().strip(".").strip()
    if not cleaned:
        return ""
    # ชื่อสงวนของ Windows (เทียบแบบไม่สนตัวพิมพ์ และไม่สนนามสกุล)
    if cleaned.split(".")[0].upper() in _WINDOWS_RESERVED_NAMES:
        return f"_{cleaned}"
    return cleaned


def build_document_path(
    folder: str,
    filename: str,
    category: str,
    patient_folder: str = "",
    subfolder: str = "",
    date: datetime | None = None,
) -> str:
    """สร้าง GCS path มาตรฐานเดียวสำหรับทุกหมวดเอกสาร (canonical builder).

    ใช้ที่เดียวทุกจุดที่เขียนไฟล์ขึ้น GCS เพื่อให้โครงสร้างตรงกันเสมอ
    (upload ใหม่, reclassify, retroactive routing)

    โครงสร้าง:
      lab / rx / dialysis  → folder/YYYY-QN/YYYY-MM/[HN_ชื่อ/]uuid.pdf
      อื่นๆ (bills ฯลฯ)    → folder/[subfolder/]YYYY-MM/uuid.pdf

    เอกสารฟอกเลือด (dialysis) ใช้โครงสร้างเดียวกับ lab/rx — แยกไตรมาส
    แล้วแยกเดือน แล้วแยกรายผู้ป่วย
    """
    # เดือน/ไตรมาสคิดตามปฏิทินไทยเสมอ — ดูเหตุผลใน to_bangkok()
    dt = to_bangkok(date)

    if category in CLINICAL_CATEGORIES:
        quarter = get_quarter(dt)
        monthly = dt.strftime("%Y-%m")
        safe_patient = _safe_folder_component(patient_folder)
        if safe_patient:
            return f"{folder}/{quarter}/{monthly}/{safe_patient}/{filename}"
        return f"{folder}/{quarter}/{monthly}/{filename}"

    return build_legacy_path(folder, filename, subfolder, date=dt)


# ─────────────────────────────────────────────────────────────
# Lab Comparison — เปรียบเทียบผลแล็บ 2 ชุด
# ─────────────────────────────────────────────────────────────

class LabComparisonResult:
    """ผลเปรียบเทียบค่าแล็บ 1 ตัว"""

    def __init__(
        self,
        analyte: str,
        old_value: float | None,
        new_value: float | None,
        unit: str = "",
        ref_min: float | None = None,
        ref_max: float | None = None,
    ):
        self.analyte = analyte
        self.old_value = old_value
        self.new_value = new_value
        self.unit = unit
        self.ref_min = ref_min
        self.ref_max = ref_max

    @property
    def change(self) -> float | None:
        if self.old_value is not None and self.new_value is not None:
            return self.new_value - self.old_value
        return None

    @property
    def change_pct(self) -> float | None:
        if self.change is not None and self.old_value and self.old_value != 0:
            return (self.change / abs(self.old_value)) * 100
        return None

    @property
    def direction(self) -> str:
        """↑ เพิ่มขึ้น, ↓ ลดลง, → คงที่, ? ไม่มีข้อมูล"""
        if self.change is None:
            return "?"
        if self.change > 0:
            return "↑"
        if self.change < 0:
            return "↓"
        return "→"

    @property
    def is_abnormal(self) -> bool:
        """ค่าใหม่ผิดปกติหรือไม่"""
        if self.new_value is None:
            return False
        if self.ref_min is not None and self.new_value < self.ref_min:
            return True
        if self.ref_max is not None and self.new_value > self.ref_max:
            return True
        return False

    @property
    def was_abnormal(self) -> bool:
        """ค่าเก่าผิดปกติหรือไม่"""
        if self.old_value is None:
            return False
        if self.ref_min is not None and self.old_value < self.ref_min:
            return True
        if self.ref_max is not None and self.old_value > self.ref_max:
            return True
        return False

    @property
    def status_change(self) -> str:
        """สถานะการเปลี่ยนแปลง: improved, worsened, stable, new"""
        if self.old_value is None:
            return "new"
        if self.new_value is None:
            return "missing"
        if self.was_abnormal and not self.is_abnormal:
            return "improved"
        if not self.was_abnormal and self.is_abnormal:
            return "worsened"
        if self.was_abnormal and self.is_abnormal:
            # ยังผิดปกติ — ดูทิศทาง
            if self.change is not None and abs(self.change) > 0.01:
                # ตรวจดูว่าเข้าใกล้ค่าปกติไหม
                if self.ref_min is not None and self.new_value < self.ref_min:
                    return "improved" if self.change > 0 else "worsened"
                if self.ref_max is not None and self.new_value > self.ref_max:
                    return "improved" if self.change < 0 else "worsened"
            return "stable_abnormal"
        return "stable"

    def to_dict(self) -> dict:
        return {
            "analyte": self.analyte,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "unit": self.unit,
            "change": self.change,
            "change_pct": round(self.change_pct, 1) if self.change_pct is not None else None,
            "direction": self.direction,
            "is_abnormal": self.is_abnormal,
            "was_abnormal": self.was_abnormal,
            "status_change": self.status_change,
            "ref_min": self.ref_min,
            "ref_max": self.ref_max,
        }


class LabComparison:
    """เปรียบเทียบผลแล็บ 2 ชุด (เก่า vs ใหม่)"""

    def __init__(
        self,
        old_doc_id: str,
        new_doc_id: str,
        old_date: str = "",
        new_date: str = "",
        old_quarter: str = "",
        new_quarter: str = "",
    ):
        self.old_doc_id = old_doc_id
        self.new_doc_id = new_doc_id
        self.old_date = old_date
        self.new_date = new_date
        self.old_quarter = old_quarter
        self.new_quarter = new_quarter
        self.results: list[LabComparisonResult] = []
        self.summary: dict = {}

    def compare(self, old_results: list[dict], new_results: list[dict]) -> None:
        """เปรียบเทียบผลแล็บ 2 ชุด

        Args:
            old_results: list ของ lab result dicts จากชุดเก่า
            new_results: list ของ lab result dicts จากชุดใหม่
        """
        # สร้าง lookup ตาม analyte name
        old_map = {}
        for r in old_results:
            name = r.get("analyte_name", "").strip().upper()
            if name:
                old_map[name] = r

        new_map = {}
        for r in new_results:
            name = r.get("analyte_name", "").strip().upper()
            if name:
                new_map[name] = r

        # รวม analyte ทั้งหมด
        all_analytes = sorted(set(list(old_map.keys()) + list(new_map.keys())))

        improved = 0
        worsened = 0
        stable = 0
        new_items = 0

        for analyte in all_analytes:
            old = old_map.get(analyte, {})
            new = new_map.get(analyte, {})

            comp = LabComparisonResult(
                analyte=analyte,
                old_value=_safe_float(old.get("value")),
                new_value=_safe_float(new.get("value")),
                unit=new.get("unit", old.get("unit", "")),
                ref_min=_safe_float(new.get("ref_min", old.get("ref_min"))),
                ref_max=_safe_float(new.get("ref_max", old.get("ref_max"))),
            )
            self.results.append(comp)

            status = comp.status_change
            if status == "improved":
                improved += 1
            elif status in ("worsened", "stable_abnormal"):
                worsened += 1
            elif status == "new":
                new_items += 1
            else:
                stable += 1

        self.summary = {
            "total": len(self.results),
            "improved": improved,
            "worsened": worsened,
            "stable": stable,
            "new_items": new_items,
        }

    def to_dict(self) -> dict:
        return {
            "old_doc_id": self.old_doc_id,
            "new_doc_id": self.new_doc_id,
            "old_date": self.old_date,
            "new_date": self.new_date,
            "old_quarter": self.old_quarter,
            "new_quarter": self.new_quarter,
            "summary": self.summary,
            "results": [r.to_dict() for r in self.results],
        }

    def format_thai_text(self) -> str:
        """แสดงผลเปรียบเทียบเป็นข้อความภาษาไทย — สำหรับ LINE"""
        lines = []
        lines.append("📊 เปรียบเทียบผลแล็บ")
        if self.old_date and self.new_date:
            lines.append(f"📅 {self.old_date} → {self.new_date}")
        if self.old_quarter and self.new_quarter:
            lines.append(f"📆 {self.old_quarter} → {self.new_quarter}")
        lines.append("")

        # แสดงผลที่เปลี่ยนแปลงสำคัญก่อน
        important = [r for r in self.results if r.status_change in ("worsened", "improved")]
        if important:
            lines.append("⚠️ ค่าที่เปลี่ยนแปลงสำคัญ:")
            for r in important:
                emoji = "🔴" if r.status_change == "worsened" else "🟢"
                old_str = f"{r.old_value:.1f}" if r.old_value is not None else "-"
                new_str = f"{r.new_value:.1f}" if r.new_value is not None else "-"
                pct = f" ({r.change_pct:+.1f}%)" if r.change_pct is not None else ""
                lines.append(f"  {emoji} {r.analyte}: {old_str} → {new_str}{pct}")
            lines.append("")

        # สรุป
        s = self.summary
        lines.append(f"📋 สรุป: ดีขึ้น {s.get('improved', 0)} / "
                      f"แย่ลง {s.get('worsened', 0)} / "
                      f"คงที่ {s.get('stable', 0)}")

        if s.get("new_items", 0):
            lines.append(f"  🆕 ค่าใหม่ {s['new_items']} รายการ")

        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# Prescription Comparison — เปรียบเทียบใบยา 2 ชุด
# ─────────────────────────────────────────────────────────────

class RxComparisonResult:
    """ผลเปรียบเทียบยา"""

    def __init__(self, drug_name: str, status: str, old_detail: str = "", new_detail: str = ""):
        self.drug_name = drug_name
        self.status = status  # added, removed, changed, unchanged
        self.old_detail = old_detail
        self.new_detail = new_detail

    def to_dict(self) -> dict:
        return {
            "drug_name": self.drug_name,
            "status": self.status,
            "old_detail": self.old_detail,
            "new_detail": self.new_detail,
        }


class RxComparison:
    """เปรียบเทียบใบยา 2 ชุด"""

    def __init__(self, old_doc_id: str, new_doc_id: str):
        self.old_doc_id = old_doc_id
        self.new_doc_id = new_doc_id
        self.results: list[RxComparisonResult] = []
        self.summary: dict = {}

    def compare(self, old_drugs: list[str], new_drugs: list[str]) -> None:
        """เปรียบเทียบรายการยา 2 ชุด (ใช้ชื่อยา)"""
        old_set = {d.strip().upper() for d in old_drugs if d.strip()}
        new_set = {d.strip().upper() for d in new_drugs if d.strip()}

        added = new_set - old_set
        removed = old_set - new_set
        unchanged = old_set & new_set

        for drug in sorted(added):
            self.results.append(RxComparisonResult(drug, "added", new_detail="เพิ่มใหม่"))
        for drug in sorted(removed):
            self.results.append(RxComparisonResult(drug, "removed", old_detail="หยุดยา"))
        for drug in sorted(unchanged):
            self.results.append(RxComparisonResult(drug, "unchanged"))

        self.summary = {
            "total_old": len(old_set),
            "total_new": len(new_set),
            "added": len(added),
            "removed": len(removed),
            "unchanged": len(unchanged),
        }

    def format_thai_text(self) -> str:
        """แสดงผลเปรียบเทียบเป็นข้อความภาษาไทย"""
        lines = []
        lines.append("💊 เปรียบเทียบใบยา")
        lines.append("")

        added = [r for r in self.results if r.status == "added"]
        removed = [r for r in self.results if r.status == "removed"]

        if added:
            lines.append("🟢 ยาที่เพิ่มใหม่:")
            for r in added:
                lines.append(f"  + {r.drug_name}")
        if removed:
            lines.append("🔴 ยาที่หยุด:")
            for r in removed:
                lines.append(f"  - {r.drug_name}")
        if not added and not removed:
            lines.append("✅ ไม่มีการเปลี่ยนแปลงรายการยา")

        s = self.summary
        lines.append("")
        lines.append(f"📋 สรุป: เดิม {s.get('total_old', 0)} รายการ → "
                      f"ใหม่ {s.get('total_new', 0)} รายการ "
                      f"(เพิ่ม {s.get('added', 0)} / หยุด {s.get('removed', 0)})")

        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "old_doc_id": self.old_doc_id,
            "new_doc_id": self.new_doc_id,
            "summary": self.summary,
            "results": [r.to_dict() for r in self.results],
        }


# ─────────────────────────────────────────────────────────────
# Service layer — ดึงข้อมูลจาก GCS แล้วเปรียบเทียบ
# ─────────────────────────────────────────────────────────────

def compare_lab_reports(
    storage,
    doc_id_old: str,
    doc_id_new: str,
    lab_prefix: str = "_lab_results/",
) -> dict:
    """เปรียบเทียบผลแล็บ 2 เอกสาร — คืน dict พร้อมแสดงผล"""
    try:
        old_data = _load_lab_json(storage, lab_prefix, doc_id_old)
        new_data = _load_lab_json(storage, lab_prefix, doc_id_new)

        if not old_data:
            return {"error": f"ไม่พบผลแล็บเอกสารเก่า (doc_id={doc_id_old[:8]}...)"}
        if not new_data:
            return {"error": f"ไม่พบผลแล็บเอกสารใหม่ (doc_id={doc_id_new[:8]}...)"}

        comp = LabComparison(
            old_doc_id=doc_id_old,
            new_doc_id=doc_id_new,
            old_date=old_data.get("extraction_date", ""),
            new_date=new_data.get("extraction_date", ""),
        )
        comp.compare(
            old_data.get("results", []),
            new_data.get("results", []),
        )

        result = comp.to_dict()
        result["text_th"] = comp.format_thai_text()
        return result

    except Exception as e:
        logger.error("compare_lab_reports failed: %s", e)
        return {"error": f"เปรียบเทียบล้มเหลว: {type(e).__name__}"}


def get_patient_lab_history(
    storage,
    doc_index,
    owner_hash: str,
    lab_prefix: str = "_lab_results/",
    patient_folder: str = "",
) -> list[dict]:
    """ดึงประวัติผลแล็บของผู้ป่วย — เรียงตามวันที่

    ถ้าระบุ patient_folder → ค้นจาก patient_hn_folder แทน owner_hash
    """
    try:
        # ดึง doc ทั้งหมดของ owner ที่เป็น lab
        if patient_folder and hasattr(doc_index, "list_by_patient_folder"):
            all_docs = doc_index.list_by_patient_folder(patient_folder)
        elif hasattr(doc_index, 'list_by_owner'):
            all_docs = doc_index.list_by_owner(owner_hash)
        else:
            all_docs = []
        lab_docs = [d for d in all_docs if d.get("category") == "lab"]

        results = []
        for doc in lab_docs:
            doc_id = doc.get("document_id", "")
            lab_data = _load_lab_json(storage, lab_prefix, doc_id)
            if lab_data and lab_data.get("results"):
                results.append({
                    "doc_id": doc_id,
                    "upload_date": doc.get("upload_date", ""),
                    "quarter": _date_to_quarter(doc.get("upload_date", "")),
                    "result_count": len(lab_data.get("results", [])),
                    "results": lab_data.get("results", []),
                })

        # เรียงตามวันที่
        results.sort(key=lambda x: x.get("upload_date", ""), reverse=True)
        return results

    except Exception as e:
        logger.error("get_patient_lab_history failed: %s", e)
        return []


def compare_latest_labs(
    storage,
    doc_index,
    owner_hash: str,
    lab_prefix: str = "_lab_results/",
) -> dict:
    """เปรียบเทียบผลแล็บล่าสุด vs ก่อนหน้า — สำหรับ LINE command"""
    history = get_patient_lab_history(storage, doc_index, owner_hash, lab_prefix)

    if len(history) < 2:
        return {
            "error": "ต้องมีผลแล็บอย่างน้อย 2 ครั้งจึงจะเปรียบเทียบได้",
            "available": len(history),
        }

    new = history[0]
    old = history[1]

    comp = LabComparison(
        old_doc_id=old["doc_id"],
        new_doc_id=new["doc_id"],
        old_date=old.get("upload_date", ""),
        new_date=new.get("upload_date", ""),
        old_quarter=old.get("quarter", ""),
        new_quarter=new.get("quarter", ""),
    )
    comp.compare(old.get("results", []), new.get("results", []))

    result = comp.to_dict()
    result["text_th"] = comp.format_thai_text()
    return result


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _safe_float(val) -> float | None:
    """แปลงเป็น float อย่างปลอดภัย"""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _load_lab_json(storage, prefix: str, doc_id: str) -> dict | None:
    """โหลด lab result JSON จาก GCS"""
    try:
        path = f"{prefix}{doc_id}.json"
        data = storage.get(path)
        if data:
            return json.loads(data if isinstance(data, str) else data.decode("utf-8"))
    except Exception as e:
        logger.warning("_load_lab_json: %s: %s", doc_id[:8], e)
    return None


def _date_to_quarter(date_str: str) -> str:
    """แปลงวันที่เป็นไตรมาส เช่น '2026-10-15' -> '2026-Q4'"""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return get_quarter(dt)
    except (ValueError, TypeError):
        return ""


# ─────────────────────────────────────────────────────────────
# RX (Prescription) Comparison — Service Layer
# ─────────────────────────────────────────────────────────────

# ── รายชื่อยาที่พบบ่อยในคลินิกไต (ใช้สำหรับ extraction) ──
_KNOWN_DRUGS = [
    # ── Antihypertensives ──
    "losartan", "enalapril", "amlodipine", "carvedilol", "metoprolol",
    "nifedipine", "diltiazem", "verapamil", "hydralazine",
    "spironolactone", "hydrochlorothiazide", "doxazosin", "atenolol",
    "prazosin", "furosemide", "manidipine", "felodipine",
    "lisinopril", "valsartan", "irbesartan", "telmisartan",
    "candesartan", "olmesartan", "propranolol", "bisoprolol",
    "labetalol", "methyldopa", "clonidine", "minoxidil",
    "indapamide", "torsemide", "bumetanide", "chlorthalidone",
    "eplerenone", "perindopril", "ramipril", "benazepril",
    "mannitol", "isosorbide",
    # Brand names (Thailand)
    "norvasc", "cozaar", "diovan", "micardis", "atacand",
    "concor", "betaloc", "lasix", "aldactone",
    # ── Statins / Lipid ──
    "atorvastatin", "simvastatin", "rosuvastatin", "fenofibrate",
    "ezetimibe", "gemfibrozil", "pravastatin", "fluvastatin",
    "lipitor", "crestor", "zocor",
    # ── Anti-platelet / Anticoagulant ──
    "aspirin", "clopidogrel", "warfarin", "enoxaparin",
    "heparin", "rivaroxaban", "apixaban", "dabigatran",
    "ticagrelor", "dipyridamole", "cilostazol",
    "plavix", "xarelto", "eliquis", "clexane",
    # ── DM / Antidiabetics ──
    "metformin", "insulin", "glipizide", "gliclazide", "sitagliptin",
    "vildagliptin", "linagliptin", "saxagliptin", "alogliptin",
    "empagliflozin", "dapagliflozin", "canagliflozin",
    "pioglitazone", "glimepiride", "acarbose", "repaglinide",
    "liraglutide", "semaglutide", "dulaglutide",
    "insulin glargine", "insulin aspart", "insulin lispro",
    "insulin nph", "insulin mixtard",
    "glucophage", "januvia", "galvus", "jardiance", "forxiga",
    # ── CKD-specific / Nephrology ──
    "calcium carbonate", "folic acid", "ferrous sulfate", "calcitriol",
    "sodium bicarbonate", "sevelamer", "lanthanum", "cinacalcet",
    "epo", "erythropoietin", "darbepoetin", "epoetin",
    "epoetin alfa", "epoetin beta",
    "venofer", "iron sucrose", "ferric carboxymaltose",
    "iron dextran", "sodium ferric gluconate",
    "alfacalcidol", "paricalcitol", "cholecalciferol",
    "calcium acetate", "caco3",
    "recormon", "eprex", "aranesp", "mircera", "cera",
    "ferinject", "injectafer",
    "sodium polystyrene sulfonate", "kayexalate",
    "patiromer", "lokelma",
    "desmopressin",
    # Thai names for CKD drugs
    "แคลเซียม คาร์บอเนต", "โฟลิค แอซิด", "เฟอรัส ซัลเฟต",
    "โซเดียม ไบคาร์บอเนต", "อีพีโอ", "อิริโทรพอยอิติน",
    "เฮปาริน", "แคลซิไตรออล",
    # ── GI ──
    "omeprazole", "pantoprazole", "ranitidine", "domperidone",
    "ondansetron", "metoclopramide", "esomeprazole", "lansoprazole",
    "rabeprazole", "famotidine", "cimetidine",
    "lactulose", "bisacodyl", "magnesium hydroxide",
    "aluminium hydroxide", "sucralfate",
    "losec", "nexium", "pariet",
    # ── Pain / Neuro ──
    "paracetamol", "tramadol", "gabapentin", "pregabalin",
    "amitriptyline", "nortriptyline",
    "celecoxib", "meloxicam", "diclofenac", "ibuprofen",
    "naproxen", "ketorolac", "morphine", "fentanyl",
    "carbamazepine", "oxcarbazepine",
    # ── Psych ──
    "fluoxetine", "diazepam", "alprazolam",
    "sertraline", "escitalopram", "mirtazapine",
    "quetiapine", "risperidone", "haloperidol",
    "lorazepam", "clonazepam", "midazolam",
    # ── Anti-convulsant ──
    "phenytoin", "levetiracetam", "valproic acid", "topiramate",
    "lamotrigine",
    # ── Antibiotics ──
    "ciprofloxacin", "amoxicillin", "cephalexin", "clindamycin",
    "metronidazole", "doxycycline", "azithromycin", "levofloxacin",
    "co-trimoxazole", "trimethoprim",
    "ceftriaxone", "cefazolin", "ceftazidime", "cefixime",
    "meropenem", "imipenem", "ertapenem",
    "vancomycin", "teicoplanin", "linezolid",
    "gentamicin", "amikacin",
    "augmentin", "amoxicillin-clavulanate",
    "piperacillin-tazobactam", "ampicillin-sulbactam",
    "fluconazole", "itraconazole",
    "nitrofurantoin", "fosfomycin",
    # ── Gout ──
    "allopurinol", "colchicine", "febuxostat",
    # ── Steroids ──
    "prednisolone", "dexamethasone", "hydrocortisone",
    "methylprednisolone", "fludrocortisone",
    # ── Immunosuppressants (transplant) ──
    "tacrolimus", "cyclosporine", "mycophenolate",
    "azathioprine", "sirolimus", "everolimus",
    "prograf", "cellcept", "sandimmun",
    # ── Supplements / Vitamins ──
    "multivitamin", "vitamin b", "vitamin d", "iron",
    "vitamin b1", "vitamin b6", "vitamin b12", "vitamin c",
    "thiamine", "pyridoxine", "cyanocobalamin",
    "ascorbic acid", "biotin", "zinc", "selenium",
    "potassium chloride", "kcl",
    "calcium", "magnesium",
    # ── Thyroid ──
    "levothyroxine", "propylthiouracil", "methimazole",
    # ── Respiratory ──
    "salbutamol", "ipratropium", "budesonide", "theophylline",
    # ── Others ──
    "sildenafil", "amlodipine-atorvastatin",
    "losartan-hydrochlorothiazide",
]

# ── Regex สำหรับจับชื่อยาจาก OCR text ──
# Pattern: ชื่อยา (1-3 คำ) ตามด้วย ขนาดยา + หน่วย (เช่น "Losartan 50 mg")
# ต้องมีขนาดยา+หน่วย ตามหลังเสมอ เพื่อลด false positive
_RX_WITH_DOSE_RE = re.compile(
    r"(?:^|\n)\s*\d*\.?\s*"                              # optional numbering
    r"([A-Za-z][A-Za-z\-]{2,20}"                         # first word of drug name
    r"(?:\s+[A-Za-z][A-Za-z\-]{2,20}){0,2})"             # optional 1-2 more words
    r"\s+[\d,.]+\s*(?:mg|mcg|g|ml|iu|unit|เม็ด|แคปซูล)",  # REQUIRED: dose + unit
    re.IGNORECASE,
)


def extract_drugs_from_text(ocr_text: str) -> list[str]:
    """ดึงรายชื่อยาจาก OCR text ของใบสั่งยา.

    ใช้ 2 วิธีร่วมกัน:
    1. Known drug matching — จับคู่กับรายชื่อยาที่รู้จัก
    2. Pattern matching — regex จับชื่อยา + ขนาด

    Returns: list ของชื่อยา (deduplicated, sorted)
    """
    if not ocr_text or len(ocr_text.strip()) < 10:
        return []

    text_lower = ocr_text.lower()
    found_drugs: set[str] = set()

    # ── Method 1: Known drug matching ──
    for drug in _KNOWN_DRUGS:
        if drug.lower() in text_lower:
            # Normalize: capitalize first letter
            found_drugs.add(drug.strip().title())

    # ── Method 2: Pattern-based extraction ──
    # จับชื่อยาจาก pattern เช่น "Losartan 50 mg" (ต้องมี dose+unit ตามหลัง)
    for match in _RX_WITH_DOSE_RE.finditer(ocr_text):
        candidate = match.group(1).strip()
        if _is_likely_drug_name(candidate):
            found_drugs.add(_normalize_drug_name(candidate))

    return sorted(found_drugs)


def _is_likely_drug_name(name: str) -> bool:
    """ตรวจสอบว่า string น่าจะเป็นชื่อยา (ไม่ใช่ header, label, etc.)"""
    if not name or len(name) < 3:
        return False
    name_lower = name.lower().strip()

    # ชื่อยาส่วนใหญ่ไม่เกิน 3 คำ (เช่น "Calcium Carbonate", "Folic Acid")
    if len(name_lower.split()) > 3:
        return False

    # ชื่อยาไม่ยาวเกิน 30 ตัวอักษร
    if len(name_lower) > 30:
        return False

    # ── คำที่ไม่ใช่ชื่อยา ──
    _non_drug_words = {
        "name", "date", "patient", "doctor", "hospital", "clinic",
        "diagnosis", "note", "page", "total", "quantity", "amount",
        "prescription", "medication", "drug", "tablet", "capsule",
        "frequency", "duration", "route", "oral", "injection",
        "morning", "evening", "night", "before", "after", "meal",
        "refill", "dispense", "instruction", "warning", "caution",
        "visit", "department", "ward", "bed", "room",
        "hello", "world", "test", "document", "this",
        "ชื่อ", "วันที่", "แพทย์", "โรงพยาบาล", "คลินิก",
        "ผู้ป่วย", "ครั้งละ", "วันละ", "เม็ด", "แคปซูล",
        "หลังอาหาร", "ก่อนอาหาร", "ก่อนนอน", "เช้า", "เย็น",
        "sig", "prn", "bid", "tid", "qid", "stat", "hs",
        "tab", "cap", "inj", "supp", "top", "inh",
    }
    if name_lower in _non_drug_words:
        return False

    # ต้องมีตัวอักษรอย่างน้อย 3 ตัว
    alpha_count = sum(1 for c in name if c.isalpha())
    if alpha_count < 3:
        return False

    # ถ้ามีแต่ตัวเลข ไม่ใช่ชื่อยา
    if name.replace(" ", "").replace("-", "").isdigit():
        return False

    # ถ้ามีคำทั่วไปมากกว่า 1 คำใน string → ไม่ใช่ชื่อยา
    _common_words = {"is", "a", "the", "this", "that", "and", "or", "for",
                     "in", "on", "at", "to", "of", "it", "be", "as", "by"}
    words = name_lower.split()
    common_count = sum(1 for w in words if w in _common_words)
    if common_count >= 2:
        return False

    return True


def _normalize_drug_name(name: str) -> str:
    """Normalize ชื่อยาให้เป็นรูปแบบมาตรฐาน"""
    # ลบ whitespace ซ้ำ
    name = re.sub(r"\s+", " ", name.strip())
    # Title case
    return name.title()


def get_patient_rx_history(
    storage,
    doc_index,
    owner_hash: str,
    patient_folder: str = "",
) -> list[dict]:
    """ดึงประวัติใบยาของผู้ป่วย — เรียงตามวันที่.

    ดึงจาก DocumentIndex: เอกสาร category=rx ของ owner
    แล้วใช้ extracted_text ดึงรายชื่อยาจาก OCR text

    ถ้าระบุ patient_folder → ค้นจาก patient_hn_folder แทน owner_hash
    (สำหรับเอกสารที่ admin/staff อัปโหลดให้ผู้ป่วย)
    """
    try:
        if patient_folder and hasattr(doc_index, "list_by_patient_folder"):
            all_docs = doc_index.list_by_patient_folder(patient_folder)
        elif hasattr(doc_index, "list_by_owner"):
            all_docs = doc_index.list_by_owner(owner_hash)
        else:
            all_docs = []
        rx_docs = [d for d in all_docs if d.get("category") == "rx"]

        results = []
        for doc in rx_docs:
            doc_id = doc.get("document_id", "")
            extracted_text = doc.get("extracted_text", "").strip()

            if not extracted_text:
                # ถ้าไม่มี extracted_text ใน index → ข้ามเอกสารนี้
                logger.info(
                    "get_patient_rx_history: doc %s has no extracted_text",
                    doc_id[:8],
                )
                continue

            # ดึงรายชื่อยาจาก OCR text
            drugs = extract_drugs_from_text(extracted_text)
            if not drugs:
                logger.info(
                    "get_patient_rx_history: doc %s extracted 0 drugs "
                    "from %d chars",
                    doc_id[:8], len(extracted_text),
                )
                continue

            results.append({
                "doc_id": doc_id,
                "upload_date": doc.get("upload_date", ""),
                "quarter": _date_to_quarter(doc.get("upload_date", "")),
                "drug_count": len(drugs),
                "drugs": drugs,
            })

        # เรียงตามวันที่ (ใหม่สุดก่อน)
        results.sort(key=lambda x: x.get("upload_date", ""), reverse=True)
        return results

    except Exception as e:
        logger.error("get_patient_rx_history failed: %s", e)
        return []


def compare_latest_rx(
    storage,
    doc_index,
    owner_hash: str,
) -> dict:
    """เปรียบเทียบใบยาล่าสุด vs ก่อนหน้า — สำหรับ LINE command.

    ใช้ pattern เดียวกับ compare_latest_labs:
    1. ดึงประวัติใบยาของผู้ป่วย
    2. เปรียบเทียบ 2 ชุดล่าสุด
    3. คืนผลเปรียบเทียบเป็น dict พร้อม text_th
    """
    history = get_patient_rx_history(storage, doc_index, owner_hash)

    if len(history) == 0:
        return {
            "error": "ไม่พบใบยาในระบบ กรุณาส่งรูปใบยาก่อน",
            "available": 0,
        }

    if len(history) < 2:
        # มีแค่ 1 ใบ — แสดงรายการยาปัจจุบัน
        current = history[0]
        drugs_text = "\n".join(f"  • {d}" for d in current["drugs"])
        return {
            "error": (
                f"💊 ใบยาปัจจุบัน ({current['upload_date']})\n"
                f"พบ {current['drug_count']} รายการ:\n"
                f"{drugs_text}\n\n"
                "ต้องมีใบยาอย่างน้อย 2 ครั้งจึงจะเปรียบเทียบได้\n"
                "กรุณาส่งใบยาเพิ่มอีก 1 ครั้ง"
            ),
            "available": 1,
        }

    new = history[0]
    old = history[1]

    comp = RxComparison(
        old_doc_id=old["doc_id"],
        new_doc_id=new["doc_id"],
    )
    comp.compare(old.get("drugs", []), new.get("drugs", []))

    result = comp.to_dict()

    # ── สร้าง text_th พร้อมวันที่ ──
    lines = []
    lines.append("💊 เปรียบเทียบใบยา")
    if old.get("upload_date") and new.get("upload_date"):
        lines.append(f"📅 {old['upload_date']} → {new['upload_date']}")
    if old.get("quarter") and new.get("quarter"):
        if old["quarter"] != new["quarter"]:
            lines.append(f"📆 {old['quarter']} → {new['quarter']}")
    lines.append("")

    added = [r for r in comp.results if r.status == "added"]
    removed = [r for r in comp.results if r.status == "removed"]

    if added:
        lines.append("🟢 ยาที่เพิ่มใหม่:")
        for r in added:
            lines.append(f"  + {r.drug_name}")
    if removed:
        lines.append("🔴 ยาที่หยุด:")
        for r in removed:
            lines.append(f"  - {r.drug_name}")
    if not added and not removed:
        lines.append("✅ ไม่มีการเปลี่ยนแปลงรายการยา")

    s = comp.summary
    lines.append("")
    lines.append(
        f"📋 สรุป: เดิม {s.get('total_old', 0)} รายการ → "
        f"ใหม่ {s.get('total_new', 0)} รายการ "
        f"(เพิ่ม {s.get('added', 0)} / หยุด {s.get('removed', 0)})"
    )

    result["text_th"] = "\n".join(lines)
    result["old_date"] = old.get("upload_date", "")
    result["new_date"] = new.get("upload_date", "")
    return result


# ─────────────────────────────────────────────────────────────
# Timeline Comparison — ปัจจุบัน vs 3 เดือน vs 6 เดือน
# ─────────────────────────────────────────────────────────────

def _find_closest_to_target(
    history: list[dict],
    target_date: datetime,
    window_days: int = 45,
) -> dict | None:
    """หาผลที่ใกล้ target_date ที่สุด ภายใน window_days

    history: list ของ dict ที่มี upload_date (YYYY-MM-DD)
    target_date: วันที่เป้าหมาย
    window_days: ช่วงวันที่ยอมรับ (±)
    """
    best = None
    best_diff = None
    for item in history:
        date_str = item.get("upload_date", "")
        if not date_str:
            continue
        try:
            item_date = datetime.strptime(date_str, "%Y-%m-%d")
        except (ValueError, TypeError):
            continue
        diff = abs((item_date - target_date).days)
        if diff <= window_days:
            if best_diff is None or diff < best_diff:
                best = item
                best_diff = diff
    return best


def _format_timeline_value(val: float | None) -> str:
    """จัดรูปแบบค่าตัวเลขสำหรับแสดงผล"""
    if val is None:
        return "-"
    if val == int(val):
        return str(int(val))
    return f"{val:.1f}"


def compare_lab_timeline(
    storage,
    doc_index,
    owner_hash: str,
    lab_prefix: str = "_lab_results/",
    patient_folder: str = "",
) -> dict:
    """เปรียบเทียบผลแล็บย้อนหลัง: ปัจจุบัน vs 3 เดือน vs 6 เดือน

    คืน dict พร้อม text_th สำหรับแสดงใน LINE
    ต้องมีผลอย่างน้อย 2 ครั้งจึงจะเปรียบเทียบได้
    """
    history = get_patient_lab_history(storage, doc_index, owner_hash, lab_prefix, patient_folder=patient_folder)

    if len(history) < 2:
        return {
            "error": (
                f"ต้องมีผลแล็บอย่างน้อย 2 ครั้งจึงจะเปรียบเทียบได้\n"
                f"(พบ {len(history)} ครั้ง)"
            ),
            "available": len(history),
        }

    # ผลล่าสุด = ปัจจุบัน
    current = history[0]
    current_date_str = current.get("upload_date", "")
    try:
        current_date = datetime.strptime(current_date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        current_date = datetime.now()

    # หาผลที่ใกล้ 3 เดือนก่อน (±45 วัน)
    try:
        from dateutil.relativedelta import relativedelta
        target_3m = current_date - relativedelta(months=3)
        target_6m = current_date - relativedelta(months=6)
    except ImportError:
        from datetime import timedelta
        target_3m = current_date - timedelta(days=90)
        target_6m = current_date - timedelta(days=180)

    # ไม่เอาผลปัจจุบัน
    past_history = history[1:]
    result_3m = _find_closest_to_target(past_history, target_3m, window_days=45)
    result_6m = _find_closest_to_target(past_history, target_6m, window_days=45)

    # ถ้า 3m กับ 6m เป็นตัวเดียวกัน ให้เอาแค่ 3m
    if (result_3m and result_6m
            and result_3m.get("doc_id") == result_6m.get("doc_id")):
        result_6m = None

    # ต้องมีอย่างน้อย 1 ผลย้อนหลัง (ไม่ใช่แค่ปัจจุบัน)
    if not result_3m and not result_6m:
        # ใช้ผลก่อนหน้าที่มี (fallback)
        result_3m = past_history[0] if past_history else None
        if not result_3m:
            return {"error": "ไม่พบผลแล็บย้อนหลังสำหรับเปรียบเทียบ"}

    # รวบรวม analytes จากทุกช่วงเวลา
    current_results = {
        r.get("analyte_name", "").strip().upper(): r
        for r in current.get("results", [])
        if r.get("analyte_name", "").strip()
    }
    results_3m = {}
    results_6m = {}
    if result_3m:
        results_3m = {
            r.get("analyte_name", "").strip().upper(): r
            for r in result_3m.get("results", [])
            if r.get("analyte_name", "").strip()
        }
    if result_6m:
        results_6m = {
            r.get("analyte_name", "").strip().upper(): r
            for r in result_6m.get("results", [])
            if r.get("analyte_name", "").strip()
        }

    all_analytes = sorted(set(
        list(current_results.keys())
        + list(results_3m.keys())
        + list(results_6m.keys())
    ))

    # สร้างผลเปรียบเทียบ
    comparisons = []
    improved = 0
    worsened = 0
    stable = 0

    for analyte in all_analytes:
        cur = current_results.get(analyte, {})
        m3 = results_3m.get(analyte, {})
        m6 = results_6m.get(analyte, {})

        cur_val = _safe_float(cur.get("value"))
        m3_val = _safe_float(m3.get("value"))
        m6_val = _safe_float(m6.get("value"))

        ref_min = _safe_float(
            cur.get("ref_min") or m3.get("ref_min") or m6.get("ref_min")
        )
        ref_max = _safe_float(
            cur.get("ref_max") or m3.get("ref_max") or m6.get("ref_max")
        )
        unit = cur.get("unit") or m3.get("unit") or m6.get("unit", "")

        # กำหนดแนวโน้ม
        trend = _determine_trend(cur_val, m3_val, m6_val, ref_min, ref_max)

        comp_item = {
            "analyte": analyte,
            "current": cur_val,
            "month_3": m3_val,
            "month_6": m6_val,
            "unit": unit,
            "ref_min": ref_min,
            "ref_max": ref_max,
            "trend": trend,
        }
        comparisons.append(comp_item)

        if trend in ("improving", "normalized"):
            improved += 1
        elif trend in ("worsening", "became_abnormal"):
            worsened += 1
        else:
            stable += 1

    # สร้างข้อความ
    lines = []
    lines.append("📊 เปรียบเทียบผลแล็บย้อนหลัง")
    lines.append(f"📅 ปัจจุบัน: {current_date_str}")
    if result_3m:
        lines.append(f"📅 ~3 เดือนก่อน: {result_3m.get('upload_date', '?')}")
    if result_6m:
        lines.append(f"📅 ~6 เดือนก่อน: {result_6m.get('upload_date', '?')}")
    lines.append("")

    # แสดงผลที่เปลี่ยนแปลงสำคัญก่อน
    important = [c for c in comparisons
                 if c["trend"] in ("worsening", "became_abnormal",
                                   "improving", "normalized")]
    if important:
        lines.append("⚠️ ค่าที่เปลี่ยนแปลงสำคัญ:")
        for c in important:
            emoji = "🔴" if c["trend"] in ("worsening", "became_abnormal") else "🟢"
            vals = []
            if result_6m and c["month_6"] is not None:
                vals.append(_format_timeline_value(c["month_6"]))
            if result_3m and c["month_3"] is not None:
                vals.append(_format_timeline_value(c["month_3"]))
            vals.append(_format_timeline_value(c["current"]))
            trend_text = _trend_thai(c["trend"])
            unit_str = f" {c['unit']}" if c["unit"] else ""
            lines.append(f"  {emoji} {c['analyte']}: {' → '.join(vals)}{unit_str} ({trend_text})")
        lines.append("")

    # แสดงค่าผิดปกติที่คงที่
    stable_abnormal = [c for c in comparisons
                       if c["trend"] == "stable_abnormal"]
    if stable_abnormal:
        lines.append("🟡 ค่าผิดปกติที่ยังไม่เปลี่ยน:")
        for c in stable_abnormal:
            vals = []
            if result_6m and c["month_6"] is not None:
                vals.append(_format_timeline_value(c["month_6"]))
            if result_3m and c["month_3"] is not None:
                vals.append(_format_timeline_value(c["month_3"]))
            vals.append(_format_timeline_value(c["current"]))
            unit_str = f" {c['unit']}" if c["unit"] else ""
            lines.append(f"  ⚡ {c['analyte']}: {' → '.join(vals)}{unit_str}")
        lines.append("")

    # สรุป
    lines.append(
        f"📋 สรุป: ดีขึ้น {improved} / แย่ลง {worsened} / คงที่ {stable}"
    )
    lines.append(f"  📈 ตรวจทั้งหมด {len(comparisons)} รายการ")

    return {
        "text_th": "\n".join(lines),
        "current_date": current_date_str,
        "date_3m": result_3m.get("upload_date", "") if result_3m else "",
        "date_6m": result_6m.get("upload_date", "") if result_6m else "",
        "summary": {
            "improved": improved,
            "worsened": worsened,
            "stable": stable,
            "total": len(comparisons),
        },
        "comparisons": comparisons,
    }


def _determine_trend(
    current: float | None,
    month_3: float | None,
    month_6: float | None,
    ref_min: float | None,
    ref_max: float | None,
) -> str:
    """กำหนดแนวโน้มจากค่า 3 จุด

    Returns: improving, worsening, stable, stable_abnormal,
             normalized, became_abnormal, new, insufficient
    """
    if current is None:
        return "insufficient"

    # ค่าก่อนหน้าที่ใกล้ที่สุด
    prev = month_3 if month_3 is not None else month_6
    if prev is None:
        return "new"

    def is_abnormal(val: float | None) -> bool:
        if val is None:
            return False
        if ref_min is not None and val < ref_min:
            return True
        if ref_max is not None and val > ref_max:
            return True
        return False

    cur_abnormal = is_abnormal(current)
    prev_abnormal = is_abnormal(prev)

    if prev_abnormal and not cur_abnormal:
        return "normalized"
    if not prev_abnormal and cur_abnormal:
        return "became_abnormal"

    if cur_abnormal and prev_abnormal:
        # ทั้งคู่ผิดปกติ — ดูทิศทาง
        diff = current - prev
        if abs(diff) < 0.01:
            return "stable_abnormal"
        # ดูว่าเข้าใกล้ค่าปกติไหม
        if ref_max is not None and current > ref_max:
            return "improving" if diff < 0 else "worsening"
        if ref_min is not None and current < ref_min:
            return "improving" if diff > 0 else "worsening"
        return "stable_abnormal"

    # ทั้งคู่ปกติ
    diff = current - prev
    if abs(diff) < 0.01:
        return "stable"

    # ดูทิศทาง — ถ้ายังอยู่ในเกณฑ์ปกติ ถือว่า stable
    return "stable"


def _trend_thai(trend: str) -> str:
    """แปลง trend เป็นข้อความไทย"""
    return {
        "improving": "↑ ดีขึ้น",
        "worsening": "↓ แย่ลง",
        "stable": "→ คงที่",
        "stable_abnormal": "⚡ ผิดปกติต่อเนื่อง",
        "normalized": "✅ กลับสู่ปกติ",
        "became_abnormal": "⚠️ เริ่มผิดปกติ",
        "new": "🆕 ค่าใหม่",
        "insufficient": "? ข้อมูลไม่พอ",
    }.get(trend, trend)


# ─────────────────────────────────────────────────────────────
# RX Timeline — เปรียบเทียบใบยาย้อนหลัง
# ─────────────────────────────────────────────────────────────

def compare_rx_timeline(
    storage,
    doc_index,
    owner_hash: str,
    patient_folder: str = "",
) -> dict:
    """เปรียบเทียบใบยาย้อนหลัง: ปัจจุบัน vs 3 เดือน vs 6 เดือน"""
    history = get_patient_rx_history(storage, doc_index, owner_hash, patient_folder=patient_folder)

    if len(history) < 2:
        if len(history) == 1:
            current = history[0]
            drugs_text = "\n".join(f"  • {d}" for d in current["drugs"])
            return {
                "error": (
                    f"💊 ใบยาปัจจุบัน ({current['upload_date']})\n"
                    f"พบ {current['drug_count']} รายการ:\n"
                    f"{drugs_text}\n\n"
                    "ต้องมีใบยาอย่างน้อย 2 ครั้งจึงจะเปรียบเทียบได้"
                ),
                "available": 1,
            }
        return {
            "error": "ไม่พบใบยาในระบบ กรุณาส่งรูปใบยาก่อน",
            "available": 0,
        }

    current = history[0]
    current_date_str = current.get("upload_date", "")
    try:
        current_date = datetime.strptime(current_date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        current_date = datetime.now()

    try:
        from dateutil.relativedelta import relativedelta
        target_3m = current_date - relativedelta(months=3)
        target_6m = current_date - relativedelta(months=6)
    except ImportError:
        from datetime import timedelta
        target_3m = current_date - timedelta(days=90)
        target_6m = current_date - timedelta(days=180)

    past_history = history[1:]
    rx_3m = _find_closest_to_target(past_history, target_3m, window_days=45)
    rx_6m = _find_closest_to_target(past_history, target_6m, window_days=45)

    if rx_3m and rx_6m and rx_3m.get("doc_id") == rx_6m.get("doc_id"):
        rx_6m = None

    if not rx_3m and not rx_6m:
        rx_3m = past_history[0] if past_history else None
        if not rx_3m:
            return {"error": "ไม่พบใบยาย้อนหลังสำหรับเปรียบเทียบ"}

    # รวบรวมยาทุกช่วง
    current_drugs = set(d.upper() for d in current.get("drugs", []))
    drugs_3m = set(d.upper() for d in rx_3m.get("drugs", [])) if rx_3m else set()
    drugs_6m = set(d.upper() for d in rx_6m.get("drugs", [])) if rx_6m else set()

    all_drugs = sorted(current_drugs | drugs_3m | drugs_6m)

    lines = []
    lines.append("💊 เปรียบเทียบใบยาย้อนหลัง")
    lines.append(f"📅 ปัจจุบัน: {current_date_str}")
    if rx_3m:
        lines.append(f"📅 ~3 เดือนก่อน: {rx_3m.get('upload_date', '?')}")
    if rx_6m:
        lines.append(f"📅 ~6 เดือนก่อน: {rx_6m.get('upload_date', '?')}")
    lines.append("")

    # ยาที่เพิ่มมาใหม่ (ไม่มีใน 3m หรือ 6m)
    all_past = drugs_3m | drugs_6m
    newly_added = current_drugs - all_past
    recently_stopped = all_past - current_drugs
    continuous = current_drugs & all_past

    if newly_added:
        lines.append("🟢 ยาที่เพิ่มใหม่:")
        for d in sorted(newly_added):
            lines.append(f"  + {d.title()}")
        lines.append("")

    if recently_stopped:
        lines.append("🔴 ยาที่หยุดแล้ว:")
        for d in sorted(recently_stopped):
            # ระบุว่าหยุดตั้งแต่เมื่อไหร่
            was_in_3m = d in drugs_3m
            was_in_6m = d in drugs_6m
            if was_in_3m and was_in_6m:
                note = "ใช้มาตลอด"
            elif was_in_3m:
                note = "ใช้ 3 เดือนก่อน"
            else:
                note = "ใช้ 6 เดือนก่อน"
            lines.append(f"  - {d.title()} ({note})")
        lines.append("")

    if continuous:
        lines.append(f"✅ ยาต่อเนื่อง: {len(continuous)} รายการ")
        lines.append("")

    # สรุป
    lines.append(
        f"📋 สรุป: ปัจจุบัน {len(current_drugs)} รายการ"
    )
    if rx_3m:
        lines.append(f"  3 เดือนก่อน {len(drugs_3m)} รายการ")
    if rx_6m:
        lines.append(f"  6 เดือนก่อน {len(drugs_6m)} รายการ")
    lines.append(
        f"  (เพิ่ม {len(newly_added)} / หยุด {len(recently_stopped)} / "
        f"ต่อเนื่อง {len(continuous)})"
    )

    return {
        "text_th": "\n".join(lines),
        "current_date": current_date_str,
        "date_3m": rx_3m.get("upload_date", "") if rx_3m else "",
        "date_6m": rx_6m.get("upload_date", "") if rx_6m else "",
        "summary": {
            "current_count": len(current_drugs),
            "newly_added": len(newly_added),
            "recently_stopped": len(recently_stopped),
            "continuous": len(continuous),
        },
    }


# ─────────────────────────────────────────────────────────────
# Dialysis History & Timeline — ใบฟอกเลือดย้อนหลัง
# ─────────────────────────────────────────────────────────────

# ── Regex สำหรับจับข้อมูลฟอกเลือดจาก OCR text ──
_DIALYSIS_PATTERNS = {
    # ══════════════════════════════════════════════════════════
    # Section 1: Pre-dialysis Assessment (น้ำหนัก/ของเหลว)
    # ══════════════════════════════════════════════════════════
    "pre_bw": re.compile(
        r"(?:pre[- ]?(?:hd[- ]?)?(?:BW|weight)|Pre\s*BW|น้ำหนักก่อน(?:ฟอก)?|Last\s*wt)\s*[:\s]*"
        r"(\d+\.?\d*)\s*(?:kg)?",
        re.IGNORECASE,
    ),
    "dry_bw": re.compile(
        r"(?:dry[- ]?(?:BW|weight)|Dry\s*BW|น้ำหนักแห้ง|DW)\s*[:\s]*"
        r"(\d+\.?\d*)\s*(?:kg)?",
        re.IGNORECASE,
    ),
    "wt_gain": re.compile(
        r"(?:Wt\s*[Gg]ain|weight\s*gain|น้ำหนักที่เพิ่ม)\s*[:\s]*"
        r"(\d+\.?\d*)\s*(?:kg)?",
        re.IGNORECASE,
    ),
    "target_uf": re.compile(
        r"(?:Target\s*UF|เป้าหมาย\s*UF)\s*[:\s]*"
        r"(\d+\.?\d*)\s*(?:kg|ml|L)?",
        re.IGNORECASE,
    ),
    "post_bw": re.compile(
        r"(?:post[- ]?(?:hd[- ]?)?(?:BW|weight)|Post\s*BW|น้ำหนักหลัง(?:ฟอก)?)\s*[:\s]*"
        r"(\d+\.?\d*)\s*(?:kg)?",
        re.IGNORECASE,
    ),
    "wt_loss": re.compile(
        r"(?:Wt\s*los[se]|weight\s*loss|น้ำหนักที่ลด|Wt\s*lose)\s*[:\s]*"
        r"(\d+\.?\d*)\s*(?:kg)?",
        re.IGNORECASE,
    ),
    "meal_drink": re.compile(
        r"(?:Meal/?drink|meal\s*and\s*drink|อาหาร/?น้ำ)\s*[:\s]*"
        r"(\d+\.?\d*)\s*(?:kg)?",
        re.IGNORECASE,
    ),

    # ══════════════════════════════════════════════════════════
    # Section 2: Hemodialysis Prescription (เครื่อง/ตัวกรอง)
    # ══════════════════════════════════════════════════════════
    "machine": re.compile(
        r"(?:Machine|เครื่อง)\s*[:\s]*([A-Za-z0-9][A-Za-z0-9 \-\.]{1,20})",
        re.IGNORECASE,
    ),
    "dialyzer": re.compile(
        r"(?:Dialyzer|Filter|ตัวกรอง)\s*[:\s]*"
        r"([A-Za-z][A-Za-z0-9 \-\.]{1,20})",
        re.IGNORECASE,
    ),
    "surface_area": re.compile(
        r"(?:Surface\s*area|พื้นที่)\s*[:\s]*(\d+\.?\d*)\s*(?:m2|m²)?",
        re.IGNORECASE,
    ),
    "use_no": re.compile(
        r"(?:Use\s*No\.?|ใช้ครั้งที่)\s*[:\s]*(\d+)",
        re.IGNORECASE,
    ),
    "last_tcv": re.compile(
        r"(?:Last\s*TCV|TCV)\s*[:\s]*([A-Za-z0-9/\-\.]+)",
        re.IGNORECASE,
    ),
    "hd_no": re.compile(
        r"(?:HD\s*(?:No\.?|no\.?|number)|ครั้งที่|H\.?D\.?\s*#?)\s*[:\s]*(\d+)",
        re.IGNORECASE,
    ),

    # ══════════════════════════════════════════════════════════
    # Section 3: Anticoagulant (ยากันเลือดแข็ง)
    # ══════════════════════════════════════════════════════════
    "duration": re.compile(
        r"(?:Duration|ระยะเวลา(?:ฟอก)?|เวลาฟอก)\s*[:\s]*"
        r"(\d+\.?\d*)\s*(?:hr|hrs|hours?|ชม\.?|ชั่วโมง)?",
        re.IGNORECASE,
    ),
    "anticoagulant_type": re.compile(
        r"(?:Heparin|LMWH|Anticoagulant|ยากันเลือดแข็ง)\s*[:\s]*"
        r"(Heparin|LMWH|Enoxaparin|None|ไม่ใช้|free)",
        re.IGNORECASE,
    ),
    "heparin_loading": re.compile(
        r"(?:Loading|โหลด)\s*[:\s]*(\d+)\s*(?:unit|u|IU)?",
        re.IGNORECASE,
    ),
    "heparin_maintenance": re.compile(
        r"(?:Maintenance|คงไว้)\s*[:\s]*(\d+)\s*(?:unit|u|IU)?",
        re.IGNORECASE,
    ),
    "o2_sat": re.compile(
        r"(?:O2\s*sat|SpO2|O2sat)\s*[:\s]*(\d+\.?\d*)\s*%?",
        re.IGNORECASE,
    ),
    "bt": re.compile(
        r"(?:BT|Body\s*temp|อุณหภูมิ)\s*[:\s]*(\d+\.?\d*)\s*(?:°?C)?",
        re.IGNORECASE,
    ),
    "hct": re.compile(
        r"(?:Hct|hematocrit)\s*[=:\s]*(\d+\.?\d*)\s*%?",
        re.IGNORECASE,
    ),
    "hb": re.compile(
        r"(?:Hb|hemoglobin|Hgb)\s*[=:\s]*(\d+\.?\d*)\s*(?:g/?d[lL])?",
        re.IGNORECASE,
    ),

    # ══════════════════════════════════════════════════════════
    # Section 4: Vascular Access (ทางเข้าหลอดเลือด)
    # ══════════════════════════════════════════════════════════
    "access_type": re.compile(
        r"(?:Access|Vascular\s*access|ทางเข้าหลอดเลือด)\s*[:\s]*"
        r"(AVF|AVG|AV\s*fistula|AV\s*graft|Perm\s*[Cc]ath|Temp\s*[Cc]ath|"
        r"permanent|temporary|DLC|TDC|Jugular|Subclavian|Femoral|"
        r"เอวีเอฟ|เอวีจี)",
        re.IGNORECASE,
    ),
    "access_site": re.compile(
        r"(?:Arm|Leg|Lt|Rt|Left|Right|ซ้าย|ขวา|แขน|ขา)\s*"
        r"(Arm|Leg|Lt|Rt|Left|Right|ซ้าย|ขวา|แขน|ขา)?",
        re.IGNORECASE,
    ),

    # ══════════════════════════════════════════════════════════
    # Section 5: Dialysate (น้ำยาล้างไต)
    # ══════════════════════════════════════════════════════════
    "dialysate_k": re.compile(
        r"(?:K\+?|Potassium)\s*[:\s]*(\d+\.?\d*)\s*(?:mEq/?L)?",
        re.IGNORECASE,
    ),
    "dialysate_ca": re.compile(
        r"(?:Ca|Calcium)\s*[:\s]*(\d+\.?\d*)\s*(?:mEq/?L)?",
        re.IGNORECASE,
    ),
    "dialysate_hco3": re.compile(
        r"(?:Hco3|HCO3|Bicarbonate)\s*[:\s]*(\d+\.?\d*)\s*(?:mEq/?L)?",
        re.IGNORECASE,
    ),
    "dialysate_temp": re.compile(
        r"(?:Dialysate\s*[Tt]emp|temp\.?\s*dialysate|อุณหภูมิน้ำยา)\s*[:\s]*"
        r"(\d+\.?\d*)\s*(?:°?C)?",
        re.IGNORECASE,
    ),
    "dialysate_flow": re.compile(
        r"(?:QD|Qd|[Dd]ialysate\s*[Ff]low)\s*[:\s]*(\d+)\s*(?:ml/?min|cc/?min)?",
        re.IGNORECASE,
    ),
    "na_start": re.compile(
        r"(?:Na\s*start|โซเดียมเริ่ม)\s*[:\s]*(\d+\.?\d*)\s*(?:mEq/?L)?",
        re.IGNORECASE,
    ),
    "na_conc_off": re.compile(
        r"(?:Na\s*conc\s*off|โซเดียมปิด)\s*[:\s]*(\d+\.?\d*)\s*(?:mEq/?L)?",
        re.IGNORECASE,
    ),

    # ══════════════════════════════════════════════════════════
    # Section 6: Medication (ยาที่ให้ระหว่างฟอก)
    # ══════════════════════════════════════════════════════════
    "epo_dose": re.compile(
        r"(?:EPO|Erythropoietin|อีพีโอ)\s*[:\s]*"
        r"(?:Eprex|Epiao|Recormon|NeoRecormon)?\s*"
        r"(?:\(?\s*(?:Vial)?\s*\)?\s*)?"
        r"(\d+)\s*(?:unit|u|IU)?",
        re.IGNORECASE,
    ),
    "epo_route": re.compile(
        r"(?:EPO|Erythropoietin).*?(IV|SC|IM|SubQ|Subcutaneous|Intravenous)",
        re.IGNORECASE,
    ),
    "iron_dose": re.compile(
        r"(?:Iron|Venofer|Ferric|เหล็ก)\s*[:\s]*(\d+)\s*(?:mg)?",
        re.IGNORECASE,
    ),

    # ══════════════════════════════════════════════════════════
    # Section 7: Vital Signs during HD (TIME table)
    # ══════════════════════════════════════════════════════════
    "blood_flow": re.compile(
        r"(?:QB|Qb|BFR|blood\s*flow|อัตราการไหลเลือด)\s*[:\s]*"
        r"(\d+\.?\d*)\s*(?:ml/?min)?",
        re.IGNORECASE,
    ),
    "pre_bp": re.compile(
        r"(?:pre[- ]?(?:hd[- ]?)?BP|ความดันก่อน(?:ฟอก)?)\s*[:\s]*"
        r"(\d{2,3})\s*/\s*(\d{2,3})",
        re.IGNORECASE,
    ),
    "uf_volume": re.compile(
        r"(?:Total\s*UF|UF\s*total|UF|ultra\s*filtration|ปริมาณน้ำที่ดึง)\s*[:\s]*"
        r"(\d+\.?\d*)\s*(?:ml|cc|L|ลิตร)?",
        re.IGNORECASE,
    ),
    "conductivity": re.compile(
        r"(?:Conduct(?:ivity)?|cond\.?)\s*[:\s]*(\d+\.?\d*)\s*(?:mS/?cm)?",
        re.IGNORECASE,
    ),
    "kt_v": re.compile(
        r"(?:Kt/?V|KT/?V)\s*[:\s]*(\d+\.?\d*)",
        re.IGNORECASE,
    ),

    # ══════════════════════════════════════════════════════════
    # Section 8: Complication (ภาวะแทรกซ้อน)
    # ══════════════════════════════════════════════════════════
    "complication": re.compile(
        r"(?:No\s*[Cc]omplication|No\s*complication|ไม่มีภาวะแทรกซ้อน|"
        r"Hypotension|Hypertension|Muscle\s*cramp|Chest\s*pain|"
        r"Headache|Fever|Arrhythmia|Nausea|Vomiting|"
        r"Blood\s*leak|Clot|Air\s*embolism|DDS|"
        r"ความดันต่ำ|ตะคริว|ปวดศีรษะ|คลื่นไส้|"
        r"เลือดรั่ว|ลิ่มเลือด)",
        re.IGNORECASE,
    ),

    # ══════════════════════════════════════════════════════════
    # Section 9: Doctor Note (บันทึกแพทย์)
    # ══════════════════════════════════════════════════════════
    "doctor_note": re.compile(
        r"(?:Doctor\s*note|Docter\s*note|บันทึกแพทย์|Pre\s*HD\s*[Nn]ote)\s*[:\s]*"
        r"(.{5,200}?)(?:\n|$)",
        re.IGNORECASE,
    ),
    "nephrologist": re.compile(
        r"(?:Nephrologist|แพทย์โรคไต)\s*[:\s]*([^\n]{3,50})",
        re.IGNORECASE,
    ),

    # ══════════════════════════════════════════════════════════
    # Section 10: Post HD / Discharge (หลังฟอก/จำหน่าย)
    # ══════════════════════════════════════════════════════════
    "post_bp": re.compile(
        r"(?:post[- ]?(?:hd[- ]?)?BP|BP\s*(?:กลับบ้าน|ก่อนกลับ|หลังฟอก)|ความดันหลัง(?:ฟอก)?)\s*[:\s]*"
        r"(\d{2,3})\s*/?\s*(\d{2,3})",
        re.IGNORECASE,
    ),
    "post_hd_note": re.compile(
        r"(?:Post\s*HD\s*note|หลังฟอกเลือด)\s*[:\s]*(.{3,200}?)(?:\n|$)",
        re.IGNORECASE,
    ),
    "discharge_by": re.compile(
        r"(?:Discharge\s*by|จำหน่าย(?:โดย)?)\s*[:\s]*"
        r"(Home|Admit|เดินกลับ|รถเข็น|กลับบ้าน|นอนต่อ|Wheelchair)",
        re.IGNORECASE,
    ),
    "net_uf_balance": re.compile(
        r"(?:Net\s*UF\s*balance|UF\s*balance|สมดุลน้ำ)\s*[:\s]*"
        r"(-?\d+\.?\d*)\s*(?:ml|cc)?",
        re.IGNORECASE,
    ),
    "total_repletement": re.compile(
        r"(?:Total\s*reple(?:te|ce)ment|น้ำเกลือทดแทน)\s*[:\s]*"
        r"(\d+\.?\d*)\s*(?:ml|cc)?",
        re.IGNORECASE,
    ),
}

# ── String fields (ไม่แปลงเป็น float) ──
_DIALYSIS_STRING_FIELDS = {
    "dialyzer", "access_type", "access_site", "machine",
    "last_tcv", "anticoagulant_type", "epo_route",
    "complication", "doctor_note", "nephrologist",
    "post_hd_note", "discharge_by",
}


def extract_dialysis_data_from_text(ocr_text: str) -> dict:
    """ดึงข้อมูลฟอกเลือดจาก OCR text — ครบ 10 หัวข้อ

    Returns: dict ของค่าที่ดึงได้ เช่น
    {"pre_bw": 65.0, "post_bw": 62.5, "uf_volume": 2500,
     "dialyzer": "SADL", "complication": "No Complication", ...}
    """
    if not ocr_text or len(ocr_text.strip()) < 10:
        return {}

    data = {}
    for key, pattern in _DIALYSIS_PATTERNS.items():
        match = pattern.search(ocr_text)
        if match:
            if key in ("pre_bp", "post_bp"):
                data[f"{key}_sys"] = _safe_float(match.group(1))
                data[f"{key}_dia"] = _safe_float(match.group(2))
            elif key in _DIALYSIS_STRING_FIELDS:
                data[key] = match.group(1).strip()
            else:
                data[key] = _safe_float(match.group(1))

    return data


def get_patient_dialysis_history(
    storage,
    doc_index,
    owner_hash: str,
    patient_folder: str = "",
) -> list[dict]:
    """ดึงประวัติใบฟอกเลือดของผู้ป่วย — เรียงตามวันที่

    ถ้าระบุ patient_folder → ค้นจาก patient_hn_folder แทน owner_hash
    """
    try:
        if patient_folder and hasattr(doc_index, "list_by_patient_folder"):
            all_docs = doc_index.list_by_patient_folder(patient_folder)
        elif hasattr(doc_index, "list_by_owner"):
            all_docs = doc_index.list_by_owner(owner_hash)
        else:
            all_docs = []
        dialysis_docs = [d for d in all_docs if d.get("category") == "dialysis"]
        logger.info(
            "get_patient_dialysis_history: found %d dialysis docs "
            "(total %d docs, folder=%s, hash=%s)",
            len(dialysis_docs), len(all_docs),
            patient_folder[:20] if patient_folder else "-",
            owner_hash[:8] if owner_hash else "-",
        )

        results = []
        for doc in dialysis_docs:
            doc_id = doc.get("document_id", "")
            extracted_text = doc.get("extracted_text", "").strip()

            if not extracted_text:
                logger.info(
                    "dialysis doc %s: no extracted_text, skipping",
                    doc_id[:8],
                )
                continue

            data = extract_dialysis_data_from_text(extracted_text)
            if not data:
                logger.info(
                    "dialysis doc %s: extract_dialysis_data returned empty "
                    "(text_len=%d)",
                    doc_id[:8], len(extracted_text),
                )
                continue

            logger.info(
                "dialysis doc %s: extracted %d params",
                doc_id[:8], len(data),
            )
            results.append({
                "doc_id": doc_id,
                "upload_date": doc.get("upload_date", ""),
                "quarter": _date_to_quarter(doc.get("upload_date", "")),
                "data": data,
                "param_count": len(data),
            })

        results.sort(key=lambda x: x.get("upload_date", ""), reverse=True)
        return results

    except Exception as e:
        logger.error("get_patient_dialysis_history failed: %s", e)
        return []


def compare_dialysis_timeline(
    storage,
    doc_index,
    owner_hash: str,
    patient_folder: str = "",
) -> dict:
    """เปรียบเทียบข้อมูลฟอกเลือดย้อนหลัง: ปัจจุบัน vs 3 เดือน vs 6 เดือน"""
    history = get_patient_dialysis_history(storage, doc_index, owner_hash, patient_folder=patient_folder)

    if len(history) < 2:
        if len(history) == 1:
            current = history[0]
            data_text = "\n".join(
                f"  • {_dialysis_param_thai(k)}: {_format_timeline_value(v)}"
                for k, v in sorted(current["data"].items())
            )
            return {
                "error": (
                    f"🩸 ข้อมูลฟอกเลือดปัจจุบัน ({current['upload_date']})\n"
                    f"พบ {current['param_count']} รายการ:\n"
                    f"{data_text}\n\n"
                    "ต้องมีข้อมูลฟอกเลือดอย่างน้อย 2 ครั้งจึงจะเปรียบเทียบได้"
                ),
                "available": 1,
            }
        return {
            "error": "ไม่พบข้อมูลฟอกเลือดในระบบ กรุณาส่งรูปใบฟอกเลือดก่อน",
            "available": 0,
        }

    current = history[0]
    current_date_str = current.get("upload_date", "")
    try:
        current_date = datetime.strptime(current_date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        current_date = datetime.now()

    try:
        from dateutil.relativedelta import relativedelta
        target_3m = current_date - relativedelta(months=3)
        target_6m = current_date - relativedelta(months=6)
    except ImportError:
        # fallback: ใช้ timedelta แทน relativedelta
        from datetime import timedelta
        target_3m = current_date - timedelta(days=90)
        target_6m = current_date - timedelta(days=180)

    past_history = history[1:]

    # ถ้ามี ≥ 2 เอกสาร → เปรียบเทียบครั้งล่าสุด vs ก่อนหน้า
    # (ไม่ต้องหา 3 เดือน/6 เดือน เพราะเอกสารอาจไม่ได้ส่งทุกเดือน)
    d_3m = None
    d_6m = None
    if past_history:
        d_3m = _find_closest_to_target(past_history, target_3m, window_days=60)
        d_6m = _find_closest_to_target(past_history, target_6m, window_days=60)

    if d_3m and d_6m and d_3m.get("doc_id") == d_6m.get("doc_id"):
        d_6m = None

    # ถ้าหา 3/6 เดือนไม่เจอ → ใช้เอกสารก่อนหน้าล่าสุด
    if not d_3m and not d_6m:
        d_3m = past_history[0] if past_history else None
        if not d_3m:
            return {"error": "ไม่พบข้อมูลฟอกเลือดย้อนหลังสำหรับเปรียบเทียบ"}

    # รวบรวมค่าทั้งหมด
    current_data = current.get("data", {})
    data_3m = d_3m.get("data", {}) if d_3m else {}
    data_6m = d_6m.get("data", {}) if d_6m else {}

    all_params = sorted(set(
        list(current_data.keys()) + list(data_3m.keys()) + list(data_6m.keys())
    ))

    lines = []
    lines.append("🩸 เปรียบเทียบฟอกเลือด 10 หัวข้อ")
    lines.append("━━━━━━━━━━━━━━━━━━")
    _prev_label = d_3m.get("upload_date", "?") if d_3m else (
        d_6m.get("upload_date", "?") if d_6m else "?"
    )
    lines.append(f"📅 ครั้งก่อน: {_prev_label}")
    lines.append(f"📅 ครั้งนี้: {current_date_str}")
    lines.append("")

    # ใช้ previous = d_3m ก่อน ถ้าไม่มีใช้ d_6m
    prev_data = data_3m if d_3m else data_6m

    changed_count = 0
    same_count = 0
    new_count = 0

    for section_name, section_keys in _DIALYSIS_SECTIONS:
        # กรองเฉพาะ keys ที่มีข้อมูลในครั้งนี้หรือครั้งก่อน
        section_items = []
        for key in section_keys:
            cur_val = current_data.get(key)
            prev_val = prev_data.get(key)
            if cur_val is not None or prev_val is not None:
                section_items.append((key, cur_val, prev_val))

        if not section_items:
            continue

        lines.append(f"\n{section_name}")

        for key, cur_val, prev_val in section_items:
            label = _dialysis_param_thai(key)
            cur_str = _format_timeline_value(cur_val) if cur_val is not None else "-"
            prev_str = _format_timeline_value(prev_val) if prev_val is not None else "-"

            if cur_val is not None and prev_val is not None:
                # มีทั้ง 2 ค่า → เปรียบเทียบ
                if isinstance(cur_val, (int, float)) and isinstance(prev_val, (int, float)):
                    diff = cur_val - prev_val
                    if abs(diff) < 0.01:
                        mark = "="
                        same_count += 1
                    elif diff > 0:
                        mark = f"↑+{abs(diff):.1f}" if abs(diff) >= 0.1 else "↑"
                        changed_count += 1
                    else:
                        mark = f"↓-{abs(diff):.1f}" if abs(diff) >= 0.1 else "↓"
                        changed_count += 1
                else:
                    # String compare
                    if str(cur_val).strip() == str(prev_val).strip():
                        mark = "="
                        same_count += 1
                    else:
                        mark = "🔄"
                        changed_count += 1
                lines.append(f"  {label}: {prev_str} → {cur_str} {mark}")
            elif cur_val is not None:
                lines.append(f"  {label}: {cur_str} (ใหม่)")
                new_count += 1
            else:
                lines.append(f"  {label}: {prev_str} → - (ไม่มี)")

    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append(
        f"📊 สรุป: เปลี่ยน {changed_count} | "
        f"คงเดิม {same_count} | "
        f"ใหม่ {new_count}"
    )

    return {
        "text_th": "\n".join(lines),
        "current_date": current_date_str,
        "date_3m": d_3m.get("upload_date", "") if d_3m else "",
        "date_6m": d_6m.get("upload_date", "") if d_6m else "",
    }


def _dialysis_param_thai(param: str) -> str:
    """แปลชื่อ parameter ฟอกเลือดเป็นไทย"""
    return {
        # Section 1: Pre-dialysis
        "pre_bw": "น้ำหนักก่อน (kg)",
        "dry_bw": "น้ำหนักแห้ง (kg)",
        "wt_gain": "น้ำหนักเพิ่ม (kg)",
        "target_uf": "เป้า UF (kg)",
        "post_bw": "น้ำหนักหลัง (kg)",
        "wt_loss": "น้ำหนักลด (kg)",
        "meal_drink": "อาหาร/น้ำ (kg)",
        # legacy names
        "pre_weight": "น้ำหนักก่อน (kg)",
        "post_weight": "น้ำหนักหลัง (kg)",
        "dry_weight": "น้ำหนักแห้ง (kg)",
        # Section 2: Prescription
        "machine": "เครื่อง",
        "dialyzer": "ตัวกรอง",
        "surface_area": "พื้นที่ (m²)",
        "use_no": "ใช้ครั้งที่",
        "last_tcv": "Last TCV",
        "hd_no": "HD ครั้งที่",
        # Section 3: Anticoagulant
        "duration": "เวลาฟอก (ชม.)",
        "anticoagulant_type": "ยากันเลือดแข็ง",
        "heparin_loading": "Heparin Loading (u)",
        "heparin_maintenance": "Heparin Maintenance (u)",
        "heparin_dose": "Heparin (u)",
        "o2_sat": "O2 sat (%)",
        "bt": "อุณหภูมิ (°C)",
        "hct": "Hct (%)",
        "hb": "Hb (g/dL)",
        # Section 4: Vascular Access
        "access_type": "ทางเข้าหลอดเลือด",
        "access_site": "ตำแหน่ง",
        # Section 5: Dialysate
        "dialysate_k": "K+ (mEq/L)",
        "dialysate_ca": "Ca (mEq/L)",
        "dialysate_hco3": "HCO3 (mEq/L)",
        "dialysate_temp": "อุณหภูมิน้ำยา (°C)",
        "dialysate_flow": "Dialysate flow (ml/min)",
        "na_start": "Na start (mEq/L)",
        "na_conc_off": "Na conc off (mEq/L)",
        # Section 6: Medication
        "epo_dose": "EPO (unit)",
        "epo_route": "EPO route",
        "iron_dose": "Iron (mg)",
        # Section 7: Vital Signs
        "blood_flow": "Blood flow QB (ml/min)",
        "pre_bp_sys": "BP ก่อนฟอก (ตัวบน)",
        "pre_bp_dia": "BP ก่อนฟอก (ตัวล่าง)",
        "uf_volume": "Total UF (ml)",
        "conductivity": "Conductivity (mS/cm)",
        "kt_v": "Kt/V",
        # Section 8: Complication
        "complication": "ภาวะแทรกซ้อน",
        # Section 9: Doctor Note
        "doctor_note": "บันทึกแพทย์",
        "nephrologist": "แพทย์โรคไต",
        # Section 10: Post HD / Discharge
        "post_bp_sys": "BP หลังฟอก (ตัวบน)",
        "post_bp_dia": "BP หลังฟอก (ตัวล่าง)",
        "post_hd_note": "Post HD note",
        "discharge_by": "จำหน่ายโดย",
        "net_uf_balance": "Net UF balance (ml)",
        "total_repletement": "น้ำเกลือทดแทน (ml)",
    }.get(param, param)


# ── Section grouping สำหรับแสดงผลเปรียบเทียบ ──
_DIALYSIS_SECTIONS = [
    ("1️⃣ Pre-dialysis", [
        "pre_bw", "dry_bw", "wt_gain", "target_uf",
        "post_bw", "wt_loss", "meal_drink",
        # legacy
        "pre_weight", "post_weight", "dry_weight",
    ]),
    ("2️⃣ Prescription", [
        "machine", "dialyzer", "surface_area", "use_no",
        "last_tcv", "hd_no",
    ]),
    ("3️⃣ Anticoagulant", [
        "duration", "anticoagulant_type",
        "heparin_loading", "heparin_maintenance", "heparin_dose",
        "o2_sat", "bt", "hct", "hb",
    ]),
    ("4️⃣ Vascular Access", [
        "access_type", "access_site",
    ]),
    ("5️⃣ Dialysate", [
        "dialysate_k", "dialysate_ca", "dialysate_hco3",
        "dialysate_temp", "dialysate_flow",
        "na_start", "na_conc_off",
    ]),
    ("6️⃣ Medication", [
        "epo_dose", "epo_route", "iron_dose",
    ]),
    ("7️⃣ Vital Signs", [
        "blood_flow", "pre_bp_sys", "pre_bp_dia",
        "uf_volume", "conductivity", "kt_v",
    ]),
    ("8️⃣ Complication", [
        "complication",
    ]),
    ("9️⃣ Doctor Note", [
        "doctor_note", "nephrologist",
    ]),
    ("🔟 Post HD / Discharge", [
        "post_bp_sys", "post_bp_dia",
        "post_hd_note", "discharge_by",
        "net_uf_balance", "total_repletement",
    ]),
]
