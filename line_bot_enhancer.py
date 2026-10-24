#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LINE Bot Enhancer — Phase 6: ปรับปรุง LINE Bot และระบบค้นหาเอกสาร

ปรับปรุง:
  1. Smart Search — ค้นหาแบบหลายคำ, fuzzy matching, กรองตามวันที่/ประเภท
  2. Command Aliases — รองรับคำสั่งธรรมชาติ เช่น "ผลแล็บล่าสุด"
  3. User Statistics — สรุปข้อมูลเอกสารของผู้ใช้
  4. Improved Help — ช่วยเหลือตามบริบท
  5. Search Pagination — แบ่งหน้าผลค้นหา

ข้อจำกัดความปลอดภัย:
  - ไม่เก็บข้อมูลระบุตัวผู้ป่วย
  - ใช้ owner_hash ในการแยกข้อมูล
  - ไม่แสดงข้อมูลข้ามผู้ใช้
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum


# ═══════════════════════════════════════════════════════════
# 1. Command Aliases — รองรับคำสั่งธรรมชาติภาษาไทย
# ═══════════════════════════════════════════════════════════

class CommandType(Enum):
    """ประเภทคำสั่งที่ระบบรองรับ."""
    SEARCH = "search"
    RECENT = "recent"
    READ_DOC = "read_doc"
    REQUEST_FILE = "request_file"
    LAB_RESULT = "lab_result"
    APPROVE_LAB = "approve_lab"
    CLINICAL_ANALYSIS = "clinical_analysis"
    TREND = "trend"
    PATIENT_REPORT = "patient_report"
    CLINIC_KPI = "clinic_kpi"
    MEDICATION_REVIEW = "medication_review"
    MONITORING_STATUS = "monitoring_status"
    NEPHROLOGY_KPI = "nephrology_kpi"
    HELP = "help"
    STATUS = "status"
    MY_STATS = "my_stats"
    # ── Feature 6: Patient Assistant ──
    APPOINTMENT = "appointment"
    VIEW_APPOINTMENTS = "view_appointments"
    CANCEL_APPOINTMENT = "cancel_appointment"
    HEALTH_ADVICE = "health_advice"
    CONTACT_STAFF = "contact_staff"
    VIEW_ESCALATIONS = "view_escalations"
    UNKNOWN = "unknown"


@dataclass
class ParsedCommand:
    """ผลลัพธ์การ parse คำสั่งจากข้อความ."""
    command_type: CommandType
    args: str = ""
    original_text: str = ""


