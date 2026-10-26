# SECURITY GAP ANALYSIS — RANONG-RAKTAI-AI

**Audit date:** 2026-10-26
**Commit audited:** `b4a6619` (`main`)
**Method:** static code audit of the actual repository. Every finding below was independently re-verified against source by the lead auditor, not accepted from a single pass.
**Access:** no production, no GCS credentials → nothing marked `VERIFIED IN PRODUCTION`.

---

## 1. Control Summary

| # | Control | Status | Evidence | Gap |
|---|---|---|---|---|
| 1 | LINE webhook signature verification | **IMPLEMENTED** (fail-closed) | `server.py:5224-5233` HMAC-SHA256 with `hmac.compare_digest`; returns False when secret unset (`5226`); enforced on both the testing path (`7812-7816`) and production path (`7871-7873`) | None. No reachable bypass found. |
| 2 | Owner-based document isolation — LINE paths | **IMPLEMENTED + TESTED** | Fail-closed at the index layer: `1487`, `1509`, `1527`, `1556-1561` (legacy docs with no owner are denied). All four consumer call sites enforce it: `3401/3407`, `3454/3461`, `3511/3517`, `3841/3847` | Robust for LINE. **Does not extend to the LIFF web API — see §2.** |
| 3 | Owner isolation — LIFF web API | ❌ **NOT IMPLEMENTED** | `server.py:9591` takes `owner_hash` directly from `request.args` | **HIGH severity. Broken authorization model — §2.** |
| 4 | External AI lock (no PHI to external AI) | **IMPLEMENTED** | `EXTERNAL_AI_CLASSIFICATION_ALLOWED = False` hardcoded `server.py:158`; `classify_with_claude` short-circuits to local keyword classification at `5567-5569`, *before* any network call; the only Anthropic call site (`5517`) is downstream of that gate; startup warns if ever flipped (`9882`) | None. Cannot be re-enabled by environment variable. Verified by CI grep too (`ci.yml:175`). |
| 5 | Secret handling | **IMPLEMENTED** | No secret value is logged. `9829` logs only `len()` of the token. `5557` deliberately logs exception *type* only, because `str(e)` can carry base64 document content | Minor: `9602` returns `str(e)` to the HTTP client (§3.4). |
| 6 | Admin endpoint authorization | **IMPLEMENTED** | All **30 of 30** `/admin/*` routes check `ADMIN_SECRET` (header `X-Admin-Secret` or `?secret=`). No unauthenticated admin route found | Secret accepted in query string → lands in access logs (§3.5). |
| 7 | PHI in logs | **IMPLEMENTED** | No patient name, HN, or `extracted_text` in any `logger.*` call. Object names are UUIDs; `808-810` deliberately avoids original filenames | None found. |
| 8 | Rate limiting / deduplication | **IMPLEMENTED** (degrades conservatively) | `RateLimiter.check` (`4779+`) three windows, increment-then-check with rollback; halves ceilings when the store is not shared (`4775-4777`). Layered dedup (`4741-4756`): event → message → sha256 | Redis `not_configured`, so dedup is **not atomic across the 2 Gunicorn workers**. Fails conservative, not open. See §3.3. |
| 9 | `USER_ID_SALT` enforcement | **IMPLEMENTED** (fail-closed) | `228-237` sets `_USER_ID_SALT_MISSING` in production; `/webhook` returns **503** at `7848-7853`. Non-production uses a random per-restart salt (`240`) | `testing-docs` mode stores real documents under a salt that rotates each restart (§3.2). |
| 10 | Path traversal | **IMPLEMENTED** | `os.path.basename` applied at `753`, `5465`, `6073`; `_safe_folder_component()` strips `..` and separators from patient folder names; `build_legacy_path` sanitizes subfolders | None found in GCS path construction. |
| 11 | Public bucket / public URL exposure | **IMPLEMENTED** (code side) | No `make_public` or `public_url` anywhere in the repo. Only `generate_signed_url` (`2771-2785`); the download proxy issues a 300-second signed URL (`8300`) behind an HMAC token with 5-minute expiry and `compare_digest` (`9740-9750`) | Chart-token HMAC key falls back to the literal `"fallback"` if `ADMIN_SECRET` is unset (`9746`) — §3.4. Bucket-side policy **UNKNOWN**. |

