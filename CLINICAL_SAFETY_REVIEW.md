# Clinical Safety Review — RANONG-RAKTAI-AI 2.0

**Reviewer:** Claude Opus 4.6 (automated audit)
**Date:** 2026-10-23
**Scope:** Clinical safety of rule-based analysis engine, alert delivery, OCR safety, authorization, and prescribing guardrails

---

## 1. Clinical Thresholds — CKD vs Maintenance HD

**Status: ⚠️ FINDING**

### CKD Staging (KDIGO 2012) — clinical_analyzer.py
- **PASS:** CKD staging follows KDIGO 2012 correctly (lines 149-169):
  - G1 >= 90, G2 >= 60, G3a >= 45, G3b >= 30, G4 >= 15, G5 < 15
- **PASS:** Severity mapping is appropriate (lines 205-211):
  - G4/G5 = CRITICAL, G3a/G3b = WARNING, G2 = MILD, G1 = NORMAL

### eGFR < 15 in Maintenance Dialysis — alert_delivery.py
- **PASS:** `create_alert()` (lines 243-250) correctly downgrades eGFR < 15 from CRITICAL to URGENT when `patient_context.get("is_dialysis_patient")` is True
- **PASS:** Test coverage exists (`test_phase2a_safety.py:292-301`)

### Finding: clinical_analyzer.py does NOT differentiate CKD vs HD patients
- `_assess_ckd()` (line 192) always classifies eGFR < 15 as `Severity.CRITICAL` regardless of patient context
- The differentiation only happens at the **alert delivery** layer, not the analysis layer
- **Impact:** The clinical analysis text will say "critical" for a stable dialysis patient's eGFR of 8, which is expected and normal for them. The alert system correctly handles this, but the analysis text shown to the physician may be misleading.
- **Recommendation:** Consider accepting patient_context in `analyze_lab_report()` to adjust CKD findings for known HD patients (e.g., severity=INFO with note "expected for maintenance dialysis")

### Critical Value Thresholds — Clinically Appropriate
| Analyte | Critical Threshold | Assessment |
|---------|-------------------|------------|
| Potassium > 6.5 mmol/L | CRITICAL | ✅ Appropriate |
| Potassium < 3.0 mmol/L | CRITICAL | ✅ Appropriate |
| Sodium < 125 mmol/L | CRITICAL | ✅ Appropriate |
| Sodium > 150 mmol/L | CRITICAL | ✅ Appropriate |
| Hemoglobin < 7.0 g/dL | CRITICAL | ✅ Appropriate |
| Calcium < 7.0 mg/dL | CRITICAL | ✅ Appropriate |
| Phosphate > 6.5 mg/dL | CRITICAL | ✅ Appropriate |
| Ca×PO₄ > 55 | CRITICAL | ✅ Appropriate |
| Bicarbonate < 15 mmol/L | CRITICAL | ✅ Appropriate |
| Albumin < 2.5 g/dL | CRITICAL | ✅ Appropriate |
| FBS < 50 mg/dL | CRITICAL | ✅ Appropriate |
| FBS > 300 mg/dL | CRITICAL | ✅ Appropriate |
| Kt/V < 1.0 | CRITICAL | ✅ Appropriate |

---

## 2. Critical Value Escalation

**Status: ✅ PASS**

### Low OCR Confidence → SOURCE_VERIFY — alert_delivery.py
- **PASS:** `CriticalAlert.__post_init__()` (line 121): `if self.ocr_confidence < 0.7: self.severity = AlertSeverity.SOURCE_VERIFY.value`
- **PASS:** `create_alert()` (line 243): Independently checks `if ocr_confidence < 0.7` and sets SOURCE_VERIFY before any other severity logic
- **PASS:** Notification text includes OCR warning (lines 307-311): "OCR confidence ต่ำ ... กรุณาตรวจสอบเอกสารต้นฉบับ"

### Escalation After Max Delivery Attempts — alert_delivery.py
- **PASS:** `should_escalate()` (line 163): Returns True when `delivery_attempts >= max_delivery_attempts` (default 5)
- **PASS:** `attempt_delivery()` (lines 382-392): Transitions to ESCALATED state and logs audit event
- **PASS:** `AlertRetryManager._escalate()` (lines 685-717): Sends escalation notification to admin or fallback to physician recipients
- **PASS:** Exponential backoff implemented (line 169): `min(30 * 2^(attempts-1), 3600)` — 30s to 1hr max
- **PASS:** State machine has valid transitions with no dead-end states (lines 61-68)
- **PASS:** PHI excluded from all notifications (line 289-311)