# Alias patterns: (regex_pattern, CommandType, group_index_for_args)
# group_index_for_args: None = no args, int = regex group number
COMMAND_ALIASES: list[tuple[str, CommandType, int | None]] = [
    # ─── ค้นหา ───
    (r"^ค้นหา\s+(.+)$", CommandType.SEARCH, 1),
    (r"^หา\s+(.+)$", CommandType.SEARCH, 1),
    (r"^search\s+(.+)$", CommandType.SEARCH, 1),

    # ─── รายการล่าสุด ───
    (r"^รายการล่าสุด$", CommandType.RECENT, None),
    (r"^ล่าสุด$", CommandType.RECENT, None),
    (r"^เอกสารล่าสุด$", CommandType.RECENT, None),
    (r"^recent$", CommandType.RECENT, None),

    # ─── อ่านเอกสาร ───
    (r"^อ่านเอกสาร\s+(.+)$", CommandType.READ_DOC, 1),
    (r"^อ่าน\s+(.+)$", CommandType.READ_DOC, 1),
    (r"^read\s+(.+)$", CommandType.READ_DOC, 1),

    # ─── ขอไฟล์ ───
    (r"^ขอไฟล์\s+(.+)$", CommandType.REQUEST_FILE, 1),
    (r"^ดาวน์โหลด\s+(.+)$", CommandType.REQUEST_FILE, 1),
    (r"^download\s+(.+)$", CommandType.REQUEST_FILE, 1),

    # ─── ผลแล็บ ───
    (r"^ผลแล็บ\s+(.+)$", CommandType.LAB_RESULT, 1),
    (r"^ผลตรวจ\s+(.+)$", CommandType.LAB_RESULT, 1),
    (r"^ผลเลือด\s+(.+)$", CommandType.LAB_RESULT, 1),
    (r"^lab\s+(.+)$", CommandType.LAB_RESULT, 1),

    # ─── ผลแล็บล่าสุด (shortcut — ไม่ต้องระบุรหัส) ───
    (r"^ผลแล็บล่าสุด$", CommandType.LAB_RESULT, None),
    (r"^ผลตรวจล่าสุด$", CommandType.LAB_RESULT, None),
    (r"^ผลเลือดล่าสุด$", CommandType.LAB_RESULT, None),

    # ─── ยืนยันผลแล็บ ───
    (r"^ยืนยันผลแล็บ\s+(.+)$", CommandType.APPROVE_LAB, 1),
    (r"^approve\s+(.+)$", CommandType.APPROVE_LAB, 1),

    # ─── วิเคราะห์แล็บ ───
    (r"^วิเคราะห์แล็บ\s+(.+)$", CommandType.CLINICAL_ANALYSIS, 1),
    (r"^วิเคราะห์\s+(.+)$", CommandType.CLINICAL_ANALYSIS, 1),
    (r"^วิเคราะห์แล็บล่าสุด$", CommandType.CLINICAL_ANALYSIS, None),
    (r"^วิเคราะห์ล่าสุด$", CommandType.CLINICAL_ANALYSIS, None),

    # ─── แนวโน้มแล็บ ───
    (r"^แนวโน้มแล็บ\s+(.+)$", CommandType.TREND, 1),
    (r"^แนวโน้ม\s+(.+)$", CommandType.TREND, 1),
    (r"^trend\s+(.+)$", CommandType.TREND, 1),
    (r"^แนวโน้มแล็บ$", CommandType.TREND, None),

    # ─── รายงานแล็บ ───
    (r"^รายงานแล็บ$", CommandType.PATIENT_REPORT, None),
    (r"^รายงาน$", CommandType.PATIENT_REPORT, None),
    (r"^report$", CommandType.PATIENT_REPORT, None),
    (r"^สรุปผลตรวจ$", CommandType.PATIENT_REPORT, None),

    # ─── KPI ───
    (r"^KPI$", CommandType.CLINIC_KPI, None),
    (r"^kpi$", CommandType.CLINIC_KPI, None),

    # ─── ตรวจยา ───
    (r"^ตรวจยา\s+(.+)$", CommandType.MEDICATION_REVIEW, 1),
    (r"^เช็คยา\s+(.+)$", CommandType.MEDICATION_REVIEW, 1),
    (r"^ตรวจสอบยา\s+(.+)$", CommandType.MEDICATION_REVIEW, 1),
    (r"^med\s+(.+)$", CommandType.MEDICATION_REVIEW, 1),

    # ─── ตารางตรวจ / Monitoring Schedule ───
    (r"^ตารางตรวจ$", CommandType.MONITORING_STATUS, None),
    (r"^กำหนดตรวจ$", CommandType.MONITORING_STATUS, None),
    (r"^แล็บถัดไป$", CommandType.MONITORING_STATUS, None),
    (r"^monitoring$", CommandType.MONITORING_STATUS, None),
    (r"^schedule$", CommandType.MONITORING_STATUS, None),

    # ─── KPI ไต / Nephrology KPI Dashboard ───
    (r"^KPI\s*ไต$", CommandType.NEPHROLOGY_KPI, None),
    (r"^kpi\s*ไต$", CommandType.NEPHROLOGY_KPI, None),
    (r"^เป้าหมายไต$", CommandType.NEPHROLOGY_KPI, None),
    (r"^nephro\s*kpi$", CommandType.NEPHROLOGY_KPI, None),
    (r"^dashboard$", CommandType.NEPHROLOGY_KPI, None),

    # ─── สถิติ ───
    (r"^สถิติ$", CommandType.MY_STATS, None),
    (r"^สถิติของฉัน$", CommandType.MY_STATS, None),
    (r"^stats$", CommandType.MY_STATS, None),
    (r"^my\s*stats$", CommandType.MY_STATS, None),

    # ─── Feature 6: นัดหมาย ───
    (r"^นัดหมาย\s+(.+)$", CommandType.APPOINTMENT, 1),
    (r"^นัด\s+(.+)$", CommandType.APPOINTMENT, 1),
    (r"^ตั้งนัด\s+(.+)$", CommandType.APPOINTMENT, 1),
    (r"^appointment\s+(.+)$", CommandType.APPOINTMENT, 1),
    (r"^ดูนัด$", CommandType.VIEW_APPOINTMENTS, None),
    (r"^นัดหมายของฉัน$", CommandType.VIEW_APPOINTMENTS, None),
    (r"^ดูนัดหมาย$", CommandType.VIEW_APPOINTMENTS, None),
    (r"^my\s*appointments?$", CommandType.VIEW_APPOINTMENTS, None),
    (r"^ยกเลิกนัด\s+(.+)$", CommandType.CANCEL_APPOINTMENT, 1),
    (r"^cancel\s+(.+)$", CommandType.CANCEL_APPOINTMENT, 1),

    # ─── Feature 6: คำแนะนำสุขภาพ ───
    (r"^สุขภาพ\s+(.+)$", CommandType.HEALTH_ADVICE, 1),
    (r"^สุขภาพ$", CommandType.HEALTH_ADVICE, None),
    (r"^คำแนะนำ\s*(.*)$", CommandType.HEALTH_ADVICE, 1),
    (r"^คำแนะนำสุขภาพ\s*(.*)$", CommandType.HEALTH_ADVICE, 1),
    (r"^health\s*(.*)$", CommandType.HEALTH_ADVICE, 1),
    (r"^ดูแลไต$", CommandType.HEALTH_ADVICE, None),
    (r"^ความรู้ไต$", CommandType.HEALTH_ADVICE, None),

    # ─── Feature 6: ติดต่อเจ้าหน้าที่ ───
    (r"^ติดต่อเจ้าหน้าที่\s*(.*)$", CommandType.CONTACT_STAFF, 1),
    (r"^ติดต่อ\s*(.*)$", CommandType.CONTACT_STAFF, 1),
    (r"^ต้องการความช่วยเหลือ\s*(.*)$", CommandType.CONTACT_STAFF, 1),
    (r"^contact\s*(.*)$", CommandType.CONTACT_STAFF, 1),
    (r"^ฉุกเฉิน\s*(.*)$", CommandType.CONTACT_STAFF, 1),

    # ─── Feature 6: Admin — ดูคำร้อง ───
    (r"^ดูคำร้อง$", CommandType.VIEW_ESCALATIONS, None),
    (r"^คำร้อง$", CommandType.VIEW_ESCALATIONS, None),
    (r"^escalations?$", CommandType.VIEW_ESCALATIONS, None),

    # ─── ช่วยเหลือ ───
    (r"^ช่วยเหลือ$", CommandType.HELP, None),
    (r"^help$", CommandType.HELP, None),
    (r"^คำสั่ง$", CommandType.HELP, None),
    (r"^เมนู$", CommandType.HELP, None),
    (r"^menu$", CommandType.HELP, None),

    # ─── สถานะ ───
    (r"^สถานะ$", CommandType.STATUS, None),
    (r"^status$", CommandType.STATUS, None),
]


