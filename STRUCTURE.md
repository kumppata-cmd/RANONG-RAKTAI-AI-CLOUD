# RANONG-RAKTAI-AI — Code Structure Guide

## Architecture Overview

```
LINE OA ──webhook──▶ server.py ──▶ GCS (documents)
                       │              │
                       ├─ OCR ────────┤
                       ├─ Classify ───┤
                       ├─ Lab Extract─┤
                       └─ Notify ─────▶ LINE Push
```

## server.py (~14,800 lines)

Main application file. Contains Flask app, webhook handler, document processing pipeline, admin endpoints, and LINE command handlers.

See the FILE STRUCTURE MAP at the top of server.py for line ranges.

### Key Globals

| Variable | Type | Description |
|----------|------|-------------|
| `app` | Flask | Flask application |
| `storage` | StorageGCS | GCS storage backend |
| `doc_index` | DocumentIndex | Per-document JSON index on GCS |
| `hn_registry` | PatientHNRegistry | LINE user → HN mapping |
| `master_registry` | MasterPatientRegistry | Clinic-wide patient registry |
| `vendor_registry` | VendorRegistry | Bill vendor detection |
| `audit_logger` | AuditLogger | PDPA audit trail |
| `_alert_recipients` | dict | Physician/nurse LINE IDs |
| `dead_letter_queue` | DeadLetterQueue | Failed document queue |
| `system_monitor` | SystemMonitor | Health monitoring |

### Adding New Features

**New LINE command**: Add in the `handle_search_command()` function (line ~5470). Follow the existing pattern:
```python
    # ── ชื่อคำสั่ง ──
    if text_stripped == "คำสั่ง" or text_stripped.startswith("คำสั่ง "):
        try:
            # ... logic
            audit_logger.log("command_name", user_hash, {})
            return "ผลลัพธ์"
        except Exception as e:
            logger.warning("Command error: %s", type(e).__name__)
            return "ขออภัย ไม่สามารถทำได้ในขณะนี้"
```

**New admin API endpoint**: Add after existing endpoints. Follow pattern:
```python
@app.route("/admin/api/v2/new-endpoint", methods=["GET"])
def api_new_endpoint():
    err = _admin_api_auth()
    if err:
        return err
    # ... logic
    return jsonify(result)
```

**New admin HTML page**: Add after existing pages. Follow pattern:
```python
@app.route("/admin/v2/new-page")
def admin_new_page():
    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    if not admin_secret:
        return "ระบบยังไม่ได้ตั้งค่า", 500
    secret = request.args.get("secret", "")
    if not secret or not hmac.compare_digest(
        secret.encode("utf-8", "surrogateescape"),
        admin_secret.encode("utf-8"),
    ):
        return "⛔ ไม่มีสิทธิ์เข้าถึง", 401
    html = f"""<!DOCTYPE html>..."""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}
```

**New external module**: Create a new .py file. Import in server.py with try/except:
```python
try:
    from new_module import new_function
    logger.info("New module loaded")
except ImportError:
    new_function = None
    logger.warning("New module not available")
```

## External Modules

| Module | Lines | Purpose |
|--------|-------|---------|
| `quarterly_compare.py` | 1,745 | Lab/Rx/Dialysis history, comparison, drug extraction |
| `lab_extractor.py` | 1,070 | Lab result extraction with 13 regex patterns |
| `nephrology_drug_db.py` | 923 | Drug database (275 drugs) |
| `alert_delivery.py` | ~1,100 | Critical lab alerts, LINE push, retry |
| `vendor_registry.py` | ~1,400 | Bill vendor detection, config-driven |
| `patient_master_registry.py` | ~1,000 | GCS-backed patient registry |
| `patient_hn_registry.py` | ~350 | LINE user → HN mapping |
| `dead_letter.py` | 272 | Failed document queue |
| `system_monitor.py` | 342 | System health checks |
| `google_drive_sync.py` | 261 | GCS → Drive backup |
| `google_sheets_sync.py` | 434 | Data → Sheets export |
| `gmail_report.py` | 277 | Email reports |

## Security Rules

- ADMIN_SECRET: `hmac.compare_digest()` (constant-time)
- LINE: Signature verification (`HMAC-SHA256`)
- `EXTERNAL_AI_CLASSIFICATION_ALLOWED = False` (hardcoded)
- No PHI in logs
- Non-root Docker (`appuser`)
- Owner-based document isolation
- GCS private access (no public URLs)

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `LINE_CHANNEL_SECRET` | Yes | LINE Messaging API |
| `LINE_CHANNEL_ACCESS_TOKEN` | Yes | LINE Push/Reply |
| `ADMIN_SECRET` | Yes | Admin endpoint auth |
| `GCS_BUCKET_NAME` | Yes | GCS bucket name |
| `RENDER_EXTERNAL_URL` | Yes | External URL for links |
| `GOOGLE_DRIVE_ENABLED` | No | Enable Drive backup |
| `GOOGLE_SHEETS_ENABLED` | No | Enable Sheets export |
| `GMAIL_ENABLED` | No | Enable email reports |

## patient_hn_folder Format

Standard format: `{clinic_hn}_{sanitized_name}` (e.g., `56001_นายปิยะ_อะนะบุหรง`)

Legacy formats (still readable):
- `HN{clinic_hn}` (e.g., `HN56001`)
- `HN{hospital_hn}` (e.g., `HN00007567`)

The `_try_history_multi()` function and `list_by_patient_folder()` handle all formats.
New documents always use the standard format via `master_registry.get_folder_for_hn()`.
