# คู่มือเตรียม Staging Environment — RANONG RAKTAI AI 2.0

> **วัตถุประสงค์:** ตั้ง staging ที่แยกจาก production ทั้งหมด เพื่อทดสอบ E2E ก่อน merge
> **ผู้ดำเนินการ:** เจ้าของบัญชี (Account Owner) — ขั้นตอนทั้งหมดทำได้ในชุดเดียว ~30 นาที

---

## สิ่งที่ต้องสร้าง (3 ระบบ แยกจาก Production)

| ระบบ | Production | Staging (ต้องสร้างใหม่) |
|------|-----------|----------------------|
| Render Service | `ranong-raktai-ai-cloud` | `ranong-raktai-ai-staging` |
| GCS Bucket | `ranong-raktai-ai-prod-2026` | `ranong-raktai-ai-staging-2026` |
| LINE Channel | Production OA | Staging OA (หรือใช้ Messaging API test) |

---

## ขั้นตอนที่ 1: สร้าง GCS Staging Bucket

### 1.1 สร้าง Bucket ใหม่
```bash
# ใน Google Cloud Console → Cloud Storage → Create Bucket
# หรือใช้ gcloud CLI:
gcloud storage buckets create gs://ranong-raktai-ai-staging-2026 \
  --project=ranong-raktai-ai \
  --location=asia-southeast1 \
  --uniform-bucket-level-access \
  --public-access-prevention=enforced
```

**สำคัญ:**
- ✅ Location: `asia-southeast1` (Singapore — เดียวกับ Render)
- ✅ Uniform bucket-level access: ON
- ✅ Public access prevention: Enforced
- ❌ ห้าม Auto Delete / Retention Policy
- ❌ ห้ามเปิด Public Access

### 1.2 สร้าง Service Account สำหรับ Staging
```bash
# สร้าง SA ใหม่ (แยกจาก production SA)
gcloud iam service-accounts create ranong-raktai-staging \
  --project=ranong-raktai-ai \
  --display-name="Ranong Raktai Staging SA"

# ให้สิทธิ์เฉพาะ staging bucket
gcloud storage buckets add-iam-policy-binding \
  gs://ranong-raktai-ai-staging-2026 \
  --member="serviceAccount:ranong-raktai-staging@ranong-raktai-ai.iam.gserviceaccount.com" \
  --role="roles/storage.objectUser"
```

**ห้าม:** ให้ staging SA เข้าถึง production bucket

### 1.3 สร้าง JSON Key
```bash
gcloud iam service-accounts keys create staging-sa-key.json \
  --iam-account=ranong-raktai-staging@ranong-raktai-ai.iam.gserviceaccount.com
```

เก็บไฟล์ `staging-sa-key.json` ไว้ใช้ในขั้นตอนที่ 3

---

## ขั้นตอนที่ 2: สร้าง LINE Staging Channel

### ตัวเลือก A: สร้าง LINE OA ใหม่สำหรับ Staging (แนะนำ)