def parse_command(text: str) -> ParsedCommand:
    """Parse ข้อความจากผู้ใช้เป็นคำสั่งที่ระบบเข้าใจ.

    รองรับ:
    - คำสั่งมาตรฐาน (เช่น "ค้นหา ผลแล็บ")
    - คำสั่งย่อ / aliases (เช่น "หา ผลแล็บ", "ล่าสุด")
    - Case-insensitive สำหรับ English commands
    """
    stripped = text.strip()
    if not stripped:
        return ParsedCommand(CommandType.UNKNOWN, original_text=text)

    for pattern, cmd_type, arg_group in COMMAND_ALIASES:
        match = re.match(pattern, stripped, re.IGNORECASE)
        if match:
            args = match.group(arg_group).strip() if arg_group else ""
            return ParsedCommand(cmd_type, args=args, original_text=stripped)

    return ParsedCommand(CommandType.UNKNOWN, original_text=stripped)


# ═══════════════════════════════════════════════════════════
# 2. Smart Search — ค้นหาแบบหลายคำ + กรองตามประเภท/วันที่
# ═══════════════════════════════════════════════════════════

# Category aliases for natural language search
CATEGORY_ALIASES: dict[str, str] = {
    # Thai
    "ใบเสร็จ": "bills",
    "บิล": "bills",
    "ใบสั่งยา": "rx",
    "ยา": "rx",
    "แล็บ": "lab",
    "ผลเลือด": "lab",
    "ผลตรวจ": "lab",
    "ผลแล็บ": "lab",
    "lab": "lab",
    "ฟอกเลือด": "dialysis",
    "ฟอกไต": "dialysis",
    "ไตเทียม": "dialysis",
    "dialysis": "dialysis",
    # English
    "bill": "bills",
    "receipt": "bills",
    "prescription": "rx",
    "laboratory": "lab",
}