---

## 2. FINDING SEC-1 (HIGH) — `/liff/api/my-labs` trusts client-supplied identity

> ### ✅ **RESOLVED (TESTED)** — commit `b61c9f2`
>
> | Change | Detail |
> |---|---|
> | Server-side token verification | `verify_liff_id_token()` posts to LINE's `/oauth2/v2.1/verify`; the signature is checked by LINE, never decoded locally |
> | `aud` re-checked locally | Without this, a token issued to **another** LIFF channel would grant access to this system's patient data |
> | `owner_hash` no longer an input | Derived from the verified `sub` via `hash_user_id()` — the client can no longer declare who it is |
> | Moved to POST | Identifiers leave the query string, so they stop landing in proxy/platform access logs |
> | Error bodies generic | `unauthorized` / `internal error`; the reason goes to the server log plus a `liff_auth_failed` audit event |
> | `dev_test_user` bypass removed | It was embedded in the page served to patients |
> | Fail-closed everywhere | Missing `LIFF_CHANNEL_ID`, LINE unreachable, `aud` mismatch, missing `sub` → rejected |
>
> **Verified with the Flask test client:**
> ```
> GET  ?owner_hash=<guess>      → 405   (the original attack vector no longer exists)
> POST {"owner_hash": <guess>}  → 400   (owner_hash is simply not accepted)
> POST {"id_token": <forged>}   → 401   unauthorized, no internal detail leaked
> ```
> 19 tests in `test_liff_auth.py`; suite now **1,224 tests, 0 failures**.
>
> **Operational requirement:** `LIFF_CHANNEL_ID` (the LINE Login channel ID) must be set in Render. Until it is, the endpoint rejects every request — fail-closed by design. `/health` now reports `liff_channel_id_set` so this is visible. Note this is **not** a regression: the endpoint returned no data for real patients before the fix either (see the functional defect below).

**Status: CONFIRMED by direct inspection of all three layers.** *(original finding preserved below for the record)*

### Evidence

Endpoint (`server.py:9577-9603`):
```python
owner_hash = request.args.get("owner_hash", "")      # 9591 — no verification
...
data = get_patient_labs_for_liff(storage=storage, owner_hash=owner_hash)   # 9596
```

Filter (`liff_app.py:93`) — equality against the value the caller supplied:
```python
if ldata.get("owner_hash") != owner_hash:
    continue
```
The same pattern gates CKD stage, latest eGFR and latest creatinine (`liff_app.py:168`).

Client (`liff_app.py:521`):
```javascript
fetch(API_BASE + '/liff/api/my-labs?owner_hash=' + encodeURIComponent(userId))
```

Verification of a LINE identity token: **absent from the entire repository.** A grep for `getIDToken`, `id_token`, `oauth2/v2.1/verify`, `verify_id_token` returns only unrelated matches in test files.

### Root cause
The endpoint treats a URL query parameter as proof of identity. The docstring asserts *"ไม่ส่งข้อมูลผู้ป่วยรายอื่น"* ("does not send other patients' data"), but the only thing preventing that is the caller's honesty about who they claim to be. This is a classic broken-authorization / IDOR defect (CWE-639): authorization is evaluated against attacker-controlled input rather than an authenticated subject.

### Exploitability — stated precisely, not inflated
`owner_hash` is **not** a raw LINE user ID. It is a salted, truncated digest (`server.py:4571-4575`):
```python
return hashlib.sha256(((user_id or "unknown") + USER_ID_SALT).encode()).hexdigest()[:32]
```
Because `USER_ID_SALT` is secret, an anonymous attacker cannot *guess* a valid `owner_hash`, so this is **not** remotely exploitable by brute force today. Severity is therefore **HIGH, not CRITICAL**. It becomes directly exploitable the moment any valid `owner_hash` leaks — e.g. via a shared link, a screenshot, an access log, a support ticket, or any admin/debug response that echoes one. The control depends entirely on a secret-that-is-not-a-credential remaining secret, which is not an authorization control.

