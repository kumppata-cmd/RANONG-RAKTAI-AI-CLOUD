"""แฟ้มเอกสารผู้ป่วย — จัดการใบผิดคน/ใบซ้ำ (22 พ.ย. 2569, แพทย์อนุมัติ)

หลักความแม่นยำที่ยึด:
1. ไม่สร้างตรรกะย้าย/จัดหมวดใหม่ — เรียกฟังก์ชันของ server.py ตัวเดียวกับ
   คำสั่ง LINE (_move_document_to_patient / _change_document_category)
   ผ่าน sys.modules ของ process เดียวกัน เพื่อไม่ให้มี "สองสมอง" ที่เพี้ยนจากกัน
2. ไม่มีการลบไฟล์จริงเลย (ระบบไม่มีสิทธิ์ลบโดยเจตนา) — "เอาออกจากแฟ้ม"
   = เปลี่ยนหมวดเป็น review + ล้าง patient_hn_folder (ย้อนกลับได้เสมอ)
3. ทุกการกระทำลง hd_doc_cleanup_log — เป็นทั้ง audit และรายงาน
   "ไฟล์ตกค้างในคอม/Drive ที่ต้องลบมือ" (สำเนาเก่าไม่ถูกลบอัตโนมัติ)
"""
import logging
import sys

logger = logging.getLogger("hd.patient_docs")

# action: refile / category / unfile · path_left: path เดิมที่สำเนายังค้าง (คอม/Drive/GCS)
CLEANUP_SQL = """
CREATE TABLE IF NOT EXISTS hd_doc_cleanup_log (
    id SERIAL PRIMARY KEY,
    doc_id TEXT NOT NULL,
    action TEXT NOT NULL,
    hn_from TEXT,
    hn_to TEXT,
    category_from TEXT,
    category_to TEXT,
    path_left TEXT,
    reason TEXT,
    user_id INTEGER,
    created_at TIMESTAMPTZ DEFAULT NOW()
)
"""


def srv():
    """โมดูล server ที่โหลดอยู่แล้วใน process (gunicorn server:app).

    ไม่ import ตรง ๆ เพื่อไม่ให้ CI/เทส (ที่ไม่ได้บูต server.py) พัง —
    ใน production มันอยู่ใน sys.modules เสมอ."""
    m = sys.modules.get("server")
    if m is None:
        raise RuntimeError("server module not loaded")
    return m


def doc_hn(doc):
    """HN ของเอกสาร — ใช้ตัวเดียวกับ lab_sync (field ก่อน, เดาจาก path เป็นสำรอง)."""
    from .lab_sync import _doc_hn
    return _doc_hn(doc)


def _doc_date(doc):
    return (doc.get("submitted_at") or doc.get("received_at")
            or doc.get("upload_date") or "")[:16]


def list_patient_docs(doc_index, hn):
    """เอกสารทุกใบของ HN (ทุกหมวด) + ธงสงสัยซ้ำ.

    สงสัยซ้ำ = หมวดเดียวกัน + วันเดียวกัน มากกว่า 1 ใบ — เป็นแค่ธงให้คนดู
    ไม่มีการตัดสินอัตโนมัติ (ใบแล็บ 2 หน้าในวันเดียวกันก็เข้าเกณฑ์นี้ได้)."""
    hn_up = (hn or "").strip().upper()
    if not hn_up:
        return []
    try:
        doc_index._ensure_fresh()
        docs = list(doc_index._docs)
    except Exception as e:
        logger.warning("patient_docs list: index unavailable: %s", e)
        return []
    out = []
    for d in docs:
        if doc_hn(d) != hn_up:
            continue
        path = d.get("gcs_object_path") or ""
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        out.append({
            "doc8": (d.get("document_id") or "")[:8],
            "category": d.get("category", "") or "",
            "date": _doc_date(d),
            "filename": path.rsplit("/", 1)[-1],
            "folder": path.rsplit("/", 1)[0],
            "is_image": ext in ("jpg", "jpeg", "png"),
            "ext": ext,
        })
    out.sort(key=lambda x: x["date"], reverse=True)
    # ธงสงสัยซ้ำ
    groups = {}
    for x in out:
        groups.setdefault((x["category"], x["date"][:10]), []).append(x)
    for g in groups.values():
        if len(g) > 1:
            for x in g:
                x["dup_suspect"] = True
    return out


def log_cleanup(db_url, **kw):
    """บันทึกการกระทำ + path ที่สำเนาเก่าค้าง (best-effort — ห้ามทำ action หลักล้ม)."""
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute(CLEANUP_SQL)
            cur.execute("""INSERT INTO hd_doc_cleanup_log
                (doc_id, action, hn_from, hn_to, category_from, category_to,
                 path_left, reason, user_id)
                VALUES (%(doc_id)s, %(action)s, %(hn_from)s, %(hn_to)s,
                        %(category_from)s, %(category_to)s, %(path_left)s,
                        %(reason)s, %(user_id)s)""", kw)
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("cleanup log: %s", e)


def unfile_document(doc8, reason, actor, db_url):
    """เอาออกจากแฟ้มผู้ป่วย → กลับกองกลาง 'รอตรวจสอบ' (ไม่ลบจริง ย้อนกลับได้).

    สองขั้น: (1) _change_document_category → review (copy + index + audit ของเดิม)
    (2) ล้าง patient_hn_folder — จุดที่ของเดิมไม่ทำเมื่อปลายทางไม่ใช่หมวดคลินิก
    ซึ่งจะทำให้ LIFF ยังเห็นในแฟ้มผู้ป่วยถ้าไม่ล้าง."""
    s = srv()
    doc, err = s._find_doc_by_prefix(doc8)
    if doc is None:
        return {"error": err}
    old_cat = doc.get("category", "")
    old_hn = doc_hn(doc)
    old_path = doc.get("gcs_object_path", "")
    doc_id = doc.get("document_id", "")
    if old_cat == "review":
        # อยู่กองกลางแล้ว — แค่ล้างแฟ้มถ้ายังติดอยู่
        r = {"status": "unchanged", "message": "เอกสารอยู่กองกลาง 'รอตรวจสอบ' อยู่แล้ว"}
    else:
        r = s._change_document_category(doc_id[:8], "review", "", actor)
        if r.get("error"):
            return r
    try:
        ok = s.doc_index.update_fields(doc_id, {"patient_hn_folder": ""})
        if not ok:
            logger.warning("unfile %s: clear patient_hn_folder failed", doc_id[:8])
    except Exception as e:
        logger.warning("unfile %s: %s", doc_id[:8], e)
    log_cleanup(db_url, doc_id=doc_id[:8], action="unfile", hn_from=old_hn, hn_to="",
                category_from=old_cat, category_to="review", path_left=old_path,
                reason=(reason or "")[:300], user_id=actor_id_of(actor))
    return {"status": "unfiled",
            "message": (f"✅ เอาเอกสาร {doc_id[:8]} ออกจากแฟ้ม {old_hn} แล้ว — "
                        f"กลับไปกองกลาง 'รอตรวจสอบ' (ไฟล์เดิมไม่ถูกลบ ย้อนกลับได้ด้วยการย้ายกลับ)")}


def actor_id_of(actor):
    """ดึง user id จาก actor string 'hd_user:<id>' — ไว้ลง cleanup log."""
    try:
        return int(str(actor).rsplit(":", 1)[-1])
    except (ValueError, TypeError):
        return None
