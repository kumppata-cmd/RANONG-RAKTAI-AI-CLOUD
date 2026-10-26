"""การแยกค่าผลแล็บจากรูปแบบเอกสารที่พบจริง

ที่มา: ตรวจ production เมื่อ 26 ต.ค. 2026 พบว่ามีเอกสารที่จัดเป็น lab
11 ใบ แต่มีไฟล์ใน _lab_results/ เพียง 2 ใบ — เครื่องตรวจจับค่าวิกฤต
จึงเคย "มองเห็น" ผลแล็บแค่ 2 ใบเท่านั้น ที่เหลือเก็บไว้แต่ไม่เคยถูก
แปลงเป็นค่าตัวเลข ระบบจึงไม่มีทางแจ้งเตือนได้ ไม่ว่าในนั้นจะมีค่า
วิกฤตหรือไม่

สาเหตุ: ทั้งสาม strategy เดิมบังคับว่าต้องมี "หน่วย" ต่อท้ายค่าเสมอ
วัดกับรูปแบบที่พบจริง 10 แบบ มี 5 แบบที่อ่านไม่ออกเลย

เทสต์ชุดนี้ล็อกไว้ว่าทั้ง 10 แบบต้องอ่านออก และหน่วยต้องไม่เพี้ยน
ข้อมูลทั้งหมดเป็นค่าสังเคราะห์ ไม่ใช่ของผู้ป่วยจริง
"""
import unittest

from lab_extractor import (
    extract_lab_results_from_text,
    _parse_generic_line,
    _clean_unit_token,
)


def _extract(text):
    rep = extract_lab_results_from_text(text, "testdoc", "regex_ocr")
    return {r.analyte: r for r in rep.results}


# ─────────────────────────────────────────────────────────────
# รูปแบบที่เคยอ่านไม่ออก
# ─────────────────────────────────────────────────────────────

class TestPreviouslyUnreadableFormats(unittest.TestCase):

    def test_no_unit_at_all(self):
        """ใบแล็บที่ไม่ระบุหน่วยเลย — เดิมได้ 0 ค่า"""
        got = _extract("Creatinine   5.2\nBUN          45\nPotassium    6.1")
        self.assertEqual(set(got), {"Creatinine", "BUN", "Potassium"})
        self.assertEqual(got["Creatinine"].value, 5.2)

    def test_unit_only_in_column_header(self):
        """หน่วยอยู่หัวคอลัมน์ ไม่ซ้ำในแต่ละแถว"""
        got = _extract(
            "Test          Result   Unit     Reference\n"
            "Creatinine    5.2               0.6-1.2\n"
            "BUN           45                7-20"
        )
        self.assertIn("Creatinine", got)
        self.assertEqual(got["Creatinine"].value, 5.2)

    def test_reference_range_in_parentheses(self):
        got = _extract("Creatinine  5.2  (0.6-1.2)\nBUN   45  (7-20)")
        self.assertEqual(set(got), {"Creatinine", "BUN"})
        self.assertEqual(got["BUN"].value, 45.0)

    def test_thai_name_without_unit(self):
        got = _extract("ครีเอตินิน   5.2\nบียูเอ็น     45")
        self.assertEqual(set(got), {"Creatinine", "BUN"})

    def test_equals_separator(self):
        got = _extract("Creatinine = 5.2 mg/dL\nBUN = 45 mg/dL")
        self.assertEqual(got["Creatinine"].value, 5.2)
        self.assertEqual(got["Creatinine"].unit, "mg/dL")

    def test_pipe_delimited_table(self):
        """ตารางคั่นด้วย | — พบบ่อยมากเมื่อ OCR อ่านเส้นตาราง"""
        got = _extract(
            "Creatinine | 5.2 | mg/dL | 0.6-1.2\nBUN | 45 | mg/dL | 7-20"
        )
        self.assertEqual(set(got), {"Creatinine", "BUN"})
        self.assertEqual(got["Creatinine"].unit, "mg/dL")

    def test_vertical_layout_value_and_unit_on_one_line(self):
        """ชื่ออยู่บรรทัดหนึ่ง ค่ากับหน่วยอยู่บรรทัดถัดไป"""
        got = _extract("Creatinine\n5.2 mg/dL\nBUN\n45 mg/dL")
        self.assertEqual(set(got), {"Creatinine", "BUN"})
        self.assertEqual(got["Creatinine"].value, 5.2)
        self.assertEqual(got["Creatinine"].unit, "mg/dL")


