"""PDPA (พ.ร.บ.คุ้มครองข้อมูลส่วนบุคคล 2562) support — Go-Live R4.

What this adds (and what it deliberately does not):
    hd_consents            one row per consent event: given / withdrawn, by type + version,
                           how it was obtained (paper / line / web) and who recorded it
    privacy notice         Thai text served at /hd/privacy (no patient data on it)
    consent form PDF       printable form for paper signature (reportlab, Loma)
    enforcement helper     has_consent(cur, patient_id, type) used by patient-facing pushes
                           when ⚙️ patient_push_requires_consent = on
    withdraw by LINE       the patient types ถอนความยินยอม / หยุดส่งข้อความ → status withdrawn

It never deletes clinical records (medical-record retention law applies);
"right to erasure" requests are logged for the physician to handle.
"""
import io
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

NOTICE_VERSION = "2026-11-13"
CONSENT_TYPES = {
    "data_processing": "เก็บรวบรวม/ใช้ข้อมูลสุขภาพเพื่อการรักษาและคุณภาพบริการ",
    "line_messaging": "รับข้อความผ่าน LINE (สรุปหลังฟอก, นัดหมาย, แจ้งเตือน)",
    "cloud_storage": "จัดเก็บเอกสารบนระบบคลาวด์ (Google Cloud / Google Drive ของคลินิก)",
}

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS hd_consents (
    id SERIAL PRIMARY KEY,
    patient_id INTEGER REFERENCES hd_patient_mapping(id),
    hn VARCHAR(20) NOT NULL,
    consent_type VARCHAR(40) NOT NULL,
    status VARCHAR(20) NOT NULL,
    notice_version VARCHAR(20),
    via VARCHAR(20),
    recorded_by VARCHAR(100),
    note TEXT,
    created_at TIMESTAMP DEFAULT NOW()
)
"""
CREATE_INDEX_SQL = "CREATE INDEX IF NOT EXISTS ix_hd_consents_hn ON hd_consents(hn, consent_type, id DESC)"


def _thai_now():
    return datetime.now(timezone.utc) + timedelta(hours=7)


def record(cur, hn, consent_type, status, via, recorded_by, note="", patient_id=None, version=NOTICE_VERSION):
    if consent_type not in CONSENT_TYPES or status not in ("given", "withdrawn"):
        raise ValueError("bad consent type/status")
    if patient_id is None:
        cur.execute("SELECT id FROM hd_patient_mapping WHERE UPPER(hn) = %s", (str(hn).upper(),))
        r = cur.fetchone()
        patient_id = r[0] if r else None
    cur.execute("""INSERT INTO hd_consents (patient_id, hn, consent_type, status, notice_version, via, recorded_by, note)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (patient_id, str(hn).upper(), consent_type, status, version, via, recorded_by, note[:300] if note else None))
    return cur.fetchone()[0]


def status_for(cur, hn):
    """{consent_type: {"status", "at", "via", "version"}} — latest event per type."""
    cur.execute("""SELECT DISTINCT ON (consent_type) consent_type, status, created_at, via, notice_version
                   FROM hd_consents WHERE hn = %s ORDER BY consent_type, id DESC""", (str(hn).upper(),))
    return {r[0]: {"status": r[1], "at": r[2].isoformat() if r[2] else None, "via": r[3], "version": r[4]} for r in cur.fetchall()}


def has_consent(cur, hn, consent_type):
    st = status_for(cur, hn).get(consent_type)
    return bool(st and st["status"] == "given")


def overview(cur):
    """Per active patient: status of each consent type (for the registry page)."""
    cur.execute("SELECT id, hn, patient_name FROM hd_patient_mapping WHERE is_active = TRUE ORDER BY patient_name")
    pats = cur.fetchall()
    cur.execute("""SELECT DISTINCT ON (hn, consent_type) hn, consent_type, status, created_at, via
                   FROM hd_consents ORDER BY hn, consent_type, id DESC""")
    st = {}
    for hn, t, s, at, via in cur.fetchall():
        st.setdefault(hn, {})[t] = {"status": s, "at": at.isoformat() if at else None, "via": via}
    out = []
    for pid, hn, name in pats:
        out.append({"patient_id": pid, "hn": hn, "name": name, "consents": st.get(str(hn).upper(), {})})
    return out


def withdraw_all(cur, hn, via="line", note=""):
    """Patient withdrew via LINE: mark messaging (and cloud/processing stay — treatment needs them; physician reviews)."""
    return record(cur, hn, "line_messaging", "withdrawn", via, "patient", note)


