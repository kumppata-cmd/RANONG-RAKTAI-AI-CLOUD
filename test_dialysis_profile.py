"""ข้อมูลไตเทียม — ตารางฟอก, ยา ESA, vascular access (2026-11-05)

ทดสอบ:
  1. ESA code mapping (E4, H4, HM, hypercrit)
  2. Schedule parsing (จ/พ/ศ → ['mon','wed','fri'])
  3. MasterPatient dialysis fields serialization
  4. import_dialysis_batch — new + update
  5. get_patients_by_day
  6. get_dialysis_summary
  7. parse_dialysis_excel (อ่าน xlsx จริง ถ้ามี)

ข้อมูลทั้งหมดเป็นค่าสังเคราะห์ · import แบบ lazy
"""
import json
import os
import unittest


def _registry_mod():
    import patient_master_registry as _m
    return _m


class TestESACodeMap(unittest.TestCase):

    def test_e4_is_epiao(self):
        m = _registry_mod()
        info = m.resolve_esa_code("E4")
        self.assertEqual(info["name"], "Epiao")
        self.assertEqual(info["dose"], 4000)

    def test_h4_is_hemax(self):
        m = _registry_mod()
        info = m.resolve_esa_code("H4")
        self.assertEqual(info["name"], "Hemax")
        self.assertEqual(info["dose"], 4000)

    def test_hm_is_hema_plus(self):
        m = _registry_mod()
        for code in ("HM", "HM4"):
            info = m.resolve_esa_code(code)
            self.assertEqual(info["name"], "Hema-plus")
            self.assertEqual(info["dose"], 4000)

    def test_hypercrit(self):
        m = _registry_mod()
        info = m.resolve_esa_code("hypercrit")
        self.assertIn("Hypercrit", info["name"])

    def test_unknown_code_returns_as_name(self):
        m = _registry_mod()
        info = m.resolve_esa_code("UNKNOWN_DRUG_X")
        self.assertEqual(info["name"], "UNKNOWN_DRUG_X")
        self.assertEqual(info["dose"], 0)

    def test_empty_returns_empty(self):
        m = _registry_mod()
        self.assertEqual(m.resolve_esa_code(""), {})


class TestScheduleParsing(unittest.TestCase):

    def test_mon_wed_fri(self):
        m = _registry_mod()
        self.assertEqual(m.parse_schedule_text("จ/พ/ศ"), ["mon", "wed", "fri"])

    def test_tue_thu_sat(self):
        m = _registry_mod()
        self.assertEqual(m.parse_schedule_text("อ/พฤ/ส"), ["tue", "thu", "sat"])

    def test_two_days(self):
        m = _registry_mod()
        self.assertEqual(m.parse_schedule_text("อ/ศ"), ["tue", "fri"])

    def test_single_day(self):
        m = _registry_mod()
        self.assertEqual(m.parse_schedule_text("ส"), ["sat"])

    def test_empty(self):
        m = _registry_mod()
        self.assertEqual(m.parse_schedule_text(""), [])
        self.assertEqual(m.parse_schedule_text(None), [])

    def test_mixed_format(self):
        m = _registry_mod()
        result = m.parse_schedule_text("จ/พฤ/ศ")
        self.assertEqual(result, ["mon", "thu", "fri"])


class TestMasterPatientDialysisFields(unittest.TestCase):

    def test_to_dict_includes_dialysis_when_set(self):
        m = _registry_mod()
        p = m.MasterPatient(
            clinic_hn="56001",
            patient_name="สมชาย ใจดี",
            dialysis_schedule="จ/พ/ศ",
            dialysis_days=["mon", "wed", "fri"],
            hd_per_week=3,
            esa_code="E4",
            esa_medication="Epiao",
            esa_dose=4000,
            vascular_access="AVF",
        )
        d = p.to_dict()
        self.assertEqual(d["dialysis_schedule"], "จ/พ/ศ")
        self.assertEqual(d["dialysis_days"], ["mon", "wed", "fri"])
        self.assertEqual(d["hd_per_week"], 3)
        self.assertEqual(d["esa_code"], "E4")
        self.assertEqual(d["esa_medication"], "Epiao")
        self.assertEqual(d["esa_dose"], 4000)
        self.assertEqual(d["vascular_access"], "AVF")

    def test_to_dict_omits_empty_dialysis_fields(self):
        m = _registry_mod()
        p = m.MasterPatient(
            clinic_hn="56001",
            patient_name="สมชาย ใจดี",
        )
        d = p.to_dict()
        self.assertNotIn("dialysis_schedule", d)
        self.assertNotIn("esa_code", d)
        self.assertNotIn("vascular_access", d)

    def test_from_dict_roundtrip(self):
        m = _registry_mod()
        original = {
            "clinic_hn": "63203",
            "hospital_hn": "63895",
            "patient_name": "วินัย มากนคร",
            "registered_at": "2026-11-05T00:00:00+00:00",
            "updated_at": "2026-11-05T00:00:00+00:00",
            "registered_by": "excel_import",
            "dialysis_schedule": "จ/พ/ศ",
            "dialysis_days": ["mon", "wed", "fri"],
            "hd_per_week": 3,
            "esa_code": "HM",
            "esa_medication": "Hema-plus",
            "esa_dose": 4000,
            "vascular_access": "AVG",
        }
        p = m.MasterPatient.from_dict(original)
        self.assertEqual(p.clinic_hn, "63203")
        self.assertEqual(p.dialysis_schedule, "จ/พ/ศ")
        self.assertEqual(p.esa_code, "HM")
        self.assertEqual(p.vascular_access, "AVG")
        # Roundtrip
        d = p.to_dict()
        self.assertEqual(d["esa_medication"], "Hema-plus")
        self.assertEqual(d["esa_dose"], 4000)

    def test_from_dict_ignores_unknown_keys(self):
        m = _registry_mod()
        p = m.MasterPatient.from_dict({
            "clinic_hn": "56001",
            "unknown_field_xyz": "should be ignored",
        })
        self.assertEqual(p.clinic_hn, "56001")
        self.assertFalse(hasattr(p, "unknown_field_xyz"))