# ─────────────────────────────────────────────────────────────
# รูปแบบที่เคยอ่านได้อยู่แล้ว — ต้องไม่พัง
# ─────────────────────────────────────────────────────────────

class TestFormatsThatAlreadyWorked(unittest.TestCase):

    def test_standard_tabular_with_units(self):
        got = _extract(
            "Creatinine   5.2   mg/dL   0.6-1.2\n"
            "BUN          45    mg/dL   7-20\n"
            "Potassium    6.1   mmol/L  3.5-5.1"
        )
        self.assertEqual(set(got), {"Creatinine", "BUN", "Potassium"})
        self.assertEqual(got["Potassium"].unit, "mmol/L")

    def test_thai_with_units(self):
        got = _extract("ครีเอตินิน   5.2   mg/dL   0.6-1.2")
        self.assertEqual(got["Creatinine"].unit, "mg/dL")

    def test_english_colon_format(self):
        got = _extract("Creatinine: 5.2 mg/dL 0.6-1.2")
        self.assertEqual(got["Creatinine"].value, 5.2)


# ─────────────────────────────────────────────────────────────
# คุณภาพของหน่วย — หน่วยผิดมีผลต่อการตรวจความสมเหตุสมผลและข้อความแจ้งเตือน
# ─────────────────────────────────────────────────────────────

class TestUnitQuality(unittest.TestCase):

    def test_lab_flag_is_not_mistaken_for_unit(self):
        """H/L ที่ห้องแล็บใส่ไว้ข้างค่า ต้องไม่กลายเป็นหน่วย

        และหน่วยจริงที่อยู่ถัดจาก flag ต้องไม่ตกหล่น
        """
        got = _extract(
            "Creatinine   5.2  H   mg/dL   0.6-1.2\n"
            "Potassium    6.1  H   mmol/L  3.5-5.1"
        )
        self.assertEqual(got["Creatinine"].unit, "mg/dL")
        self.assertEqual(got["Potassium"].unit, "mmol/L")

    def test_reference_number_is_not_mistaken_for_unit(self):
        """ตัวเลขจากช่วงอ้างอิงต้องไม่กลายเป็นหน่วย"""
        got = _extract(
            "Creatinine    5.2               0.6-1.2\n"
            "BUN           45                7-20"
        )
        for r in got.values():
            self.assertFalse(
                r.unit.replace(".", "").isdigit(),
                f"{r.analyte} ได้หน่วยเป็นตัวเลข: {r.unit!r}",
            )

    def test_clean_unit_token_rejects_non_units(self):
        for bad in ("5", "0.6", "7-20", "<200", "H", "L", "HH", "High", ""):
            with self.subTest(token=bad):
                self.assertEqual(_clean_unit_token(bad), "")
        for good in ("mg/dL", "mmol/L", "g/dL", "%", "mL/min/1.73m^2"):
            with self.subTest(token=good):
                self.assertEqual(_clean_unit_token(good), good)

    def test_missing_unit_lowers_confidence(self):
        """ไม่มีหน่วยให้ตรวจ = มั่นใจน้อยลง ควรให้คนดูซ้ำ"""
        with_unit = _extract("Creatinine   5.2   mg/dL   0.6-1.2")
        without = _extract("Creatinine   5.2")
        self.assertLess(
            without["Creatinine"].confidence,
            with_unit["Creatinine"].confidence,
        )


# ─────────────────────────────────────────────────────────────
# ต้องไม่สร้างผลตรวจจากข้อความที่ไม่ใช่ผลตรวจ
# ─────────────────────────────────────────────────────────────

