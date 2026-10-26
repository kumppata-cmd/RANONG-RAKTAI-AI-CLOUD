# ARCHITECTURE AS-IS — RANONG-RAKTAI-AI

**Audit date:** 2026-10-26 · **Commit:** `b4a6619` (`main`) · **Method:** static code audit
**Access:** no production, no GCS → runtime behaviour is inferred from code, not observed.

---

## 1. Deployment Topology

```
LINE Messaging API ──webhook──►  Render (Docker, region: singapore)
                                 gunicorn server:app
                                 --workers 2 --timeout 120
                                 port 10000, health: /health
                                        │
                                        ├──► Google Cloud Storage
                                        │     bucket: ranong-raktai-ai-prod-2026
                                        │     (directive states asia-southeast3 / Bangkok —
                                        │      UNVERIFIED, no GCS access)
                                        │
                                        └──► LINE Push/Reply API (outbound)

LIFF webview ──────HTTP────────►  /liff/api/my-labs   ⚠️ see SECURITY_GAP_ANALYSIS SEC-1
Admin browser ─────HTTP────────►  30 × /admin/* routes (ADMIN_SECRET)

Windows host (Cloud Sync "v2.2-hardened") ──► GCS     ⚠️ NOT LINKED this session — UNVERIFIED
```

**Observations**
- Single stateless web service; no worker process, no scheduler, no message broker.
- **No relational database.** All persistence is GCS JSON objects.
- Compute region (`singapore`) differs from the stated bucket region (Bangkok) → cross-region traffic. Flagged, not a defect.
- A self keep-alive thread pings `/health/live` every 600s (`server.py:9907-9920`) to keep the instance warm.

---

## 2. Request Flow — LINE document ingestion (primary clinical path)

```
POST /webhook
 │
 ├─ 1. Signature verification   HMAC-SHA256, fail-closed      server.py:5224-5233, 7871-7873
 ├─ 2. USER_ID_SALT present?    if missing in prod → 503      server.py:7848-7853
 ├─ 3. Event dedup              event → message → sha256      server.py:4741-4756
 ├─ 4. Rate limit               3 windows, halved if unshared server.py:4779+
 │
 ├─ 5. image/file → reply 200 IMMEDIATELY, then background thread
 │                                                            server.py (_async_process_image/_file)
 │      └─ download from LINE → _handle_incoming_document
 │           └─ multi-page buffer (/tmp, fcntl.flock)
 │                └─ process_file_async
 │                     ├─ validate_file  (ext allowlist, size)  ⚠️ no malware scan
 │                     ├─ classify (LOCAL keywords only — external AI hard-locked)
 │                     ├─ text extraction: pypdf → OCR fallback (Tesseract tha+eng)
 │                     ├─ HN extraction from OCR → master registry match
 │                     ├─ staff patient-context fallback        (added 2026-10-26)
 │                     ├─ copy_to_category → GCSStorage.save
 │                     │    └─ build_document_path (canonical, single builder)
 │                     ├─ doc_index.add  (per-doc JSON + owner_hash)
 │                     └─ lab extraction → rules → create_alert (if critical)
 │                                                         ⚠️ delivery chain broken: CS-1..CS-4
 │
 └─ 6. text → handle_search_command → parse_command (CommandType)
          └─ PROTECTED_COMMANDS gated by AUTHORIZED_USERS      server.py:3167, 3214-3219
```

### Storage path convention (canonical, post-2026-10-26 fix)
```
lab / rx / dialysis →  <FOLDER>/YYYY-QN/YYYY-MM/[HN_ชื่อ/]<uuid>.<ext>
bills               →  02_BILLS_RECEIPTS/<company>/YYYY-MM/<uuid>.<ext>
```
Built by one function, `quarterly_compare.build_document_path()`, used by all four write sites (upload, reclassify, retroactive route, staff context). Patient-folder names are sanitised against traversal.

---

## 3. Component Map

| Layer | Modules | Notes |
|---|---|---|
| **Entry / HTTP** | `server.py` (39 routes) | Monolith, 9,949 lines — primary architectural debt |
| **Interface** | `line_bot_enhancer.py` (command parser), `line_patient_assistant.py`, `liff_app.py`, `dashboard.py`, `lab_dashboard.py`, `clinical_dashboard.py` | |
| **Identity** | `patient_master_registry.py`, `patient_hn_registry.py`, `patient_identity.py` | Central registry = HN ↔ name ↔ folder |
| **Ingestion** | OCR + classification inside `server.py`; `ocr_review.py`, `ocr_safety.py` | Tesseract tha+eng; multi-pass |
| **Clinical rules (deterministic)** | `lab_rules.py`, `lab_models.py`, `lab_extractor.py`, `lab_tracker.py`, `clinical_analyzer.py`, `ckd_progression.py`, `medication_review.py`, `nephrology_drug_db.py`, `nephrology_kpi.py`, `monitoring_schedule.py` | **No LLM imports — verified clean** |
| **Safety workflow** | `alert_delivery.py` (P0-A), `physician_approval.py` (P0-B), `document_workflow.py`, `audit_trail.py` | Modules instantiated at `server.py:1839`, `1861` |
| **Storage / paths** | `GCSStorage` class in `server.py`, `quarterly_compare.py` | |
| **Reporting** | `clinical_reports.py`, `auto_report.py`, `health_check.py` | |

---

## 4. Data Architecture (GCS as database)

