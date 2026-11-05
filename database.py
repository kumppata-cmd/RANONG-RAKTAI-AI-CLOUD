#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PostgreSQL Database Layer for RANONG-RAKTAI-AI

Phase 1.1: Foundation Upgrade — ย้ายจาก GCS JSON → PostgreSQL
- ไม่กระทบระบบเดิม: GCS ยังเป็น file storage
- PostgreSQL สำหรับ structured data: patients, documents, labs, alerts
- Dual-write mode: เขียนทั้ง GCS + PostgreSQL ระหว่าง migration
- Fallback: ถ้า PostgreSQL ล้ม ยังใช้ GCS ได้

Usage:
    db = Database(os.getenv("DATABASE_URL"))
    if db.is_ready:
        db.upsert_patient(patient_dict)
        results = db.search_documents(hn="56001", category="lab")

Environment:
    DATABASE_URL=postgresql://user:pass@host:5432/dbname
"""

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager

logger = logging.getLogger("database")

# ─────────────────────────────────────────────────────
# Database Connection
# ─────────────────────────────────────────────────────

_pg_available = False
try:
    import psycopg2
    import psycopg2.extras
    import psycopg2.pool
    _pg_available = True
except ImportError:
    pass


class Database:
    """PostgreSQL database layer — ใช้ connection pool สำหรับ Gunicorn workers"""

    def __init__(self, database_url: str = ""):
        self._pool = None
        self._status = "not_configured"
        self._error = None
        self._url = database_url

        if not database_url:
            self._status = "not_configured"
            return

        if not _pg_available:
            self._status = "unavailable"
            self._error = "psycopg2 ไม่ได้ติดตั้ง — pip install psycopg2-binary"
            logger.warning(f"Database: {self._error}")
            return

        try:
            self._pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=5,  # Render basic = 5 connections max
                dsn=database_url,
                connect_timeout=5,
            )
            # Test connection + create tables
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
            self._create_tables()
            self._status = "connected"
            logger.info("Database: PostgreSQL เชื่อมต่อสำเร็จ")
        except Exception as e:
            self._status = "error"
            self._error = f"{type(e).__name__}: {e}"
            logger.error(f"Database: เชื่อมต่อล้มเหลว — {self._error}")

    @property
    def is_ready(self) -> bool:
        return self._status == "connected" and self._pool is not None

    @property
    def status(self) -> str:
        return self._status

    @property
    def error(self) -> str | None:
        return self._error

    def health(self) -> dict:
        """Health check สำหรับ /health endpoint"""
        info = {"status": self._status}
        if self._error:
            info["error"] = self._error
        if self.is_ready:
            try:
                with self._get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT COUNT(*) FROM patients")
                        info["patients"] = cur.fetchone()[0]
                        cur.execute("SELECT COUNT(*) FROM documents")
                        info["documents"] = cur.fetchone()[0]
                        cur.execute("SELECT COUNT(*) FROM lab_results")
                        info["lab_results"] = cur.fetchone()[0]
            except Exception as e:
                info["error"] = type(e).__name__
        return info

    @contextmanager
    def _get_conn(self):
        """Get connection from pool with auto-commit"""
        conn = self._pool.getconn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    # ─────────────────────────────────────────────────
    # Schema Creation
    # ─────────────────────────────────────────────────

    def _create_tables(self):
        """สร้าง tables ทั้งหมด — idempotent (IF NOT EXISTS)"""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                -- ทะเบียนผู้ป่วย
                CREATE TABLE IF NOT EXISTS patients (
                    id SERIAL PRIMARY KEY,
                    clinic_hn VARCHAR(20) UNIQUE,
                    hospital_hn VARCHAR(20),
                    patient_name VARCHAR(200) NOT NULL DEFAULT '',
                    registered_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW(),
                    registered_by VARCHAR(50) DEFAULT 'admin',
                    notes TEXT DEFAULT '',
                    -- Dialysis fields
                    dialysis_schedule VARCHAR(50) DEFAULT '',
                    dialysis_days JSONB DEFAULT '[]',
                    hd_per_week INTEGER DEFAULT 0,
                    esa_code VARCHAR(20) DEFAULT '',
                    esa_medication VARCHAR(100) DEFAULT '',
                    esa_dose INTEGER DEFAULT 0,
                    esa_unit VARCHAR(20) DEFAULT 'unit',
                    vascular_access VARCHAR(100) DEFAULT '',
                    shift VARCHAR(50) DEFAULT '',
                    bed_number VARCHAR(20) DEFAULT '',
                    -- LINE linking
                    line_user_hash VARCHAR(64),
                    line_linked_at TIMESTAMPTZ,
                    -- Metadata
                    is_active BOOLEAN DEFAULT TRUE,
                    clinic_code VARCHAR(10) DEFAULT '27970',
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS idx_patients_hospital_hn
                    ON patients(hospital_hn) WHERE hospital_hn != '';
                CREATE INDEX IF NOT EXISTS idx_patients_name
                    ON patients USING gin(to_tsvector('simple', patient_name));
                CREATE INDEX IF NOT EXISTS idx_patients_line_hash
                    ON patients(line_user_hash) WHERE line_user_hash IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_patients_dialysis_days
                    ON patients USING gin(dialysis_days);

                -- เอกสาร (index)
                CREATE TABLE IF NOT EXISTS documents (
                    id SERIAL PRIMARY KEY,
                    doc_id VARCHAR(64) UNIQUE NOT NULL,
                    original_name VARCHAR(500) DEFAULT '',
                    category VARCHAR(50) DEFAULT 'other',
                    subcategory VARCHAR(100) DEFAULT '',
                    patient_hn_folder VARCHAR(100) DEFAULT '',
                    upload_date VARCHAR(20) DEFAULT '',
                    gcs_object_path TEXT DEFAULT '',
                    gcs_index_path TEXT DEFAULT '',
                    content_type VARCHAR(50) DEFAULT '',
                    file_size INTEGER DEFAULT 0,
                    -- OCR & Classification
                    ocr_text TEXT DEFAULT '',
                    ocr_confidence REAL,
                    classification_method VARCHAR(50) DEFAULT '',
                    -- Bill fields
                    bill_company VARCHAR(100) DEFAULT '',
                    bill_amount REAL,
                    bill_date VARCHAR(20) DEFAULT '',
                    -- Status
                    owner_hash VARCHAR(64) DEFAULT '',
                    status VARCHAR(20) DEFAULT 'indexed',
                    -- Metadata
                    metadata JSONB DEFAULT '{}',
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS idx_docs_patient
                    ON documents(patient_hn_folder);
                CREATE INDEX IF NOT EXISTS idx_docs_category
                    ON documents(category);
                CREATE INDEX IF NOT EXISTS idx_docs_upload_date
                    ON documents(upload_date);
                CREATE INDEX IF NOT EXISTS idx_docs_owner
                    ON documents(owner_hash) WHERE owner_hash != '';
                CREATE INDEX IF NOT EXISTS idx_docs_bill_company
                    ON documents(bill_company) WHERE bill_company != '';

                -- ผลแล็บ (structured)
                CREATE TABLE IF NOT EXISTS lab_results (
                    id SERIAL PRIMARY KEY,
                    doc_id VARCHAR(64) REFERENCES documents(doc_id),
                    patient_hn VARCHAR(20),
                    specimen_date VARCHAR(20) DEFAULT '',
                    -- ค่าแล็บ (เก็บทุกค่าในรูปแบบ key-value)
                    analyte VARCHAR(100) NOT NULL,
                    value REAL,
                    value_text VARCHAR(100) DEFAULT '',
                    unit VARCHAR(50) DEFAULT '',
                    ref_low REAL,
                    ref_high REAL,
                    flags JSONB DEFAULT '[]',
                    -- Calculated
                    is_critical BOOLEAN DEFAULT FALSE,
                    is_abnormal BOOLEAN DEFAULT FALSE,
                    -- Metadata
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS idx_labs_patient
                    ON lab_results(patient_hn);
                CREATE INDEX IF NOT EXISTS idx_labs_analyte
                    ON lab_results(analyte);
                CREATE INDEX IF NOT EXISTS idx_labs_date
                    ON lab_results(specimen_date);
                CREATE INDEX IF NOT EXISTS idx_labs_critical
                    ON lab_results(is_critical) WHERE is_critical = TRUE;
                CREATE INDEX IF NOT EXISTS idx_labs_doc
                    ON lab_results(doc_id);

                -- Alerts
                CREATE TABLE IF NOT EXISTS alerts (
                    id SERIAL PRIMARY KEY,
                    alert_id VARCHAR(64) UNIQUE,
                    doc_id VARCHAR(64),
                    patient_hn VARCHAR(20) DEFAULT '',
                    owner_hash VARCHAR(64) DEFAULT '',
                    alert_level VARCHAR(20) DEFAULT 'warning',
                    analyte VARCHAR(100) DEFAULT '',
                    value REAL,
                    unit VARCHAR(50) DEFAULT '',
                    message TEXT DEFAULT '',
                    -- Status
                    acknowledged BOOLEAN DEFAULT FALSE,
                    acknowledged_by VARCHAR(64),
                    acknowledged_at TIMESTAMPTZ,
                    delivered BOOLEAN DEFAULT FALSE,
                    delivered_at TIMESTAMPTZ,
                    -- Metadata
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS idx_alerts_patient
                    ON alerts(patient_hn);
                CREATE INDEX IF NOT EXISTS idx_alerts_pending
                    ON alerts(acknowledged) WHERE acknowledged = FALSE;
                CREATE INDEX IF NOT EXISTS idx_alerts_level
                    ON alerts(alert_level);

                -- Audit log
                CREATE TABLE IF NOT EXISTS audit_log (
                    id SERIAL PRIMARY KEY,
                    event_type VARCHAR(100) NOT NULL,
                    user_hash VARCHAR(64) DEFAULT '',
                    details JSONB DEFAULT '{}',
                    ip_address VARCHAR(45) DEFAULT '',
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS idx_audit_event
                    ON audit_log(event_type);
                CREATE INDEX IF NOT EXISTS idx_audit_date
                    ON audit_log(created_at);

                -- HN Registry (LINE ↔ Patient linking)
                CREATE TABLE IF NOT EXISTS hn_links (
                    id SERIAL PRIMARY KEY,
                    owner_hash VARCHAR(64) UNIQUE NOT NULL,
                    clinic_hn VARCHAR(20) NOT NULL,
                    hospital_hn VARCHAR(20) DEFAULT '',
                    patient_name VARCHAR(200) DEFAULT '',
                    display_name VARCHAR(200) DEFAULT '',
                    linked_at TIMESTAMPTZ DEFAULT NOW(),
                    linked_by VARCHAR(50) DEFAULT 'self',
                    is_active BOOLEAN DEFAULT TRUE
                );

                CREATE INDEX IF NOT EXISTS idx_hn_links_clinic
                    ON hn_links(clinic_hn);
                CREATE INDEX IF NOT EXISTS idx_hn_links_hospital
                    ON hn_links(hospital_hn) WHERE hospital_hn != '';

                -- Expenses
                CREATE TABLE IF NOT EXISTS expenses (
                    id SERIAL PRIMARY KEY,
                    doc_id VARCHAR(64),
                    company VARCHAR(100) NOT NULL,
                    amount REAL DEFAULT 0,
                    vat REAL DEFAULT 0,
                    bill_date DATE,
                    bill_number VARCHAR(100) DEFAULT '',
                    description TEXT DEFAULT '',
                    category VARCHAR(50) DEFAULT 'supplies',
                    month_year VARCHAR(7) DEFAULT '',
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS idx_expenses_company
                    ON expenses(company);
                CREATE INDEX IF NOT EXISTS idx_expenses_month
                    ON expenses(month_year);

                -- Appointments
                CREATE TABLE IF NOT EXISTS appointments (
                    id SERIAL PRIMARY KEY,
                    appointment_id VARCHAR(64) UNIQUE,
                    owner_hash VARCHAR(64) NOT NULL,
                    patient_hn VARCHAR(20) DEFAULT '',
                    date DATE NOT NULL,
                    time VARCHAR(10) DEFAULT '',
                    purpose VARCHAR(200) DEFAULT '',
                    status VARCHAR(20) DEFAULT 'scheduled',
                    notes TEXT DEFAULT '',
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS idx_appt_owner
                    ON appointments(owner_hash);
                CREATE INDEX IF NOT EXISTS idx_appt_date
                    ON appointments(date);
                CREATE INDEX IF NOT EXISTS idx_appt_patient
                    ON appointments(patient_hn) WHERE patient_hn != '';
                """)
        logger.info("Database: schema created/verified")

    # ─────────────────────────────────────────────────
    # Patient CRUD
    # ─────────────────────────────────────────────────

    def upsert_patient(self, data: dict) -> int | None:
        """Insert หรือ update ผู้ป่วย — ใช้ clinic_hn เป็น key"""
        if not self.is_ready:
            return None
        clinic_hn = (data.get("clinic_hn") or "").strip().upper()
        if not clinic_hn:
            return None
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO patients (
                            clinic_hn, hospital_hn, patient_name,
                            registered_at, updated_at, registered_by, notes,
                            dialysis_schedule, dialysis_days, hd_per_week,
                            esa_code, esa_medication, esa_dose, esa_unit,
                            vascular_access, shift, bed_number
                        ) VALUES (
                            %(clinic_hn)s, %(hospital_hn)s, %(patient_name)s,
                            %(registered_at)s, NOW(), %(registered_by)s, %(notes)s,
                            %(dialysis_schedule)s, %(dialysis_days)s, %(hd_per_week)s,
                            %(esa_code)s, %(esa_medication)s, %(esa_dose)s, %(esa_unit)s,
                            %(vascular_access)s, %(shift)s, %(bed_number)s
                        )
                        ON CONFLICT (clinic_hn) DO UPDATE SET
                            hospital_hn = EXCLUDED.hospital_hn,
                            patient_name = EXCLUDED.patient_name,
                            updated_at = NOW(),
                            notes = EXCLUDED.notes,
                            dialysis_schedule = EXCLUDED.dialysis_schedule,
                            dialysis_days = EXCLUDED.dialysis_days,
                            hd_per_week = EXCLUDED.hd_per_week,
                            esa_code = EXCLUDED.esa_code,
                            esa_medication = EXCLUDED.esa_medication,
                            esa_dose = EXCLUDED.esa_dose,
                            esa_unit = EXCLUDED.esa_unit,
                            vascular_access = EXCLUDED.vascular_access,
                            shift = EXCLUDED.shift,
                            bed_number = EXCLUDED.bed_number
                        RETURNING id
                    """, {
                        "clinic_hn": clinic_hn,
                        "hospital_hn": (data.get("hospital_hn") or "").strip().upper(),
                        "patient_name": (data.get("patient_name") or "").strip(),
                        "registered_at": data.get("registered_at") or datetime.now(timezone.utc).isoformat(),
                        "registered_by": data.get("registered_by", "admin"),
                        "notes": data.get("notes", ""),
                        "dialysis_schedule": data.get("dialysis_schedule", ""),
                        "dialysis_days": json.dumps(data.get("dialysis_days", [])),
                        "hd_per_week": int(data.get("hd_per_week", 0) or 0),
                        "esa_code": data.get("esa_code", ""),
                        "esa_medication": data.get("esa_medication", ""),
                        "esa_dose": int(data.get("esa_dose", 0) or 0),
                        "esa_unit": data.get("esa_unit", "unit"),
                        "vascular_access": data.get("vascular_access", ""),
                        "shift": data.get("shift", ""),
                        "bed_number": str(data.get("bed_number", "")),
                    })
                    row = cur.fetchone()
                    return row[0] if row else None
        except Exception as e:
            logger.warning(f"DB upsert_patient error: {e}")
            return None

    def get_patient(self, clinic_hn: str) -> dict | None:
        """ดึงข้อมูลผู้ป่วยจาก clinic_hn"""
        if not self.is_ready:
            return None
        try:
            with self._get_conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        "SELECT * FROM patients WHERE clinic_hn = %s AND is_active",
                        (clinic_hn.strip().upper(),)
                    )
                    return dict(cur.fetchone()) if cur.rowcount else None
        except Exception as e:
            logger.warning(f"DB get_patient error: {e}")
            return None

    def get_patients_by_day(self, day_en: str) -> list[dict]:
        """ดึงผู้ป่วยที่ฟอกในวันที่ระบุ — ใช้ JSONB query"""
        if not self.is_ready:
            return []
        try:
            with self._get_conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        "SELECT * FROM patients WHERE dialysis_days ? %s AND is_active ORDER BY clinic_hn",
                        (day_en,)
                    )
                    return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            logger.warning(f"DB get_patients_by_day error: {e}")
            return []

    def count_patients(self) -> int:
        """นับจำนวนผู้ป่วยทั้งหมด"""
        if not self.is_ready:
            return 0
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM patients WHERE is_active")
                    return cur.fetchone()[0]
        except Exception:
            return 0

    def search_patients(self, query: str, limit: int = 20) -> list[dict]:
        """ค้นหาผู้ป่วยจากชื่อ, HN"""
        if not self.is_ready:
            return []
        try:
            with self._get_conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("""
                        SELECT * FROM patients
                        WHERE is_active AND (
                            clinic_hn ILIKE %s
                            OR hospital_hn ILIKE %s
                            OR patient_name ILIKE %s
                        )
                        ORDER BY clinic_hn
                        LIMIT %s
                    """, (f"%{query}%", f"%{query}%", f"%{query}%", limit))
                    return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            logger.warning(f"DB search_patients error: {e}")
            return []

    def get_dialysis_summary(self) -> dict:
        """สรุปข้อมูลไตเทียม — ใช้ SQL aggregate"""
        if not self.is_ready:
            return {}
        try:
            with self._get_conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    # จำนวนรวม
                    cur.execute("SELECT COUNT(*) as total FROM patients WHERE is_active")
                    total = cur.fetchone()["total"]

                    cur.execute("SELECT COUNT(*) as c FROM patients WHERE is_active AND dialysis_schedule != ''")
                    with_schedule = cur.fetchone()["c"]

                    # แยกตามวัน
                    by_day = {}
                    for day in ["mon", "tue", "wed", "thu", "fri", "sat"]:
                        cur.execute(
                            "SELECT COUNT(*) as c FROM patients WHERE is_active AND dialysis_days ? %s",
                            (day,)
                        )
                        by_day[day] = cur.fetchone()["c"]

                    # แยกตาม ESA
                    cur.execute("""
                        SELECT esa_code, COUNT(*) as c
                        FROM patients
                        WHERE is_active AND esa_code != ''
                        GROUP BY esa_code
                        ORDER BY c DESC
                    """)
                    esa_counts = {r["esa_code"]: r["c"] for r in cur.fetchall()}

                    return {
                        "total_patients": total,
                        "with_schedule": with_schedule,
                        "by_day": by_day,
                        "esa_counts": esa_counts,
                    }
        except Exception as e:
            logger.warning(f"DB dialysis_summary error: {e}")
            return {}

    # ─────────────────────────────────────────────────
    # Document CRUD
    # ─────────────────────────────────────────────────

    def upsert_document(self, doc: dict) -> int | None:
        """Insert หรือ update เอกสาร"""
        if not self.is_ready:
            return None
        doc_id = doc.get("doc_id") or doc.get("document_id", "")
        if not doc_id:
            return None
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO documents (
                            doc_id, original_name, category, subcategory,
                            patient_hn_folder, upload_date, gcs_object_path,
                            content_type, file_size, ocr_text,
                            classification_method, bill_company, owner_hash,
                            status, metadata
                        ) VALUES (
                            %(doc_id)s, %(original_name)s, %(category)s, %(subcategory)s,
                            %(patient_hn_folder)s, %(upload_date)s, %(gcs_object_path)s,
                            %(content_type)s, %(file_size)s, %(ocr_text)s,
                            %(classification_method)s, %(bill_company)s, %(owner_hash)s,
                            %(status)s, %(metadata)s
                        )
                        ON CONFLICT (doc_id) DO UPDATE SET
                            category = EXCLUDED.category,
                            subcategory = EXCLUDED.subcategory,
                            patient_hn_folder = EXCLUDED.patient_hn_folder,
                            status = EXCLUDED.status,
                            metadata = EXCLUDED.metadata,
                            updated_at = NOW()
                        RETURNING id
                    """, {
                        "doc_id": doc_id,
                        "original_name": doc.get("original_name", ""),
                        "category": doc.get("category", "other"),
                        "subcategory": doc.get("subcategory", ""),
                        "patient_hn_folder": doc.get("patient_hn_folder", ""),
                        "upload_date": doc.get("upload_date", ""),
                        "gcs_object_path": doc.get("gcs_object_path", ""),
                        "content_type": doc.get("content_type", ""),
                        "file_size": int(doc.get("file_size", 0) or 0),
                        "ocr_text": doc.get("extracted_text", "")[:10000],
                        "classification_method": doc.get("classification_method", ""),
                        "bill_company": doc.get("bill_company", ""),
                        "owner_hash": doc.get("owner_hash", ""),
                        "status": doc.get("status", "indexed"),
                        "metadata": json.dumps({
                            k: v for k, v in doc.items()
                            if k not in (
                                "doc_id", "document_id", "original_name",
                                "category", "subcategory", "patient_hn_folder",
                                "upload_date", "gcs_object_path", "content_type",
                                "file_size", "extracted_text", "classification_method",
                                "bill_company", "owner_hash", "status",
                            )
                        }),
                    })
                    row = cur.fetchone()
                    return row[0] if row else None
        except Exception as e:
            logger.warning(f"DB upsert_document error: {e}")
            return None

    def search_documents(
        self, hn: str = "", category: str = "", limit: int = 50
    ) -> list[dict]:
        """ค้นหาเอกสาร — เร็วกว่า GCS scan 100x"""
        if not self.is_ready:
            return []
        try:
            with self._get_conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    conditions = ["1=1"]
                    params = []
                    if hn:
                        conditions.append("patient_hn_folder ILIKE %s")
                        params.append(f"%{hn}%")
                    if category:
                        conditions.append("category = %s")
                        params.append(category)
                    params.append(limit)
                    cur.execute(f"""
                        SELECT * FROM documents
                        WHERE {' AND '.join(conditions)}
                        ORDER BY upload_date DESC, created_at DESC
                        LIMIT %s
                    """, params)
                    return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            logger.warning(f"DB search_documents error: {e}")
            return []

    def count_documents_by_month(self, year_month: str) -> dict:
        """นับเอกสารแยกประเภทในเดือนที่ระบุ — สำหรับ สถิติเดือนนี้"""
        if not self.is_ready:
            return {}
        try:
            with self._get_conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("""
                        SELECT category, COUNT(*) as count
                        FROM documents
                        WHERE upload_date LIKE %s
                        GROUP BY category
                    """, (f"{year_month}%",))
                    return {r["category"]: r["count"] for r in cur.fetchall()}
        except Exception as e:
            logger.warning(f"DB count_documents error: {e}")
            return {}

    # ─────────────────────────────────────────────────
    # Lab Results
    # ─────────────────────────────────────────────────

    def insert_lab_result(self, result: dict) -> int | None:
        """บันทึกผลแล็บ structured"""
        if not self.is_ready:
            return None
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO lab_results (
                            doc_id, patient_hn, specimen_date,
                            analyte, value, value_text, unit,
                            ref_low, ref_high, flags,
                            is_critical, is_abnormal
                        ) VALUES (
                            %(doc_id)s, %(patient_hn)s, %(specimen_date)s,
                            %(analyte)s, %(value)s, %(value_text)s, %(unit)s,
                            %(ref_low)s, %(ref_high)s, %(flags)s,
                            %(is_critical)s, %(is_abnormal)s
                        ) RETURNING id
                    """, {
                        "doc_id": result.get("doc_id", ""),
                        "patient_hn": result.get("patient_hn", ""),
                        "specimen_date": result.get("specimen_date", ""),
                        "analyte": result.get("analyte", ""),
                        "value": result.get("value"),
                        "value_text": str(result.get("value", "")),
                        "unit": result.get("unit", ""),
                        "ref_low": result.get("ref_low"),
                        "ref_high": result.get("ref_high"),
                        "flags": json.dumps(result.get("flags", [])),
                        "is_critical": bool(result.get("is_critical", False)),
                        "is_abnormal": bool(result.get("is_abnormal", False)),
                    })
                    row = cur.fetchone()
                    return row[0] if row else None
        except Exception as e:
            logger.warning(f"DB insert_lab_result error: {e}")
            return None

    def get_lab_history(self, patient_hn: str, analyte: str = "",
                        limit: int = 20) -> list[dict]:
        """ดึงประวัติแล็บของผู้ป่วย — เรียงจากใหม่→เก่า"""
        if not self.is_ready:
            return []
        try:
            with self._get_conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    if analyte:
                        cur.execute("""
                            SELECT * FROM lab_results
                            WHERE patient_hn = %s AND analyte ILIKE %s
                            ORDER BY specimen_date DESC, created_at DESC
                            LIMIT %s
                        """, (patient_hn, f"%{analyte}%", limit))
                    else:
                        cur.execute("""
                            SELECT * FROM lab_results
                            WHERE patient_hn = %s
                            ORDER BY specimen_date DESC, created_at DESC
                            LIMIT %s
                        """, (patient_hn, limit))
                    return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            logger.warning(f"DB get_lab_history error: {e}")
            return []

    def get_hb_trend(self, patient_hn: str, limit: int = 10) -> list[dict]:
        """ดึง Hb trend สำหรับ ESA optimization"""
        return self.get_lab_history(patient_hn, analyte="hemoglobin", limit=limit)

    # ─────────────────────────────────────────────────
    # Migration: GCS → PostgreSQL
    # ─────────────────────────────────────────────────

    def migrate_patients(self, registry) -> dict:
        """ย้ายข้อมูลจาก MasterPatientRegistry → PostgreSQL"""
        if not self.is_ready:
            return {"error": "database not ready"}
        imported = 0
        errors = 0
        for p in registry.list_all():
            try:
                data = p.to_dict()
                self.upsert_patient(data)
                imported += 1
            except Exception:
                errors += 1
        return {"imported": imported, "errors": errors}

    def migrate_documents(self, doc_index) -> dict:
        """ย้ายข้อมูลจาก DocumentIndex → PostgreSQL"""
        if not self.is_ready:
            return {"error": "database not ready"}
        imported = 0
        errors = 0
        for doc in (doc_index._docs if hasattr(doc_index, '_docs') else []):
            try:
                doc_copy = dict(doc)
                doc_copy["doc_id"] = doc_copy.pop("document_id", doc_copy.get("doc_id", ""))
                self.upsert_document(doc_copy)
                imported += 1
            except Exception:
                errors += 1
        return {"imported": imported, "errors": errors}