class _FakeBucket:
    def __init__(self):
        self.objects = {}
    def blob(self, name):
        return _FakeBlob(name, self)
    def list_blobs(self, prefix="", max_results=None):
        import time
        blobs = []
        for n in sorted(self.objects):
            if n.startswith(prefix):
                blobs.append(_FakeBlob(n, self))
        return blobs


class _FakeBlob:
    def __init__(self, name, bucket):
        self.name = name
        self._bucket = bucket
        import datetime as dt
        self.time_created = dt.datetime(2026, 11, 5, 10, 0, tzinfo=dt.timezone.utc)
    def exists(self):
        return self.name in self._bucket.objects
    def download_as_text(self, encoding="utf-8"):
        return self._bucket.objects[self.name].decode()
    def upload_from_string(self, data, content_type=None, if_generation_match=None):
        self._bucket.objects[self.name] = data if isinstance(data, bytes) else data.encode()


class _FakeStorage:
    is_ready = True
    bucket_name = "test-bkt"
    def __init__(self, bucket):
        self._bucket = bucket
        self._client = None
    def blob(self, name):
        return self._bucket.blob(name)
    def list_blobs(self, prefix="", max_results=None):
        return self._bucket.list_blobs(prefix, max_results)


class TestImportDialysisBatch(unittest.TestCase):

    def setUp(self):
        self.m = _registry_mod()
        self.bucket = _FakeBucket()
        self.reg = self.m.MasterPatientRegistry(storage=_FakeStorage(self.bucket))
        self.reg._loaded = True  # skip GCS load

    def test_import_new_patients(self):
        new_c, upd_c, skip_c, msgs = self.reg.import_dialysis_batch([
            {
                "clinic_hn": "56001",
                "hospital_hn": "7567",
                "patient_name": "นาย ปิยะ อะนะบุหรง",
                "dialysis_schedule": "จ/พ/ศ",
                "hd_per_week": 3,
                "esa_code": "E4",
            },
            {
                "clinic_hn": "63203",
                "hospital_hn": "63895",
                "patient_name": "นาย วินัย มากนคร",
                "dialysis_schedule": "จ/พ/ศ",
                "hd_per_week": 3,
                "esa_code": "HM",
                "vascular_access": "AVF",
            },
        ])
        self.assertEqual(new_c, 2)
        self.assertEqual(upd_c, 0)

        # ตรวจข้อมูล ESA
        p1 = self.reg.match_hn("56001")
        self.assertIsNotNone(p1)
        self.assertEqual(p1.esa_medication, "Epiao")
        self.assertEqual(p1.esa_dose, 4000)
        self.assertEqual(p1.dialysis_days, ["mon", "wed", "fri"])

        p2 = self.reg.match_hn("63203")
        self.assertIsNotNone(p2)
        self.assertEqual(p2.esa_medication, "Hema-plus")
        self.assertEqual(p2.vascular_access, "AVF")

    def test_import_updates_existing(self):
        # ลงทะเบียนก่อน
        self.reg.import_dialysis_batch([{
            "clinic_hn": "56001",
            "patient_name": "ปิยะ",
            "dialysis_schedule": "จ/พ/ศ",
            "hd_per_week": 3,
            "esa_code": "E4",
        }])

        # import อีกครั้งด้วยข้อมูลเพิ่ม
        new_c, upd_c, _, msgs = self.reg.import_dialysis_batch([{
            "clinic_hn": "56001",
            "patient_name": "ปิยะ",
            "dialysis_schedule": "อ/พฤ/ส",
            "hd_per_week": 2,
            "esa_code": "H4",
            "vascular_access": "AVG",
        }])
        self.assertEqual(new_c, 0)
        self.assertEqual(upd_c, 1)

        p = self.reg.match_hn("56001")
        self.assertEqual(p.dialysis_schedule, "อ/พฤ/ส")
        self.assertEqual(p.esa_medication, "Hemax")
        self.assertEqual(p.vascular_access, "AVG")


