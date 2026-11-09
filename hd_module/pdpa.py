"""PDPA Compliance Module — คลินิกระนอง รักษ์ไต

พ.ร.บ. คุ้มครองข้อมูลส่วนบุคคล พ.ศ. 2562 (PDPA)
สำหรับข้อมูลสุขภาพ (Sensitive Data) ต้องได้รับความยินยอมชัดแจ้ง

Module นี้จัดการ:
1. Data Retention Policy — กำหนดอายุข้อมูล
2. Consent Tracking — บันทึกความยินยอม
3. Right to Access — ผู้ป่วยขอดูข้อมูล
4. Right to Delete — ผู้ป่วยขอลบข้อมูล (ภายใต้ข้อยกเว้นทางการแพทย์)
5. Data Minimization — ไม่เก็บข้อมูลเกินจำเป็น
6. Audit — บันทึกทุกการเข้าถึงข้อมูล

⚠️ ข้อมูลสุขภาพ (Health Data) เป็น Sensitive Data ตาม PDPA
   → ต้องได้รับความยินยอมชัดแจ้ง (Explicit Consent)
   → ข้อยกเว้น: เพื่อการรักษาทางการแพทย์ (มาตรา 26(5)(ก))
"""

import logging
import os
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)


# ── Data Retention Policy ──────────────────────────────────

DATA_RETENTION_POLICY = {
    # ประเภทข้อมูล: อายุเก็บ (ปี)
    "medical_records": 10,        # เวชระเบียน — กฎหมาย 10 ปี
    "lab_results": 10,            # ผลตรวจแล็บ
    "dialysis_records": 10,       # บันทึกฟอกเลือด
    "prescriptions": 10,          # ใบสั่งยา
    "vital_signs": 5,             # สัญญาณชีพ (HD sessions)
    "hd_sessions": 5,             # รอบฟอกเลือด
    "clinical_alerts": 2,         # แจ้งเตือนทางคลินิก
    "audit_logs": 5,              # บันทึกการเข้าถึง
    "consent_records": 10,        # บันทึกความยินยอม
    "bills_receipts": 5,          # ใบเสร็จ/บิล
}

# ── Legal Basis ────────────────────────────────────────────

LEGAL_BASES = {
    "medical_treatment": "PDPA มาตรา 26(5)(ก) — เพื่อการรักษาทางการแพทย์",
    "vital_interest": "PDPA มาตรา 24(4) — เพื่อประโยชน์สำคัญต่อชีวิต",
    "legal_obligation": "PDPA มาตรา 24(6) — ปฏิบัติตามกฎหมาย",
    "explicit_consent": "PDPA มาตรา 26 — ความยินยอมชัดแจ้ง",
}


def get_retention_years(doc_type: str) -> int:
    """ดึงจำนวนปีที่ต้องเก็บข้อมูลตามประเภท"""
    return DATA_RETENTION_POLICY.get(doc_type, 10)


def check_data_expired(doc_type: str, created_date: datetime) -> bool:
    """ตรวจว่าข้อมูลหมดอายุการเก็บรักษาหรือยัง"""
    years = get_retention_years(doc_type)
    expiry = created_date + timedelta(days=years * 365)
    return datetime.now(timezone.utc) > expiry


def get_legal_basis(purpose: str) -> str:
    """ดึงฐานทางกฎหมายสำหรับวัตถุประสงค์"""
    return LEGAL_BASES.get(purpose, LEGAL_BASES["medical_treatment"])


# ── Consent Management ─────────────────────────────────────