### Simultaneous functional defect (same root cause)
The client sends the **raw** `profile.userId`, while every writer stores the **hashed** value (`server.py:7290`, `7692` pass `user_hash`). The equality check at `liff_app.py:93` therefore cannot match for any real patient, so the endpoint returns an empty result for legitimate users. **The feature is both insecure by design and non-functional.** This is also strong evidence the path was never exercised end-to-end against real data.

### Aggravating factors
1. Patient identifiers travel in a **URL query string** → persisted in Render and proxy access logs.
2. `liff_app.py:488` hardcodes a `dev_test_user` bypass when `LIFF_ID` is unset.
3. `server.py:9602` returns `str(e)` to the caller, leaking internal detail on error.

### Required fix (minimum safe change)
Verify a LIFF ID token server-side against LINE's endpoint, derive `owner_hash` with `hash_user_id()` from the **verified** `sub` claim, and never accept `owner_hash` as input. Move the call to POST so identifiers leave the query string. Remove the `dev_test_user` bypass from production builds and return a generic error body.

---

## 3. Secondary Findings

### 3.1 ✅ **RESOLVED (TESTED)** — `ca184db`

`is_valid_owner_hash()` enforces the real contract (32 lowercase hex, matching `hash_user_id()` output) at the two boundaries that accept an `owner_hash` from outside:

- **`/admin/assign-owner`** — validated **atomically across the whole batch** before a single assignment is made. This matters because the endpoint binds an owner permanently and refuses to re-bind (`already_owned`), so one malformed value would make a document unreachable forever with no path to repair through that endpoint.
- **`/admin/api/v2/hn-registry/register`** — a wrong binding here puts one patient's documents in another patient's folder.

Note the connection to SEC-1: had this guard existed from the start, the LIFF defect (client sending a raw `userId` where a salted hash was expected) would have surfaced as an immediate 400 on day one instead of going unnoticed until this audit.

*(original finding below)*

### 3.1 (MEDIUM) Identifier-contract mismatch is unguarded
Nothing in the codebase asserts that an `owner_hash` is a 32-hex-character salted digest. A one-line format guard at every boundary that accepts an `owner_hash` would have turned SEC-1's functional break into an immediate 400 error. Recommend a validator plus a test.

### 3.2 ✅ **RESOLVED (TESTED)** — `ca184db`

The salt requirement now keys on *whether documents are stored*, not on production alone: `_SALT_REQUIRED = IS_PRODUCTION or IS_TESTING_DOCS`. Plain `testing` still uses a random salt, deliberately — nothing is stored there, and forcing a salt would make the whole 1,269-test suite unrunnable.

All four combinations were executed to confirm production is untouched:

| Mode | `USER_ID_SALT` | `_USER_ID_SALT_MISSING` | Change |
|---|---|---|---|
| `production` | absent | `True` (fail-closed) | unchanged |
| `production` | set | `False` | unchanged |
| `testing-docs` | absent | `True` (fail-closed) | **new** |
| `testing` | absent | `False` (random salt) | unchanged |

*(original finding below)*

### 3.2 (MEDIUM) `testing-docs` mode orphans real documents
`server.py:238-240`: when `IS_PRODUCTION` is False the salt is random per restart, yet this mode still stores real documents. After any restart no owner can match previously stored documents — fail-closed (no leak) but **silent, permanent data inaccessibility**. Recommend requiring an explicit persistent salt whenever document storage is enabled, in any mode.

### 3.3 (MEDIUM) Dedup is not atomic across workers
`/health` reports `state_store: {"status":"not_configured","shared_across_workers":false}` with 2 Gunicorn workers. Rate limiting compensates by halving ceilings, but deduplication has no cross-worker atomicity. Clinical relevance — whether this can produce duplicate clinical notifications mistaken for separate clinical events (directive §6 P0-A) — is assessed in `CLINICAL_SAFETY_GAP_ANALYSIS.md`.

