"""🧬 Lab Merge — "ผลเลือด 1 ชุดต่อ 1 วัน" ไม่ว่ามาจาก LINE OA (รูปถ่าย) หรือ HD (กรอก/อ่านใบ) (26 พ.ย. 2569)

ปัญหาที่แพทย์พบ: ใบเดียวกันส่งเข้า LINE OA แล้วและกรอก/อ่านใน HD อีก → 2 แถววันเดียวกัน ·
การ์ด "ล่าสุด" ใช้แถวที่วันที่ใหม่สุดแม้แถวนั้นว่างเกือบหมด → หน้าเปิดมาเป็นขีดทั้งหมดทั้งที่ด้านล่างมีค่า

ฟังก์ชันล้วน (ไม่แตะ DB) — ใช้โดย labs-unified:
  merge_by_date(entries)      → 1 แถวต่อวันที่ (ค่าแต่ละตัวเลือกตามลำดับความน่าเชื่อถือ) + แหล่งที่มารวม
  latest_by_analyte(entries)  → ค่าล่าสุดที่ "มีจริง" ของแต่ละตัว พร้อมวันที่ (สำหรับการ์ดล่าสุด)
"""

QUAL_FIELDS = ("hep_b", "hep_c", "anti_hbs", "anti_hiv", "anti_hbc")
META_FIELDS = ("date", "source_type", "source", "verified_status", "doc_row_id", "sources", "pending", "rows")


def _rank(e):
    """ลำดับความน่าเชื่อถือของแถว (มากกว่า = เชื่อกว่า):
    กรอกมือ/อ่านใบที่พยาบาลยืนยันแล้ว (manual) = ยืนยันแล้วจากรูป (verified) > รอยืนยัน (pending) > ปฏิเสธ"""
    st = (e.get("verified_status") or "").lower()
    if e.get("source_type") == "manual" or st == "manual":
        return 3
    return {"verified": 3, "pending": 1, "rejected": 0}.get(st, 1)


def analyte_fields(entries):
    out = []
    for e in entries:
        for k in e:
            if k not in META_FIELDS and k not in out:
                out.append(k)
    return out


def merge_by_date(entries):
    """entries (ใหม่→เก่า, แต่ละตัวมี date/source_type/source/verified_status/doc_row_id + ค่า) →
    list 1 แถวต่อวันที่: ค่าตัวไหนมีหลายแหล่ง → ใช้ของแถวที่ rank สูงกว่า (เท่ากัน → แถวที่มาก่อน)"""
    by_date = {}
    order = []
    for e in entries:
        d = e.get("date")
        if d not in by_date:
            by_date[d] = []
            order.append(d)
        by_date[d].append(e)
    fields = analyte_fields(entries)
    out = []
    for d in order:
        group = sorted(by_date[d], key=_rank, reverse=True)
        row = {"date": d, "sources": [], "pending": False, "rows": len(group),
               "doc_row_id": None, "verified_status": None, "source_type": group[0].get("source_type"),
               "source": group[0].get("source") or ""}
        for e in group:
            stype = e.get("source_type") or "manual"
            if stype not in row["sources"]:
                row["sources"].append(stype)
            st = (e.get("verified_status") or "").lower()
            if stype == "photo":
                if st == "pending":
                    row["pending"] = True
                if row["doc_row_id"] is None and e.get("doc_row_id"):
                    row["doc_row_id"] = e["doc_row_id"]
            if row["verified_status"] is None or _rank(e) > _rank({"verified_status": row["verified_status"], "source_type": "photo"}):
                row["verified_status"] = "manual" if stype == "manual" else (st or "pending")
        for f in fields:
            val = None
            for e in group:                      # เรียงตาม rank แล้ว
                v = e.get(f)
                if v is not None and v != "":
                    val = v
                    break
            row[f] = val
        out.append(row)
    return out


def latest_by_analyte(entries):
    """{field: {"value", "date", "source_type"}} — ค่าล่าสุดที่ไม่ว่างของแต่ละตัว (ข้ามแถวที่ rejected)"""
    out = {}
    for e in sorted(entries, key=lambda x: x.get("date") or "", reverse=True):
        if (e.get("verified_status") or "").lower() == "rejected":
            continue
        for f, v in e.items():
            if f in META_FIELDS or v is None or v == "" or f in out:
                continue
            out[f] = {"value": v, "date": e.get("date"), "source_type": e.get("source_type"),
                      "pending": (e.get("source_type") == "photo" and (e.get("verified_status") or "") == "pending")}
    return out


def row_is_blank(row, shown_fields):
    """แถวที่คอลัมน์ที่แสดงในตารางว่างหมด → บอกว่ามีค่าอื่นอะไรบ้าง (เช่น Iron, TIBC, HBs Ab)"""
    if any(row.get(f) is not None for f in shown_fields):
        return []
    return [f for f in row if f not in META_FIELDS and f not in shown_fields and row.get(f) is not None]
