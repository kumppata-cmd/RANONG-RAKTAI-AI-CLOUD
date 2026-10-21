# RANONG RAKTAI AI — Cloud Deployment
# Dockerfile สำหรับ Render (ติดตั้ง Tesseract OCR + Thai language)

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
    && rm -rf /var/lib/apt/lists/*

# ตั้งค่า working directory
WORKDIR /app

# คัดลอก requirements และติดตั้ง dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# คัดลอก source code
COPY server.py .
COPY lab_models.py .
COPY lab_extractor.py .
COPY lab_rules.py .

# สร้างโฟลเดอร์ชั่วคราว
RUN mkdir -p /tmp/ranong_raktai/logs

# Expose port (Render กำหนด PORT ผ่าน env)
EXPOSE 10000

# รัน Gunicorn (ใช้ PORT จาก Render ENV, fallback 10000)
CMD gunicorn server:app --bind "0.0.0.0:${PORT:-10000}" --workers 2 --timeout 120 --access-logfile -
