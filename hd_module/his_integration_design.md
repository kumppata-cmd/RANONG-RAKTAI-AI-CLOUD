# F6: HIS Integration Design — เชื่อมระบบ รพ.ระนอง

## สถานะ: 📋 Design Only (ต้อง API จากทาง รพ.)

## แนวคิด
เชื่อม RANONG-RAKTAI-AI กับระบบ HIS (Hospital Information System) ของ รพ.ระนอง
เพื่อดึงข้อมูลอัตโนมัติ แทนการถ่ายรูปส่ง LINE

## ข้อมูลที่ต้องการดึง
1. **ผลแล็บ** — CBC, BUN, Cr, Electrolytes (ดึงอัตโนมัติเมื่อมีผลใหม่)
2. **รายการยา** — ใบสั่งยา prescription (EPO, Iron, Antihypertensives)
3. **ข้อมูลผู้ป่วย** — ชื่อ, HN, diagnosis, แพ้ยา
4. **นัดหมาย** — ตารางนัดพบแพทย์, ตาราง Dialysis

## Architecture Options

### Option A: HL7 FHIR REST API (แนะนำ)
```
รพ.ระนอง HIS → FHIR Server → RANONG-RAKTAI-AI
                                 ↓
                           PostgreSQL (HD tables)
```
- Standard: HL7 FHIR R4
- Endpoints: /Patient, /Observation, /MedicationRequest
- Auth: OAuth 2.0 Client Credentials
- Polling interval: ทุก 15 นาที

### Option B: Direct Database Read-Only View
```
รพ.ระนอง HIS DB → Read-only View → VPN → RANONG-RAKTAI-AI
```
- ง่ายกว่า FHIR แต่ต้องเข้าถึง DB โดยตรง
- ต้อง VPN เชื่อม Cloud ↔ รพ.
- Security concern สูง

### Option C: File-based Integration (CSV/HL7v2)
```
รพ.ระนอง HIS → Export CSV/HL7v2 → GCS → RANONG-RAKTAI-AI
```
- ใช้ Windows sync ที่มีอยู่ (gcs_sync.py)
- รพ. export file ไว้ที่ folder → sync ขึ้น GCS อัตโนมัติ
- ง่ายที่สุด แต่ไม่ real-time (5 นาที delay)

## สิ่งที่ต้องได้จาก รพ.
1. **API documentation** หรือ **DB schema** ของ HIS
2. **Credentials** สำหรับเข้าถึง (VPN, API key)
3. **ตัวอย่างข้อมูล** (anonymized) เพื่อ test mapping
4. **Contact person** ฝ่าย IT รพ.ระนอง

## Implementation Plan (เมื่อได้ API แล้ว)
1. สร้าง `hd_module/his_connector.py` — adapter pattern
2. เพิ่ม auto-sync scheduler ใน `__init__.py`
3. Map HIS data → HD tables (hd_patient_mapping, lab results)
4. LINE notification เมื่อมีผลแล็บใหม่
5. Test with anonymized data → verify with real data
