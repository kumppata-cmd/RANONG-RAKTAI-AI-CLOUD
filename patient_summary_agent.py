#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Patient Summary Agent — Multi-Agent Flow สรุปข้อมูลผู้ป่วยครบจบ

รวมข้อมูลจากทุก module:
- Lab Intelligence → ผลแล็บล่าสุด + CKD staging
- Treatment Tracker → ยา/การรักษาล่าสุด
- MasterPatientRegistry → ข้อมูลฟอก + ESA
- Appointment → นัดหมายถัดไป
→ สร้าง Flex Message สรุปครบจบ
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def build_patient_summary_flex(
    hn: str,
    master_registry=None,
    lab_agent=None,
    treatment_tracker=None,
    appointment_store=None,
    storage=None,
) -> Optional[dict]:
    """สร้าง Flex Message สรุปข้อมูลผู้ป่วยแบบ Multi-Agent.

    Returns:
        dict (Flex Message) หรือ None ถ้าไม่พบ HN
    """
    if not master_registry:
        return None

    patient = master_registry.get(hn)
    if not patient:
        return None

    name = patient.get("name", f"HN {hn}")
    esa = patient.get("esa_drug", "ไม่ระบุ")
    esa_dose = patient.get("esa_dose", "")
    schedule_days = patient.get("schedule_days", "ไม่ระบุ")
    vascular = patient.get("vascular_access", "ไม่ระบุ")
    dry_weight = patient.get("dry_weight", "")

    # ─── Lab Agent ───
    lab_text = "ยังไม่มีผลแล็บ"
    ckd_stage = ""
    try:
        if lab_agent and hasattr(lab_agent, 'get_latest_summary'):
            lab_summary = lab_agent.get_latest_summary(hn)
            if lab_summary:
                lab_text = lab_summary
        # ลอง CKD staging
        if lab_agent and hasattr(lab_agent, 'classify_ckd_full'):
            egfr = patient.get("latest_egfr")
            if egfr:
                ckd = lab_agent.classify_ckd_full(egfr)
                ckd_stage = f"CKD {ckd.get('stage', '?')}"
    except Exception as e:
        logger.warning(f"Lab agent error for HN {hn}: {e}")

    # ─── Treatment Agent ───
    treatment_text = "ยังไม่มีประวัติปรับยา"
    pending_count = 0
    try:
        if treatment_tracker:
            history = treatment_tracker.get_history(hn, limit=3)
            if history:
                lines = []
                for ch in history:
                    lines.append(f"• {ch.get('change_date', '')[:10]} "
                                 f"{ch.get('action', '')} {ch.get('drug_name', '')}")
                treatment_text = "\n".join(lines)

            pending = treatment_tracker.get_pending_followups(hn)
            pending_count = len(pending) if pending else 0
    except Exception as e:
        logger.warning(f"Treatment agent error for HN {hn}: {e}")

    # ─── Appointment Agent ───
    next_appointment = "ไม่มีนัดหมาย"
    try:
        if appointment_store and hasattr(appointment_store, 'list_for_hn'):
            appts = appointment_store.list_for_hn(hn, limit=1)
            if appts:
                a = appts[0]
                next_appointment = f"{a.get('date', '')} {a.get('type', '')}"
    except Exception as e:
        logger.warning(f"Appointment agent error for HN {hn}: {e}")

    # ─── Build Flex ───
    def _info_row(label, value, color="#555555"):
        return {
            "type": "box", "layout": "horizontal", "spacing": "sm",
            "contents": [
                {"type": "text", "text": label, "size": "sm", "color": "#999999",
                 "flex": 2},
                {"type": "text", "text": str(value), "size": "sm", "color": color,
                 "flex": 3, "wrap": True},
            ],
        }

    body_contents = [
        # Header
        {"type": "text", "text": f"📋 {name}", "weight": "bold", "size": "lg",
         "color": "#1E88E5"},
        {"type": "text", "text": f"HN {hn}" + (f" | {ckd_stage}" if ckd_stage else ""),
         "size": "sm", "color": "#999999"},
        {"type": "separator", "margin": "md"},

        # ข้อมูลฟอก
        {"type": "text", "text": "🏥 ข้อมูลไตเทียม", "weight": "bold", "size": "sm",
         "color": "#2E8B57", "margin": "md"},
        _info_row("ตารางฟอก", schedule_days),
        _info_row("ESA", f"{esa} {esa_dose}"),
        _info_row("Vascular", vascular),
    ]

    if dry_weight:
        body_contents.append(_info_row("DW", f"{dry_weight} kg"))

    body_contents.extend([
        {"type": "separator", "margin": "md"},

        # ผลแล็บ
        {"type": "text", "text": "🔬 ผลแล็บล่าสุด", "weight": "bold", "size": "sm",
         "color": "#E65100", "margin": "md"},
        {"type": "text", "text": lab_text, "size": "sm", "color": "#555555",
         "wrap": True},

        {"type": "separator", "margin": "md"},

        # การรักษา
        {"type": "text", "text": "💊 ปรับยาล่าสุด", "weight": "bold", "size": "sm",
         "color": "#6735B7", "margin": "md"},
        {"type": "text", "text": treatment_text, "size": "sm", "color": "#555555",
         "wrap": True},
    ])

    if pending_count > 0:
        body_contents.append(
            {"type": "text", "text": f"⏳ รอติดตามผล: {pending_count} รายการ",
             "size": "xs", "color": "#C62828", "margin": "xs"})

    body_contents.extend([
        {"type": "separator", "margin": "md"},
        # นัดหมาย
        {"type": "text", "text": "📅 นัดหมายถัดไป", "weight": "bold", "size": "sm",
         "color": "#00796B", "margin": "md"},
        {"type": "text", "text": next_appointment, "size": "sm", "color": "#555555"},
    ])

    # Quick Reply
    qr_items = [
        {"type": "action", "action": {"type": "message",
         "label": f"🔬 วิเคราะห์ {hn}", "text": f"วิเคราะห์ {hn}"}},
        {"type": "action", "action": {"type": "message",
         "label": f"📊 กราฟ {hn}", "text": f"กราฟ {hn}"}},
        {"type": "action", "action": {"type": "message",
         "label": f"💊 ปรับยา {hn}", "text": f"ปรับยา {hn}"}},
        {"type": "action", "action": {"type": "message",
         "label": f"📋 เปรียบเทียบ {hn}", "text": f"เปรียบเทียบ {hn}"}},
    ]

    return {
        "type": "flex",
        "altText": f"📋 สรุป {name} (HN {hn})",
        "quickReply": {"items": qr_items},
        "contents": {
            "type": "bubble",
            "body": {
                "type": "box", "layout": "vertical", "spacing": "sm",
                "paddingAll": "16px",
                "contents": body_contents,
            },
        },
    }