class TestNoFalsePositives(unittest.TestCase):

    NOT_LAB = (
        "ใบเสร็จรับเงิน เลขที่ 12345",
        "ชื่อผู้ป่วย สมชาย ใจดี อายุ 65 ปี",
        "โรงพยาบาลระนอง โทร 077 812 630",
        "รวมเป็นเงิน 1500 บาท",
        "หน้า 1 จาก 3",
        "HN 57042",
        "วันที่ 14 ตุลาคม 2569",
        "Invoice No IV2610000440",
        "ห้อง 2 เตียง 15",
    )

    def test_non_lab_lines_produce_nothing(self):
        for line in self.NOT_LAB:
            with self.subTest(line=line):
                self.assertEqual(
                    _extract(line), {}, f"ไม่ควรอ่านเป็นผลตรวจ: {line!r}"
                )

    def test_generic_parser_requires_known_analyte(self):
        """ชื่อที่ไม่อยู่ในรายการ analyte ต้องไม่ถูกรับ"""
        for line in ("Widget 5.2 mg/dL", "ค่าบริการ 500 บาท",
                     "Temperature 36.5 C"):
            with self.subTest(line=line):
                self.assertIsNone(_parse_generic_line(line))

    def test_whole_bill_document_yields_no_lab_results(self):
        bill = (
            "NIKKISO MEDICAL (THAILAND) CO., LTD.\n"
            "ใบกำกับภาษี เลขที่ IV2610000440\n"
            "รายการ: HEMODIALYSIS PACKAGE 12 ชุด\n"
            "ราคาต่อหน่วย 3750 บาท\n"
            "รวมทั้งสิ้น 45000 บาท\n"
        )
        self.assertEqual(_extract(bill), {})


# ─────────────────────────────────────────────────────────────
# ค่าวิกฤตต้องยังถูกตรวจจับได้หลังแก้
# ─────────────────────────────────────────────────────────────

