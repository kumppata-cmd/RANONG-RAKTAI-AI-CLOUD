# SYSTEM INVENTORY — RANONG-RAKTAI-AI

**Audit date:** 2026-10-26
**Audit type:** Read-only, Mission 1 (System Discovery)
**Repo commit audited:** `b4a6619` on branch `main`
**Auditor access:** Local repo clone + GitHub API (`gh`). **No production access, no GCS credentials** — see "Access Limitations".

Status vocabulary used throughout: `IMPLEMENTED` / `TESTED` / `VERIFIED IN STAGING` / `VERIFIED IN PRODUCTION` / `BLOCKED` / `NOT IMPLEMENTED` / `UNKNOWN`.

No item in this document is marked `VERIFIED IN PRODUCTION`, because production could not be reached during this audit.

---

## 1. Access Limitations (declared first, per directive §17)

| Target | Access | Consequence for this audit |
|---|---|---|
| Git repository | ✅ Available (local clone, commit `b4a6619`) | Code claims verifiable |
| GitHub Actions history | ✅ Available via `gh run list` | CI/CD claims verifiable |
| Render production (`ranong-raktai-ai-cloud.onrender.com`) | ❌ **BLOCKED** — agent proxy returns 403 (`connect_rejected`); browser pane MCP disconnected; `device_bash` reports device not connected | No runtime/production verification possible |
| GCS bucket `ranong-raktai-ai-prod-2026` | ❌ **BLOCKED** — no credentialed account in container (`gcloud auth list` → none) | IAM, encryption, versioning, retention, public-access-prevention **UNKNOWN** |
| Windows host (Cloud Sync, VBS/Task Scheduler) | ❌ **BLOCKED** — not linked this session | Cloud Sync `v2.2-hardened` claims **UNVERIFIED** |

**Consequence:** baseline claims in directive §1.2 (70/70 tests, 16 consecutive syncs) and §1.1 (bucket region/class/public access) could **not** be confirmed or refuted. They remain `UNKNOWN`, not accepted.

---

## 2. Repository Composition

| Metric | Value |
|---|---|
| Python files (root) | 63 |
| Non-test modules (root) | 32 |
| Test files (root, `test_*.py`) | 31 |
| Separate pytest suite | `tests/` (7 test modules + `conftest.py`) |
| Largest module | `server.py` — 9,949 lines |
| Flask routes | 39 total, of which **30 are `/admin/*`** |
| Deployment | Docker on Render (`render.yaml`, `Dockerfile`) |

### 2.1 Non-test modules (32)

**Core / entry point**
- `server.py` (9,949) — Flask app, LINE webhook, all 39 routes, GCS storage class, OCR pipeline, command dispatch. **Monolith; primary technical debt item.**

**Patient identity & registry**
- `patient_master_registry.py` (973) — central patient registry (HN ↔ name ↔ folder), HN extraction from OCR
- `patient_hn_registry.py` — LINE-account→HN linking, quarter helpers, `resolve_patient_folder()`
- `patient_identity.py` — identity/ownership helpers

**Clinical**
- `clinical_analyzer.py` (1,133), `clinical_reports.py` (827), `clinical_dashboard.py`
- `ckd_progression.py` (808), `lab_rules.py`, `lab_models.py`, `lab_extractor.py`, `lab_tracker.py` (820), `lab_alert.py`, `lab_dashboard.py` (932)
- `medication_review.py` (1,177), `nephrology_drug_db.py` (923), `nephrology_kpi.py`, `monitoring_schedule.py`

**Workflow & safety-critical (P0)**
- `alert_delivery.py` (772) — critical alert delivery (P0-A)
- `physician_approval.py` (877) — doctor approval workflow (P0-B)
- `document_workflow.py` (1,147), `audit_trail.py`, `ocr_review.py`, `ocr_safety.py`

**Interface**
- `line_bot_enhancer.py` (771) — command parser (`parse_command`, `CommandType`)
- `line_patient_assistant.py` (903), `dashboard.py` (1,163), `liff_app.py`, `auto_report.py`, `health_check.py`
- `quarterly_compare.py` (870) — GCS path builders + lab/rx comparison