---

## 3. OCR Verification Gate

**Status: ⚠️ FINDING**

### OCR Safety Validator — ocr_safety.py
- **PASS:** Three-tier confidence system (lines 34-37):
  - `min_auto_confidence: 0.85` — below requires manual review
  - `low_confidence: 0.70` — below requires source verification
  - `critical_confidence: 0.50` — below marks result as invalid
- **PASS:** Implausible values marked invalid and require review (lines 166-167)
- **PASS:** Clinical plausibility ranges defined for all major analytes (lines 82-105)
- **PASS:** Unit validation with alias normalization (lines 108-122)

### Physician Verification Step — server.py + lab_models.py
- **PASS:** `ReviewStatus` enum defines PENDING_REVIEW / APPROVED / REJECTED (lab_models.py:26-29)
- **PASS:** Lab reports start as `pending_review` by default (lab_models.py docstring line 12)
- **PASS:** Physician approval command exists in server.py (lines 2469-2577) with authorization check (only PHYSICIAN_USERS or ADMIN_USERS)

### Finding: Clinical analysis runs on unverified (pending_review) lab reports
- In `server.py` lines 4469-4488, `analyze_lab_report()` is called immediately after lab extraction, without checking `lab_report.status == ReviewStatus.APPROVED`
- The analysis runs on `pending_review` data — before any physician has verified the OCR extraction
- **Impact:** Clinical findings (including CRITICAL alerts) are generated and stored from potentially incorrect OCR data. The analysis results include a disclaimer, but are stored and can be viewed before physician approval.
- **Mitigating factors:**
  1. The OCR safety validator flags low-confidence results
  2. Alert delivery downgrades low-confidence OCR to SOURCE_VERIFY severity
  3. All clinical output includes "ข้อมูลประกอบ" disclaimer
- **Recommendation:** Either (a) defer clinical analysis until after physician approval, or (b) prominently tag analysis results from unverified labs with a "PENDING VERIFICATION" warning in the stored JSON and displayed text

---

## 4. Patient-Level Authorization

**Status: ✅ PASS**

### User Authentication — server.py
- **PASS:** `is_authorized_user()` function (line 1968) checks user_hash against AUTHORIZED_USERS list
- **PASS:** Main webhook handler checks authorization early (lines 2074-2094): unauthorized access is logged and rejected
- **PASS:** AUTHORIZED_USERS loaded from environment variable (line 140)

### Owner Hash Checks — server.py
- **PASS:** `get_with_owner_check()` (line 1251) verifies owner_hash before returning patient data
- **PASS:** Document read commands check authorization (line 2196-2203): unauthorized access logged as `read_unauthorized`
- **PASS:** File request commands check authorization (line 2249-2258): unauthorized logged as `file_request_unauthorized`
- **PASS:** Lab result commands check authorization (line 2306-2313): unauthorized logged as `lab_result_unauthorized`
- **PASS:** Lab approval restricted to PHYSICIAN_USERS / ADMIN_USERS (lines 2470-2473)
- **PASS:** Search is owner-scoped (line 1187-1203): `Fail Closed` — documents without owner_hash are hidden
- **PASS:** `list_by_owner()` and `recent_by_owner()` filter by owner_hash (lines 1219-1243)
- **PASS:** `assign_owner()` prevents reassignment of existing owner_hash (line 1304)

---

## 5. No Auto-Prescribing

**Status: ✅ PASS**

### Codebase-Wide Search
- **No prescribing functions found:** No `prescribe()`, `order_medication()`, `adjust_dose()`, `auto_treat()`, or `change_medication()` functions exist anywhere in the codebase
- **PASS:** All recommendations use advisory language:
  - "พิจารณา..." (consider...) — clinical_analyzer.py throughout
  - "ปรึกษาแพทย์..." (consult physician...) — medication_review.py:288, 370, 403
  - Never "ให้ยา", "สั่งยา", or "จ่ายยา" in output