# Date range keywords
DATE_RANGE_KEYWORDS: dict[str, int] = {
    "วันนี้": 1,
    "today": 1,
    "สัปดาห์นี้": 7,
    "สัปดาห์": 7,
    "week": 7,
    "เดือนนี้": 30,
    "เดือน": 30,
    "month": 30,
    "3เดือน": 90,
    "quarter": 90,
    "6เดือน": 180,
    "ปีนี้": 365,
    "ปี": 365,
    "year": 365,
}


@dataclass
class SearchFilter:
    """ตัวกรองการค้นหาที่แยกจากข้อความ."""
    keywords: list[str] = field(default_factory=list)
    category: str | None = None
    date_range_days: int | None = None
    original_query: str = ""


def parse_search_query(query: str) -> SearchFilter:
    """แยกข้อความค้นหาเป็น keywords + filters.

    ตัวอย่าง:
    - "ผลแล็บ เดือนนี้" → keywords=[], category="lab", date_range=30
    - "ใบเสร็จ 2024" → keywords=["2024"], category="bills"
    - "BUN creatinine" → keywords=["bun", "creatinine"]
    """
    sf = SearchFilter(original_query=query)
    words = query.strip().split()
    remaining: list[str] = []

    for word in words:
        word_lower = word.lower()

        # Check category aliases
        if word_lower in CATEGORY_ALIASES and sf.category is None:
            sf.category = CATEGORY_ALIASES[word_lower]
            continue

        # Check date range keywords
        if word_lower in DATE_RANGE_KEYWORDS and sf.date_range_days is None:
            sf.date_range_days = DATE_RANGE_KEYWORDS[word_lower]
            continue

        remaining.append(word)

    sf.keywords = remaining
    return sf


def smart_search(
    docs: list[dict],
    search_filter: SearchFilter,
    owner_hash: str,
    max_results: int = 10,
) -> list[dict]:
    """ค้นหาเอกสารอัจฉริยะ — owner-aware, multi-keyword, category + date filter.

    ปรับปรุงจาก search_by_owner:
    - รองรับหลาย keywords (AND logic)
    - กรองตามประเภทเอกสาร
    - กรองตามช่วงเวลา
    - จัดลำดับตามความเกี่ยวข้อง (relevance score)
    """
    now = datetime.now()
    scored_results: list[tuple[float, dict]] = []

    for doc in reversed(docs):  # ใหม่ก่อน
        # Owner check — Fail Closed
        doc_owner = doc.get("owner_hash", "")
        if not doc_owner or doc_owner != owner_hash:
            continue

        # Category filter
        if search_filter.category:
            if doc.get("category", "") != search_filter.category:
                continue

        # Date range filter
        if search_filter.date_range_days is not None:
            upload_date_str = doc.get("upload_date", "")
            if upload_date_str:
                try:
                    upload_date = datetime.strptime(upload_date_str, "%Y-%m-%d")
                    cutoff = now - timedelta(days=search_filter.date_range_days)
                    if upload_date < cutoff:
                        continue
                except ValueError:
                    pass  # ข้ามถ้าวันที่ parse ไม่ได้

        # Keyword matching — multi-keyword AND
        searchable = " ".join([
            doc.get("category", ""),
            doc.get("original_filename", ""),
            doc.get("extracted_text", ""),
            doc.get("upload_date", ""),
            doc.get("mime_type", ""),
        ]).lower()

        if search_filter.keywords:
            all_match = True
            match_score = 0.0
            for kw in search_filter.keywords:
                kw_lower = kw.lower()
                if kw_lower in searchable:
                    # Count occurrences for relevance scoring
                    count = searchable.count(kw_lower)
                    match_score += min(count, 5)  # cap per keyword
                else:
                    all_match = False
                    break
            if not all_match:
                continue
        else:
            match_score = 1.0  # No keyword filter = base score

        # Recency bonus — newer docs get slight boost
        upload_date_str = doc.get("upload_date", "")
        recency_bonus = 0.0
        if upload_date_str:
            try:
                upload_date = datetime.strptime(upload_date_str, "%Y-%m-%d")
                days_old = (now - upload_date).days
                recency_bonus = max(0, 1.0 - days_old / 365)
            except ValueError:
                pass

        total_score = match_score + recency_bonus * 0.5
        scored_results.append((total_score, doc))

    # Sort by score descending
    scored_results.sort(key=lambda x: x[0], reverse=True)
    return [doc for _, doc in scored_results[:max_results]]


