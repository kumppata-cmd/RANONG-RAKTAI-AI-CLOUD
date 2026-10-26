"""Registry read-path caching — ลดภาระ GCS โดยไม่เสียความถูกต้อง

ปัญหาเดิม:
  ทุกเมธอดอ่าน (match_hn, count, list_all, search) สั่ง _loaded = False
  ทุกครั้ง ทำให้เรียก GCS ใหม่ทั้ง list + download ทุกการเรียก
  การประมวลผลเอกสาร 1 ใบเรียกเมธอดเหล่านี้หลายครั้ง วัดได้จริง
  10 GCS round-trips ต่อเอกสาร 1 ใบ ทั้งที่เป็นข้อมูลชุดเดียวกัน
  และทั้งหมดอยู่ในเส้นทางประมวลผล webhook จึงหน่วงทุกเอกสาร

สิ่งที่ต้องคงไว้:
  - เส้นทางเขียนต้อง force reload (read-modify-write ข้าม worker)
  - worker ที่เขียนต้องเห็นข้อมูลของตัวเองทันที ไม่ว่า TTL ยาวแค่ไหน
  - TTL หมดอายุแล้วต้องโหลดใหม่จริง

ข้อมูลทั้งหมดเป็นค่าสังเคราะห์
"""
import json
import time
import unittest

import patient_master_registry as pmr


class CountingBlob:
    def __init__(self, name, payload, stats):
        self.name = name
        self._payload = payload
        self._stats = stats
        self.uploaded = None

    def exists(self):
        return True

    def download_as_text(self):
        self._stats["download"] += 1
        return self._payload

    def upload_from_string(self, data, content_type=None):
        self.uploaded = data
        return True


class CountingStorage:
    """นับ GCS operation จริง เพื่อวัดภาระที่เกิดขึ้น"""

    def __init__(self, patients=None):
        self.stats = {"list": 0, "download": 0}
        self._payload = json.dumps(
            {"patients": patients or []}, ensure_ascii=False
        )

    def list_blobs(self, prefix=None, **kwargs):
        self.stats["list"] += 1
        return [CountingBlob(
            "_patient_master/registry_v1.json", self._payload, self.stats,
        )]

    def blob(self, name):
        return CountingBlob(name, self._payload, self.stats)

    def round_trips(self):
        return self.stats["list"] + self.stats["download"]

    def reset(self):
        self.stats["list"] = 0
        self.stats["download"] = 0


def sample_patients(n=8):
    return [
        {
            "clinic_hn": f"5704{i}",
            "hospital_hn": f"0006057{i}",
            "patient_name": f"Synthetic {i}",
            "folder_name": f"5704{i}_Synthetic_{i}",
        }
        for i in range(n)
    ]


class TestReadPathCaching(unittest.TestCase):

    def setUp(self):
        self.storage = CountingStorage(sample_patients())
        self.reg = pmr.MasterPatientRegistry(storage=self.storage)

    def test_repeated_reads_do_not_refetch_within_ttl(self):
        """[perf] การอ่านซ้ำภายใน TTL ต้องไม่เรียก GCS ใหม่

        จำลองการประมวลผลเอกสาร 1 ใบที่ OCR เจอ HN 2 ตัว ซึ่งเป็น
        รูปแบบจริงที่เกิดขึ้นในระบบ
        """
        self.reg._cache_ttl = 300
        self.storage.reset()

        self.reg.count()
        for hn in ("57042", "57043"):
            self.reg.get_folder_for_hn(hn)
        self.reg.get_folder_for_hn("57042")
        self.reg.match_hn("57042")

        # โหลดครั้งเดียวพอ (list + download = 2)
        self.assertLessEqual(
            self.storage.round_trips(), 2,
            "การอ่านซ้ำภายใน TTL ไม่ควรเรียก GCS ใหม่",
        )

    def test_first_read_does_load(self):
        """ครั้งแรกต้องโหลดจริง ไม่ใช่คืนค่าว่าง"""
        self.storage.reset()
        self.assertEqual(self.reg.count(), 8)
        self.assertGreater(self.storage.round_trips(), 0)

    def test_reload_after_ttl_expires(self):
        """TTL หมดอายุต้องโหลดใหม่ — ไม่งั้นข้อมูลค้างถาวร"""
        self.reg._cache_ttl = 0.05
        self.reg.match_hn("57042")
        self.storage.reset()
        time.sleep(0.1)
        self.reg.match_hn("57042")
        self.assertGreater(
            self.storage.round_trips(), 0, "TTL หมดแล้วต้องโหลดใหม่",
        )

    def test_invalidate_cache_forces_reload(self):
        self.reg._cache_ttl = 300
        self.reg.match_hn("57042")
        self.storage.reset()
        self.reg.invalidate_cache()
        self.reg.match_hn("57042")
        self.assertGreater(self.storage.round_trips(), 0)

    def test_ttl_zero_restores_original_behaviour(self):
        """ตั้ง TTL = 0 ต้องกลับไปโหลดทุกครั้งเหมือนพฤติกรรมเดิม

        เป็นทางถอยถ้าพบปัญหาความสดของข้อมูลใน production
        """
        self.reg._cache_ttl = 0
        self.reg.match_hn("57042")
        self.storage.reset()
        self.reg.match_hn("57042")
        self.assertGreater(self.storage.round_trips(), 0)


