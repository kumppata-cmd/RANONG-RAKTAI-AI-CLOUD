"""Google Sheets sync — ส่งออกข้อมูลสรุปผู้ป่วยไป Sheets

ใช้ Service Account ตัวเดียวกับ GCS (ADC)
ต้องเปิด Sheets API ใน Google Cloud Console
ต้อง share Spreadsheet กับ Service Account email

ENV VARS:
- GOOGLE_SHEETS_ID: Spreadsheet ID ที่จะเขียน
- GOOGLE_SHEETS_ENABLED: "true" เพื่อเปิดใช้งาน

ไม่ log ข้อมูลผู้ป่วย (PDPA)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

SHEETS_ENABLED = os.getenv("GOOGLE_SHEETS_ENABLED", "").strip().lower() == "true"
SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID", "").strip()

_sheets_service = None


def _get_sheets_service():
    """สร้าง Sheets API service (lazy init, ใช้ ADC)."""
    global _sheets_service
    if _sheets_service is not None:
        return _sheets_service
    try:
        import google.auth
        from googleapiclient.discovery import build

        credentials, _project = google.auth.default(
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        _sheets_service = build(
            "sheets", "v4", credentials=credentials, cache_discovery=False
        )
        logger.info("Google Sheets service initialized")
        return _sheets_service
    except Exception as e:
        logger.warning("Google Sheets init failed: %s", type(e).__name__)
        return None


def is_available() -> bool:
    """ตรวจว่า Sheets พร้อมใช้งาน."""
    return SHEETS_ENABLED and bool(SHEETS_ID)


def get_status() -> dict:
    """สถานะการเชื่อมต่อ Sheets."""
    status: dict = {
        "enabled": SHEETS_ENABLED,
        "spreadsheet_id": (SHEETS_ID[:8] + "...") if SHEETS_ID else "",
        "connected": False,
    }
    if not is_available():
        return status
    service = _get_sheets_service()
    if not service:
        return status
    try:
        info = service.spreadsheets().get(
            spreadsheetId=SHEETS_ID, fields="properties.title"
        ).execute()
        status["connected"] = True
        status["title"] = info.get("properties", {}).get("title", "")
    except Exception as e:
        status["error"] = type(e).__name__
    return status


# ─── Sheet helpers ───────────────────────────────────────────


def _ensure_sheet(service, sheet_name: str) -> bool:
    """ตรวจว่า sheet มีอยู่ ถ้าไม่มีให้สร้าง."""
    try:
        info = service.spreadsheets().get(
            spreadsheetId=SHEETS_ID
        ).execute()
        existing = [
            s["properties"]["title"]
            for s in info.get("sheets", [])
        ]
        if sheet_name in existing:
            return True

        # สร้าง sheet ใหม่
        body = {
            "requests": [
                {
                    "addSheet": {
                        "properties": {"title": sheet_name}
                    }
                }
            ]
        }
        service.spreadsheets().batchUpdate(
            spreadsheetId=SHEETS_ID, body=body
        ).execute()
        logger.info("Sheets: created sheet '%s'", sheet_name)
        return True
    except Exception as e:
        logger.warning(
            "Sheets ensure sheet failed: %s", type(e).__name__
        )
        return False


def _write_rows(
    service,
    sheet_name: str,
    rows: list[list[Any]],
    start_cell: str = "A1",
    clear_first: bool = True,
) -> dict:
    """เขียนข้อมูลลง sheet.

    Args:
        service: Sheets API service
        sheet_name: ชื่อ sheet
        rows: ข้อมูลที่จะเขียน [[header], [row1], ...]
        start_cell: cell เริ่มต้น
        clear_first: ล้างข้อมูลเดิมก่อนเขียน
    """
    try:
        range_name = f"{sheet_name}!{start_cell}"

        if clear_first:
            service.spreadsheets().values().clear(
                spreadsheetId=SHEETS_ID,
                range=f"{sheet_name}",
                body={},
            ).execute()

        body = {"values": rows}
        result = service.spreadsheets().values().update(
            spreadsheetId=SHEETS_ID,
            range=range_name,
            valueInputOption="USER_ENTERED",
            body=body,
        ).execute()

        return {
            "success": True,
            "updated_cells": result.get("updatedCells", 0),
            "updated_rows": result.get("updatedRows", 0),
        }
    except Exception as e:
        logger.warning("Sheets write failed: %s", type(e).__name__)
        return {"error": f"เขียนข้อมูลไม่สำเร็จ: {type(e).__name__}"}


def _append_rows(
    service, sheet_name: str, rows: list[list[Any]]
) -> dict:
    """เพิ่ม rows ต่อท้าย sheet."""
    try:
        range_name = f"{sheet_name}!A1"
        body = {"values": rows}
        result = service.spreadsheets().values().append(
            spreadsheetId=SHEETS_ID,
            range=range_name,
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body=body,
        ).execute()
        return {
            "success": True,
            "updated_rows": result.get("updates", {}).get(
                "updatedRows", 0
            ),
        }
    except Exception as e:
        logger.warning("Sheets append failed: %s", type(e).__name__)
        return {"error": f"เพิ่มข้อมูลไม่สำเร็จ: {type(e).__name__}"}


# ─── Sync functions ──────────────────────────────────────────


def sync_patient_registry(master_registry) -> dict:
    """Sync รายชื่อผู้ป่วยทั้งหมดไป Sheets.

    Sheet: "ทะเบียนผู้ป่วย"
    Columns: #, HN คลินิก, HN รพ., ชื่อ-สกุล, วันลงทะเบียน
    """
    if not is_available():
        return {"error": "Google Sheets ไม่ได้เปิดใช้งาน"}

    service = _get_sheets_service()
    if not service:
        return {"error": "ไม่สามารถเชื่อมต่อ Google Sheets"}

    sheet_name = "ทะเบียนผู้ป่วย"
    _ensure_sheet(service, sheet_name)

    try:
        patients = master_registry.list_all() if master_registry else []

        rows: list[list[Any]] = [
            ["#", "HN คลินิก", "HN รพ.", "ชื่อ-สกุล", "วันลงทะเบียน"],
        ]
        for i, p in enumerate(patients, 1):
            rows.append([
                i,
                getattr(p, "clinic_hn", ""),
                getattr(p, "hospital_hn", ""),
                getattr(p, "patient_name", ""),
                getattr(p, "registered_at", ""),
            ])

        result = _write_rows(service, sheet_name, rows)

        # ไม่ log ข้อมูลผู้ป่วย
        logger.info(
            "Sheets sync registry: %d patients",
            len(patients),
        )
        result["patient_count"] = len(patients)
        result["sheet"] = sheet_name
        return result
    except Exception as e:
        logger.warning("Sheets sync registry failed: %s", type(e).__name__)
        return {"error": f"sync ไม่สำเร็จ: {type(e).__name__}"}


def sync_lab_summary(doc_index, storage) -> dict:
    """Sync สรุปผลแล็บล่าสุดของทุกคนไป Sheets.

    Sheet: "สรุปผลแล็บ"
    Columns: HN, ชื่อ, วันที่, Cr, BUN, Hb, K, Ca, PO4, ...
    """
    if not is_available():
        return {"error": "Google Sheets ไม่ได้เปิดใช้งาน"}

    service = _get_sheets_service()
    if not service:
        return {"error": "ไม่สามารถเชื่อมต่อ Google Sheets"}

    sheet_name = "สรุปผลแล็บ"
    _ensure_sheet(service, sheet_name)

    try:
        # ดึงเอกสาร lab ทั้งหมด
        all_docs = []
        if hasattr(doc_index, "_docs"):
            all_docs = [
                d for d in doc_index._docs
                if d.get("category") == "lab"
            ]

        rows: list[list[Any]] = [
            [
                "HN", "วันที่", "Cr", "BUN", "Hb", "Hct",
                "K", "Na", "Ca", "PO4", "Albumin", "eGFR",
            ],
        ]

        # Group by patient folder, latest only
        patient_labs: dict[str, dict] = {}
        for doc in all_docs:
            pf = doc.get("patient_hn_folder", "")
            if not pf:
                continue
            upload_date = doc.get("upload_date", "")
            existing = patient_labs.get(pf)
            if not existing or upload_date > existing.get(
                "upload_date", ""
            ):
                patient_labs[pf] = doc

        lab_prefix = "_lab_results/"
        import json

        for pf, doc in sorted(patient_labs.items()):
            doc_id = doc.get("document_id", "")
            lab_path = f"{lab_prefix}{doc_id}.json"
            try:
                raw = storage.get(lab_path) if storage else None
                if not raw:
                    continue
                lab_data = json.loads(raw)
                results = lab_data.get("results", [])
            except Exception:
                continue

            # สร้าง map ของ analyte → value
            vals: dict[str, str] = {}
            for r in results:
                a = str(r.get("analyte", "")).lower()
                v = str(r.get("value", ""))
                if "creatinine" in a or a == "cr":
                    vals["Cr"] = v
                elif "bun" in a or "urea" in a:
                    vals["BUN"] = v
                elif "hemoglobin" in a or a in ("hb", "hgb"):
                    vals["Hb"] = v
                elif "hematocrit" in a or a in ("hct",):
                    vals["Hct"] = v
                elif "potassium" in a or a == "k":
                    vals["K"] = v
                elif "sodium" in a or a == "na":
                    vals["Na"] = v
                elif "calcium" in a or a == "ca":
                    vals["Ca"] = v
                elif "phosph" in a or "po4" in a:
                    vals["PO4"] = v
                elif "albumin" in a or a == "alb":
                    vals["Albumin"] = v
                elif "egfr" in a or "gfr" in a:
                    vals["eGFR"] = v

            rows.append([
                pf.split("_")[0] if "_" in pf else pf,
                doc.get("upload_date", ""),
                vals.get("Cr", ""),
                vals.get("BUN", ""),
                vals.get("Hb", ""),
                vals.get("Hct", ""),
                vals.get("K", ""),
                vals.get("Na", ""),
                vals.get("Ca", ""),
                vals.get("PO4", ""),
                vals.get("Albumin", ""),
                vals.get("eGFR", ""),
            ])

        result = _write_rows(service, sheet_name, rows)
        logger.info(
            "Sheets sync lab: %d patients", len(patient_labs)
        )
        result["patient_count"] = len(patient_labs)
        result["sheet"] = sheet_name
        return result
    except Exception as e:
        logger.warning("Sheets sync lab failed: %s", type(e).__name__)
        return {"error": f"sync ไม่สำเร็จ: {type(e).__name__}"}


def sync_kpi_data(kpi_data: dict) -> dict:
    """Sync KPI data ไป Sheets.

    Sheet: "KPI"
    """
    if not is_available():
        return {"error": "Google Sheets ไม่ได้เปิดใช้งาน"}

    service = _get_sheets_service()
    if not service:
        return {"error": "ไม่สามารถเชื่อมต่อ Google Sheets"}

    sheet_name = "KPI"
    _ensure_sheet(service, sheet_name)

    try:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        rows: list[list[Any]] = [
            ["KPI", "ค่า", "เป้าหมาย", "อัปเดต"],
        ]
        for key, val in kpi_data.items():
            if isinstance(val, dict):
                rows.append([
                    key,
                    val.get("value", ""),
                    val.get("target", ""),
                    now,
                ])
            else:
                rows.append([key, val, "", now])

        result = _write_rows(service, sheet_name, rows)
        result["sheet"] = sheet_name
        return result
    except Exception as e:
        logger.warning("Sheets sync KPI failed: %s", type(e).__name__)
        return {"error": f"sync ไม่สำเร็จ: {type(e).__name__}"}


def append_document_log(doc_meta: dict) -> dict:
    """เพิ่ม row เมื่อมีเอกสารใหม่.

    Sheet: "บันทึกเอกสาร"
    """
    if not is_available():
        return {"error": "Google Sheets ไม่ได้เปิดใช้งาน"}

    service = _get_sheets_service()
    if not service:
        return {"error": "ไม่สามารถเชื่อมต่อ Google Sheets"}

    sheet_name = "บันทึกเอกสาร"
    _ensure_sheet(service, sheet_name)

    try:
        # ตรวจว่ามี header row หรือยัง
        existing = service.spreadsheets().values().get(
            spreadsheetId=SHEETS_ID,
            range=f"{sheet_name}!A1:F1",
        ).execute()
        if not existing.get("values"):
            _write_rows(
                service,
                sheet_name,
                [["วันที่", "รหัส", "ประเภท", "แฟ้ม", "ขนาด"]],
                clear_first=False,
            )

        # ไม่ log ข้อมูลผู้ป่วย — เก็บแค่ metadata
        row = [
            doc_meta.get("upload_date", ""),
            doc_meta.get("document_id", "")[:8],
            doc_meta.get("category", ""),
            doc_meta.get("patient_hn_folder", "").split("_")[0]
            if doc_meta.get("patient_hn_folder")
            else "",
            doc_meta.get("file_size", ""),
        ]

        result = _append_rows(service, sheet_name, [row])
        logger.info("Sheets: appended doc log")
        return result
    except Exception as e:
        logger.warning(
            "Sheets append doc log failed: %s", type(e).__name__
        )
        return {"error": f"เพิ่มข้อมูลไม่สำเร็จ: {type(e).__name__}"}
