# CLINICAL SAFETY GAP ANALYSIS — RANONG-RAKTAI-AI

**Audit date:** 2026-10-26
**Commit audited:** `b4a6619` (`main`)
**Last updated:** 2026-10-26 — P0-A alert pipeline **RESOLVED (TESTED)**

> ## Batch 2 status — CS-1, CS-2, CS-3, CS-4 → **RESOLVED (TESTED)**
>
> Commit: `27ea4ae` · New test file: `test_critical_alert_pipeline.py` (31 cases) · Suite: **1,179 tests, 0 failures, 0 errors**
>
> | ID | Fix | Verification |
> |---|---|---|
> | **CS-2** | `find_alert_id_by_prefix()` resolves the 8-char code shown to clinicians (refuses ambiguous or too-short input); LINE commands `รับทราบ`/`แก้ไขแล้ว`/`ack`/`resolve` restricted to `PHYSICIAN_USERS` or `ADMIN_USERS`; new `แจ้งเตือนค้าง` listing | Full round-trip test: code parsed out of the **actual notification text** → resolved → acknowledged → state `ACKNOWLEDGED`, `alert_acknowledged` audited |
> | **CS-1** | Dedicated driver thread (120s, `ALERT_DRIVER_INTERVAL`/`ALERT_DRIVER_ENABLED`) replacing the `/health/ready` piggyback nothing called | Retry proven to fire and deliver; backoff respected; **lock proven by call counting** — free→1, held→stays 1, released→2 |
> | **CS-3** | `ack_timeout_sec` (900s), `seconds_awaiting_ack()`, `should_escalate_unacknowledged()`, wired into driver; `_escalate(reason=…)` with distinct messages, admin-first targeting, and ack instructions included | `SENT` + overdue → state `ESCALATED`, `escalation_reason="acknowledgement_overdue"`, audited; in-window not escalated; escalation message contains no owner hash or lab value |
> | **CS-4** | `allow_simulated_delivery` defaults **False**; server passes `not IS_PRODUCTION` | No sender → `DELIVERY_FAILED` with `no_sender_configured`, and **no** `alert_sent` audit entry |
>
> ### Additional defect found and fixed during the repair
> `format_active_alerts()` displayed only `doc_id`, never the **alert code** that the acknowledgment command requires — so even after adding the command, a clinician reading the pending-alerts list would have had no code to reply with. This is the *same* class of defect as CS-2 itself (an instruction referencing something the user cannot act on) and is now covered by a test.
>
> ### Still open in P0-A
> - **Multi-recipient redundancy** (was noted P2): `attempt_delivery` still `break`s after the first successful recipient. Unchanged in this batch.
> - **Production verification: BLOCKED.** Everything above is verified by test, not against the live service. The highest-value outstanding check remains reading the live `_alerts/` prefix to see whether real critical alerts were already lost while the pipeline was broken — this needs GCS credentials.
**Method:** static code audit. **Every P0 finding below was independently re-verified against source by the lead auditor.**
**Access:** no production, no GCS credentials → no finding is marked `VERIFIED IN PRODUCTION`.

Both safety modules are instantiated at startup (`server.py:1839`, `1861`), so neither is dead on arrival — but several critical sub-paths are provably unreachable.

---

## 1. P0-A — Critical Alert Delivery

Directive §6 P0-A requires ten capabilities. Measured against code:

| # | Required capability | Status | Evidence | Gap |
|---|---|---|---|---|
| 1 | Alert generation | **IMPLEMENTED** | `server.py:6455` `create_alert` on `level=='critical'` lab findings | — |
| 2 | Durable alert queue | **IMPLEMENTED** | GCS prefix `_alerts/` (`alert_delivery.py:176`), `_save_alert:198` | — |
| 3 | **Delivery worker** | ❌ **NOT IMPLEMENTED** | See CS-1 below | **Retries never execute** |
| 4 | Delivery status tracking | **IMPLEMENTED** | `AlertState` enum `alert_delivery.py:42-51`; event log `add_event:138` | — |
| 5 | Retry w/ bounded exponential backoff | **IMPLEMENTED but never driven** | `get_retry_delay:166-169` (base 30s, cap 3600s); `should_retry:154` bounded by `max_delivery_attempts` | Logic correct, never invoked |
| 6 | Dead-letter handling | **PARTIAL, never driven** | Terminal `ESCALATED` state `:160-164`, `_escalate:685` | No separate DLQ; escalation not driven |
| 7 | **Recipient acknowledgment** | ❌ **NOT IMPLEMENTED** | See CS-2 below | **Clinician cannot acknowledge by any route** |
| 8 | **Escalation on overdue ack** | ❌ **NOT IMPLEMENTED** | See CS-3 below | Delivered-but-ignored alert never escalates |
| 9 | Audit logging | **IMPLEMENTED** | `alert_sent:370`, `alert_escalated:388`, `alert_acknowledged:423` | — |
| 10 | Monitoring of delivery failures | **PARTIAL** | `get_active_alerts:463` via admin LINE command (`server.py:4528`) | Pull-only, human-initiated; no automated failure alerting |