**Utility**
- `create_test_pdf.py`

---

## 3. Test Inventory — measured, not reported

Executed `python3 -m unittest discover -s . -p "test_*.py"` on commit `b4a6619`:

```
Ran 1194 tests in 4.706s
FAILED (failures=1, errors=3, skipped=14)
```

| Result | Count | Detail |
|---|---|---|
| Passed | ~1176 | — |
| **Failed** | 1 | `test_staging_verification.TestRollbackReadiness.test_git_branch_not_main` — asserts the checkout is *not* on `main`. Environment-dependent test, not a product defect. Status: **test design defect**. |
| **Errors** | 3 | Import failures in `tests/` subdirectory: `ModuleNotFoundError: No module named 'pytest'` (e.g. `tests/test_workflow.py:13`). pytest is **not** in `requirements.txt`. |
| Skipped | 14 | Matches the "14 BLOCKED" figure in directive §1.4. Skips are **not** passes. |

**Finding INV-1:** the `tests/` pytest suite cannot run in this environment at all (dependency absent). Any historical aggregate pass count that included it is unreliable.

---

## 4. CI/CD Inventory — verified against real run history

Two workflows exist: `.github/workflows/ci.yml` and `.github/workflows/deploy-check.yml`.

### 4.1 Real run history (`gh run list`, last 15 runs)

| Workflow | Result pattern | Duration |
|---|---|---|
| `CI — RANONG RAKTAI AI` | success since 2026-10-25 17:10; **failure** on runs before that (16:49, 16:08) | ~50–63s |
| `deploy-check.yml` | ❌ **failure on every single run** | **0s** |

### 4.2 Root cause of "No jobs were run" — **CONFIRMED**

`deploy-check.yml` is **invalid YAML** and cannot be loaded by GitHub, so zero jobs are scheduled:

```
.github/workflows/deploy-check.yml
  ScannerError: mapping values are not allowed here
  line 75, column 56
```

Line 75:
```yaml
run: pip-audit --strict --desc || echo "WARNING: vulnerabilities found"
```
The value is an unquoted YAML plain scalar containing `": "`, which YAML interprets as a mapping separator. `gh run view` concurs: *"This run likely failed because of a workflow file issue."*

Status: **CONFIRMED DEFECT**, reproduced deterministically. This is directive item **P0-C**.

### 4.3 Finding CI-1 (severity: critical) — green CI is not evidence of passing tests

`ci.yml` job `test` (line 88–97):
```yaml
run: |
  python -m pytest tests/ -v --tb=short || true
```

Two independent defects:
1. **`|| true` discards all test failures** — the step cannot fail.
2. It runs **only `tests/`**, never the 31 root-level `test_*.py` files — i.e. **none of the 1194 measured unittest tests run in CI.**

Therefore a green `CI — RANONG RAKTAI AI` badge currently attests only to: `py_compile` success, flake8 (`E9,F63,F7,F82`) clean, a secrets grep, and an `EXTERNAL_AI_CLASSIFICATION_ALLOWED = False` grep. **It does not attest that any test passed.** This is precisely the failure mode directive §6 P0-C warns against.

Other swallowed gates:

| File:line | Swallowed check |
|---|---|
| `ci.yml:97` | pytest (`\|\| true`) |
| `ci.yml:144` | pip-audit (`\|\| echo`) |
| `ci.yml:218` | pip list --outdated (`\|\| true`) |
| `ci.yml:224` | pip check (`\|\| echo`) |
| `deploy-check.yml:65` | pytest (`\|\| true`) |
| `deploy-check.yml:75` | pip-audit (`\|\| echo`) — also the YAML break |

Gates that **do** fail correctly: `ci.yml:99-117` compile-all-modules (`sys.exit(1)`), and the `EXTERNAL_AI_CLASSIFICATION_ALLOWED` grep (`exit 1`).

---

## 5. Deployment Configuration