# ═══════════════════════════════════════════════════════════
# 3. User Statistics — สรุปสถิติเอกสารของผู้ใช้
# ═══════════════════════════════════════════════════════════

@dataclass
class UserStats:
    """สรุปสถิติเอกสารของผู้ใช้."""
    total_documents: int = 0
    category_counts: dict[str, int] = field(default_factory=dict)
    lab_reports_count: int = 0
    clinical_analyses_count: int = 0
    latest_upload_date: str = ""
    earliest_upload_date: str = ""
    approved_labs: int = 0
    pending_labs: int = 0


def compute_user_stats(
    user_docs: list[dict],
    lab_report_count: int = 0,
    clinical_analysis_count: int = 0,
    approved_count: int = 0,
    pending_count: int = 0,
) -> UserStats:
    """คำนวณสถิติเอกสารของผู้ใช้.

    Args:
        user_docs: รายการ metadata ของเอกสารผู้ใช้
        lab_report_count: จำนวนผลแล็บที่ดึงได้
        clinical_analysis_count: จำนวนผลวิเคราะห์
        approved_count: จำนวนผลแล็บที่ยืนยันแล้ว
        pending_count: จำนวนผลแล็บที่รอยืนยัน
    """
    stats = UserStats(
        total_documents=len(user_docs),
        lab_reports_count=lab_report_count,
        clinical_analyses_count=clinical_analysis_count,
        approved_labs=approved_count,
        pending_labs=pending_count,
    )

    cat_names = {
        "bills": "ใบเสร็จ/บิล",
        "rx": "ใบสั่งยา",
        "lab": "ผลตรวจแล็บ",
        "dialysis": "เอกสารฟอกเลือด",
        "review": "รอตรวจสอบ",
    }

    dates: list[str] = []
    for doc in user_docs:
        cat = doc.get("category", "review")
        cat_label = cat_names.get(cat, cat)
        stats.category_counts[cat_label] = stats.category_counts.get(cat_label, 0) + 1

        upload_date = doc.get("upload_date", "")
        if upload_date:
            dates.append(upload_date)

    if dates:
        dates.sort()
        stats.earliest_upload_date = dates[0]
        stats.latest_upload_date = dates[-1]

    return stats


def user_stats_text_th(stats: UserStats) -> str:
    """แสดงสถิติผู้ใช้เป็นข้อความภาษาไทย."""
    if stats.total_documents == 0:
        return (
            "📊 สถิติเอกสารของคุณ\n\n"
            "ยังไม่มีเอกสารในระบบ\n"
            "ส่งเอกสาร (PDF, TXT, JPG, PNG) เพื่อเริ่มใช้งาน"
        )

    lines = [
        "📊 สถิติเอกสารของคุณ\n",
        f"📁 เอกสารทั้งหมด: {stats.total_documents} ฉบับ",
    ]

    # Category breakdown
    if stats.category_counts:
        lines.append("\n📂 แยกตามประเภท:")
        for cat, count in sorted(stats.category_counts.items()):
            lines.append(f"  • {cat}: {count}")

    # Lab info
    if stats.lab_reports_count > 0:
        lines.append(f"\n🔬 ผลแล็บที่ดึงได้: {stats.lab_reports_count} ชุด")
        if stats.approved_labs > 0:
            lines.append(f"  ✅ ยืนยันแล้ว: {stats.approved_labs}")
        if stats.pending_labs > 0:
            lines.append(f"  ⏳ รอยืนยัน: {stats.pending_labs}")

    if stats.clinical_analyses_count > 0:
        lines.append(f"📋 ผลวิเคราะห์ทางคลินิก: {stats.clinical_analyses_count} ชุด")

    # Date range
    if stats.earliest_upload_date:
        lines.append(
            f"\n📅 ช่วงเวลา: {stats.earliest_upload_date} "
            f"— {stats.latest_upload_date}"
        )

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
# 4. Improved Help — ช่วยเหลือตามบริบท
# ═══════════════════════════════════════════════════════════