### 3.4 (LOW) Chart-token key degrades to a literal
`server.py:9746`: the HMAC key falls back to the string `"fallback"` when `ADMIN_SECRET` is unset, making chart tokens forgeable in that configuration. Impact is limited because chart payloads are de-identified (`9624-9627`). Recommend failing closed instead.

### 3.5 (LOW) `ADMIN_SECRET` accepted as a query parameter
~30 admin routes accept `?secret=` (e.g. `9610`), so the secret is written to access logs and browser history. Recommend header-only, or short-lived signed admin tokens.

---

## 4. UNKNOWN — requires access to settle

| Question | How to settle |
|---|---|
| GCS IAM policy, public-access-prevention, bucket encryption, object versioning, retention, audit logging | `gcloud storage buckets describe` + `get-iam-policy` with credentials. **Code cannot prove bucket policy** — the repo creates no public URLs, but that is not the same as the bucket being private. |
| Whether `USER_ID_SALT`, `ADMIN_SECRET`, `LINE_CHANNEL_SECRET` are actually set in Render production | `/health` `production_checklist` block (`server.py:8454-8462`), or the Render dashboard |
| Dependency CVE posture | `pip-audit` runs in CI but its result is **discarded** (`deploy-check.yml:75`, `ci.yml:144` end in `\|\| echo`). No vulnerability posture is currently established anywhere. |
| Whether the LIFF endpoint is exposed to the public internet or only inside the LINE webview | Render routing config + LIFF channel settings |

---

## 5. Threat-model coverage vs directive §13

| Threat | Current posture |
|---|---|
| Prompt injection | Low exposure — external AI is hard-disabled; classification is local keyword matching |
| **Cross-patient data leakage** | ❌ **GAP — SEC-1** (LIFF). LINE paths are sound. |
| Unauthorized tool execution | Low exposure — no LLM tool-calling layer exists yet |
| Compromised credentials | Partial — secrets never logged; but admin secret travels in URLs (§3.5) |
| Cloud misconfiguration | **UNKNOWN** — no GCS access |
| Malicious uploaded documents | Partial — `validate_file`, `_quarantine/` prefix, extension allowlist. No malware scanning (directive §9 requires it) → **NOT IMPLEMENTED** |
| Dependency compromise | **UNKNOWN** — pip-audit result discarded |
| Data poisoning / AI hallucination | Not yet applicable — no RAG/LLM generation path in use |
| Unauthorized clinical publication | See `CLINICAL_SAFETY_GAP_ANALYSIS.md` (P0-B) |
| Backup compromise | **NOT IMPLEMENTED** — no backup exists to compromise (`SYSTEM_INVENTORY.md` §7) |

---

## 6. Ranked remediation order

1. ~~**SEC-1** — LIFF authorization~~ → ✅ **RESOLVED** (`b61c9f2`)
2. **§3.2** — persistent salt whenever document storage is enabled (MEDIUM; prevents silent data loss)
3. **§3.1** — `owner_hash` format validator + test (MEDIUM; cheap defence in depth)
4. **§4 / TD-14** — pip-audit now *runs* and is visible, but still advisory because the specific CVE is unidentified (CI logs unreachable from the audit environment). See `TECHNICAL_DEBT_REGISTER.md` TD-14.
5. **§3.4, §3.5** — fail-closed chart key; header-only admin secret (LOW)

### Security status after batches 1–4

| Finding | Status |
|---|---|
| SEC-1 LIFF authorization | ✅ RESOLVED (TESTED) |
| §3.1 identifier-contract guard | ✅ RESOLVED (TESTED) — `ca184db` |
| §3.2 `testing-docs` salt rotation | ✅ RESOLVED (TESTED) — `ca184db` |
| §3.3 dedup not atomic across workers | OPEN (MEDIUM) — partially mitigated: the alert driver now takes a cross-worker `flock`, so duplicate *clinical notifications* from the retry path are prevented |
| §3.4 chart-token key fallback | OPEN (LOW) |
| §3.5 admin secret in query string | OPEN (LOW) |
| Malware scanning of uploads | NOT IMPLEMENTED |
| GCS bucket posture | **UNKNOWN** — still no credentials |
