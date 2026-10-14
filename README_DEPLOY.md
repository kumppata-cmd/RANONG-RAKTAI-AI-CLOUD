# RANONG RAKTAI AI — Cloud Deployment Guide

ระบบจัดการเอกสารทางการแพทย์ คลินิกระนอง รักษ์ไต  
Deploy บน **Render Managed Cloud**

---

## สถาปัตยกรรม

```
LINE OA  →  Render (Gunicorn + Flask)  →  Claude API (จำแนกเอกสาร)
                    ↓                            ↓
            Tesseract OCR              Keyword Fallback
                    ↓
          Cloud Storage (S3/GCS)
```

## ขั้นตอน Deploy

### 1. เตรียม GitHub Repository

```bash
cd CLOUD_DEPLOY
git init
git add .
git commit -m "Initial cloud deployment"
git remote add origin https://github.com/<your-username>/ranong-raktai-cloud.git
git push -u origin main
```

### 2. สร้าง Web Service บน Render

1. ไปที่ [render.com](https://render.com) → **New** → **Web Service**
2. เชื่อม GitHub repository
3. Render จะตรวจพบ `render.yaml` อัตโนมัติ
4. เลือก region: **Singapore** (ใกล้ไทยที่สุด)
5. Plan: **Starter** ($7/mo) ขึ้นไป

### 3. ตั้งค่า Environment Variables

ใน Render Dashboard → **Environment**:

| Variable | คำอธิบาย | ตัวอย่าง |
|----------|---------|---------|
| `LINE_CHANNEL_SECRET` | LINE Channel Secret | (จาก LINE Developers) |
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE Channel Access Token | (จาก LINE Developers) |
| `ANTHROPIC_API_KEY` | Claude API Key | (ตั้งค่าใน Render Dashboard) |
| `ENABLE_AI_CLASSIFY` | เปิด/ปิด AI | `true` |
| `STORAGE_BACKEND` | ชนิด storage | `local` / `s3` / `gcs` |

> **สำคัญ**: ห้ามใส่ค่า secret ใน code หรือ `render.yaml` — ใช้ Render Dashboard เท่านั้น

### 4. อัปเดต LINE Webhook URL

1. ไปที่ [LINE Developers Console](https://developers.line.biz)
2. เลือก Channel → **Messaging API**
3. เปลี่ยน Webhook URL เป็น:
   ```
   https://ranong-raktai-ai.onrender.com/webhook
   ```
4. เปิด **Use webhook** = On
5. กด **Verify** เพื่อทดสอบ

---

## ไฟล์ในโปรเจกต์

| ไฟล์ | คำอธิบาย |
|------|---------|
| `server.py` | Flask application หลัก |
| `requirements.txt` | Python dependencies |
| `Dockerfile` | Docker image (Tesseract + Python) |
| `render.yaml` | Render Blueprint configuration |
| `.gitignore` | ป้องกันข้อมูลลับเข้า Git |
| `test_cloud.py` | ทดสอบด้วยข้อมูลจำลอง |

---

## การจัดเก็บเอกสาร (Storage)

### ปัจจุบัน: Local (Ephemeral)
- ไฟล์เก็บที่ `/tmp/ranong_raktai/`
- **ข้อจำกัด**: ไฟล์จะหายเมื่อ Render redeploy หรือ restart

### อนาคต: Cloud Storage
เมื่อพร้อมใช้งานจริง ให้เปลี่ยน:

**Amazon S3:**
```
STORAGE_BACKEND=s3
STORAGE_BUCKET=ranong-raktai-docs
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=ap-southeast-1
```

**Google Cloud Storage:**
```
STORAGE_BACKEND=gcs
STORAGE_BUCKET=ranong-raktai-docs
GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
```

---

## OCR บน Cloud

Dockerfile ติดตั้ง Tesseract OCR + ภาษาไทย อัตโนมัติ:
- `tesseract-ocr` — OCR engine
- `tesseract-ocr-tha` — Thai language data
- `tesseract-ocr-eng` — English language data

---

## Endpoints

| Path | Method | คำอธิบาย |
|------|--------|---------|
| `/` | GET | สถานะระบบ |
| `/health` | GET | Health check (Render ใช้ตรวจสอบ) |
| `/webhook` | POST | LINE Webhook (รับเอกสาร) |

---

## ความแตกต่างจาก Windows Version

| รายการ | Windows | Cloud |
|--------|---------|-------|
| Server | Flask dev | Gunicorn |
| Tesseract | `C:\Program Files\...` | `/usr/bin/tesseract` (apt) |
| Storage | Local disk ถาวร | Ephemeral → Cloud Storage |
| Tunnel | ngrok | Render HTTPS URL |
| Config | `.env` file | Render Dashboard |
| Port | 5000 | PORT env (Render กำหนด) |

---

## สิ่งที่ยังต้องทำ (TODO)

- [ ] สร้าง GitHub repository และ push code
- [ ] สร้าง Render Web Service
- [ ] ตั้งค่า Environment Variables ใน Render Dashboard
- [ ] เชื่อมต่อ Cloud Storage (S3 หรือ GCS)
- [ ] อัปเดต LINE Webhook URL
- [ ] ทดสอบกับ LINE OA จริง
- [ ] ตั้งค่า Custom Domain (ถ้าต้องการ)
- [ ] ตั้งค่า Monitoring / Alerting

---

## กฎความปลอดภัย

1. ห้ามนำ `.env`, API Keys, Tokens เข้า GitHub
2. ห้ามส่งข้อมูลผู้ป่วยจริงเข้า AI ระหว่างทดสอบ
3. ห้ามลบไฟล์ต้นฉบับ (คัดลอกเท่านั้น)
4. ห้ามแก้ไขข้อมูลทางการแพทย์
5. ตรวจสอบ LINE Signature ทุกครั้ง
6. ระบบ Windows เดิมยังใช้งานได้ตามปกติ