### Explicit Safety Declarations
- **clinical_analyzer.py line 21:** "ห้ามสั่งยา เปลี่ยนยา หรือสั่งการรักษาโดยอัตโนมัติ"
- **medication_review.py lines 15-16:** "ผลเป็น 'ข้อมูลประกอบ' ไม่ใช่การสั่งยาหรือเปลี่ยนยา"
- **lab_alert.py line 13:** "ห้าม AI สั่งยาหรือแนะนำการรักษาเฉพาะเจาะจง"
- **lab_tracker.py line 15:** "ไม่สั่งยาหรือสั่งการรักษา — แจ้งเตือนและแนะนำเท่านั้น"
- **nephrology_kpi.py line 21:** "ห้ามสั่งยา เปลี่ยนยา หรือสั่งการรักษาโดยอัตโนมัติ"

### Test Coverage for No-Prescribing Guardrail
- **test_clinical_analyzer.py:726-727:** Asserts no "สั่งยา" or "เปลี่ยนยา" in recommendations
- **test_lab_tracker.py:628-629:** Same assertion on alert recommendations
- **test_medication_review.py:577-590:** Checks output contains no prescribing verbs
- **test_phase2b_clinical.py:580-589:** Forbidden word list includes "ให้หยุดยา", "ต้องเปลี่ยนยา", "สั่งยา"
- **test_phase2b_clinical.py:755-761:** Checks no prescribing functions exist in modules
- **test_phase2c_hardening.py:606-626:** Additional prescribing language checks

---

## 6. Clinical Disclaimers

**Status: ✅ PASS**

### Disclaimer in Clinical Analysis — clinical_analyzer.py
- **PASS:** `ClinicalAnalysis.disclaimer` field (lines 109-112): "ผลวิเคราะห์นี้เป็นข้อมูลประกอบการตัดสินใจเท่านั้น ไม่ใช่การวินิจฉัยโรค — ต้องผ่านการตรวจสอบโดยแพทย์ก่อนนำไปใช้"
- **PASS:** Disclaimer included in summary text (line 791): `_generate_summary()` appends disclaimer
- **PASS:** Disclaimer exported in `to_dict()` (line 125)

### Disclaimer in Medication Review — medication_review.py
- **PASS:** `MedicationReviewResult.disclaimer` (line 218)
- **PASS:** `MedicationReview.disclaimer` (lines 1025-1026)
- **PASS:** Disclaimer appended to formatted text output (lines 805, 873, 1176)

### Disclaimer in KPI Dashboard — nephrology_kpi.py
- **PASS:** `KpiDashboard.disclaimer` (line 207): "Dashboard นี้เป็นข้อมูลประกอบการพิจารณา"

### Test Coverage
- **test_clinical_analyzer.py:543-548:** Asserts disclaimer contains "ข้อมูลประกอบ" and "แพทย์"
- **test_clinical_analyzer.py:709-714:** Asserts disclaimer is never empty
- **test_clinical_analyzer.py:780-784:** Asserts disclaimer appears in summary text
- **test_lab_tracker.py:609-612, 631-639:** Asserts disclaimer in tracker output
- **test_phase2b_clinical.py:234-235, 486-487, 565-566:** Report/dashboard/review disclaimer tests

---

## Summary

| Area | Status | Notes |
|------|--------|-------|
| 1. Clinical Thresholds | ⚠️ FINDING | Thresholds correct; CKD vs HD differentiation only at alert layer, not analysis layer |
| 2. Critical Value Escalation | ✅ PASS | Low OCR → SOURCE_VERIFY; escalation after max attempts; exponential backoff |
| 3. OCR Verification Gate | ⚠️ FINDING | OCR validator robust; but clinical analysis runs before physician approval |
| 4. Patient-Level Authorization | ✅ PASS | Owner hash checked on all clinical commands; fail-closed design |
| 5. No Auto-Prescribing | ✅ PASS | No prescribing functions; advisory language only; extensive test coverage |
| 6. Clinical Disclaimers | ✅ PASS | Disclaimer in all clinical outputs; tested |

### Recommendations (Priority Order)

1. **[MEDIUM]** Tag clinical analysis from unverified labs with "PENDING VERIFICATION" status, or defer analysis until physician approval (Area 3)
2. **[LOW]** Accept patient_context in `analyze_lab_report()` to adjust CKD staging text for known maintenance dialysis patients (Area 1)

### Overall Assessment
The system demonstrates strong clinical safety design with defense-in-depth: rule-based analysis only (no external AI), mandatory disclaimers, no auto-prescribing, fail-closed authorization, OCR confidence gating, and proper alert escalation. The two findings are enhancement opportunities, not safety-critical gaps, because mitigating controls (disclaimers, alert severity downgrade, physician approval workflow) are already in place.
