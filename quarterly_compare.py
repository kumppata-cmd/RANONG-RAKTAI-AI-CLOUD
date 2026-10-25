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

def build_quarterly_path(folder: str, filename: str, date: datetime | None = None) -> str:
    """สร้าง GCS path สำหรับเอกสารแบ่งไตรมาส.

    เช่น: 04_LAB_RESULTS/2026-Q4/uuid.pdf
    เก็บ YYYY-MM ไว้เป็น subfolder ย่อยเพื่อ backward compatibility
    """
    dt = date or datetime.now(timezone.utc)
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
    dt = date or datetime.now(timezone.utc)
    quarter = get_quarter(dt)
    if patient_folder:
        return f"{folder}/{quarter}/{patient_folder}/{filename}"
    return f"{folder}/{quarter}/{filename}"


def build_legacy_path(folder: str, filename: str, subfolder: str = "") -> str:
    """สร้าง GCS path แบบเดิม (backward compatible).

    เช่น: 01_LINE_INBOX/2026-10/uuid.pdf
    """
    monthly = datetime.now(timezone.utc).strftime("%Y-%m")
    if subfolder:
        safe_sub = subfolder.replace("/", "_").replace("\\", "_").replace("..", "_")
        return f"{folder}/{safe_sub}/{monthly}/{filename}"
    return f"{folder}/{monthly}/{filename}"


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
) -> list[dict]:
    """ดึงประวัติผลแล็บของผู้ป่วย — เรียงตามวันที่"""
    try:
        # ดึง doc ทั้งหมดของ owner ที่เป็น lab
        all_docs = doc_index.list_by_owner(owner_hash) if hasattr(doc_index, 'list_by_owner') else []
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
    # Antihypertensives
    "losartan", "enalapril", "amlodipine", "carvedilol", "metoprolol",
    "nifedipine", "diltiazem", "verapamil", "hydralazine",
    "spironolactone", "hydrochlorothiazide", "doxazosin", "atenolol",
    "prazosin", "furosemide", "manidipine", "felodipine",
    # Statins / Lipid
    "atorvastatin", "simvastatin", "rosuvastatin", "fenofibrate",
    # Anti-platelet / Anticoagulant
    "aspirin", "clopidogrel", "warfarin",
    # DM
    "metformin", "insulin", "glipizide", "gliclazide", "sitagliptin",
    # CKD-specific
    "calcium carbonate", "folic acid", "ferrous sulfate", "calcitriol",
    "sodium bicarbonate", "sevelamer", "lanthanum", "cinacalcet",
    "epo", "erythropoietin", "darbepoetin", "epoetin", "venofer",
    # GI
    "omeprazole", "pantoprazole", "ranitidine", "domperidone",
    "ondansetron",
    # Pain / Neuro
    "paracetamol", "tramadol", "gabapentin", "pregabalin",
    "amitriptyline",
    # Psych
    "fluoxetine", "diazepam", "alprazolam",
    # Anti-convulsant
    "phenytoin", "levetiracetam",
    # Antibiotics
    "ciprofloxacin", "amoxicillin", "cephalexin", "clindamycin",
    "metronidazole", "doxycycline", "azithromycin", "levofloxacin",
    "co-trimoxazole",
    # Gout
    "allopurinol", "colchicine", "febuxostat",
    # Steroids
    "prednisolone", "dexamethasone",
    # Others
    "multivitamin", "vitamin b", "vitamin d", "iron",
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
) -> list[dict]:
    """ดึงประวัติใบยาของผู้ป่วย — เรียงตามวันที่.

    ดึงจาก DocumentIndex: เอกสาร category=rx ของ owner
    แล้วใช้ extracted_text ดึงรายชื่อยาจาก OCR text
    """
    try:
        all_docs = (
            doc_index.list_by_owner(owner_hash)
            if hasattr(doc_index, "list_by_owner")
            else []
        )
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
