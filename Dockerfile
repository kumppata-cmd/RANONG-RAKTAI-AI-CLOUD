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