1. ไปที่ [LINE Developers Console](https://developers.line.biz/)
2. สร้าง Provider ใหม่ หรือใช้ Provider เดิม
3. สร้าง **Messaging API Channel** ใหม่:
   - Channel name: `Ranong Raktai AI (Staging)`
   - Plan: Free (เพียงพอสำหรับ staging)
4. เปิด Webhook:
   - Webhook URL: `https://ranong-raktai-ai-staging.onrender.com/webhook`
   - Use webhook: ON
5. บันทึก:
   - **Channel Secret** → ใช้ในขั้นตอนที่ 3
   - **Channel Access Token** (Long-lived) → Issue แล้วใช้ในขั้นตอนที่ 3

### ตัวเลือก B: ใช้ Production LINE Channel ชั่วคราว

⚠️ ใช้ได้เฉพาะกรณีที่ Staging URL ยังไม่ได้ตั้งเป็น Webhook URL
- ทดสอบด้วย `curl` ส่ง webhook โดยตรงแทน
- ไม่ต้องสร้าง LINE Channel ใหม่
- จำกัด: ทดสอบได้เฉพาะ API endpoint ไม่สามารถทดสอบ LINE reply

---

## ขั้นตอนที่ 3: สร้าง Render Staging Service

### 3.1 สร้าง Web Service ใหม่บน Render

1. ไปที่ [Render Dashboard](https://dashboard.render.com/)
2. **New** → **Web Service**
3. เลือก Repository: `kumppata-cmd/RANONG-RAKTAI-AI-CLOUD`
4. ตั้งค่า:
   - **Name:** `ranong-raktai-ai-staging`
   - **Region:** Singapore
   - **Branch:** `release/v2.0-rc1`
   - **Runtime:** Docker
   - **Plan:** Starter ($7/mo)
   - **Auto-Deploy:** OFF ❗

### 3.2 ตั้ง Environment Variables

ไปที่ **Environment** tab ของ staging service แล้วตั้งค่าทั้งหมดนี้:

| Variable | Value | หมายเหตุ |
|----------|-------|---------|
| `APP_ENV` | `production` | ใช้ production mode เพื่อทดสอบ behavior จริง |
| `STORAGE_BACKEND` | `gcs` | ต้องเป็น gcs เท่านั้น |
| `GCS_BUCKET_NAME` | `ranong-raktai-ai-staging-2026` | ❗ ต้องเป็น staging bucket |
| `GCS_CREDENTIALS_BASE64` | *(base64 ของ staging-sa-key.json)* | ดูวิธีด้านล่าง |
| `LINE_CHANNEL_SECRET` | *(จาก Staging LINE Channel)* | ❗ ห้ามใช้ production |
| `LINE_CHANNEL_ACCESS_TOKEN` | *(จาก Staging LINE Channel)* | ❗ ห้ามใช้ production |
| `USER_ID_SALT` | *(สร้างใหม่ เช่น `openssl rand -hex 32`)* | ❗ ห้ามใช้ production salt |
| `AUTHORIZED_USERS` | *(ว่างไว้ก่อน — เพิ่มหลัง enroll)* | |
| `ADMIN_USERS` | *(ว่างไว้ก่อน — เพิ่มหลัง enroll)* | |
| `BASE_DIR` | `/tmp/ranong_raktai` | เหมือน production |
| `ENABLE_AI_CLASSIFY` | `true` | |

**วิธีสร้าง GCS_CREDENTIALS_BASE64:**
```bash
base64 -w0 staging-sa-key.json
# คัดลอก output ทั้งหมด วางใน Render ENV
```

### 3.3 Deploy

1. ใน Render Dashboard → Manual Deploy → Deploy latest commit
2. รอจน status เป็น **Live** (~3-5 นาที)
3. ตรวจสอบ health:
```bash
curl https://ranong-raktai-ai-staging.onrender.com/health
```

ต้องได้:
```json
{
  "status": "healthy",
  "storage": "gcs",
  "gcs_ready": true,
  ...
}
```

---

## ขั้นตอนที่ 4: ตั้ง LINE Webhook URL

1. กลับไป LINE Developers Console
2. ตั้ง Webhook URL: `https://ranong-raktai-ai-staging.onrender.com/webhook`
3. กด **Verify** → ต้องได้ 200 OK

---

## ขั้นตอนที่ 5: Enroll ผู้ใช้ทดสอบ

1. ส่งข้อความ `ลงทะเบียน` ใน Staging LINE OA
2. ระบบจะแสดง user_hash
3. คัดลอก hash ไปตั้งใน Render ENV:
   - `AUTHORIZED_USERS=<hash>`
   - `ADMIN_USERS=<hash>`
4. Redeploy เพื่อให้ ENV มีผล

---

## Checklist ก่อนทดสอบ E2E

- [ ] Staging Render service → Live + /health returns healthy
- [ ] GCS staging bucket → สร้างแล้ว + SA มีสิทธิ์
- [ ] LINE staging channel → Webhook verified
- [ ] ผู้ใช้ทดสอบ → Enrolled + อยู่ใน AUTHORIZED_USERS
- [ ] ไม่ได้ใช้ production credentials ใดๆ

---

## ⛔ ข้อห้ามเด็ดขาด

1. **ห้ามใช้ Production GCS bucket** (`ranong-raktai-ai-prod-2026`) กับ staging
2. **ห้ามใช้ Production LINE credentials** กับ staging
3. **ห้ามใช้ Production USER_ID_SALT** กับ staging
4. **ห้ามส่งข้อมูลผู้ป่วยจริง** ไปยัง staging — ใช้เฉพาะข้อมูลสมมติ
5. **ห้ามเปิด Auto-Deploy** บน staging จนกว่าจะผ่าน E2E

---

## หลังตั้งเสร็จ — แจ้งกลับ

เมื่อตั้ง staging เสร็จ แจ้งข้อมูลต่อไปนี้กลับมา:
1. Staging URL: `https://ranong-raktai-ai-staging.onrender.com`
2. `/health` response
3. LINE Webhook verify result
4. User hash ของผู้ทดสอบ

จากนั้นจะดำเนินการ **Task 6: Synthetic E2E Testing** ต่อทันที