class TestCriticalValuesStillDetected(unittest.TestCase):

    def test_critical_potassium_without_unit_is_still_flagged(self):
        """ค่าวิกฤตในใบที่ไม่มีหน่วย — เดิมอ่านไม่ออกจึงไม่มีทางแจ้งเตือน"""
        rep = extract_lab_results_from_text(
            "Potassium   7.2", "d", "regex_ocr"
        )
        self.assertEqual(len(rep.results), 1)
        r = rep.results[0]
        self.assertEqual(r.analyte, "Potassium")
        self.assertEqual(r.value, 7.2)
        self.assertTrue(
            any("critical" in f for f in r.flags),
            f"ค่า K 7.2 ต้องถูกตั้งธงว่าวิกฤต ได้: {r.flags}",
        )

    def test_report_warns_about_critical_values(self):
        rep = extract_lab_results_from_text(
            "Potassium | 7.2 | mmol/L", "d", "regex_ocr"
        )
        self.assertTrue(
            any("วิกฤต" in w for w in rep.warnings),
            f"ต้องมีคำเตือนค่าวิกฤต ได้: {rep.warnings}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ═══════════════════════════════════════════════════════════
# Endpoint อ่านค่าผลแล็บจากเอกสารเดิมซ้ำ
# ═══════════════════════════════════════════════════════════

import os  # noqa: E402

_TEST_SECRET = "test-admin-secret-lab-reextract"


def _server():
    import server as _s
    return _s


class ReextractFixture(unittest.TestCase):
    """index จำลอง + ที่เก็บข้อมูลจำลอง — ไม่แตะ GCS จริง"""

    # ใบที่เดิมอ่านไม่ออก (ไม่มีหน่วย) และมีค่าวิกฤต
    CRITICAL_TEXT = "Potassium   7.2\nCreatinine  5.2"
    # ใบที่อ่านออกและค่าปกติ
    NORMAL_TEXT = "Creatinine | 1.0 | mg/dL | 0.6-1.2"

    def setUp(self):
        self.s = _server()
        self._secret = os.environ.get("ADMIN_SECRET")
        os.environ["ADMIN_SECRET"] = _TEST_SECRET
        self._docs = list(self.s.doc_index._docs)
        self._fresh = self.s.doc_index._ensure_fresh
        self.s.doc_index._ensure_fresh = lambda: None

        self.s.doc_index._docs = [
            {"document_id": "aaaa1111" + "0" * 8, "category": "lab",
             "upload_date": "2026-10-20", "ocr_status": "ocr",
             "extracted_text": self.CRITICAL_TEXT},
            {"document_id": "bbbb2222" + "0" * 8, "category": "lab",
             "upload_date": "2026-10-21", "ocr_status": "native",
             "extracted_text": self.NORMAL_TEXT},
            {"document_id": "cccc3333" + "0" * 8, "category": "lab",
             "upload_date": "2026-10-22", "ocr_status": "ocr",
             "extracted_text": ""},                      # ไม่มีข้อความ
            {"document_id": "dddd4444" + "0" * 8, "category": "lab",
             "upload_date": "2026-10-23", "ocr_status": "ocr",
             "extracted_text": "ใบเสร็จรับเงิน 1500 บาท"},  # ไม่ใช่ผลแล็บ
            {"document_id": "eeee5555" + "0" * 8, "category": "bills",
             "upload_date": "2026-10-24", "ocr_status": "ocr",
             "extracted_text": self.CRITICAL_TEXT},       # คนละหมวด
        ]

        self.written = {}
        self._put = self.s.storage.put
        self._get = self.s.storage.get
        self.s.storage.put = lambda path, data, **kw: self.written.__setitem__(
            path, data
        )
        self.s.storage.get = lambda path: None  # ยังไม่มีผลแล็บเดิมเลย
        self.client = self.s.app.test_client()
        self.headers = {"X-Admin-Secret": _TEST_SECRET}

    def tearDown(self):
        self.s.doc_index._docs = self._docs
        self.s.doc_index._ensure_fresh = self._fresh
        self.s.storage.put = self._put
        self.s.storage.get = self._get
        if self._secret is None:
            os.environ.pop("ADMIN_SECRET", None)
        else:
            os.environ["ADMIN_SECRET"] = self._secret

    def _call(self, qs=""):
        r = self.client.get(
            "/admin/api/v2/labs/reextract" + qs, headers=self.headers
        )
        self.assertEqual(r.status_code, 200, r.data[:200])
        return r.get_json()


class TestReextractDryRun(ReextractFixture):

    def test_requires_admin_auth(self):
        r = self.client.get("/admin/api/v2/labs/reextract")
        self.assertEqual(r.status_code, 401)

    def test_query_string_secret_is_not_accepted(self):
        r = self.client.get(
            f"/admin/api/v2/labs/reextract?secret={_TEST_SECRET}"
        )
        self.assertEqual(r.status_code, 401)

    def test_dry_run_is_the_default_and_writes_nothing(self):
        d = self._call()
        self.assertTrue(d["dry_run"])
        self.assertFalse(d["applied"])
        self.assertEqual(self.written, {}, "dry-run ต้องไม่เขียนอะไรเลย")
        for doc in d["documents"]:
            self.assertFalse(doc["written"])

    def test_finds_documents_that_are_now_readable(self):
        d = self._call()
        ids = {doc["document_id"] for doc in d["documents"]}
        self.assertIn("aaaa1111", ids)
        self.assertIn("bbbb2222", ids)

    def test_counts_documents_with_critical_values(self):
        d = self._call()
        self.assertGreaterEqual(d["documents_with_critical_values"], 1)
        crit = [x for x in d["documents"] if x["document_id"] == "aaaa1111"]
        self.assertEqual(len(crit), 1)
        self.assertGreaterEqual(crit[0]["critical"], 1)

    def test_document_without_text_is_counted_separately(self):
        d = self._call()
        self.assertEqual(d["no_text_available"], 1)

    def test_non_lab_text_is_reported_as_still_unreadable(self):
        d = self._call()
        self.assertEqual(d["still_unreadable"], 1)
        ids = {doc["document_id"] for doc in d["documents"]}
        self.assertNotIn("dddd4444", ids)

    def test_other_categories_are_not_touched_by_default(self):
        d = self._call()
        ids = {doc["document_id"] for doc in d["documents"]}
        self.assertNotIn("eeee5555", ids)
        self.assertEqual(d["categories"], ["lab"])

    def test_include_review_widens_the_scan(self):
        d = self._call("?include_review=1")
        self.assertEqual(d["categories"], ["lab", "review"])

    def test_response_contains_no_patient_data(self):
        """ผลลัพธ์ต้องส่งต่อให้คนอื่นดูได้ — ห้ามมีค่าผลตรวจหรือชื่อรายการ"""
        import json as _json
        blob = _json.dumps(self._call(), ensure_ascii=False).lower()
        for leak in ("potassium", "creatinine", "7.2", "5.2",
                     "ใบเสร็จ", "extracted_text"):
            self.assertNotIn(leak, blob, f"พบข้อมูลที่ไม่ควรมี: {leak}")

    def test_document_ids_are_truncated(self):
        for doc in self._call()["documents"]:
            self.assertEqual(len(doc["document_id"]), 8)


class TestReextractApply(ReextractFixture):

    def test_apply_writes_lab_results(self):
        d = self._call("?apply=1")
        self.assertFalse(d["dry_run"])
        self.assertTrue(d["applied"])
        self.assertEqual(len(self.written), d["newly_readable"])
        for path in self.written:
            self.assertTrue(path.startswith(self.s.LAB_RESULTS_PREFIX))

    def test_apply_marks_each_document_written(self):
        d = self._call("?apply=1")
        self.assertTrue(all(doc["written"] for doc in d["documents"]))

    def test_documents_that_already_have_results_are_skipped(self):
        self.s.storage.get = lambda path: b"{}"   # มีผลแล็บอยู่แล้วทุกใบ
        d = self._call("?apply=1")
        self.assertEqual(d["newly_readable"], 0)
        self.assertEqual(self.written, {}, "ต้องไม่เขียนทับของเดิม")
        self.assertGreater(d["already_extracted"], 0)

    def test_write_failure_is_reported_not_swallowed(self):
        def boom(*a, **k):
            raise RuntimeError("GCS down")
        self.s.storage.put = boom
        d = self._call("?apply=1")
        self.assertGreater(d["write_failed"], 0)
        self.assertTrue(all(not doc["written"] for doc in d["documents"]))


class TestUnreadableDiagnostics(ReextractFixture):
    """สัญญาณวินิจฉัยเอกสารที่อ่านค่าไม่ออก — ต้องไม่มีข้อมูลผู้ป่วย"""

    def test_unreadable_documents_are_listed(self):
        d = self._call()
        ids = {u["document_id"] for u in d["unreadable_documents"]}
        self.assertIn("dddd4444", ids)   # "ใบเสร็จรับเงิน 1500 บาท"

    def test_non_lab_document_shows_no_analyte_names(self):
        """ไม่พบชื่อรายการตรวจเลย = น่าจะไม่ใช่ใบแล็บตั้งแต่แรก"""
        d = self._call()
        entry = next(
            u for u in d["unreadable_documents"]
            if u["document_id"] == "dddd4444"
        )
        self.assertEqual(entry["analyte_names_found"], 0)
        self.assertGreater(entry["text_length"], 0)
        self.assertGreater(entry["numbers_found"], 0)

    def test_lab_like_document_shows_analyte_names(self):
        """พบชื่อรายการตรวจแต่อ่านค่าไม่ได้ = ใบแล็บรูปแบบที่ยังไม่รองรับ"""
        self.s.doc_index._docs.append({
            "document_id": "ffff6666" + "0" * 8, "category": "lab",
            "upload_date": "2026-10-25", "ocr_status": "ocr",
            # มีชื่อรายการตรวจ แต่ค่าถูกตัดขาดจนอ่านไม่ได้
            "extracted_text": "Creatinine ......\nPotassium ......",
        })
        d = self._call()
        entry = next(
            u for u in d["unreadable_documents"]
            if u["document_id"] == "ffff6666"
        )
        self.assertGreaterEqual(entry["analyte_names_found"], 2)

    def test_diagnostics_contain_no_patient_data(self):
        import json as _json
        blob = _json.dumps(
            self._call()["unreadable_documents"], ensure_ascii=False
        ).lower()
        for leak in ("ใบเสร็จ", "บาท", "creatinine", "potassium", "1500"):
            self.assertNotIn(leak, blob, f"พบข้อมูลที่ไม่ควรมี: {leak}")

    def test_counts_match_the_listing(self):
        d = self._call()
        self.assertEqual(d["still_unreadable"], len(d["unreadable_documents"]))


# ═══════════════════════════════════════════════════════════
# รูปแบบคอลัมน์ — ชื่อทั้งชุดก่อน แล้วค่าทั้งชุด
#
# pypdf อ่าน PDF ที่จัดเป็นคอลัมน์แบบนี้ และพบจริงบน production:
# เอกสาร 2 ใบที่พบชื่อรายการตรวจ 11 ชนิดแต่อ่านค่าไม่ได้เลย
# ═══════════════════════════════════════════════════════════

ELEVEN = ["Creatinine", "BUN", "Potassium", "Sodium", "Chloride",
          "Calcium", "Phosphate", "Albumin", "Hemoglobin",
          "Hematocrit", "eGFR"]
ELEVEN_VALUES = [5.2, 45.0, 6.1, 138.0, 102.0, 8.9, 5.4, 3.2, 9.1, 28.0, 12.0]


class TestColumnMajorLayout(unittest.TestCase):

    def _column_text(self, names, values):
        return "Laboratory Report\n" + "\n".join(names) + "\n" + "\n".join(
            str(v) for v in values
        ) + "\n"

    def test_all_values_pair_with_the_right_analyte(self):
        got = _extract(self._column_text(ELEVEN, ELEVEN_VALUES))
        self.assertEqual(len(got), 11)
        for name, expected in zip(ELEVEN, ELEVEN_VALUES):
            with self.subTest(analyte=name):
                self.assertEqual(got[name].value, expected)

    def test_last_analyte_is_not_paired_with_the_first_value(self):
        """กรณีที่พบจริงและอันตรายที่สุด

        ตรรกะ lookahead เดิมจับชื่อตัวสุดท้ายของชุด (eGFR) คู่กับค่าตัวแรก
        ของอีกชุด (5.2 ซึ่งเป็นของ Creatinine) ได้ "eGFR = 5.2"
        ซึ่งอ่านว่าไตวายระยะสุดท้าย — แจ้งเตือนเท็จที่อันตราย
        และยังบังไม่ให้ค่า eGFR จริงถูกอ่าน
        """
        got = _extract(self._column_text(ELEVEN, ELEVEN_VALUES))
        self.assertEqual(got["eGFR"].value, 12.0)
        self.assertNotEqual(got["eGFR"].value, 5.2)

    def test_mismatched_counts_produce_nothing(self):
        """จำนวนชื่อไม่เท่าจำนวนค่า → ไม่จับคู่เลย

        ถ้าจับคู่ทั้งที่จำนวนไม่ตรง ค่าจะเลื่อนทั้งแถวและผิดทุกตัว
        ในงานนี้ 'ไม่ได้ค่า' ปลอดภัยกว่า 'ได้ค่าผิด'
        """
        got = _extract(self._column_text(ELEVEN[:4], [5.2, 45.0, 6.1]))
        self.assertEqual(got, {})

    def test_mismatch_is_reported_as_a_warning(self):
        rep = extract_lab_results_from_text(
            self._column_text(ELEVEN[:4], [5.2, 45.0, 6.1]),
            "d", "regex_pdf_text",
        )
        self.assertTrue(
            any("คอลัมน์" in w for w in rep.warnings),
            f"ต้องเตือนว่าจำนวนไม่ตรง ได้: {rep.warnings}",
        )

    def test_column_values_carry_lower_confidence(self):
        """จับคู่ตามลำดับเชื่อถือได้น้อยกว่าชื่อกับค่าบนบรรทัดเดียวกัน"""
        col = _extract(self._column_text(ELEVEN, ELEVEN_VALUES))
        inline = _extract("Creatinine   5.2   mg/dL   0.6-1.2")
        self.assertLess(
            col["Creatinine"].confidence, inline["Creatinine"].confidence
        )

    def test_short_runs_are_not_treated_as_columns(self):
        """ชื่อติดกันแค่ 2 รายการ ไม่นับเป็นรูปแบบคอลัมน์

        กันเอกสารทั่วไปที่บังเอิญมีชื่อรายการตรวจติดกันสองบรรทัด
        """
        got = _extract("Creatinine\nBUN\n5.2\n45\n")
        self.assertNotIn(
            "(column-major)", [r.raw_text for r in got.values()],
            "ชื่อติดกันแค่ 2 รายการไม่ควรถูกจับคู่แบบคอลัมน์",
        )

    def test_column_layout_with_units_on_value_lines(self):
        text = (
            "Creatinine\nBUN\nPotassium\n"
            "5.2 mg/dL\n45 mg/dL\n6.1 mmol/L\n"
        )
        got = _extract(text)
        self.assertEqual(got["Creatinine"].value, 5.2)
        self.assertEqual(got["Potassium"].unit, "mmol/L")

    def test_critical_value_in_column_layout_is_flagged(self):
        got = _extract(self._column_text(
            ["Creatinine", "BUN", "Potassium"], [1.0, 15.0, 7.4]
        ))
        self.assertTrue(
            any("critical" in f for f in got["Potassium"].flags),
            f"K 7.4 ต้องถูกตั้งธงวิกฤต ได้: {got['Potassium'].flags}",
        )


class TestLineShapeDiagnostic(unittest.TestCase):
    """ตัวนับรูปร่างบรรทัด — ใช้ระบุรูปแบบการจัดวางโดยไม่เปิดเผยเนื้อหา"""

    def setUp(self):
        self.shape = _server()._line_shape

    def test_inline_table_counts_name_with_number(self):
        s = self.shape(
            "Creatinine 5.2 mg/dL\nBUN 45 mg/dL\nPotassium 6.1 mmol/L"
        )
        self.assertEqual(s["lines_name_with_number"], 3)
        self.assertEqual(s["lines_name_only"], 0)

    def test_column_layout_counts_name_only_and_number_only(self):
        s = self.shape("Creatinine\nBUN\nPotassium\n5.2\n45\n6.1")
        self.assertEqual(s["lines_name_only"], 3)
        self.assertEqual(s["lines_number_only"], 3)
        self.assertEqual(s["max_consecutive_name_lines"], 3)

    def test_non_lab_text_counts_as_other(self):
        s = self.shape("ใบเสร็จรับเงิน\nขอบคุณที่ใช้บริการ")
        self.assertEqual(s["lines_name_with_number"], 0)
        self.assertEqual(s["lines_name_only"], 0)
        self.assertGreater(s["lines_other"], 0)

    def test_shape_contains_no_document_text(self):
        import json as _json
        blob = _json.dumps(
            self.shape("Creatinine 5.2 mg/dL\nชื่อผู้ป่วย สมชาย"),
            ensure_ascii=False,
        )
        for leak in ("Creatinine", "สมชาย", "5.2"):
            self.assertNotIn(leak, blob)

    def test_all_counters_are_integers(self):
        for v in self.shape("Creatinine 5.2").values():
            self.assertIsInstance(v, int)


class TestMaskedLinePatterns(unittest.TestCase):
    """รูปแบบบรรทัดแบบปิดบัง — ต้องบอกโครงสร้างโดยไม่เผยเนื้อหา"""

    def setUp(self):
        self.mask = _server()._masked_line_patterns

    def test_structure_is_preserved(self):
        got = self.mask("Creatinine    9.9   mg/dL   (0.6-1.2)")
        self.assertEqual(got, ["xxxxxxxxxx    9.9   xx/xx   (9.9-9.9)"])

    def test_no_letters_or_digits_survive(self):
        text = (
            "ชื่อผู้ป่วย สมชาย ใจดี อายุ 65 ปี\n"
            "HN 57042 โรงพยาบาลระนอง\n"
            "Creatinine 5.2 mg/dL\n"
        )
        for line in self.mask(text):
            for ch in line:
                self.assertFalse(
                    ch.isalpha() and ch != "x",
                    f"ตัวอักษรหลุดออกมา: {ch!r} ใน {line!r}",
                )
                self.assertFalse(
                    ch.isdigit() and ch != "9",
                    f"ตัวเลขหลุดออกมา: {ch!r} ใน {line!r}",
                )

    def test_thai_vowels_and_tone_marks_are_masked(self):
        """สระและวรรณยุกต์ไทยต้องถูกปิดบังด้วย ไม่ใช่เหลือไว้"""
        line = self.mask("ครีเอตินิน 5.2")[0]
        for mark in "ีเิ":
            self.assertNotIn(mark, line)

    def test_blank_lines_are_skipped(self):
        self.assertEqual(self.mask("a 1\n\n\nb 2"), ["x 9", "x 9"])

    def test_limit_is_respected(self):
        text = "\n".join(f"Creatinine {i}.0 mg/dL" for i in range(40))
        self.assertLessEqual(len(self.mask(text, limit=5)), 5)

    def test_empty_text_gives_empty_list(self):
        self.assertEqual(self.mask(""), [])
        self.assertEqual(self.mask("   \n  \n"), [])
