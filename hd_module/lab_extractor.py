"""N4: PDF Lab Report Auto-Extract — ดึงค่าแล็บจากข้อความ OCR

ใช้ regex patterns เพื่อดึงค่าตรวจแล็บทั่วไป:
- CBC: Hb, Hct, WBC, Plt
- Renal: BUN, Cr, eGFR, UA
- Electrolytes: Na, K, Cl, HCO3, Ca, PO4, Mg
- Hepatic: AST, ALT, ALP, Albumin
- Diabetes: FBS, HbA1c
- Dialysis: Kt/V, URR
- Others: PTH, Ferritin, TIBC, Iron
"""

import re
import logging

logger = logging.getLogger(__name__)

# Pattern: lab_name → (regex_pattern, unit, normal_range)
LAB_PATTERNS = {
    # CBC
    "Hb": (r"(?:Hb|Hemoglobin|hgb)[:\s]*(\d+\.?\d*)", "g/dL", "12-16"),
    "Hct": (r"(?:Hct|Hematocrit)[:\s]*(\d+\.?\d*)", "%", "36-48"),
    "WBC": (r"(?:WBC|White\s*[Bb]lood)[:\s]*(\d+\.?\d*)", "×10³/µL", "4-11"),
    "Plt": (r"(?:Plt|Platelet)[:\s]*(\d+\.?\d*)", "×10³/µL", "150-400"),

    # Renal
    "BUN": (r"(?:BUN|Blood\s*Urea)[:\s]*(\d+\.?\d*)", "mg/dL", "7-20"),
    "Cr": (r"(?:Cr|Creatinine|Creat)[:\s]*(\d+\.?\d*)", "mg/dL", "0.6-1.2"),
    "eGFR": (r"(?:eGFR|GFR)[:\s]*(\d+\.?\d*)", "mL/min", ">60"),
    "UA": (r"(?:UA|Uric\s*[Aa]cid)[:\s]*(\d+\.?\d*)", "mg/dL", "3-7"),

    # Electrolytes
    "Na": (r"(?:Na|Sodium)[:\s]*(\d+\.?\d*)", "mEq/L", "135-145"),
    "K": (r"(?:\bK\b|Potassium)[:\s]*(\d+\.?\d*)", "mEq/L", "3.5-5.0"),
    "Cl": (r"(?:Cl|Chloride)[:\s]*(\d+\.?\d*)", "mEq/L", "96-106"),
    "HCO3": (r"(?:HCO3|Bicarb)[:\s]*(\d+\.?\d*)", "mEq/L", "22-26"),
    "Ca": (r"(?:\bCa\b|Calcium)[:\s]*(\d+\.?\d*)", "mg/dL", "8.5-10.5"),
    "PO4": (r"(?:PO4|Phosph)[:\s]*(\d+\.?\d*)", "mg/dL", "2.5-4.5"),
    "Mg": (r"(?:\bMg\b|Magnesium)[:\s]*(\d+\.?\d*)", "mg/dL", "1.5-2.5"),

    # Hepatic
    "Albumin": (r"(?:Albumin|Alb)[:\s]*(\d+\.?\d*)", "g/dL", "3.5-5.0"),
    "AST": (r"(?:AST|SGOT)[:\s]*(\d+\.?\d*)", "U/L", "<40"),
    "ALT": (r"(?:ALT|SGPT)[:\s]*(\d+\.?\d*)", "U/L", "<41"),

    # Diabetes
    "FBS": (r"(?:FBS|Fasting\s*[Bb]lood\s*[Ss]ugar|Glucose)[:\s]*(\d+\.?\d*)", "mg/dL", "70-100"),
    "HbA1c": (r"(?:HbA1c|A1C|A1c)[:\s]*(\d+\.?\d*)", "%", "<6.5"),

    # Dialysis adequacy
    "Kt/V": (r"(?:Kt/?V|KTV)[:\s]*(\d+\.?\d*)", "", "≥1.2"),
    "URR": (r"(?:URR|Urea\s*Reduction)[:\s]*(\d+\.?\d*)", "%", "≥65"),

    # Iron/Anemia
    "Ferritin": (r"(?:Ferritin)[:\s]*(\d+\.?\d*)", "ng/mL", "100-500"),
    "Iron": (r"(?:Iron|Fe\b)[:\s]*(\d+\.?\d*)", "µg/dL", "60-170"),
    "TIBC": (r"(?:TIBC)[:\s]*(\d+\.?\d*)", "µg/dL", "250-370"),
    "TSAT": (r"(?:TSAT|Transferrin\s*Sat)[:\s]*(\d+\.?\d*)", "%", "20-50"),
    "iPTH": (r"(?:iPTH|PTH|Parathyroid)[:\s]*(\d+\.?\d*)", "pg/mL", "150-600"),
}


def extract_lab_values(text: str) -> dict:
    """ดึงค่าแล็บจาก OCR text → dict ของค่าที่พบ

    Returns: {
        "found": [{"name": "Hb", "value": 10.5, "unit": "g/dL",
                   "normal": "12-16", "flag": "L"}, ...],
        "count": 5,
    }
    """
    if not text:
        return {"found": [], "count": 0}

    results = []
    text_clean = text.replace("\n", " ").replace("\r", " ")

    for lab_name, (pattern, unit, normal) in LAB_PATTERNS.items():
        matches = re.finditer(pattern, text_clean, re.IGNORECASE)
        for m in matches:
            try:
                val = float(m.group(1))
                # Simple flag logic
                flag = ""
                if "-" in normal:
                    lo, hi = normal.split("-")
                    if val < float(lo):
                        flag = "L"
                    elif val > float(hi):
                        flag = "H"
                elif normal.startswith(">"):
                    if val < float(normal[1:]):
                        flag = "L"
                elif normal.startswith("<"):
                    if val > float(normal[1:]):
                        flag = "H"

                results.append({
                    "name": lab_name,
                    "value": val,
                    "unit": unit,
                    "normal_range": normal,
                    "flag": flag,
                })
                break  # Only first match per lab
            except (ValueError, IndexError):
                continue

    return {"found": results, "count": len(results)}


def format_lab_summary_th(lab_data: dict) -> str:
    """Format ค่าแล็บเป็นข้อความภาษาไทยสำหรับ LINE"""
    items = lab_data.get("found", [])
    if not items:
        return "ไม่พบค่าแล็บในเอกสาร"

    lines = ["📋 ผลตรวจแล็บ:"]
    for item in items:
        flag = ""
        if item["flag"] == "H":
            flag = " ⬆️"
        elif item["flag"] == "L":
            flag = " ⬇️"
        lines.append(
            f"  {item['name']}: {item['value']} {item['unit']}"
            f" ({item['normal_range']}){flag}"
        )
    return "\n".join(lines)


def format_lab_summary_en(lab_data: dict) -> str:
    """Format lab values in English for reports"""
    items = lab_data.get("found", [])
    if not items:
        return "No lab values found in document"

    lines = ["📋 Lab Results:"]
    for item in items:
        flag = ""
        if item["flag"] == "H":
            flag = " ↑HIGH"
        elif item["flag"] == "L":
            flag = " ↓LOW"
        lines.append(
            f"  {item['name']}: {item['value']} {item['unit']}"
            f" (ref: {item['normal_range']}){flag}"
        )
    return "\n".join(lines)