| Prefix | Contents |
|---|---|
| `_index/docs/{doc_id}.json` | per-document index incl. `owner_hash`, `extracted_text`, `category`, `gcs_object_path` |
| `_patient_master/` | central patient registry |
| `_hn_registry/`, `_patients/` | LINE-account → HN links |
| `_lab_results/`, `_clinical_analysis/` | extracted values, analyses, approval status |
| `_alerts/`, `_escalations/` | alert records + escalation state (P0-A) |
| `_workflow/` | approval workflow state (P0-B) |
| `_audit/` | audit trail |
| `_state/pending/`, `_state/patient_ctx/` | short-lived runtime state (cross-worker) |
| `_doc_associations/`, `_appointments/`, `_health_check/`, `_quarantine/` | supporting |

**Architectural properties and limits**

1. **No delete permission.** The service account lacks `storage.objects.delete`. "Deletion" is tombstone-by-overwrite (e.g. `_clear_pending_classification` writes an `expired` marker). Safe against accidental loss; means state objects accumulate with no lifecycle policy.
2. **No transactions.** State is read-modify-write per request. With 2 workers, concurrent writes are last-write-wins — except `physician_approval.py`, which correctly uses **compare-and-set on GCS object generation** (`:258-310`). That pattern is the right primitive and is not used elsewhere.
3. **No shared cache/queue.** `REDIS_URL` unset → `state_store: not_configured`; dedup/rate-limit fall back to per-worker memory. Rate limits are halved to compensate; dedup is **not** atomic across workers.
4. **Registry reads force a GCS reload on every call** (`master_registry.count()`, `list_all()`, `match_hn()` each set `_loaded = False`). Correct for multi-worker freshness, costly per request — a latency/cost item, not a defect.
5. **Clinical data is unstructured JSON**, not a queryable schema. Directive §7 asks whether PostgreSQL is justified; see `TECHNICAL_DEBT_REGISTER.md` TD-6.

---

## 5. Authorization Model (as-is)

Four independent environment allowlists, all keyed by salted hash of LINE user ID:

| Variable | Purpose | Checked by |
|---|---|---|
| `AUTHORIZED_USERS` | may use document search + protected clinical commands | `is_authorized_user()` |
| `ADMIN_USERS` | admin LINE commands (KPI, escalations) | membership test |
| `PHYSICIAN_USERS` | approve/reject clinical reports | `is_physician()` |
| `ADMIN_LINE_HASH` | patient-registry write commands | equality test |
| `ADMIN_SECRET` | all 30 `/admin/*` HTTP routes | header or query param |

**Structural weakness:** `AUTHORIZED_USERS` is a single flat list with no patient / staff / physician distinction — `is_authorized_user()` ≠ `is_physician()`. This is the root of finding CS-5: unapproved clinical analyses are *labelled* rather than *withheld*, so exposure depends on allowlist membership rather than an enforced gate. The LIFF web path bypasses this model entirely (SEC-1).

---

## 6. What the architecture does well (preserve)

1. **External AI is hard-locked at code level** — `EXTERNAL_AI_CLASSIFICATION_ALLOWED = False` cannot be re-enabled by environment variable, and the short-circuit sits upstream of any network call. CI greps for it.
2. **Deterministic clinical logic is cleanly separated from language generation** — no LLM import in any rules module (directive §8.3 satisfied).
3. **Fail-closed authorization at the document-index layer** — legacy documents without an owner are denied rather than shared.
4. **Atomic no-overwrite writes** — `if_generation_match=0` on upload; 412 treated as idempotent success.
5. **Compare-and-set approval versioning** with invalidation on content correction.
6. **Async webhook with immediate 200** — avoids LINE timeouts.
7. **1194 local tests**, nearly all passing.

## 7. Principal architectural weaknesses

| ID | Weakness | Consequence |
|---|---|---|
| A-1 | `server.py` is a 9,949-line monolith holding routes, storage, OCR, classification, and dispatch | Change risk; the 4-way path-construction divergence fixed on 2026-10-26 was a direct symptom |
| A-2 | **No background worker or scheduler** | Root cause of CS-1: alert retry/escalation has nowhere to run and was piggybacked onto an uncalled health route |
| A-3 | No shared state store | Dedup not atomic across workers |
| A-4 | GCS-as-database, no schema or transactions | Limits queryability, trend integrity, and concurrent correctness |
| A-5 | Flat authorization allowlist | CS-5: approval cannot be enforced by role |
| A-6 | LIFF path outside the authorization model | SEC-1 |
| A-7 | No backup/DR layer at all | `SYSTEM_INVENTORY.md` §7 |

---

## 8. Honest status summary

| Subsystem | Status |
|---|---|
| Document ingestion & filing | **IMPLEMENTED + TESTED** (locally); production behaviour UNVERIFIED this session |
| Sub-folder routing (quarterly/monthly/patient) | **IMPLEMENTED + TESTED**; previously VERIFIED IN PRODUCTION on 2026-10-25 via live dry-run |
| Lab extraction & deterministic rules | **IMPLEMENTED + TESTED** |
| CKD staging | **IMPLEMENTED but clinically incorrect** — CS-6 |
| Critical alert delivery | ❌ **BROKEN** — CS-1, CS-2, CS-3, CS-4 |
| Doctor approval | **IMPLEMENTED, weakly enforced** — CS-5 |
| LIFF patient portal | ❌ **INSECURE + NON-FUNCTIONAL** — SEC-1 |
| CI/CD | ❌ **BROKEN** — invalid YAML; test failures swallowed |
| Backup / DR | ❌ **NOT IMPLEMENTED** |
| Cloud Sync (Windows) | **UNKNOWN** — host not linked |
| GCS bucket security posture | **UNKNOWN** — no credentials |