def generate_help_text(is_admin: bool = False, has_docs: bool = True) -> str:
    """สร้างข้อความช่วยเหลือตามบริบทของผู้ใช้.

    Args:
        is_admin: ผู้ใช้เป็น admin หรือไม่
        has_docs: ผู้ใช้มีเอกสารแล้วหรือยัง
    """
    sections = []

    if not has_docs:
        sections.append(
            "🏥 ระบบจัดการเอกสาร คลินิกระนอง รักษ์ไต\n\n"
            "เริ่มต้นใช้งาน:\n"
            "📎 ส่งภาพหรือไฟล์เอกสาร (PDF, TXT, JPG, PNG)\n"
            "   ระบบจะจำแนกประเภทและจัดเก็บอัตโนมัติ"
        )
    else:
        sections.append("📋 คำสั่งที่ใช้ได้:")

    # Document commands
    sections.append(
        "\n🔍 ค้นหาเอกสาร:\n"
        "  ค้นหา [คำค้น]\n"
        "  ค้นหา แล็บ เดือนนี้\n"
        "  ค้นหา ใบเสร็จ สัปดาห์นี้\n"
        "  รายการล่าสุด / ล่าสุด"
    )

    sections.append(
        "\n📄 เรียกดูเอกสาร:\n"
        "  อ่านเอกสาร [รหัส] / อ่าน [รหัส]\n"
        "  ขอไฟล์ [รหัส] / ดาวน์โหลด [รหัส]"
    )

    # Lab commands
    sections.append(
        "\n🔬 ผลตรวจ Lab:\n"
        "  ผลแล็บ [รหัส] / ผลแล็บล่าสุด\n"
        "  วิเคราะห์แล็บ [รหัส] / วิเคราะห์แล็บล่าสุด\n"
        "  แนวโน้มแล็บ [เดือน/3เดือน/6เดือน/ปี]\n"
        "  รายงานแล็บ / รายงาน"
    )

    # Medication review
    sections.append(
        "\n💊 ตรวจสอบยา:\n"
        "  ตรวจยา [รายการยา]\n"
        "  เช่น: ตรวจยา Metformin, Ibuprofen"
    )

    # Monitoring schedule
    sections.append(
        "\n📅 ตารางตรวจติดตาม:\n"
        "  ตารางตรวจ / กำหนดตรวจ / แล็บถัดไป\n"
        "  KPIไต / เป้าหมายไต / dashboard"
    )

    # Stats
    sections.append(
        "\n📊 สถิติ:\n"
        "  สถิติ / stats\n"
        "  สถานะ / status"
    )

    # Feature 6: Patient assistant commands
    sections.append(
        "\n📅 นัดหมาย:\n"
        "  นัดหมาย [วัตถุประสงค์] [วันที่] [เวลา]\n"
        "  เช่น: นัดหมาย ฟอกไต 25/12/69 09:00\n"
        "  ดูนัด — ดูนัดหมายที่จะมาถึง\n"
        "  ยกเลิกนัด [รหัส]"
    )

    sections.append(
        "\n🏥 คำแนะนำสุขภาพ:\n"
        "  สุขภาพ — ดูหัวข้อทั้งหมด\n"
        "  สุขภาพ [หัวข้อ] เช่น สุขภาพ อาหาร\n"
        "  ดูแลไต / ความรู้ไต"
    )

    sections.append(
        "\n📨 ติดต่อเจ้าหน้าที่:\n"
        "  ติดต่อเจ้าหน้าที่ [ข้อความ]\n"
        "  ฉุกเฉิน [อาการ]"
    )

    # Admin commands
    if is_admin:
        sections.append(
            "\n🔧 คำสั่งผู้ดูแล:\n"
            "  ยืนยันผลแล็บ [รหัส]\n"
            "  KPI\n"
            "  ดูคำร้อง — ดูคำร้องจากผู้ป่วย"
        )

    # Tips
    sections.append(
        "\n💡 เคล็ดลับ:\n"
        "• ใช้คำค้นหลายคำเพื่อกรอง เช่น 'ค้นหา BUN creatinine'\n"
        "• กรองตามช่วงเวลา: 'ค้นหา แล็บ สัปดาห์นี้'\n"
        "• พิมพ์ 'ผลแล็บล่าสุด' เพื่อดูผลตรวจล่าสุดโดยไม่ต้องหารหัส\n"
        "• พิมพ์ 'สุขภาพ' เพื่อดูคำแนะนำสุขภาพไต\n"
        "• ผลแล็บหลายหน้า: ส่งภาพทีละรูปต่อเนื่อง ระบบจะรวมอัตโนมัติ"
    )

    return "\n".join(sections)


