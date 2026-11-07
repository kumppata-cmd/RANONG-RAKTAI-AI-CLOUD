# RANONG RAKTAI AI 2.0 — Cloud Deployment
# Dockerfile สำหรับ Render (ติดตั้ง Tesseract OCR + Thai language)
# Security: Non-root runtime user, least-privilege filesystem

FROM python:3.12-slim

# ติดตั้ง Tesseract OCR + ภาษาไทย + อังกฤษ
# หมายเหตุ: Debian Bookworm เปลี่ยนชื่อ libgl1-mesa-glx → libgl1
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-tha \
        tesseract-ocr-eng \
        libgl1 \
        libglib2.0-0 \
        fonts-thai-tlwg \
    && rm -rf /var/lib/apt/lists/*

# ตั้งค่า working directory
WORKDIR /app

# คัดลอก requirements และติดตั้ง dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# คัดลอก source code — ทั้ง modules เดิมและใหม่
COPY server.py .
COPY lab_models.py .
COPY lab_extractor.py .
COPY lab_rules.py .
COPY clinical_analyzer.py .
COPY lab_tracker.py .
COPY clinical_reports.py .
COPY medication_review.py .
COPY line_bot_enhancer.py .
COPY lab_alert.py .
COPY physician_approval.py .
COPY alert_delivery.py .
COPY patient_identity.py .
COPY ocr_safety.py .
COPY health_check.py .
COPY audit_trail.py .
COPY dashboard.py .
COPY ocr_review.py .
COPY lab_dashboard.py .
COPY line_patient_assistant.py .
COPY document_workflow.py .
COPY patient_hn_registry.py .
COPY quarterly_compare.py .
COPY vendor_registry.py .
COPY monitoring_schedule.py .
COPY nephrology_kpi.py .
COPY patient_master_registry.py .
COPY import_dialysis_excel.py .
COPY database.py .
COPY lab_intelligence_agent.py .
COPY utils.py .
COPY treatment_tracker.py .
COPY ckd_knowledge.py .
COPY patient_summary_agent.py .
COPY patient_link_code.py .
COPY appointment_reminder.py .

# Phase A-F: Clinical Intelligence Platform 2.0
COPY clinical_dashboard.py .
COPY auto_report.py .
COPY ckd_progression.py .
COPY nephrology_drug_db.py .
COPY liff_app.py .

# V4.0: Platform modules
COPY event_logger.py .
COPY backup_manager.py .
COPY approval_workflow.py .
COPY rbac.py .
COPY policy_engine.py .
COPY scheduled_jobs.py .

# V5.0: Multi-Agent + Conversational + No-Code Workflow
COPY agent_orchestrator.py .
COPY thai_intent_parser.py .
COPY workflow_engine.py .
COPY workflow_ui.py .
COPY v5_integration.py .

# Google Integration + Finance + Monitoring modules
COPY google_drive_sync.py .
COPY google_sheets_sync.py .
COPY gmail_report.py .
COPY invoice_parser.py .
COPY expense_tracker.py .
COPY dead_letter.py .
COPY system_monitor.py .

# Clinical Analytics + Scheduling modules
COPY auto_weekly_report.py .
COPY dialysis_adequacy.py .
COPY epo_response_tracker.py .
COPY hd_schedule_reminder.py .
COPY ckd_staging.py .

# HD Smart Monitoring Module (Phase 1)
COPY hd_module/ ./hd_module/

# Rich Menu icons (Staff/Patient/Sub + legacy v2)
COPY rich_menu_v2.png .
COPY rich_menu_staff.png .
COPY rich_menu_patient.png .
COPY rich_menu_sub.png .

# สร้าง non-root user สำหรับ runtime
RUN groupadd -r appuser && \
    useradd -r -g appuser -d /app -s /sbin/nologin appuser

# สร้างโฟลเดอร์ชั่วคราวที่ appuser เข้าถึงได้
RUN mkdir -p /tmp/ranong_raktai/logs && \
    chown -R appuser:appuser /tmp/ranong_raktai && \
    chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Expose port (Render กำหนด PORT ผ่าน env)
EXPOSE 10000

# รัน Gunicorn (ใช้ PORT จาก Render ENV, fallback 10000)
CMD gunicorn server:app --bind "0.0.0.0:${PORT:-10000}" --workers 2 --timeout 120 --access-logfile -
