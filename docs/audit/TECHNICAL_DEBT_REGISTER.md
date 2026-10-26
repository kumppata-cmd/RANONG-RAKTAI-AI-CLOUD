# TECHNICAL DEBT REGISTER — RANONG-RAKTAI-AI

**Audit date:** 2026-10-26 · **Audited commit:** `b4a6619` (`main`)
**Last updated:** 2026-10-26 — Batch 1 (P0-C CI/CD Recovery) **RESOLVED & VERIFIED**

> ## Batch 1 status — TD-1, TD-2, TD-3, TD-4 → **RESOLVED (VERIFIED IN CI)**
>
> Commits: `3690503`, `1a75f30`
>
> | Item | Status | Verification evidence |
> |---|---|---|
> | TD-1 YAML invalid | ✅ RESOLVED | `yaml.safe_load` parses both workflows. Push `3690503` produced **no** 0s deploy-check failure — the "No jobs were run" symptom is gone |
> | TD-2 swallowed test gates | ✅ **RESOLVED & PROVEN** | CI run `40270798487`: `Tests — unittest suite (root): success` + `Tests — pytest suite (tests/): success`. **Gate-bite proof:** PR #16 (branch `ci/verify-gate-bites`) added one deliberately failing test → run `40270949046` = **failure**, step `Tests — unittest suite (root): failure`, later steps `skipped`. Before the fix this was green. PR closed, not merged. |
> | TD-3 env-dependent test failure | ✅ RESOLVED | Local root suite: 1148 tests, **0 failures, 0 errors**, 15 skipped |
> | TD-4 pytest undeclared | ✅ RESOLVED | `requirements-dev.txt` (pinned). CI `Tests — pytest suite (tests/): success` — the 46 previously-unrunnable tests now pass |
> | pip-audit hidden by `\|\| echo` | ⚠️ PARTIAL — now **visible**, not yet gating | See TD-14 below. Promotion to a hard gate was attempted and **reverted** |
>
> **Newly established fact:** the `tests/` pytest suite (46 tests) **passes**. This was `UNKNOWN` at audit time because pytest cannot be installed in the audit container (proxy denies PyPI with 403).
>
> ### TD-14 (NEW, P1) — pip-audit reports a finding; identity not yet known
>
> **Correction of an earlier claim in this document.** An interim revision of this register asserted that pip-audit "passes". That was **wrong**, and the error is instructive: the `success` observed in run `40270798487` was produced by `continue-on-error: true`, which reports a step as `success` even when the command exits non-zero. Promoting it to a hard gate (`1a75f30`) proved the opposite — run `40271014104` shows `pip-audit: failure`.
>
> **Second, more serious finding from that attempt:** when pip-audit failed as a hard gate, GitHub **skipped every later step in the same job** — including `ตรวจสอบ EXTERNAL_AI_CLASSIFICATION_ALLOWED = False` and `ตรวจสอบ no public GCS access`. An unrelated dependency CVE could therefore silence the two most important security controls in the pipeline. Fixed by reordering: the must-always-run greps now execute **before** pip-audit, which sits last with `continue-on-error`.
>
> **Current state:** pip-audit runs for real and its full output is in the step log; it does not block deploys and no longer masks other controls.
>
> ⚠️ **Known limitation — do not misread this step's status.** `continue-on-error: true` causes the Actions **API to report `conclusion: success`** for this step even when `pip-audit` exits non-zero. This is confirmed by comparing run `40270798487` (with `continue-on-error` → reported `success`) against run `40271014104` (same code, as a hard gate → `failure`). So `|| echo` and `continue-on-error` are **equally opaque to the API**; the only advantage of `continue-on-error` is that the command's real output remains in the step log. **The only reliable way to see the actual result is to open this step's log in the GitHub UI.** This exact masking caused one incorrect conclusion earlier in this batch, which is why it is documented here prominently.
>
> **Why not a hard gate yet:** the specific CVE is unidentified. `pip-audit` cannot be installed in the audit container (proxy denies PyPI, 403) and CI logs cannot be fetched (`results-receiver.actions.githubusercontent.com` denied, 403). Blocking all deployments on an unassessed finding would obstruct urgent clinical safety fixes, which are higher priority.
>
> **Next action:** read the step's log in the GitHub UI to identify the advisory, then either upgrade the dependency or add `--ignore-vuln <ID>` with a documented review reason, and only then remove `continue-on-error`. **Do not delete the step to make CI green** (directive §2.7).
>
> **Residual:** remote branch `ci/verify-gate-bites` could not be deleted — the sandbox proxy denies both the REST delete and `git push --delete`. Harmless (PR closed, never merged, `main` unaffected); delete it manually when convenient.