| Item | Value | Evidence |
|---|---|---|
| Platform | Render, Docker runtime | `render.yaml` |
| Region | `singapore` | `render.yaml` |
| Plan declared | `starter` | `render.yaml` |
| Health check path | `/health` | `render.yaml` |
| Server command | `gunicorn server:app --workers 2 --timeout 120` | `Dockerfile` |
| Exposed port | 10000 (`${PORT:-10000}`) | `Dockerfile` |
| Health endpoints | `/health`, `/health/live`, `/health/ready` | `server.py` |
| Keep-alive | self-ping, 600s, env-toggleable | `server.py:9907-9912` |

**Note GEO-1:** Render region is `singapore` while the directive states the GCS bucket is `asia-southeast3` (Bangkok). Cross-region egress/latency — not a defect, flagged for architecture review. Actual bucket region **UNKNOWN** (no GCS access).

### 5.1 Runtime dependencies (`requirements.txt`)

flask 3.1.0, gunicorn 23.0.0, requests 2.32.3, pypdf 5.1.0, PyMuPDF 1.25.1, pytesseract 0.3.13, Pillow 11.1.0, google-cloud-storage 2.18.2, google-auth 2.35.0, matplotlib 3.9.3.

All pinned to exact versions (good). **`pytest` is absent** → cause of the 3 import errors in §3. Vulnerability status **UNKNOWN** (pip-audit result is swallowed in CI and not run here).

### 5.2 Configuration surface — 27 environment variables

`ADMIN_LINE_HASH`, `ADMIN_SECRET`, `ADMIN_USERS`, `ANTHROPIC_API_KEY`, `APP_ENV`, `AUTHORIZED_USERS`, `BASE_DIR`, `DEDUP_CONTENT_TTL`, `DEDUP_TTL_SECONDS`, `ENABLE_AI_CLASSIFY`, `GCS_BUCKET_NAME`, `GOOGLE_APPLICATION_CREDENTIALS`, `KEEPALIVE_ENABLED`, `KEEPALIVE_INTERVAL`, `LINE_CHANNEL_ACCESS_TOKEN`, `LINE_CHANNEL_SECRET`, `MAX_OCR_PAGES`, `OCR_DPI`, `OCR_MAX_CONCURRENT`, `PHYSICIAN_USERS`, `PORT`, `RATE_LIMIT_PER_DAY`, `RATE_LIMIT_PER_HOUR`, `RATE_LIMIT_PER_MINUTE`, `REDIS_URL`, `RENDER_EXTERNAL_URL`, `STORAGE_BACKEND`, `STORAGE_BUCKET`, `USER_ID_SALT`.

Authorization is configured by **environment allowlist** (`AUTHORIZED_USERS`, `ADMIN_USERS`, `PHYSICIAN_USERS`, `ADMIN_LINE_HASH`). Actual production values **UNKNOWN**; see `SECURITY_GAP_ANALYSIS.md`.

---

## 6. Data Architecture (as inferred from code)

No relational database. All state is GCS objects. Prefixes in use:

| Prefix | Purpose |
|---|---|
| `_index/` | per-document index (`_index/docs/{doc_id}.json`) |
| `_patient_master/` | central patient registry |
| `_patients/`, `_hn_registry/` | LINE-account→HN linking |
| `_lab_results/`, `_clinical_analysis/` | extracted lab + analysis output |
| `_alerts/`, `_escalations/` | alert + escalation records (P0-A) |
| `_workflow/` | approval/workflow state (P0-B) |
| `_audit/` | audit trail |
| `_state/` | runtime state (`_state/pending/`, `_state/patient_ctx/`) |
| `_doc_associations/`, `_appointments/`, `_health_check/`, `_quarantine/` | supporting state |

Document folders: `02_BILLS_RECEIPTS`, `03_PRESCRIPTIONS`, `04_LAB_RESULTS`, `05_DIALYSIS_RECORDS`, `06_AI_REVIEW`.

**Finding DATA-1:** The deployment has **no `storage.objects.delete` permission** (established in prior sessions and visible in code design — `_clear_pending_classification` overwrites with an expired marker rather than deleting, `server.py`). State "deletion" is therefore tombstone-by-overwrite. This is a deliberate safety choice but means state objects accumulate indefinitely with no lifecycle policy (see §7).