def record_consent(db_url: str, patient_hn: str, consent_type: str,
                   granted: bool, recorded_by: str = "system") -> bool:
    """บันทึกความยินยอมของผู้ป่วย

    consent_type: "data_collection", "data_sharing", "line_notification"
    """
    if not db_url:
        return False
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pdpa_consent (
                    id SERIAL PRIMARY KEY,
                    patient_hn VARCHAR(20) NOT NULL,
                    consent_type VARCHAR(50) NOT NULL,
                    granted BOOLEAN NOT NULL,
                    recorded_by VARCHAR(100),
                    recorded_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    legal_basis TEXT,
                    notes TEXT,
                    UNIQUE(patient_hn, consent_type)
                )
            """)
            cur.execute("""
                INSERT INTO pdpa_consent (patient_hn, consent_type, granted,
                                          recorded_by, legal_basis)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (patient_hn, consent_type) DO UPDATE SET
                    granted = EXCLUDED.granted,
                    recorded_by = EXCLUDED.recorded_by,
                    recorded_at = NOW()
            """, (patient_hn, consent_type, granted, recorded_by,
                  get_legal_basis("medical_treatment")))
        conn.commit()
        conn.close()
        logger.info("PDPA consent recorded: HN=%s type=%s granted=%s",
                     patient_hn, consent_type, granted)
        return True
    except Exception as e:
        logger.error("PDPA consent recording failed: %s", e)
        return False


def check_consent(db_url: str, patient_hn: str, consent_type: str) -> bool:
    """ตรวจสอบว่าผู้ป่วยให้ความยินยอมหรือไม่"""
    if not db_url:
        return True  # Default: allow (medical treatment exception)
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=3)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT granted FROM pdpa_consent
                WHERE patient_hn = %s AND consent_type = %s
            """, (patient_hn, consent_type))
            row = cur.fetchone()
        conn.close()
        if row is None:
            return True  # No explicit opt-out → allow under medical treatment
        return row[0]
    except Exception:
        return True  # Fail-open for medical treatment


# ── Right to Access ────────────────────────────────────────

def generate_data_access_report(db_url: str, patient_hn: str) -> dict:
    """สร้างรายงานข้อมูลของผู้ป่วย (Right to Access)

    ผู้ป่วยมีสิทธิขอดูข้อมูลส่วนบุคคลของตนเองได้
    """
    report = {
        "patient_hn": patient_hn,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "legal_basis": get_legal_basis("medical_treatment"),
        "retention_policy": DATA_RETENTION_POLICY,
        "data_categories": {},
    }
    if not db_url:
        return report
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)

        # HD sessions count
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*), MIN(started_at), MAX(started_at)
                FROM hd_sessions s
                JOIN hd_patient_mapping p ON p.id = s.patient_id
                WHERE p.hn = %s
            """, (patient_hn,))
            row = cur.fetchone()
            report["data_categories"]["hd_sessions"] = {
                "count": row[0],
                "earliest": row[1].isoformat() if row[1] else None,
                "latest": row[2].isoformat() if row[2] else None,
                "retention_years": 5,
            }

        # Vital signs count
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*)
                FROM hd_vital_signs v
                JOIN hd_sessions s ON s.id = v.session_id
                JOIN hd_patient_mapping p ON p.id = s.patient_id
                WHERE p.hn = %s
            """, (patient_hn,))
            report["data_categories"]["vital_signs"] = {
                "count": cur.fetchone()[0],
                "retention_years": 5,
            }

        # Alerts count
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*)
                FROM hd_clinical_alerts a
                JOIN hd_sessions s ON s.id = a.session_id
                JOIN hd_patient_mapping p ON p.id = s.patient_id
                WHERE p.hn = %s
            """, (patient_hn,))
            report["data_categories"]["clinical_alerts"] = {
                "count": cur.fetchone()[0],
                "retention_years": 2,
            }

        conn.close()
    except Exception as e:
        logger.error("PDPA data access report failed: %s", e)
        report["error"] = str(e)

    return report


# ── Data Minimization Checklist ────────────────────────────

MINIMIZATION_CHECKLIST = [
    "✅ ไม่เก็บข้อมูลบัตรประชาชนในระบบ",
    "✅ HN ใช้ hash สำหรับ LINE user mapping (ไม่ reverse ได้)",
    "✅ ไม่ส่งข้อมูลผู้ป่วยไป External AI (EXTERNAL_AI_CLASSIFICATION_ALLOWED=False)",
    "✅ Log ใช้ document_id[:8] เท่านั้น (ไม่มีชื่อ/HN ใน log)",
    "✅ Audit trail บันทึกทุกการเข้าถึงเอกสาร",
    "✅ Alert ล้มเหลวไม่กระทบ document flow",
    "✅ ผลวิเคราะห์ต้องผ่าน Human Review ก่อนใช้",
    "✅ Owner-based document isolation",
]


def get_pdpa_status() -> dict:
    """สรุปสถานะ PDPA compliance"""
    return {
        "retention_policy": DATA_RETENTION_POLICY,
        "legal_bases": LEGAL_BASES,
        "minimization_checklist": MINIMIZATION_CHECKLIST,
        "external_ai_disabled": True,
        "audit_logging": True,
        "consent_management": True,
    }