class TestGetPatientsByDay(unittest.TestCase):

    def setUp(self):
        self.m = _registry_mod()
        self.bucket = _FakeBucket()
        self.reg = self.m.MasterPatientRegistry(storage=_FakeStorage(self.bucket))
        self.reg._loaded = True
        self.reg.import_dialysis_batch([
            {"clinic_hn": "56001", "patient_name": "A", "dialysis_schedule": "จ/พ/ศ",
             "hd_per_week": 3, "esa_code": "E4"},
            {"clinic_hn": "56002", "patient_name": "B", "dialysis_schedule": "อ/พฤ/ส",
             "hd_per_week": 3, "esa_code": "H4"},
            {"clinic_hn": "56003", "patient_name": "C", "dialysis_schedule": "จ/พ/ศ",
             "hd_per_week": 3, "esa_code": "HM"},
        ])

    def test_monday_has_two_patients(self):
        pts = self.reg.get_patients_by_day("mon")
        self.assertEqual(len(pts), 2)
        hns = {p.clinic_hn for p in pts}
        self.assertEqual(hns, {"56001", "56003"})

    def test_tuesday_has_one_patient(self):
        pts = self.reg.get_patients_by_day("tue")
        self.assertEqual(len(pts), 1)
        self.assertEqual(pts[0].clinic_hn, "56002")

    def test_sunday_empty(self):
        pts = self.reg.get_patients_by_day("sun")
        self.assertEqual(len(pts), 0)


class TestDialysisSummary(unittest.TestCase):

    def setUp(self):
        self.m = _registry_mod()
        self.bucket = _FakeBucket()
        self.reg = self.m.MasterPatientRegistry(storage=_FakeStorage(self.bucket))
        self.reg._loaded = True
        self.reg.import_dialysis_batch([
            {"clinic_hn": "56001", "patient_name": "A", "dialysis_schedule": "จ/พ/ศ",
             "hd_per_week": 3, "esa_code": "E4"},
            {"clinic_hn": "56002", "patient_name": "B", "dialysis_schedule": "อ/พฤ/ส",
             "hd_per_week": 3, "esa_code": "E4"},
            {"clinic_hn": "56003", "patient_name": "C", "dialysis_schedule": "จ/พ/ศ",
             "hd_per_week": 2, "esa_code": "H4", "vascular_access": "AVF"},
        ])

    def test_summary_counts(self):
        s = self.reg.get_dialysis_summary()
        self.assertEqual(s["total_patients"], 3)
        self.assertEqual(s["with_schedule"], 3)
        self.assertEqual(s["by_day"]["mon"], 2)  # 56001 + 56003
        self.assertEqual(s["by_day"]["tue"], 1)  # 56002
        self.assertEqual(s["esa_counts"]["E4"], 2)
        self.assertEqual(s["esa_counts"]["H4"], 1)
        self.assertIn("AVF", s["vascular_access_counts"])


class TestParseDialysisExcel(unittest.TestCase):
    """ทดสอบ parser ด้วยไฟล์ Excel จริง (ถ้ามี)"""

    _EXCEL_PATH = (
        "/mnt/user-data/attachments/12135b82-8ef0-5cb0-b3da-242d5c97ee3e/"
        "19990e39-E0B895E0B8B2E0B8A3E0B8B2E0B887E0B89FE0B8ADE0B881E0B980E0B8A5"
        "E0B8B7E0B8ADE0B894E0B981E0B8A5E0B8B0E0B895E0B8B4E0B894E0B895E0B8B2E0B8A1"
        "E0B8A2E0B8B2_ESA_2569.xlsx"
    )

    @unittest.skipUnless(
        os.path.exists(_EXCEL_PATH),
        "Excel file not present (CI/staging)"
    )
    def test_parse_real_excel(self):
        from import_dialysis_excel import parse_dialysis_excel
        patients = parse_dialysis_excel(self._EXCEL_PATH)
        self.assertEqual(len(patients), 141)

        # ตรวจ HN 56001 (คนแรก)
        p1 = next(p for p in patients if p["clinic_hn"] == "56001")
        self.assertEqual(p1["patient_name"], "นาย ปิยะ อะนะบุหรง")
        self.assertEqual(p1["dialysis_schedule"], "จ/พ/ศ")
        self.assertEqual(p1["hd_per_week"], 3)
        self.assertEqual(p1["esa_code"], "E4")

        # ตรวจ HN 63203 (วินัย มากนคร)
        p2 = next(p for p in patients if p["clinic_hn"] == "63203")
        self.assertEqual(p2["esa_code"], "HM")


if __name__ == "__main__":
    unittest.main()