### Credit where due: the API-success-vs-receipt distinction is modelled correctly
A successful LINE API call sets state `SENT`, **not** acknowledged (`alert_delivery.py:364-366`), and the module header explicitly states that API acceptance ≠ clinician acknowledgement (`alert_delivery.py:10`). The *design intent* satisfies directive §6 P0-A. The failure is that the acknowledgment half was never wired up, so receipt can never in fact be confirmed.

---

### CS-1 (P0, CONFIRMED) — No delivery worker exists; retries and escalations never run

**Verified by exhaustive call-site search.** `check_and_retry` has exactly **one** non-test call site in the entire repository:

```
server.py:8374   alert_retry_manager.check_and_retry(      ← inside the /health/ready route
```

But nothing in the deployed configuration ever requests `/health/ready`:

| Caller | Path actually used | Evidence |
|---|---|---|
| Render platform health check | `/health` | `render.yaml:11` `healthCheckPath: /health` |
| Internal keep-alive thread | `/health/live` | `server.py:9920` |
| External cron / scheduler | **none exists** | `server.py:9903` comment: *"ไม่ใช้ external cron"* |

**Root cause:** the retry driver was piggybacked onto a readiness probe that no configured component calls — a path mismatch (`/health/ready` vs `/health` and `/health/live`).

**Clinical consequence:** a critical lab alert whose first delivery attempt fails stays in `DELIVERY_FAILED` **forever**. It is never retried and never escalated. The bounded-backoff retry logic and the escalation logic are both correct and both unreachable.

---

### CS-2 (P0, CONFIRMED) — Acknowledgment is unreachable dead code

**Verified by exhaustive call-site search** across all `.py` files excluding `test*`:

```
alert_delivery.py:402   def acknowledge_alert(...)    ← definition only
alert_delivery.py:435   def resolve_alert(...)        ← definition only
```

Zero call sites. There is no `CommandType` for acknowledgment, no LINE command, and no HTTP route that reaches either method.

**Clinical consequence:** there is **no mechanism by which a clinician can acknowledge a critical result.** Consequently the system can never distinguish "a doctor has seen this critical potassium" from "this alert was pushed into the void". The entire acknowledgment/escalation safety model is inert. For a kidney clinic handling critical hyperkalaemia, this is the single most consequential gap in the system.

---

### CS-3 (P0, CONFIRMED) — Escalation keys only on delivery failure, never on acknowledgment timeout

`should_escalate` (`alert_delivery.py:160`) triggers only from the `DELIVERY_FAILED` state, and `check_and_retry` (`:653`, `:672`) contains no acknowledgment-deadline branch.

**Clinical consequence:** an alert that is *successfully delivered* and then ignored is indistinguishable from one that was handled. No escalation window, no on-call fallback. Directive §6 P0-A explicitly requires "Escalation when acknowledgment is overdue".

---

### CS-4 (P1, CONFIRMED) — Silent fail-open: alerts marked `SENT` with nothing transmitted

Verified at `alert_delivery.py:350-362`:

```python
                    success = send_push_fn(recipient_id, alert.notification_text)
                    if success:
                        delivery_success = True
                        break
                ...
        else:
            # Testing mode — simulate delivery
            delivery_success = True        # ← reached whenever send_push_fn is falsy
```