class TestCacheDoesNotBreakCorrectness(unittest.TestCase):

    def setUp(self):
        self.storage = CountingStorage(sample_patients())
        self.reg = pmr.MasterPatientRegistry(storage=self.storage)
        self.reg._cache_ttl = 300

    def test_lookup_still_finds_registered_patient(self):
        self.assertEqual(
            self.reg.get_folder_for_hn("57042"), "57042_Synthetic_2"
        )

    def test_lookup_still_rejects_unknown_hn(self):
        self.assertEqual(self.reg.get_folder_for_hn("99999"), "")

    def test_leading_zero_matching_still_works(self):
        """ความสามารถที่เพิ่มไว้ก่อนหน้าต้องไม่หายไปเพราะแคช"""
        self.assertEqual(
            self.reg.get_folder_for_hn("60572"), "57042_Synthetic_2"
        )

    def test_count_and_list_agree(self):
        self.assertEqual(self.reg.count(), len(self.reg.list_all()))


class TestWritePathStillForcesReload(unittest.TestCase):
    """เส้นทางเขียนต้องไม่ถูกแคชบัง — ความถูกต้องสำคัญกว่าความเร็ว"""

    def setUp(self):
        self.storage = CountingStorage([])
        self.reg = pmr.MasterPatientRegistry(storage=self.storage)
        # TTL ยาวมาก เพื่อพิสูจน์ว่าการเขียนไม่ถูกแคชบัง
        self.reg._cache_ttl = 300

    def test_writer_sees_its_own_write_immediately(self):
        """[สำคัญ] worker ที่เขียนต้องเห็นข้อมูลของตัวเองทันที

        ถ้าแคชบัง จะลงทะเบียนผู้ป่วยแล้วหาไม่เจอทันทีหลังลงทะเบียน
        ซึ่งทำให้เจ้าหน้าที่สับสนและอาจลงซ้ำ
        """
        self.assertEqual(self.reg.count(), 0)
        ok, _msg = self.reg.register(
            clinic_hn="57099", patient_name="Synthetic New",
        )
        self.assertTrue(ok)
        self.assertEqual(self.reg.count(), 1)
        self.assertEqual(
            self.reg.get_folder_for_hn("57099"), "57099_Synthetic_New",
        )

    def test_register_reloads_before_modifying(self):
        """ต้องอ่านของล่าสุดก่อนแก้ ไม่งั้นทับข้อมูลของ worker อื่น"""
        self.reg.count()          # อุ่นแคชไว้
        self.storage.reset()
        self.reg.register(clinic_hn="57098", patient_name="Synthetic Two")
        self.assertGreater(
            self.storage.round_trips(), 0,
            "เส้นทางเขียนต้องโหลดใหม่ก่อนแก้เสมอ",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
