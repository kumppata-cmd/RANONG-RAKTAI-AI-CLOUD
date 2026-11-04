# =============================================================================
# IMPORT DIALYSIS EXCEL — นำเข้าข้อมูลตารางฟอกเลือดจาก Excel
# คลินิกระนอง รักษ์ไต × RANONG RAKTAI AI
#
# อ่านไฟล์ Excel "ตารางฟอกเลือดและติดตามยา ESA" → แปลงเป็น list[dict]
# สำหรับส่งเข้า MasterPatientRegistry.import_dialysis_batch()
#
# ESA code mapping (จากแพทย์):
#   E4  = Epiao 4000 unit
#   H4  = Hemax 4000 unit
#   HM4 = Hema-plus 4000 unit (ในไฟล์อาจเขียน "HM")
#
# Safety:
#   - ไม่ส่งข้อมูลผู้ป่วยไป External AI
#   - ใช้ openpyxl อ่าน local file เท่านั้น
# =============================================================================

import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# วันภาษาไทย → English day code
_DAY_MAP = {
    "จ": "mon", "จันทร์": "mon",
    "อ": "tue", "อังคาร": "tue",
    "พ": "wed", "พุธ": "wed",
    "พฤ": "thu", "พฤหัสบดี": "thu",
    "ศ": "fri", "ศุกร์": "fri",
    "ส": "sat", "เสาร์": "sat",
}


def _clean_hn(val) -> str:
    """ทำความสะอาด HN — ตัดทศนิยม, ช่องว่าง"""
    if val is None:
        return ""
    if isinstance(val, float):
        return str(int(val))
    return str(val).strip().split(".")[0]


def _parse_schedule(val) -> str:
    """แปลง schedule text → normalized"""
    if not val:
        return ""
    return str(val).strip()


def parse_dialysis_excel(file_path: str) -> list[dict]:
    """อ่าน Excel และแปลงเป็น list[dict] สำหรับ import

    Args:
        file_path: path ไปยังไฟล์ Excel

    Returns:
        list of dicts with keys:
          clinic_hn, hospital_hn, patient_name,
          dialysis_schedule, hd_per_week, esa_code
    """
    try:
        import openpyxl
    except ImportError:
        logger.error("openpyxl not installed")
        return []

    wb = openpyxl.load_workbook(file_path, data_only=True, read_only=True)

    # ใช้ sheet แรก (ตารางฟอกเลือด)
    ws = wb[wb.sheetnames[0]]
    results = []

    for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if not row or len(row) < 13:
            continue

        # ข้ามแถวว่าง
        seq = row[0]  # ลำดับ
        name = row[1]  # ชื่อ-สกุล
        if not name or (seq is None and not str(name).strip()):
            continue

        hospital_hn = _clean_hn(row[2])   # HN รพ.
        clinic_hn = _clean_hn(row[3])     # HN คลินิก
        schedule = _parse_schedule(row[4])  # รอบตามต้นฉบับ
        hd_per_week = int(row[5]) if row[5] else 0  # HD/สัปดาห์
        esa_code = str(row[12]).strip() if row[12] else ""  # ESA ตามต้นฉบับ

        # กะ/เวลา (col 13), เตียง (col 14), หมายเหตุ (col 15)
        shift = str(row[13]).strip() if len(row) > 13 and row[13] else ""
        bed = str(row[14]).strip() if len(row) > 14 and row[14] else ""

        if not clinic_hn and not hospital_hn:
            continue

        patient_name = str(name).strip()

        results.append({
            "clinic_hn": clinic_hn,
            "hospital_hn": hospital_hn,
            "patient_name": patient_name,
            "dialysis_schedule": schedule,
            "hd_per_week": hd_per_week,
            "esa_code": esa_code,
            "vascular_access": "",  # ยังไม่มีในไฟล์ Excel — เพิ่มภายหลัง
            "shift": shift if shift != "None" and shift != "NaN" else "",
            "bed_number": bed if bed != "None" and bed != "NaN" else "",
        })

    wb.close()
    logger.info("parse_dialysis_excel: parsed %d patients from %s", len(results), file_path)
    return results


def generate_import_summary(patients: list[dict]) -> str:
    """สร้างข้อความสรุปจาก parse result — ใช้แสดงให้แพทย์ยืนยัน"""
    if not patients:
        return "ไม่พบข้อมูลผู้ป่วยในไฟล์"

    total = len(patients)
    esa_counts = {}
    schedule_counts = {}
    for p in patients:
        esa = p.get("esa_code", "")
        if esa:
            esa_counts[esa] = esa_counts.get(esa, 0) + 1
        sched = p.get("dialysis_schedule", "")
        if sched:
            schedule_counts[sched] = schedule_counts.get(sched, 0) + 1

    lines = [
        f"📋 สรุปข้อมูลจาก Excel: {total} คน",
        "",
        "💊 ยา ESA:",
    ]
    for code, count in sorted(esa_counts.items(), key=lambda x: -x[1]):
        lines.append(f"  {code}: {count} คน")

    lines.append("")
    lines.append("📅 รอบฟอก (Top 5):")
    for sched, count in sorted(schedule_counts.items(), key=lambda x: -x[1])[:5]:
        lines.append(f"  {sched}: {count} คน")

    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python import_dialysis_excel.py <excel_file>")
        sys.exit(1)
    patients = parse_dialysis_excel(sys.argv[1])
    print(generate_import_summary(patients))
    print(f"\nTotal: {len(patients)} patients parsed")
    # Save to JSON for inspection
    out_path = sys.argv[1].rsplit(".", 1)[0] + "_parsed.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(patients, f, ensure_ascii=False, indent=2)
    print(f"Saved to: {out_path}")