Disposition vocabulary per directive §4: **KEEP / REFACTOR / REPLACE / DEPRECATE / INVESTIGATE**.
Deprecation must be evidence-based; nothing below is marked DEPRECATE without evidence.

---

## 1. Components to KEEP (directive §4 — Preserve the Working Foundation)

| Component | Why preserve | Evidence |
|---|---|---|
| External-AI code lock | Hardcoded `False`, short-circuits before any network call, cannot be re-enabled by env, CI-enforced | `server.py:158`, `5567-5569`, `ci.yml:175` |
| Deterministic rules separated from LLM | No LLM import in any rules module — directive §8.3 requirement already met | grep clean across `lab_rules.py`, `clinical_analyzer.py`, `ckd_progression.py`, `medication_review.py` |
| Fail-closed owner isolation (document index) | Legacy docs without owner are denied, not shared | `server.py:1487`, `1509`, `1527`, `1556-1561` |
| Atomic no-overwrite GCS writes | `if_generation_match=0`; 412 treated as idempotent success | `server.py:803-818` |
| Compare-and-set approval versioning + invalidation on correction | Correct concurrency primitive; `STALE_AFTER_CORRECTION` works | `physician_approval.py:258-310`, `server.py:3894-3895` |
| LINE signature verification | HMAC-SHA256 + `compare_digest`, fail-closed, no reachable bypass | `server.py:5224-5233`, `7871-7873` |
| `USER_ID_SALT` production enforcement | Returns 503 rather than degrading hashing | `server.py:228-237`, `7848-7853` |
| Canonical document path builder | Single builder used by all four write sites; sanitised | `quarterly_compare.build_document_path()` |
| Async webhook (immediate 200) | Prevents LINE timeouts | `server.py` `_async_process_image/_file` |
| 1194-test local suite | Genuine asset; near-fully passing | measured 2026-10-26 |

---

## 2. Debt Register

### TD-1 — `deploy-check.yml` is invalid YAML (P0)
- **Evidence:** `ScannerError: mapping values are not allowed here`, line 75 col 56. Every run fails at 0s. `gh run view`: *"This run likely failed because of a workflow file issue."*
- **Root cause:** `run: pip-audit --strict --desc || echo "WARNING: vulnerabilities found"` — an unquoted YAML plain scalar containing `": "`.
- **Disposition:** **REFACTOR** (one-line fix: block scalar `run: |`).
- **Risk:** none — the workflow currently does nothing at all.
- **Rollback:** revert the single file.
- **Acceptance:** `yaml.safe_load` parses; a run appears with duration > 0s and real job results.

### TD-2 — CI swallows all test failures (P0)
- **Evidence:** `ci.yml:97` and `deploy-check.yml:65` end in `|| true`; `ci.yml:144`, `:218`, `:224` and `deploy-check.yml:75` end in `|| echo`.
- **Additional defect:** CI runs only `pytest tests/` — **none** of the 31 root-level `test_*.py` files (1194 tests) execute in CI.
- **Consequence:** a green badge attests only to `py_compile`, flake8 subset, and two greps. It is **not** evidence any test passed — exactly the failure mode directive §6 P0-C warns against.
- **Disposition:** **REFACTOR** — remove `|| true`, add `python -m unittest discover`, make pip-audit a real (or explicitly advisory-but-reported) gate.
- **Risk:** medium — enabling real gates will surface the 1 pre-existing failure (TD-3) and the 3 import errors (TD-4); both must be fixed in the same change or CI goes red.
- **Acceptance:** CI fails when a test fails (prove by temporarily breaking one test locally).