The guard is merely `if send_push_fn:`; it is **not** gated on `IS_PRODUCTION`. `line_push_fn` remains `None` whenever `LINE_CHANNEL_ACCESS_TOKEN` is unset, and `server.py:1867-1871` only logs a warning in that case.

**Clinical consequence:** with a missing or revoked LINE token in production, every critical alert is recorded as `SENT`, audit-logged as `alert_sent`, and **nothing is transmitted to anyone**. This is worse than the failure mode directive §6 P0-A warns about: it equates *no submission at all* with confirmed delivery. The comment calling this "Testing mode" is misleading — the condition does not test for testing mode.

**Related (P2):** `alert_delivery.py:355` `break`s after the first successful recipient, so there is no multi-recipient redundancy for critical alerts.

---

## 2. P0-B — Doctor Approval Workflow

| # | Required capability | Status | Evidence | Gap |
|---|---|---|---|---|
| 1 | Pending review queue | **IMPLEMENTED** | `list_pending_reports:352` | — |
| 2 | Authenticated clinician identity | **IMPLEMENTED (weak)** | env allowlist `PHYSICIAN_USERS` (`server.py:1825-1829`); `is_physician:242` | Hashed-ID allowlist; no real authentication or credential binding |
| 3 | Role-based permissions | **IMPLEMENTED** | `PhysicianRole:37`; `is_admin:246`; `_validate_physician:327` | — |
| 4 | Reviewable source evidence | **IMPLEMENTED** | `get_report_for_review:401` | — |
| 5 | Approve / reject / request revision | **PARTIAL** | `approve_report:451`, `reject_report:545`, `correct_and_approve:618` | No distinct "request revision" state |
| 6 | Version-specific approval | **IMPLEMENTED (strong)** | `_check_version:343`, `StaleVersionError:191`, compare-and-set via GCS generation `:258-310` | — |
| 7 | Approval timestamp | **IMPLEMENTED** | `:418` | — |
| 8 | Immutable decision history | **IMPLEMENTED** | `ReviewHistoryEntry:54-72`, `add_history:171` | — |
| 9 | **Prevention of unauthorized publication** | ⚠️ **PARTIAL** | See CS-5 | Approval is advisory text, not an access gate |
| 10 | Invalidation on material content change | **IMPLEMENTED (strong)** | `STALE_AFTER_CORRECTION` (`server.py:3044`, `3080`, `3894-3895`) | — |

The version-control half of this workflow (items 6, 8, 10) is genuinely well built — compare-and-set on GCS object generation is the right primitive, and approval invalidation on correction is implemented properly.

---

### CS-5 (P1) — Approval status is advisory, not enforcing

**Answer to the directive's key question — "can an unapproved high-risk clinical recommendation reach a patient today, and by which code path?"**

**Not a patient today — but not because the approval gate stops it.**

`CommandType.CLINICAL_ANALYSIS` is in `PROTECTED_COMMANDS` (`server.py:3167`); a user outside `AUTHORIZED_USERS` is denied and audited (`:3214-3219`). So a patient who is not on the allowlist cannot reach clinical analysis at all.

However, for **anyone on that allowlist**, the path
`handle_search_command → CLINICAL_ANALYSIS → server.py:3908-3923`
returns the **complete** `summary_text_th` of a `PENDING_VERIFICATION` (unapproved) analysis, prefixed only with a banner: *"⏳ ผลวิเคราะห์ยังไม่ผ่านการตรวจสอบโดยแพทย์"*.

The content is not withheld — only labelled. And `AUTHORIZED_USERS` is a single flat allowlist that makes **no patient / staff / physician distinction**: `is_authorized_user()` is not `is_physician()`. Therefore the only thing standing between unapproved AI clinical content and a non-clinician reader is an environment-variable membership decision. **Adding any patient to `AUTHORIZED_USERS` would immediately expose unapproved analyses to them**, with no code change and no warning.

Directive §6 P0-B requires that "No high-risk patient-facing clinical recommendation may bypass the required approval gate." The current design satisfies this by *configuration*, not by *enforcement*. Recommended fix: withhold the analysis body (not merely banner it) until `APPROVED`, and split the flat allowlist into distinct patient / staff / physician roles.

---

## 3. Deterministic Rules vs LLM Language — IMPLEMENTED (requirement met)

Directive §8.3 requires deterministic clinical calculation be kept separate from LLM-generated language. **This requirement is met.**

