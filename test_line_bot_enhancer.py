#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests for line_bot_enhancer.py — Phase 6
ทดสอบระบบปรับปรุง LINE Bot และค้นหาเอกสาร

ใช้ข้อมูล DEMO เท่านั้น — ไม่มีข้อมูลผู้ป่วยจริง
"""

import unittest
from datetime import datetime, timedelta

from line_bot_enhancer import (
    CATEGORY_ALIASES,
    CAT_EMOJI,
    CAT_NAMES_TH,
    CommandType,
    ParsedCommand,
    SearchFilter,
    UserStats,
    compute_user_stats,
    find_latest_doc_by_category,
    find_latest_lab_doc,
    format_search_results_enhanced,
    generate_help_text,
    parse_command,
    parse_search_query,
    smart_search,
    user_stats_text_th,
)


# ═══════════════════════════════════════════════════════════
# Test Fixtures — DEMO data only
# ═══════════════════════════════════════════════════════════

OWNER_A = "hash_owner_a_demo"
OWNER_B = "hash_owner_b_demo"

TODAY_STR = datetime.now().strftime("%Y-%m-%d")
YESTERDAY_STR = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
LAST_WEEK_STR = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
LAST_MONTH_STR = (datetime.now() - timedelta(days=25)).strftime("%Y-%m-%d")
OLD_DATE_STR = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")


def make_doc(doc_id, category, owner, date, text="", filename=""):
    return {
        "document_id": doc_id,
        "category": category,
        "owner_hash": owner,
        "upload_date": date,
        "extracted_text": text,
        "original_filename": filename,
        "mime_type": "application/pdf",
    }


DEMO_DOCS = [
    make_doc("doc_lab_001", "lab", OWNER_A, TODAY_STR,
             "BUN 25 Creatinine 1.2 eGFR 65", "lab_result_oct.pdf"),
    make_doc("doc_lab_002", "lab", OWNER_A, LAST_WEEK_STR,
             "Hemoglobin 11.5 Hematocrit 35", "cbc_result.pdf"),
    make_doc("doc_lab_003", "lab", OWNER_A, OLD_DATE_STR,
             "BUN 30 Creatinine 1.5", "old_lab.pdf"),
    make_doc("doc_bill_001", "bills", OWNER_A, YESTERDAY_STR,
             "ค่ารักษา 1500 บาท", "receipt_oct.pdf"),
    make_doc("doc_rx_001", "rx", OWNER_A, LAST_MONTH_STR,
             "Metformin 500mg Losartan 50mg", "prescription.pdf"),
    make_doc("doc_dial_001", "dialysis", OWNER_A, LAST_WEEK_STR,
             "HD 4 ชั่วโมง UF 2.5L", "dialysis_record.pdf"),
    # Owner B docs — should NOT appear in Owner A searches
    make_doc("doc_lab_b01", "lab", OWNER_B, TODAY_STR,
             "Potassium 5.8", "lab_b.pdf"),
    make_doc("doc_bill_b01", "bills", OWNER_B, TODAY_STR,
             "ค่ายา 800 บาท", "bill_b.pdf"),
    # No owner doc — should be excluded
    make_doc("doc_noown_01", "lab", "", TODAY_STR, "test"),
]


# ═══════════════════════════════════════════════════════════
# 1. Command Parsing Tests
# ═══════════════════════════════════════════════════════════

class TestParseCommand(unittest.TestCase):
    """ทดสอบ parse_command — จดจำคำสั่งธรรมชาติ."""

    def test_search_standard(self):
        r = parse_command("ค้นหา ผลเลือด")
        self.assertEqual(r.command_type, CommandType.SEARCH)
        self.assertEqual(r.args, "ผลเลือด")

    def test_search_alias_ha(self):
        r = parse_command("หา BUN")
        self.assertEqual(r.command_type, CommandType.SEARCH)
        self.assertEqual(r.args, "BUN")

    def test_search_english(self):
        r = parse_command("search creatinine")
        self.assertEqual(r.command_type, CommandType.SEARCH)
        self.assertEqual(r.args, "creatinine")

    def test_recent_standard(self):
        r = parse_command("รายการล่าสุด")
        self.assertEqual(r.command_type, CommandType.RECENT)

    def test_recent_short(self):
        r = parse_command("ล่าสุด")
        self.assertEqual(r.command_type, CommandType.RECENT)

    def test_recent_doc(self):
        r = parse_command("เอกสารล่าสุด")
        self.assertEqual(r.command_type, CommandType.RECENT)

    def test_read_doc_standard(self):
        r = parse_command("อ่านเอกสาร abc123")
        self.assertEqual(r.command_type, CommandType.READ_DOC)
        self.assertEqual(r.args, "abc123")

    def test_read_doc_short(self):
        r = parse_command("อ่าน xyz789")
        self.assertEqual(r.command_type, CommandType.READ_DOC)
        self.assertEqual(r.args, "xyz789")

    def test_request_file_standard(self):
        r = parse_command("ขอไฟล์ doc001")
        self.assertEqual(r.command_type, CommandType.REQUEST_FILE)

    def test_request_file_download(self):
        r = parse_command("ดาวน์โหลด doc001")
        self.assertEqual(r.command_type, CommandType.REQUEST_FILE)

    def test_lab_result_with_id(self):
        r = parse_command("ผลแล็บ abc123")
        self.assertEqual(r.command_type, CommandType.LAB_RESULT)
        self.assertEqual(r.args, "abc123")

    def test_lab_result_alias_blood(self):
        r = parse_command("ผลเลือด abc123")
        self.assertEqual(r.command_type, CommandType.LAB_RESULT)

    def test_lab_result_latest(self):
        r = parse_command("ผลแล็บล่าสุด")
        self.assertEqual(r.command_type, CommandType.LAB_RESULT)
        self.assertEqual(r.args, "")

    def test_lab_result_latest_blood(self):
        r = parse_command("ผลเลือดล่าสุด")
        self.assertEqual(r.command_type, CommandType.LAB_RESULT)
        self.assertEqual(r.args, "")

    def test_approve_lab(self):
        r = parse_command("ยืนยันผลแล็บ abc123")
        self.assertEqual(r.command_type, CommandType.APPROVE_LAB)

    def test_clinical_analysis(self):
        r = parse_command("วิเคราะห์แล็บ abc123")
        self.assertEqual(r.command_type, CommandType.CLINICAL_ANALYSIS)

    def test_clinical_analysis_short(self):
        r = parse_command("วิเคราะห์ abc123")
        self.assertEqual(r.command_type, CommandType.CLINICAL_ANALYSIS)

    def test_clinical_analysis_latest(self):
        r = parse_command("วิเคราะห์แล็บล่าสุด")
        self.assertEqual(r.command_type, CommandType.CLINICAL_ANALYSIS)
        self.assertEqual(r.args, "")

    def test_trend_with_period(self):
        r = parse_command("แนวโน้มแล็บ 3เดือน")
        self.assertEqual(r.command_type, CommandType.TREND)
        self.assertEqual(r.args, "3เดือน")

    def test_trend_default(self):
        r = parse_command("แนวโน้มแล็บ")
        self.assertEqual(r.command_type, CommandType.TREND)
        self.assertEqual(r.args, "")

    def test_patient_report(self):
        r = parse_command("รายงานแล็บ")
        self.assertEqual(r.command_type, CommandType.PATIENT_REPORT)

    def test_patient_report_short(self):
        r = parse_command("รายงาน")
        self.assertEqual(r.command_type, CommandType.PATIENT_REPORT)

    def test_patient_report_summary(self):
        r = parse_command("สรุปผลตรวจ")
        self.assertEqual(r.command_type, CommandType.PATIENT_REPORT)

    def test_clinic_kpi(self):
        r = parse_command("KPI")
        self.assertEqual(r.command_type, CommandType.CLINIC_KPI)

    def test_clinic_kpi_lowercase(self):
        r = parse_command("kpi")
        self.assertEqual(r.command_type, CommandType.CLINIC_KPI)

    def test_medication_review(self):
        r = parse_command("ตรวจยา Metformin, Ibuprofen")
        self.assertEqual(r.command_type, CommandType.MEDICATION_REVIEW)
        self.assertEqual(r.args, "Metformin, Ibuprofen")

    def test_medication_review_alias(self):
        r = parse_command("เช็คยา Losartan")
        self.assertEqual(r.command_type, CommandType.MEDICATION_REVIEW)

    def test_help_standard(self):
        r = parse_command("ช่วยเหลือ")
        self.assertEqual(r.command_type, CommandType.HELP)

    def test_help_menu(self):
        r = parse_command("เมนู")
        self.assertEqual(r.command_type, CommandType.HELP)

    def test_stats(self):
        r = parse_command("สถิติ")
        self.assertEqual(r.command_type, CommandType.MY_STATS)

    def test_stats_english(self):
        r = parse_command("stats")
        self.assertEqual(r.command_type, CommandType.MY_STATS)

    def test_status(self):
        r = parse_command("สถานะ")
        self.assertEqual(r.command_type, CommandType.STATUS)

    def test_unknown(self):
        r = parse_command("สวัสดีครับ")
        self.assertEqual(r.command_type, CommandType.UNKNOWN)

    def test_empty(self):
        r = parse_command("")
        self.assertEqual(r.command_type, CommandType.UNKNOWN)

    def test_whitespace_handling(self):
        r = parse_command("  ค้นหา   BUN  ")
        self.assertEqual(r.command_type, CommandType.SEARCH)
        self.assertEqual(r.args, "BUN")

    def test_original_text_preserved(self):
        r = parse_command("ค้นหา ผลเลือด")
        self.assertEqual(r.original_text, "ค้นหา ผลเลือด")


# ═══════════════════════════════════════════════════════════
# 2. Search Query Parsing Tests
# ═══════════════════════════════════════════════════════════

class TestParseSearchQuery(unittest.TestCase):
    """ทดสอบ parse_search_query — แยก keywords + filters."""

    def test_simple_keyword(self):
        sf = parse_search_query("BUN")
        self.assertEqual(sf.keywords, ["BUN"])
        self.assertIsNone(sf.category)
        self.assertIsNone(sf.date_range_days)

    def test_multi_keywords(self):
        sf = parse_search_query("BUN creatinine eGFR")
        self.assertEqual(sf.keywords, ["BUN", "creatinine", "eGFR"])

    def test_category_thai(self):
        sf = parse_search_query("แล็บ")
        self.assertEqual(sf.category, "lab")
        self.assertEqual(sf.keywords, [])

    def test_category_bills(self):
        sf = parse_search_query("ใบเสร็จ")
        self.assertEqual(sf.category, "bills")

    def test_category_rx(self):
        sf = parse_search_query("ใบสั่งยา")
        self.assertEqual(sf.category, "rx")

    def test_category_dialysis(self):
        sf = parse_search_query("ฟอกเลือด")
        self.assertEqual(sf.category, "dialysis")

    def test_date_range_today(self):
        sf = parse_search_query("วันนี้")
        self.assertEqual(sf.date_range_days, 1)

    def test_date_range_week(self):
        sf = parse_search_query("สัปดาห์นี้")
        self.assertEqual(sf.date_range_days, 7)

    def test_date_range_month(self):
        sf = parse_search_query("เดือนนี้")
        self.assertEqual(sf.date_range_days, 30)

    def test_combined_category_and_date(self):
        sf = parse_search_query("แล็บ เดือนนี้")
        self.assertEqual(sf.category, "lab")
        self.assertEqual(sf.date_range_days, 30)
        self.assertEqual(sf.keywords, [])

    def test_combined_all(self):
        sf = parse_search_query("แล็บ BUN เดือนนี้")
        self.assertEqual(sf.category, "lab")
        self.assertEqual(sf.date_range_days, 30)
        self.assertEqual(sf.keywords, ["BUN"])

    def test_original_query_preserved(self):
        sf = parse_search_query("แล็บ BUN")
        self.assertEqual(sf.original_query, "แล็บ BUN")

    def test_first_category_wins(self):
        sf = parse_search_query("แล็บ ใบเสร็จ")
        self.assertEqual(sf.category, "lab")
        self.assertEqual(sf.keywords, ["ใบเสร็จ"])

    def test_english_category(self):
        sf = parse_search_query("lab")
        self.assertEqual(sf.category, "lab")


# ═══════════════════════════════════════════════════════════
# 3. Smart Search Tests
# ═══════════════════════════════════════════════════════════

class TestSmartSearch(unittest.TestCase):
    """ทดสอบ smart_search — owner-aware, multi-keyword, filters."""

    def test_owner_isolation(self):
        """Owner A ต้องไม่เห็นเอกสาร Owner B."""
        sf = SearchFilter(keywords=[])
        results = smart_search(DEMO_DOCS, sf, OWNER_A)
        doc_ids = [d["document_id"] for d in results]
        self.assertNotIn("doc_lab_b01", doc_ids)
        self.assertNotIn("doc_bill_b01", doc_ids)

    def test_owner_b_sees_own(self):
        sf = SearchFilter(keywords=[])
        results = smart_search(DEMO_DOCS, sf, OWNER_B)
        doc_ids = [d["document_id"] for d in results]
        self.assertIn("doc_lab_b01", doc_ids)
        self.assertNotIn("doc_lab_001", doc_ids)

    def test_no_owner_excluded(self):
        """เอกสารไม่มี owner_hash ต้องไม่แสดง."""
        sf = SearchFilter(keywords=[])
        results = smart_search(DEMO_DOCS, sf, OWNER_A)
        doc_ids = [d["document_id"] for d in results]
        self.assertNotIn("doc_noown_01", doc_ids)

    def test_keyword_filter(self):
        sf = SearchFilter(keywords=["BUN"])
        results = smart_search(DEMO_DOCS, sf, OWNER_A)
        self.assertTrue(len(results) > 0)
        for doc in results:
            self.assertIn("BUN", doc.get("extracted_text", "").upper())

    def test_multi_keyword_and(self):
        """หลายคำ = AND logic."""
        sf = SearchFilter(keywords=["BUN", "Creatinine"])
        results = smart_search(DEMO_DOCS, sf, OWNER_A)
        self.assertTrue(len(results) > 0)
        for doc in results:
            text = doc.get("extracted_text", "").lower()
            self.assertIn("bun", text)
            self.assertIn("creatinine", text)

    def test_category_filter(self):
        sf = SearchFilter(category="bills")
        results = smart_search(DEMO_DOCS, sf, OWNER_A)
        for doc in results:
            self.assertEqual(doc["category"], "bills")

    def test_category_and_keyword(self):
        sf = SearchFilter(keywords=["BUN"], category="lab")
        results = smart_search(DEMO_DOCS, sf, OWNER_A)
        self.assertTrue(len(results) > 0)
        for doc in results:
            self.assertEqual(doc["category"], "lab")
            self.assertIn("BUN", doc["extracted_text"])

    def test_date_filter_today(self):
        sf = SearchFilter(date_range_days=1)
        results = smart_search(DEMO_DOCS, sf, OWNER_A)
        for doc in results:
            self.assertEqual(doc["upload_date"], TODAY_STR)

    def test_date_filter_week(self):
        sf = SearchFilter(date_range_days=7)
        results = smart_search(DEMO_DOCS, sf, OWNER_A)
        cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        for doc in results:
            self.assertGreaterEqual(doc["upload_date"], cutoff)

    def test_date_filter_excludes_old(self):
        sf = SearchFilter(date_range_days=30)
        results = smart_search(DEMO_DOCS, sf, OWNER_A)
        doc_ids = [d["document_id"] for d in results]
        self.assertNotIn("doc_lab_003", doc_ids)  # 200 days old

    def test_max_results(self):
        sf = SearchFilter(keywords=[])
        results = smart_search(DEMO_DOCS, sf, OWNER_A, max_results=2)
        self.assertLessEqual(len(results), 2)

    def test_no_results(self):
        sf = SearchFilter(keywords=["nonexistent_xyz_123"])
        results = smart_search(DEMO_DOCS, sf, OWNER_A)
        self.assertEqual(len(results), 0)

    def test_empty_owner_hash_denied(self):
        sf = SearchFilter(keywords=[])
        results = smart_search(DEMO_DOCS, sf, "")
        self.assertEqual(len(results), 0)

    def test_relevance_scoring(self):
        """เอกสารที่ตรงคำค้นมากกว่าควรมาก่อน."""
        sf = SearchFilter(keywords=["BUN"])
        results = smart_search(DEMO_DOCS, sf, OWNER_A)
        # doc_lab_001 has "BUN 25 Creatinine 1.2 eGFR 65"
        # doc_lab_003 has "BUN 30 Creatinine 1.5"
        # Both match, both should appear
        self.assertTrue(len(results) >= 2)


# ═══════════════════════════════════════════════════════════
# 4. User Statistics Tests
# ═══════════════════════════════════════════════════════════

class TestUserStats(unittest.TestCase):
    """ทดสอบ compute_user_stats."""

    def _get_owner_a_docs(self):
        return [d for d in DEMO_DOCS if d.get("owner_hash") == OWNER_A]

    def test_total_count(self):
        docs = self._get_owner_a_docs()
        stats = compute_user_stats(docs)
        self.assertEqual(stats.total_documents, len(docs))

    def test_category_counts(self):
        docs = self._get_owner_a_docs()
        stats = compute_user_stats(docs)
        self.assertEqual(stats.category_counts.get("ผลตรวจแล็บ", 0), 3)
        self.assertEqual(stats.category_counts.get("ใบเสร็จ/บิล", 0), 1)
        self.assertEqual(stats.category_counts.get("ใบสั่งยา", 0), 1)
        self.assertEqual(stats.category_counts.get("เอกสารฟอกเลือด", 0), 1)

    def test_date_range(self):
        docs = self._get_owner_a_docs()
        stats = compute_user_stats(docs)
        self.assertEqual(stats.latest_upload_date, TODAY_STR)
        self.assertEqual(stats.earliest_upload_date, OLD_DATE_STR)

    def test_lab_counts(self):
        docs = self._get_owner_a_docs()
        stats = compute_user_stats(docs, lab_report_count=3,
                                   approved_count=1, pending_count=2)
        self.assertEqual(stats.lab_reports_count, 3)
        self.assertEqual(stats.approved_labs, 1)
        self.assertEqual(stats.pending_labs, 2)

    def test_empty_docs(self):
        stats = compute_user_stats([])
        self.assertEqual(stats.total_documents, 0)
        self.assertEqual(stats.category_counts, {})

    def test_text_output_has_docs(self):
        docs = self._get_owner_a_docs()
        stats = compute_user_stats(docs, lab_report_count=2)
        text = user_stats_text_th(stats)
        self.assertIn("📊", text)
        self.assertIn("เอกสารทั้งหมด", text)
        self.assertIn(str(len(docs)), text)

    def test_text_output_no_docs(self):
        stats = compute_user_stats([])
        text = user_stats_text_th(stats)
        self.assertIn("ยังไม่มีเอกสาร", text)

    def test_text_output_lab_info(self):
        docs = self._get_owner_a_docs()
        stats = compute_user_stats(docs, lab_report_count=3,
                                   approved_count=1, pending_count=2)
        text = user_stats_text_th(stats)
        self.assertIn("ผลแล็บที่ดึงได้: 3", text)
        self.assertIn("ยืนยันแล้ว: 1", text)
        self.assertIn("รอยืนยัน: 2", text)


# ═══════════════════════════════════════════════════════════
# 5. Help Text Tests
# ═══════════════════════════════════════════════════════════

class TestHelpText(unittest.TestCase):
    """ทดสอบ generate_help_text."""

    def test_regular_user_with_docs(self):
        text = generate_help_text(is_admin=False, has_docs=True)
        self.assertIn("ค้นหา", text)
        self.assertIn("ผลแล็บ", text)
        self.assertIn("ตรวจยา", text)
        self.assertNotIn("ผู้ดูแล", text)

    def test_admin_user(self):
        text = generate_help_text(is_admin=True, has_docs=True)
        self.assertIn("ผู้ดูแล", text)
        self.assertIn("ยืนยันผลแล็บ", text)
        self.assertIn("KPI", text)

    def test_new_user_no_docs(self):
        text = generate_help_text(is_admin=False, has_docs=False)
        self.assertIn("เริ่มต้นใช้งาน", text)
        self.assertIn("PDF", text)

    def test_tips_section(self):
        text = generate_help_text()
        self.assertIn("เคล็ดลับ", text)
        self.assertIn("ค้นหา แล็บ สัปดาห์นี้", text)


# ═══════════════════════════════════════════════════════════
# 6. Search Results Formatting Tests
# ═══════════════════════════════════════════════════════════

class TestFormatSearchResults(unittest.TestCase):
    """ทดสอบ format_search_results_enhanced."""

    def test_empty_results(self):
        text = format_search_results_enhanced([])
        self.assertIn("ไม่พบ", text)

    def test_empty_results_with_query(self):
        text = format_search_results_enhanced([], query="BUN")
        self.assertIn("ลอง", text)

    def test_results_have_emoji(self):
        results = [DEMO_DOCS[0]]  # lab doc
        text = format_search_results_enhanced(results)
        self.assertIn("🔬", text)

    def test_results_have_category_name(self):
        results = [DEMO_DOCS[3]]  # bills doc
        text = format_search_results_enhanced(results)
        self.assertIn("ใบเสร็จ/บิล", text)

    def test_results_have_doc_id(self):
        results = [DEMO_DOCS[0]]
        text = format_search_results_enhanced(results)
        self.assertIn("doc_lab_", text)

    def test_results_have_suggestions(self):
        results = [DEMO_DOCS[0]]
        text = format_search_results_enhanced(results)
        self.assertIn("อ่าน", text)
        self.assertIn("ขอไฟล์", text)

    def test_lab_results_have_lab_suggestion(self):
        results = [DEMO_DOCS[0]]  # lab
        text = format_search_results_enhanced(results)
        self.assertIn("ผลแล็บ", text)

    def test_pagination_header(self):
        # 6 docs, page_size=3
        owner_a_docs = [d for d in DEMO_DOCS if d["owner_hash"] == OWNER_A]
        text = format_search_results_enhanced(
            owner_a_docs, page=1, page_size=3
        )
        self.assertIn("หน้า 1/", text)

    def test_pagination_page2(self):
        owner_a_docs = [d for d in DEMO_DOCS if d["owner_hash"] == OWNER_A]
        text = format_search_results_enhanced(
            owner_a_docs, page=2, page_size=3
        )
        self.assertIn("หน้า 2/", text)

    def test_count_header(self):
        results = [DEMO_DOCS[0], DEMO_DOCS[1]]
        text = format_search_results_enhanced(results, query="BUN")
        self.assertIn("พบ 2 เอกสาร", text)
        self.assertIn("BUN", text)

    def test_text_preview(self):
        results = [DEMO_DOCS[0]]  # has extracted_text
        text = format_search_results_enhanced(results)
        self.assertIn("BUN", text)


# ═══════════════════════════════════════════════════════════
# 7. Latest Document Helper Tests
# ═══════════════════════════════════════════════════════════

class TestFindLatestDoc(unittest.TestCase):
    """ทดสอบ find_latest_doc_by_category / find_latest_lab_doc."""

    def test_latest_any(self):
        doc = find_latest_doc_by_category(DEMO_DOCS, OWNER_A)
        self.assertIsNotNone(doc)
        self.assertEqual(doc["upload_date"], TODAY_STR)

    def test_latest_lab(self):
        doc = find_latest_lab_doc(DEMO_DOCS, OWNER_A)
        self.assertIsNotNone(doc)
        self.assertEqual(doc["category"], "lab")
        self.assertEqual(doc["document_id"], "doc_lab_001")

    def test_latest_bills(self):
        doc = find_latest_doc_by_category(DEMO_DOCS, OWNER_A, category="bills")
        self.assertIsNotNone(doc)
        self.assertEqual(doc["category"], "bills")

    def test_latest_rx(self):
        doc = find_latest_doc_by_category(DEMO_DOCS, OWNER_A, category="rx")
        self.assertIsNotNone(doc)
        self.assertEqual(doc["category"], "rx")

    def test_owner_isolation(self):
        doc = find_latest_lab_doc(DEMO_DOCS, OWNER_A)
        self.assertNotEqual(doc["document_id"], "doc_lab_b01")

    def test_owner_b_latest(self):
        doc = find_latest_lab_doc(DEMO_DOCS, OWNER_B)
        self.assertEqual(doc["document_id"], "doc_lab_b01")

    def test_nonexistent_category(self):
        doc = find_latest_doc_by_category(DEMO_DOCS, OWNER_A, category="xray")
        self.assertIsNone(doc)

    def test_empty_owner(self):
        doc = find_latest_doc_by_category(DEMO_DOCS, "")
        self.assertIsNone(doc)


# ═══════════════════════════════════════════════════════════
# 8. Category Aliases & Constants Tests
# ═══════════════════════════════════════════════════════════

class TestConstants(unittest.TestCase):
    """ทดสอบ constants และ aliases."""

    def test_category_aliases_complete(self):
        categories = set(CATEGORY_ALIASES.values())
        self.assertIn("lab", categories)
        self.assertIn("bills", categories)
        self.assertIn("rx", categories)
        self.assertIn("dialysis", categories)

    def test_cat_emoji_complete(self):
        self.assertIn("lab", CAT_EMOJI)
        self.assertIn("bills", CAT_EMOJI)
        self.assertIn("rx", CAT_EMOJI)
        self.assertIn("dialysis", CAT_EMOJI)

    def test_cat_names_th_complete(self):
        self.assertIn("lab", CAT_NAMES_TH)
        self.assertIn("bills", CAT_NAMES_TH)
        self.assertIn("rx", CAT_NAMES_TH)
        self.assertIn("dialysis", CAT_NAMES_TH)

    def test_thai_aliases(self):
        self.assertEqual(CATEGORY_ALIASES["ผลเลือด"], "lab")
        self.assertEqual(CATEGORY_ALIASES["ใบเสร็จ"], "bills")
        self.assertEqual(CATEGORY_ALIASES["ฟอกไต"], "dialysis")
        self.assertEqual(CATEGORY_ALIASES["ยา"], "rx")


# ═══════════════════════════════════════════════════════════
# 9. Edge Cases
# ═══════════════════════════════════════════════════════════

class TestEdgeCases(unittest.TestCase):
    """ทดสอบ edge cases."""

    def test_search_case_insensitive(self):
        sf = SearchFilter(keywords=["bun"])
        results = smart_search(DEMO_DOCS, sf, OWNER_A)
        self.assertTrue(len(results) > 0)

    def test_search_empty_docs(self):
        sf = SearchFilter(keywords=["test"])
        results = smart_search([], sf, OWNER_A)
        self.assertEqual(len(results), 0)

    def test_parse_command_whitespace_only(self):
        r = parse_command("   ")
        self.assertEqual(r.command_type, CommandType.UNKNOWN)

    def test_search_filter_empty_query(self):
        sf = parse_search_query("")
        self.assertEqual(sf.keywords, [])
        self.assertIsNone(sf.category)

    def test_format_results_single_page(self):
        results = [DEMO_DOCS[0]]
        text = format_search_results_enhanced(results, page=1, page_size=5)
        self.assertNotIn("หน้า", text)  # single page = no pagination

    def test_pagination_clamp(self):
        """Page > total_pages ถูก clamp."""
        results = [DEMO_DOCS[0], DEMO_DOCS[1]]
        text = format_search_results_enhanced(results, page=99, page_size=5)
        self.assertIn("พบ 2", text)

    def test_doc_without_extracted_text(self):
        doc = make_doc("no_text", "lab", OWNER_A, TODAY_STR, "", "test.pdf")
        text = format_search_results_enhanced([doc])
        self.assertIn("ผลตรวจแล็บ", text)

    def test_date_range_invalid_date(self):
        """เอกสารที่วันที่ parse ไม่ได้ — ไม่ถูกกรองออก."""
        doc = make_doc("bad_date", "lab", OWNER_A, "not-a-date", "test")
        sf = SearchFilter(date_range_days=30)
        results = smart_search([doc], sf, OWNER_A)
        self.assertEqual(len(results), 1)

    def test_stats_no_dates(self):
        doc = make_doc("no_date", "lab", OWNER_A, "", "test")
        stats = compute_user_stats([doc])
        self.assertEqual(stats.earliest_upload_date, "")
        self.assertEqual(stats.latest_upload_date, "")

    def test_long_query_truncated_in_header(self):
        long_query = "a" * 100
        text = format_search_results_enhanced(
            [DEMO_DOCS[0]], query=long_query
        )
        # Query should be truncated to 30 chars
        self.assertNotIn("a" * 100, text)


if __name__ == "__main__":
    unittest.main()