**Finding DATA-2:** State is stored as individual JSON objects read/written per request, with no transactions. Concurrent writes from 2 Gunicorn workers are last-write-wins. Relevant to directive §7 (Trusted Clinical Data Platform).

---

## 7. Backup & Disaster Recovery (P0-D)

**Status: NOT IMPLEMENTED.**

Searched the entire repo for backup/restore/versioning/retention logic and infrastructure-as-code:
- No backup script, job, or endpoint.
- No bucket versioning, lifecycle, or retention configuration in the repo.
- No restore procedure or documented RPO/RTO.
- The only matches for "backup" are flake8 `--exclude=*.backup*` patterns in both workflow files.

Actual bucket-side protection (object versioning, soft delete, retention policy) is **UNKNOWN** — it may be configured in GCP outside this repo, but no GCS access was available to confirm. Per directive §6 P0-D, *"A successful synchronization is not proof of recoverability."*

---

## 8. Monitoring & Observability

| Capability | Status | Evidence |
|---|---|---|
| Liveness/readiness endpoints | IMPLEMENTED | `/health`, `/health/live`, `/health/ready` |
| Rich health payload (GCS state, checklist, counts) | IMPLEMENTED | `/health` response observed 2026-10-25 |
| Self keep-alive ping | IMPLEMENTED | `server.py:9907-9912` |
| Structured audit trail | IMPLEMENTED (module exists) | `audit_trail.py`, `_audit/` prefix |
| External uptime monitoring / alerting on failure | NOT IMPLEMENTED (none found in repo) | — |
| Log aggregation / retention | UNKNOWN | depends on Render config, not in repo |
| Shared state store (Redis) | **NOT CONFIGURED** | `/health` reported `state_store: {"status":"not_configured","shared_across_workers":false}` |

**Finding MON-1:** With Redis unconfigured, dedup and rate-limiting fall back to per-worker in-memory state across 2 Gunicorn workers. Code compensates by halving rate limits (`effective_halved: true` observed in `/health`), but **deduplication is not atomic across workers**. Severity depends on whether duplicate clinical notifications can be produced — see `CLINICAL_SAFETY_GAP_ANALYSIS.md`.

---

## 9. Cross-reference to directive §1.4 reported problems

| Reported problem | Audit result |
|---|---|
| CI/CD "No jobs were run" | ✅ **CONFIRMED & ROOT-CAUSED** — invalid YAML, `deploy-check.yml:75` |
| Lint/Syntax Check failed in some runs | ✅ **CONFIRMED** — `CI` runs failed before 2026-10-25 17:10; now passing |
| Critical alerts may not complete delivery | → see `CLINICAL_SAFETY_GAP_ANALYSIS.md` (P0-A) |
| Doctor approval needs end-to-end verification | → see `CLINICAL_SAFETY_GAP_ANALYSIS.md` (P0-B) |
| Backup/restore readiness unverified | ✅ **WORSE THAN REPORTED** — NOT IMPLEMENTED in repo (§7) |
| Staging 994 PASS / 14 BLOCKED / 0 FAIL, Conditional GO | ⚠️ **NOT REPRODUCIBLE AS STATED.** Measured today: 1194 tests, 1 failure, 3 errors, 14 skipped. The 14 skips align with "14 BLOCKED". Skips and `\|\| true` CI steps must not be counted as acceptance. |

---

## 10. Immediate conclusions

1. **CI/CD is the top blocker** and is cheap to fix with high leverage: one YAML syntax error disables the entire deploy-check workflow, and `|| true` makes the passing workflow evidentially worthless. Until fixed, *no* deployment gate exists.
2. **P0-D backup/DR is absent**, not merely unverified.
3. **1194 tests exist and nearly all pass locally** — a genuine asset, currently invisible to CI.
4. **Production/GCS verification is blocked**; all runtime claims stay `UNKNOWN` until access is restored.