# ═══════════════════════════════════════════════════════════
# 5. Enhanced Format — ปรับปรุงรูปแบบข้อความค้นหา
# ═══════════════════════════════════════════════════════════

CAT_EMOJI: dict[str, str] = {
    "bills": "🧾",
    "rx": "💊",
    "lab": "🔬",
    "dialysis": "🩸",
    "review": "📝",
}

CAT_NAMES_TH: dict[str, str] = {
    "bills": "ใบเสร็จ/บิล",
    "rx": "ใบสั่งยา",
    "lab": "ผลตรวจแล็บ",
    "dialysis": "เอกสารฟอกเลือด",
    "review": "รอตรวจสอบ",
}


def format_search_results_enhanced(
    results: list[dict],
    query: str = "",
    page: int = 1,
    page_size: int = 5,
) -> str:
    """จัดรูปแบบผลค้นหาแบบปรับปรุง — มี emoji, ข้อมูลเพิ่มเติม, แนะนำคำสั่ง.

    Args:
        results: รายการเอกสารที่พบ
        query: คำค้นเดิม (สำหรับแสดงบริบท)
        page: หน้าที่แสดง (1-based)
        page_size: จำนวนผลลัพธ์ต่อหน้า
    """
    if not results:
        suggestions = [
            "ไม่พบเอกสารที่ตรงกับคำค้น",
        ]
        if query:
            suggestions.append(f"ลองค้นหาด้วยคำอื่น หรือพิมพ์ 'ล่าสุด'")
        return "\n".join(suggestions)

    total = len(results)
    total_pages = (total + page_size - 1) // page_size
    page = max(1, min(page, total_pages))

    start_idx = (page - 1) * page_size
    end_idx = min(start_idx + page_size, total)
    page_results = results[start_idx:end_idx]

    header = f"📋 พบ {total} เอกสาร"
    if query:
        header += f" (ค้นหา: {query[:30]})"
    if total_pages > 1:
        header += f"\nหน้า {page}/{total_pages}"

    lines = [header, ""]

    for i, doc in enumerate(page_results, start=start_idx + 1):
        cat = doc.get("category", "review")
        emoji = CAT_EMOJI.get(cat, "📄")
        cat_name = CAT_NAMES_TH.get(cat, cat)
        date_str = doc.get("upload_date", "ไม่ทราบ")
        doc_id = doc.get("document_id", "")[:8]

        # Extract brief preview from text
        text_preview = ""
        extracted = doc.get("extracted_text", "").strip()
        if extracted:
            # ตัดเอาเฉพาะข้อความสั้นๆ
            preview = extracted[:60].replace("\n", " ").strip()
            if preview:
                text_preview = f"\n   {preview}…"

        lines.append(
            f"{i}. {emoji} [{cat_name}] {date_str}\n"
            f"   รหัส: {doc_id}…{text_preview}"
        )

    # Suggestions
    lines.append("")
    lines.append("พิมพ์:")
    lines.append("  'อ่าน [รหัส]' — ดูเนื้อหา")
    lines.append("  'ขอไฟล์ [รหัส]' — ดาวน์โหลด")
    if any(d.get("category") == "lab" for d in page_results):
        lines.append("  'ผลแล็บ [รหัส]' — ดูผลตรวจ")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
# 6. Latest Document Helper — ดึงเอกสารล่าสุดตามประเภท
# ═══════════════════════════════════════════════════════════

def find_latest_doc_by_category(
    docs: list[dict],
    owner_hash: str,
    category: str | None = None,
) -> dict | None:
    """ค้นหาเอกสารล่าสุดของผู้ใช้ ตามประเภท (ถ้าระบุ).

    Owner-aware — ดูเฉพาะเอกสารของผู้ใช้คนนั้น
    """
    latest = None
    latest_date = ""

    for doc in docs:
        doc_owner = doc.get("owner_hash", "")
        if not doc_owner or doc_owner != owner_hash:
            continue

        if category and doc.get("category", "") != category:
            continue

        upload_date = doc.get("upload_date", "")
        if upload_date >= latest_date:
            latest_date = upload_date
            latest = doc

    return latest


def find_latest_lab_doc(
    docs: list[dict],
    owner_hash: str,
) -> dict | None:
    """ค้นหาเอกสาร Lab ล่าสุดของผู้ใช้."""
    return find_latest_doc_by_category(docs, owner_hash, category="lab")