def notice_text(clinic_name="คลินิกระนอง รักษ์ไต", contact=""):
    return f"""ประกาศความเป็นส่วนตัว (Privacy Notice) — {clinic_name}
ฉบับวันที่ {NOTICE_VERSION}

1. ข้อมูลที่เก็บรวบรวม
ชื่อ-นามสกุล, เลขประจำตัวผู้ป่วย (HN), วันเกิด, ข้อมูลการรักษาฟอกไต (น้ำหนัก, ความดัน, ค่าการฟอก, ยา, ผลตรวจทางห้องปฏิบัติการ), เอกสารทางการแพทย์ที่ท่านส่งให้คลินิก และบัญชี LINE ของท่านเมื่อท่านเลือกผูกบัญชี

2. วัตถุประสงค์
เพื่อการรักษาพยาบาล การติดตามอาการ การนัดหมาย การแจ้งเตือนความปลอดภัย การประกันคุณภาพของหน่วยไตเทียม และการเบิกจ่ายตามสิทธิการรักษา

3. ฐานทางกฎหมาย
การรักษาพยาบาลและประโยชน์สำคัญต่อชีวิต (มาตรา 26) · การส่งข้อความผ่าน LINE และการจัดเก็บบนคลาวด์ใช้ความยินยอมของท่าน ซึ่งถอนได้ทุกเมื่อโดยไม่กระทบการรักษา

4. การจัดเก็บและความปลอดภัย
ข้อมูลเก็บในระบบของคลินิกบนผู้ให้บริการคลาวด์ (Google Cloud ภูมิภาคเอเชีย) และสำเนาใน Google Drive ของคลินิก เข้าถึงได้เฉพาะบุคลากรที่ได้รับสิทธิ์ มีการบันทึกการเข้าถึงทุกครั้ง ไม่ส่งข้อมูลให้ระบบปัญญาประดิษฐ์ภายนอก เวชระเบียนเก็บตามที่กฎหมายกำหนด (ไม่น้อยกว่า 5 ปี)

5. การเปิดเผย
เฉพาะแก่โรงพยาบาลที่ส่งต่อการรักษา หน่วยงานเบิกจ่ายตามสิทธิ และหน่วยงานตามกฎหมายเท่านั้น

6. สิทธิของท่าน
ขอเข้าถึง/สำเนา แก้ไข คัดค้าน ระงับ ถอนความยินยอม และร้องเรียน — แจ้งเจ้าหน้าที่ที่หน่วยไตเทียม หรือพิมพ์ "ถอนความยินยอม" ใน LINE เพื่อหยุดรับข้อความ

7. ติดต่อ
{clinic_name}{(' · ' + contact) if contact else ''}
"""


def consent_form_pdf(clinic_name="คลินิกระนอง รักษ์ไต", patient_name="", hn=""):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from reportlab.lib import colors
    from .daily_records import _styles, register_fonts
    register_fonts()
    st = _styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=14 * mm, bottomMargin=14 * mm,
                            title="หนังสือให้ความยินยอม")
    el = [Paragraph(f"หนังสือให้ความยินยอมในการเก็บรวบรวม ใช้ และเปิดเผยข้อมูลส่วนบุคคล — {clinic_name}", st["h1"]),
          Paragraph(f"ฉบับ {NOTICE_VERSION}", st["base"]), Spacer(1, 6),
          Paragraph(f"ข้าพเจ้า {patient_name or '…………………………………………'} HN {hn or '……………'} ได้อ่านประกาศความเป็นส่วนตัวของคลินิกแล้ว และให้ความยินยอมดังนี้", st["base"]),
          Spacer(1, 6)]
    rows = [["", "รายการ", "ยินยอม", "ไม่ยินยอม"]]
    for i, (k, v) in enumerate(CONSENT_TYPES.items(), 1):
        rows.append([str(i), v, "☐", "☐"])
    t = Table(rows, colWidths=[8 * mm, 118 * mm, 22 * mm, 24 * mm])
    t.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), "Loma"), ("FONTSIZE", (0, 0), (-1, -1), 10),
                           ("GRID", (0, 0), (-1, -1), 0.4, colors.grey), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EEF7")),
                           ("ALIGN", (2, 1), (-1, -1), "CENTER"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    el += [t, Spacer(1, 8),
           Paragraph("ข้าพเจ้าทราบว่าการถอนความยินยอมข้อ 2–3 ทำได้ทุกเมื่อโดยไม่กระทบการรักษา และข้อมูลการรักษายังคงถูกเก็บตามกฎหมายเวชระเบียน", st["base"]),
           Spacer(1, 18),
           Table([["ลงชื่อ ………………………………… ผู้ป่วย/ผู้แทน", "ลงชื่อ ………………………………… เจ้าหน้าที่"],
                  ["(…………………………………)", "(…………………………………)"],
                  ["วันที่ ……/……/………", "วันที่ ……/……/………"]], colWidths=[86 * mm, 86 * mm],
                 style=TableStyle([("FONTNAME", (0, 0), (-1, -1), "Loma"), ("FONTSIZE", (0, 0), (-1, -1), 10)])),
           Spacer(1, 14), Paragraph(notice_text(clinic_name).replace("\n", "<br/>"), st["small"] if "small" in st else st["base"])]
    doc.build(el)
    return buf.getvalue()