### TD-3 — Environment-dependent test asserts "not on main" (P1)
- **Evidence:** `test_staging_verification.TestRollbackReadiness.test_git_branch_not_main` — `AssertionError: 'main' == 'main'`.
- **Root cause:** test encodes a staging assumption; fails by design on `main`.
- **Disposition:** **REFACTOR** — skip unless an explicit staging env marker is set.
- **Note:** this failure **pre-dates** the 2026-10-25/26 work (verified by `git stash` on 2026-10-25).

### TD-4 — `tests/` pytest suite cannot run; `pytest` not a declared dependency (P1)
- **Evidence:** 3 import errors, `ModuleNotFoundError: No module named 'pytest'` (`tests/test_workflow.py:13`); `pytest` absent from `requirements.txt`.
- **Consequence:** 7 test modules never run locally. CI installs pytest ad-hoc (`ci.yml:86`) but discards results (TD-2) — so they effectively never gate anywhere.
- **Disposition:** **REFACTOR** — add `requirements-dev.txt` with pinned `pytest`; run both suites in CI.

### TD-5 — `server.py` monolith, 9,949 lines (P1)
- **Evidence:** holds 39 routes, `GCSStorage`, OCR, classification, command dispatch, keep-alive.
- **Demonstrated harm:** the 4-way divergence in GCS path construction (fixed 2026-10-26, commits `379b236`/`926c790`) was a direct consequence — the same logic existed in four places and drifted.
- **Disposition:** **REFACTOR, incrementally and only behind tests.** Extract in this order: (1) `GCSStorage` → `storage_gcs.py`; (2) webhook/command dispatch → `line_router.py`; (3) admin routes → `admin_api.py`.
- **Explicitly NOT:** a rewrite. Directive §2.4 forbids deleting working functionality to simplify.
- **Risk:** high if done broadly; low if one extraction per PR with the full suite green.

### TD-6 — GCS-as-database: no schema, no transactions (P2 / architectural)
- **Evidence:** 14 state prefixes of hand-rolled JSON; read-modify-write per request; last-write-wins with 2 workers (except `physician_approval.py`, which uses CAS correctly).
- **Disposition:** **INVESTIGATE** — directive §7 explicitly says *"Do not introduce a new database if existing infrastructure already meets the requirements."*
- **Decision inputs:** current volume is small (35 indexed documents observed 2026-10-25). The real pressure is queryability for trends and referential integrity for clinical timelines, not scale. **Recommendation: defer.** Do not adopt PostgreSQL during Phase 1; revisit after P0 safety work, and if adopted, start with the alert/approval workflow tables where transactional correctness genuinely matters.

### TD-7 — No background worker or scheduler (P0 — root cause of CS-1)
- **Evidence:** `check_and_retry` has one call site, inside `/health/ready` (`server.py:8374`); Render calls `/health` (`render.yaml:11`), keep-alive calls `/health/live` (`server.py:9920`); `server.py:9903` states no external cron is used.
- **Consequence:** alert retry and escalation never run.
- **Disposition:** **REPLACE** the piggyback with a real driver. Options, cheapest first: (a) call `check_and_retry` from the existing keep-alive thread (already running every 600s) — smallest safe change, no new infrastructure; (b) Render Cron Job; (c) dedicated worker process.
- **Recommendation:** (a) for Phase 1, with a bounded per-tick work limit and a lock to avoid double-processing across 2 workers.

### TD-8 — Redis unconfigured → dedup not atomic across workers (P2)
- **Evidence:** `/health` → `state_store: {"status":"not_configured","shared_across_workers":false}`; 2 Gunicorn workers.
- **Mitigation in place:** rate ceilings halved (`server.py:4775-4777`); layered dedup (event → message → sha256).
- **Disposition:** **INVESTIGATE** — quantify whether duplicate *clinical notifications* are actually reachable before adding Redis. Directive §6 P0-A requires preventing duplicate clinical notifications being mistaken for separate events.

