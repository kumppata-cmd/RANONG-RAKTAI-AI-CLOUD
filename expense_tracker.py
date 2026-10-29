"""Expense Tracker — ติดตามค่าใช้จ่ายคลินิกไต

เก็บข้อมูล invoice ที่ parse แล้วใน GCS (JSON per invoice)
สรุปค่าใช้จ่ายรายเดือน/รายบริษัท/รายปี
ตรวจ invoice ซ้ำ

Storage: GCS path _expenses/{year}/{month}/{doc_id}.json

ไม่ log ยอดเงินหรือรายละเอียดบิล
"""

import json
import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

EXPENSE_PREFIX = "_expenses/"


class ExpenseTracker:
    """ติดตามค่าใช้จ่ายคลินิก — GCS-backed"""

    def __init__(self, storage):
        self._storage = storage
        self._cache: list[dict] = []
        self._cache_ts: float = 0
        self._cache_ttl: float = 60.0  # 1 minute cache

    # ─── Write ────────────────────────────────────────────────

    def record_invoice(
        self, doc_id: str, parsed_invoice: dict
    ) -> bool:
        """บันทึก invoice ที่ parse แล้ว

        Args:
            doc_id: document ID
            parsed_invoice: ผลจาก invoice_parser.parse_invoice()

        Returns:
            True if saved successfully
        """
        if not doc_id or not parsed_invoice:
            return False

        try:
            # Determine year/month from invoice date or current date
            inv_date = parsed_invoice.get("invoice_date", "")
            if inv_date and len(inv_date) >= 7:
                year = inv_date[:4]
                month = inv_date[5:7]
            else:
                now = datetime.now(timezone.utc)
                year = str(now.year)
                month = f"{now.month:02d}"

            # Build expense record (don't store line items — just summary)
            record = {
                "doc_id": doc_id,
                "invoice_number": (
                    parsed_invoice.get("invoice_number", "")[:30]
                ),
                "invoice_date": inv_date,
                "vendor_name": (
                    parsed_invoice.get("vendor_name", "")[:50]
                ),
                "tax_id": parsed_invoice.get("tax_id", ""),
                "total_amount": parsed_invoice.get("total_amount", 0),
                "vat_amount": parsed_invoice.get("vat_amount", 0),
                "subtotal": parsed_invoice.get("subtotal", 0),
                "payment_status": (
                    parsed_invoice.get("payment_status", "unknown")
                ),
                "item_count": len(
                    parsed_invoice.get("items", [])
                ),
                "confidence": parsed_invoice.get("confidence", 0),
                "recorded_at": (
                    datetime.now(timezone.utc).isoformat()
                ),
            }

            path = f"{EXPENSE_PREFIX}{year}/{month}/{doc_id}.json"
            data = json.dumps(record, ensure_ascii=False)
            self._storage.put(path, data)

            # Invalidate cache
            self._cache_ts = 0

            # Don't log amounts (financial data)
            logger.info(
                "Expense recorded: doc=%s, year=%s, month=%s",
                doc_id[:8], year, month,
            )
            return True
        except Exception as e:
            logger.warning(
                "Expense record failed: %s", type(e).__name__
            )
            return False

    # ─── Read ─────────────────────────────────────────────────

    def _load_month(self, year: int, month: int) -> list[dict]:
        """Load all expenses for a given month"""
        prefix = f"{EXPENSE_PREFIX}{year:04d}/{month:02d}/"
        results: list[dict] = []
        try:
            if hasattr(self._storage, "list_blobs"):
                blobs = self._storage.list_blobs(prefix=prefix)
                for blob in blobs:
                    try:
                        data = self._storage.get(blob.name)
                        if data:
                            record = json.loads(data)
                            results.append(record)
                    except Exception:
                        pass
            elif hasattr(self._storage, "_bucket"):
                bucket = self._storage._bucket
                if bucket:
                    blobs = bucket.list_blobs(prefix=prefix)
                    for blob in blobs:
                        try:
                            content = blob.download_as_text()
                            record = json.loads(content)
                            results.append(record)
                        except Exception:
                            pass
        except Exception as e:
            logger.warning(
                "Load expenses %04d/%02d failed: %s",
                year, month, type(e).__name__,
            )
        return results

    def get_monthly_summary(
        self, year: int, month: int
    ) -> dict:
        """สรุปค่าใช้จ่ายรายเดือน

        Returns:
            {
                "year": 2026, "month": 10,
                "total": 150000.0,
                "vat_total": 9800.0,
                "invoice_count": 12,
                "by_vendor": {"NIKKISO": 80000, ...},
                "by_status": {"paid": 10, "unpaid": 2},
                "invoices": [...]
            }
        """
        expenses = self._load_month(year, month)

        total = 0.0
        vat_total = 0.0
        by_vendor: dict[str, float] = {}
        by_status: dict[str, int] = {}

        for e in expenses:
            amt = e.get("total_amount", 0) or 0
            total += amt
            vat_total += e.get("vat_amount", 0) or 0

            vendor = e.get("vendor_name", "ไม่ระบุ") or "ไม่ระบุ"
            by_vendor[vendor] = by_vendor.get(vendor, 0) + amt

            status = e.get("payment_status", "unknown")
            by_status[status] = by_status.get(status, 0) + 1

        return {
            "year": year,
            "month": month,
            "total": round(total, 2),
            "vat_total": round(vat_total, 2),
            "invoice_count": len(expenses),
            "by_vendor": {
                k: round(v, 2)
                for k, v in sorted(
                    by_vendor.items(), key=lambda x: -x[1]
                )
            },
            "by_status": by_status,
            "invoices": [
                {
                    "doc_id": e.get("doc_id", ""),
                    "invoice_number": e.get("invoice_number", ""),
                    "invoice_date": e.get("invoice_date", ""),
                    "vendor_name": e.get("vendor_name", ""),
                    "total_amount": e.get("total_amount", 0),
                    "payment_status": e.get(
                        "payment_status", "unknown"
                    ),
                }
                for e in sorted(
                    expenses,
                    key=lambda x: x.get("invoice_date", ""),
                    reverse=True,
                )
            ],
        }

    def get_vendor_summary(
        self, vendor: str, months: int = 6
    ) -> dict:
        """สรุปค่าใช้จ่ายรายบริษัท ย้อนหลัง N เดือน

        Args:
            vendor: ชื่อบริษัท (case-insensitive partial match)
            months: จำนวนเดือนย้อนหลัง

        Returns:
            {
                "vendor": "NIKKISO",
                "months": [...],
                "grand_total": 500000.0,
                "invoice_count": 30,
                "avg_monthly": 83333.0
            }
        """
        now = datetime.now(timezone.utc)
        vendor_upper = (vendor or "").upper()

        monthly_data: list[dict] = []
        grand_total = 0.0
        total_invoices = 0

        for i in range(months):
            # Calculate year/month going back
            m = now.month - i
            y = now.year
            while m < 1:
                m += 12
                y -= 1

            month_expenses = self._load_month(y, m)
            vendor_expenses = [
                e for e in month_expenses
                if vendor_upper in (
                    e.get("vendor_name", "")
                ).upper()
            ]

            month_total = sum(
                e.get("total_amount", 0) or 0
                for e in vendor_expenses
            )
            grand_total += month_total
            total_invoices += len(vendor_expenses)

            monthly_data.append({
                "year": y,
                "month": m,
                "total": round(month_total, 2),
                "invoice_count": len(vendor_expenses),
            })

        return {
            "vendor": vendor,
            "months": monthly_data,
            "grand_total": round(grand_total, 2),
            "invoice_count": total_invoices,
            "avg_monthly": round(
                grand_total / max(months, 1), 2
            ),
        }

    def get_yearly_summary(self, year: int) -> dict:
        """สรุปค่าใช้จ่ายรายปี

        Returns:
            {
                "year": 2026,
                "grand_total": 1800000.0,
                "by_month": [{month: 1, total: ...}, ...],
                "by_vendor": {"NIKKISO": 960000, ...}
            }
        """
        by_month: list[dict] = []
        by_vendor: dict[str, float] = {}
        grand_total = 0.0
        total_invoices = 0

        for m in range(1, 13):
            month_expenses = self._load_month(year, m)
            month_total = sum(
                e.get("total_amount", 0) or 0
                for e in month_expenses
            )
            grand_total += month_total
            total_invoices += len(month_expenses)

            by_month.append({
                "month": m,
                "total": round(month_total, 2),
                "invoice_count": len(month_expenses),
            })

            for e in month_expenses:
                v = e.get("vendor_name", "ไม่ระบุ") or "ไม่ระบุ"
                by_vendor[v] = (
                    by_vendor.get(v, 0)
                    + (e.get("total_amount", 0) or 0)
                )

        return {
            "year": year,
            "grand_total": round(grand_total, 2),
            "invoice_count": total_invoices,
            "by_month": by_month,
            "by_vendor": {
                k: round(v, 2)
                for k, v in sorted(
                    by_vendor.items(), key=lambda x: -x[1]
                )
            },
        }

    def detect_duplicate_invoice(
        self, invoice_no: str, vendor: str, amount: float
    ) -> dict | None:
        """ตรวจ invoice ซ้ำ

        Returns:
            Matching expense record if duplicate found, None otherwise
        """
        if not invoice_no:
            return None

        inv_upper = invoice_no.upper().strip()
        vendor_upper = (vendor or "").upper()

        # Check recent 3 months
        now = datetime.now(timezone.utc)
        for i in range(3):
            m = now.month - i
            y = now.year
            while m < 1:
                m += 12
                y -= 1

            expenses = self._load_month(y, m)
            for e in expenses:
                e_inv = (
                    e.get("invoice_number", "")
                ).upper().strip()
                e_vendor = (
                    e.get("vendor_name", "")
                ).upper()
                e_amt = e.get("total_amount", 0) or 0

                # Same invoice number + same vendor
                if e_inv == inv_upper and vendor_upper in e_vendor:
                    return e
                # Same invoice number + same amount
                if (
                    e_inv == inv_upper
                    and abs(e_amt - amount) < 0.01
                ):
                    return e

        return None

    def get_status(self) -> dict:
        """สถานะระบบ expense tracker"""
        now = datetime.now(timezone.utc)
        current = self._load_month(now.year, now.month)
        return {
            "enabled": True,
            "current_month_invoices": len(current),
            "current_month_total": round(
                sum(
                    e.get("total_amount", 0) or 0
                    for e in current
                ),
                2,
            ),
        }