- No LLM or external-AI import in `clinical_analyzer.py`, `lab_rules.py`, `ckd_progression.py`, `medication_review.py`, or `alert_delivery.py` (grep clean).
- External AI is hard-locked: `EXTERNAL_AI_CLASSIFICATION_ALLOWED = False` (`server.py:158`), and `classify_with_claude` returns local keyword classification *before* any network call (`:5567-5569`).
- **No code path was found by which an LLM can alter a rule result.**

This is a genuine strength and should be preserved as-is (see `TECHNICAL_DEBT_REGISTER.md` — KEEP).

---

## 4. CS-6 (P0 clinical correctness, CONFIRMED) — CKD staged from a single eGFR, without chronicity

The directive flags this specifically. **Confirmed by direct inspection** (`clinical_analyzer.py:151-171`):

```python
def classify_ckd_stage(egfr: float) -> CKDStage:
    """จัดระดับ CKD จาก eGFR ตาม KDIGO 2012."""
    if egfr >= 90:   return CKDStage.G1
    elif egfr >= 60: return CKDStage.G2
    ...
```

The function takes **one** eGFR value and returns a CKD stage. There is no ≥3-month persistence requirement and no albuminuria or other kidney-damage marker.

**Why this is clinically wrong:** KDIGO 2012 defines CKD as abnormality of kidney structure or function present for **>3 months**. G1 (eGFR ≥90) and G2 (eGFR 60–89) are **not CKD at all** in the absence of a marker of kidney damage. As written, this function labels a person with entirely normal kidney function as "CKD G1", and a single transient dip (e.g. during dehydration or AKI) as a chronic stage.

The staged result reaches user-facing output at `server.py:3902`. Note that `ckd_progression.py:89` `MIN_SPAN_DAYS = 90` exists but governs **trend reliability only** — it does not gate staging.

**Recommended fix:** require ≥2 eGFR values ≥90 days apart plus a damage marker (ACR/albuminuria) before assigning any stage; where those are unavailable, report the eGFR category explicitly as "eGFR category, not a CKD diagnosis" and flag the missing inputs rather than inferring chronicity. The docstring's claim of KDIGO 2012 conformance should be corrected until the criteria are actually implemented.

---

## 5. Ranked Clinical Safety Blockers

| Rank | ID | Severity | Finding | Root cause |
|---|---|---|---|---|
| 1 | **CS-2** | **P0** | Clinician cannot acknowledge a critical alert by any route | Service methods implemented, never wired to a command or route |
| 2 | **CS-1** | **P0** | Retries/escalations never execute | Driver piggybacked on `/health/ready`, which nothing calls |
| 3 | **CS-3** | **P0** | Delivered-but-ignored alert never escalates | Escalation keyed only on `DELIVERY_FAILED` |
| 4 | **CS-6** | **P0** | CKD stage assigned from one isolated eGFR | Chronicity + damage-marker criteria not implemented |
| 5 | **CS-4** | **P1** | Alerts marked `SENT` with nothing transmitted | Fail-open `else` branch not gated on production |
| 6 | **CS-5** | **P1** | Approval advisory, not enforcing; flat allowlist | Content bannered rather than withheld; no role separation |

**CS-1 through CS-4 together mean the critical-alert pipeline cannot be considered functional.** Each of the four is individually sufficient to prevent a critical result from reliably reaching a human. Per directive §14, *"Any unresolved P0 clinical safety issue blocks clinical production release."*

---

## 6. UNKNOWN — requires access to settle

| Question | How to settle |
|---|---|
| Are `LINE_CHANNEL_ACCESS_TOKEN`, `PHYSICIAN_USERS`, `ALERT_RECIPIENTS*` actually populated in Render? | Render dashboard, or `/health` `production_checklist` |
| Does any external monitor poll `/health/ready` (which would partly mitigate CS-1)? | Render config / any external uptime service |
| Are there alerts currently stuck in `SENT` or `DELIVERY_FAILED` in the live `_alerts/` prefix? | GCS read access — **this is the highest-value single query** once credentials exist, because it would show whether real critical alerts have already been lost |

Until the third question is answered, the operational impact of CS-1–CS-4 on patients already treated is **UNKNOWN and should be assumed non-zero.**