### TD-9 — Unbounded state-object growth, no lifecycle policy (P2)
- **Evidence:** no delete permission by design; tombstone-by-overwrite; no bucket lifecycle config in repo.
- **Disposition:** **INVESTIGATE** — needs GCS access. Must be reconciled with the standing instruction *"ห้ามตั้ง GCS Auto Delete หรือ Retention Period"* (no auto-delete/retention), so any lifecycle proposal requires explicit approval and must not threaten clinical records.

### TD-10 — `ADMIN_SECRET` accepted in query strings (P2)
- **Evidence:** ~30 admin routes accept `?secret=` (e.g. `server.py:9610`) → written to access logs and browser history.
- **Disposition:** **REFACTOR** — header-only, or short-lived signed admin tokens. Coordinate with the admin dashboard, which currently relies on query params.

### TD-11 — No malware scanning of uploaded documents (P2)
- **Evidence:** `validate_file` checks extension and size; `_quarantine/` prefix exists; no scanner.
- **Directive:** §9 lists malware scanning in the required pipeline.
- **Disposition:** **INVESTIGATE** — weigh against the no-external-data-transfer constraint; a local scanner (e.g. ClamAV in the image) avoids sending documents off-host.

### TD-12 — Cross-region compute/storage (P3)
- **Evidence:** Render `region: singapore` (`render.yaml`) vs stated bucket region Bangkok (`asia-southeast3`).
- **Disposition:** **INVESTIGATE** — latency/egress only; actual bucket region UNKNOWN without GCS access. Not a defect.

### TD-13 — Windows Cloud Sync startup mechanism (P2, directive §5-A)
- **Evidence:** **none available this session** — the Windows host is not linked.
- **Disposition:** **INVESTIGATE.** Directive §5-A asks whether VBS startup should migrate to a Windows Service / Task Scheduler, but explicitly warns *"Do not migrate merely for cosmetic reasons."* No evidence exists yet that the current mechanism is failing.
- **Required access:** link the Windows host, then inspect startup entries, logs, and restart behaviour.

---

## 3. Explicitly NOT debt — do not "simplify" these

| Item | Why it must stay |
|---|---|
| No-delete GCS permission | Deliberate safety control. Tombstone-by-overwrite is the correct consequence, not a bug. |
| Registry force-reload on every read | Required for correctness across 2 workers. Optimise with a short TTL cache only if profiling justifies it. |
| Halved rate limits when store unshared | Deliberate conservative degradation. |
| Banner on unapproved analyses | Keep the banner **and** add enforcement (CS-5). Do not remove the banner. |
| 14 skipped tests | Investigate individually; skipping is sometimes correct. Never convert a skip to a pass to raise a score. |

---

## 4. Prioritised remediation sequence

Ordered by (safety impact × confidence) ÷ risk of change:

| Order | Item | Why first |
|---|---|---|
| 1 | **TD-1** CI YAML | One line; unblocks every other gate; zero risk |
| 2 | **TD-3 + TD-4** test hygiene | Prerequisite for TD-2 (otherwise enabling gates turns CI red) |
| 3 | **TD-2** real CI gates | Converts 1194 tests from invisible to enforcing |
| 4 | **TD-7 + CS-1/2/3** alert pipeline | Highest clinical impact; needs a driver first |
| 5 | **CS-6** CKD chronicity | Clinical correctness; needs clinician sign-off on the rule |
| 6 | **SEC-1** LIFF authorization | Fixes a broken auth model and a broken feature together |
| 7 | **CS-5** approval enforcement + role split | Structural; larger change |
| 8 | **P0-D** backup/DR | Requires GCS access to design or verify |
| 9 | TD-5 monolith extraction | Only once CI genuinely gates |
| 10 | TD-6, TD-8–TD-13 | Investigate with evidence before acting |

**Note:** items 4–8 are the directive's P0 set. Items 1–3 are sequenced ahead of them deliberately: without a working CI gate there is no way to prove any subsequent safety fix actually holds, and directive §5-F instructs pausing feature work while CI/CD is broken.
