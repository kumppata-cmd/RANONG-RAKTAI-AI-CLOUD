# HD Smart Monitoring — Offline Draft Mode Design

## 1. Overview

Offline Draft Mode ช่วยให้พยาบาลบันทึกข้อมูลข้างเตียงได้แม้ internet ขัดข้อง
ข้อมูลจะถูกเก็บในอุปกรณ์ (tablet/phone) และ sync กลับเมื่อกลับมาออนไลน์

**หลักการ**: ข้อมูลต้องไม่สูญหาย ไม่ซ้ำซ้อน ไม่ปะปนกันระหว่าง user

## 2. Client-Side Storage

### 2.1 IndexedDB (Primary)
- Database: `hd_offline_db`
- Object Stores:
  - `draft_vitals` — vital signs ที่บันทึก offline
  - `draft_uf` — UF records
  - `draft_observations` — machine observations
  - `sync_queue` — pending sync items
  - `user_session` — JWT token + user info (encrypted)

### 2.2 localStorage (Fallback)
- ใช้เมื่อ IndexedDB ไม่รองรับ
- จำกัด 5MB — เพียงพอสำหรับ 1 shift (4 ชม.)

## 3. Sync Protocol

### 3.1 Queue-Based
```
[บันทึก offline] → IndexedDB sync_queue → [online detected] → POST /hd/api/v1/sync
```

### 3.2 Sync Flow
1. Client ตรวจสอบ online status (navigator.onLine + heartbeat)
2. เมื่อ online → ดึง sync_queue ทั้งหมด
3. ส่งทีละ batch (max 50 records)
4. Server validate + save → return success/conflict
5. Client ลบ items ที่ sync สำเร็จ
6. Retry items ที่ fail (max 3 attempts)

### 3.3 Heartbeat
- Ping `/hd/api/v1/health` ทุก 30 วินาที
- ถ้า fail 3 ครั้งติดกัน → ย้ายเข้า offline mode
- แสดง indicator บนหน้าจอ (🟢 Online / 🔴 Offline)

## 4. Conflict Resolution

### 4.1 Strategy: Server Wins + Merge UI
- ถ้า record ถูกแก้ไขบน server ระหว่างที่ client offline:
  - Server version wins โดย default
  - แสดง conflict UI ให้ user เลือก
  - เก็บทั้ง 2 version ใน audit trail

### 4.2 Conflict Detection
- ทุก record มี `version` (incrementing integer)
- Client ส่ง `expected_version` ไปกับ sync request
- ถ้า `expected_version` != `current_version` → conflict

### 4.3 Merge UI
```
┌─────────────────────────────┐
│ ⚠️ ข้อมูลชนกัน              │
│                             │
│ ข้อมูลของคุณ (offline):     │
│   SBP: 140, HR: 88         │
│                             │
│ ข้อมูลบน server:            │
│   SBP: 135, HR: 85         │
│                             │
│ [ใช้ของฉัน] [ใช้ server] [ยกเลิก] │
└─────────────────────────────┘
```

## 5. Data Integrity

### 5.1 Checksums
- ทุก draft record มี SHA-256 checksum
- ตรวจสอบก่อน sync ว่าข้อมูลไม่ถูกแก้ไข
- ถ้า checksum ไม่ตรง → alert user

### 5.2 Timestamps
- ใช้ client timestamp (recorded_at) สำหรับ clinical data
- ใช้ server timestamp (synced_at) สำหรับ audit
- เก็บทั้ง 2 timestamps เสมอ

### 5.3 Ordering
- sync_queue เรียงตาม `recorded_at`
- ส่งตามลำดับ — vital signs ต้องมา before UF ของ session เดียวกัน

## 6. User Verification

### 6.1 On Reconnect
- เมื่อกลับมา online → ตรวจ JWT validity
- ถ้า JWT expired → แสดง PIN prompt (ไม่ full login)
- PIN = 6 digits ที่ user ตั้งไว้

### 6.2 Offline Authentication
- เก็บ hashed PIN ใน IndexedDB (bcrypt)
- Lock หลัง 3 ครั้ง → ต้อง full login เมื่อ online
- Auto-lock หลัง 15 นาที idle

### 6.3 Device Registration
- แต่ละ device ต้องลงทะเบียนกับ server ก่อนใช้ offline mode
- Device ID = fingerprint (hardware + browser)
- Admin approve device ก่อนเปิดใช้ offline

## 7. Security Considerations

- ข้อมูลใน IndexedDB encrypt ด้วย AES-256 (Web Crypto API)
- Clear data เมื่อ user logout
- Auto-clear หลัง 24 ชม. ไม่ใช้งาน
- ห้ามเก็บข้อมูลผู้ป่วยจริงใน localStorage (ใช้ IndexedDB + encryption)
- Sync over HTTPS เท่านั้น

## 8. Implementation Notes

- Phase 1: Design only (this document)
- Phase 2: Implement basic offline queue
- Phase 3: Add conflict resolution UI
- Phase 4: Add device registration + encrypted storage
- ต้อง test กับ Chrome DevTools Network throttling
- ต้อง test กับ airplane mode จริง
