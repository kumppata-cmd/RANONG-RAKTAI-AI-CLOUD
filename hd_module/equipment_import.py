"""Parse the clinic's dialysis-machine register (CSV / XLSX) into hd_equipment rows.

Columns are found by header (Thai or English, any order):
    เครื่องที่ / เตียง / bed · ยี่ห้อ / brand · รุ่น / model · รหัสเครื่อง / S/N / serial ·
    ปีที่ซื้อ / purchase_year (พ.ศ. or ค.ศ.) · สัญญาสิ้นสุด / contract_end (Thai long date,
    d/m/Y, ISO, พ.ศ. ok) · ประเภท / type · สถานะ / status · ผู้รับจ้าง / vendor · หมายเหตุ
Rows whose brand is blank/"ว่าง" are skipped (empty slots on the form).
"""
import io
import re
from datetime import date, datetime

TH_MONTHS = {"มกราคม": 1, "กุมภาพันธ์": 2, "มีนาคม": 3, "เมษายน": 4, "พฤษภาคม": 5, "มิถุนายน": 6, "กรกฎาคม": 7, "กรกฏาคม": 7,
             "สิงหาคม": 8, "กันยายน": 9, "ตุลาคม": 10, "พฤศจิกายน": 11, "ธันวาคม": 12,
             "ม.ค.": 1, "ก.พ.": 2, "มี.ค.": 3, "เม.ย.": 4, "พ.ค.": 5, "มิ.ย.": 6, "ก.ค.": 7, "ส.ค.": 8, "ก.ย.": 9, "ต.ค.": 10, "พ.ย.": 11, "ธ.ค.": 12}
HEADERS = {"เครื่องที่": "bed", "เตียง": "bed", "bed": "bed", "no": "bed", "ลำดับ": "bed",
           "ยี่ห้อ": "brand", "ยี่ห้อ/ผลิตภัณฑ์": "brand", "brand": "brand", "ผลิตภัณฑ์": "brand",
           "รุ่น": "model", "model": "model",
           "รหัสเครื่อง": "serial", "s/n": "serial", "serial": "serial", "serialnumber": "serial", "หมายเลขเครื่อง": "serial",
           "ปีที่ซื้อ": "purchase_year", "ปีที่ซื้อ/เช่า": "purchase_year", "purchase_year": "purchase_year", "ปี": "purchase_year",
           "สัญญาสิ้นสุด": "contract_end", "วันสิ้นสุดสัญญา": "contract_end", "contract_end": "contract_end", "สัญญา": "contract_end",
           "ประเภท": "type", "type": "type", "สถานะ": "status", "status": "status",
           "ผู้รับจ้าง": "vendor", "vendor": "vendor", "บริษัท": "vendor", "หมายเหตุ": "notes", "notes": "notes"}
TYPE_MAP = {"เครื่องฟอก": "dialysis_machine", "เครื่องไตเทียม": "dialysis_machine", "dialysis": "dialysis_machine", "ro": "ro_system",
            "เครื่องชั่ง": "scale", "defib": "defibrillator"}
STATUS_MAP = {"ใช้งาน": "active", "active": "active", "สำรอง": "spare", "spare": "spare", "ซ่อม": "repair", "repair": "repair",
              "ปลดระวาง": "retired", "retired": "retired"}
BRAND_FIX = {"fresineus": "Fresenius", "fresenius": "Fresenius", "nikkiso": "NIKKISO", "nipro": "Nipro", "b.braun": "B. Braun", "bbraun": "B. Braun"}


def _clean(v):
    return "" if v is None else re.sub(r"\s+", " ", str(v)).strip()


def _norm_h(h):
    return re.sub(r"\s+", "", _clean(h)).lower()


def parse_thai_date(v):
    """'12 เมษายน 2570' / '31 ธ.ค. 2568' / '13/7/2569' / date / ISO → date or None."""
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = _clean(v)
    m = re.match(r"^(\d{1,2})\s+([ก-๙.]+)\s+(\d{4})$", s)
    if m and m.group(2) in TH_MONTHS:
        y = int(m.group(3))
        return date(y - 543 if y > 2400 else y, TH_MONTHS[m.group(2)], int(m.group(1)))
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            d = datetime.strptime(s, fmt).date()
            return d.replace(year=d.year - 543) if d.year > 2400 else d
        except ValueError:
            pass
    return None


def parse_year(v):
    s = _clean(v)
    m = re.search(r"\d{4}", s)
    if not m:
        return None
    y = int(m.group(0))
    return y - 543 if y > 2400 else y


def _rows_from_file(filename, data):
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        return [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
    import csv
    return [row for row in csv.reader(io.StringIO(data.decode("utf-8-sig", errors="replace")))]


def parse_rows(raw):
    hdr = None
    for i, r in enumerate(raw[:15]):
        m = {ci: HEADERS[_norm_h(h)] for ci, h in enumerate(r) if _norm_h(h) in HEADERS}
        if "brand" in m.values() and ("serial" in m.values() or "model" in m.values()):
            hdr, start = m, i + 1
            break
    if not hdr:
        return [], ["ไม่พบหัวตาราง (ต้องมี ยี่ห้อ และ รหัสเครื่อง/รุ่น)"]
    out, errors = [], []
    for n, r in enumerate(raw[start:], start=start + 1):
        rec = {}
        for ci, f in hdr.items():
            rec[f] = r[ci] if ci < len(r) else None
        brand = _clean(rec.get("brand"))
        if not brand or brand in ("ว่าง", "-"):
            continue
        serial = _clean(rec.get("serial"))
        if serial in ("ว่าง", "-"):
            serial = ""
        bed = None
        bm = re.search(r"\d+", _clean(rec.get("bed")))
        if bm:
            bed = int(bm.group(0))
        ce = parse_thai_date(rec.get("contract_end"))
        if _clean(rec.get("contract_end")) and not ce and _clean(rec.get("contract_end")) not in ("ว่าง", "-"):
            errors.append(f"แถว {n}: อ่านวันสิ้นสุดสัญญาไม่ได้ ({_clean(rec.get('contract_end'))})")
        t = _clean(rec.get("type")).lower()
        typ = next((v for k, v in TYPE_MAP.items() if k in t), "dialysis_machine")
        st = STATUS_MAP.get(_clean(rec.get("status")).lower()) if rec.get("status") else None
        out.append({"row": n, "bed": bed, "brand": BRAND_FIX.get(brand.lower(), brand), "model": _clean(rec.get("model")) or None,
                    "serial": serial or None, "purchase_year": parse_year(rec.get("purchase_year")),
                    "contract_end": ce.isoformat() if ce else None, "type": typ, "status": st,
                    "vendor": _clean(rec.get("vendor")) or None, "notes": _clean(rec.get("notes")) or None})
    return out, errors


def parse_file(filename, data):
    return parse_rows(_rows_from_file(filename, data))
