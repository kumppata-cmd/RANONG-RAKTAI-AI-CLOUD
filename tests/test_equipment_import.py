import unittest
from datetime import date

from hd_module.equipment_import import parse_rows, parse_thai_date, parse_year


class EquipmentImportTests(unittest.TestCase):
    def test_dates_and_years(self):
        self.assertEqual(parse_thai_date("12 เมษายน 2570"), date(2027, 4, 12))
        self.assertEqual(parse_thai_date("31 ธ.ค. 2568"), date(2025, 12, 31))
        self.assertEqual(parse_thai_date("13/7/2569"), date(2026, 7, 13))
        self.assertEqual(parse_thai_date("2026-08-31"), date(2026, 8, 31))
        self.assertIsNone(parse_thai_date("ว่าง"))
        self.assertEqual(parse_year("2567"), 2024)
        self.assertEqual(parse_year(2024), 2024)
        self.assertIsNone(parse_year(""))

    def test_parse_form_layout(self):
        raw = [["รายละเอียดของเครื่องไตเทียม"],
               ["เครื่องที่", "ยี่ห้อ/ผลิตภัณฑ์", "รุ่น", "รหัสเครื่อง", "ปีที่ซื้อ/เช่า", "สัญญาสิ้นสุด"],
               [1, "NIKKISO", "DBB-EXA", "J2450892", 2567, "12 เมษายน 2570"],
               [12, "ว่าง", "ว่าง", "ว่าง", "ว่าง", "ว่าง"],
               [19, "Fresineus", "4008-B", "2V5AVH40", "2556", "31 สิงหาคม 2569"],
               [22, "NIKKISO", "DBB-07", "J1805075", 2568, "วันที่ไม่รู้"]]
        rows, errors = parse_rows(raw)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["bed"], 1)
        self.assertEqual((rows[0]["brand"], rows[0]["serial"], rows[0]["purchase_year"], rows[0]["contract_end"]), ("NIKKISO", "J2450892", 2024, "2027-04-12"))
        self.assertEqual(rows[1]["brand"], "Fresenius")       # typo on the form corrected
        self.assertEqual(rows[1]["purchase_year"], 2013)
        self.assertIsNone(rows[2]["contract_end"])
        self.assertEqual(len(errors), 1)
        self.assertEqual(rows[0]["type"], "dialysis_machine")

    def test_no_header(self):
        rows, errors = parse_rows([["a", "b"], [1, 2]])
        self.assertEqual(rows, [])
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
