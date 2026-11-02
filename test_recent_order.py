"""'รายการล่าสุด' ต้องเรียงตามเวลาเข้าระบบ ไม่ใช่ตามชื่อไฟล์ index

เหตุการณ์จริง 2026-11-02 11:17: หลัง restart แพทย์พิมพ์ 'รายการล่าสุด'
ได้บิลเมื่อวาน + ใบยาเดือน 9 แต่ใบฟอกเลือดที่เพิ่งส่ง 10:57 ไม่โผล่
สาเหตุ: GCS list_blobs คืนตามชื่อไฟล์ (document_id = hex สุ่ม)
DocumentIndex เก็บตามลำดับนั้น และ recent_by_owner ใช้ reversed(_docs)
→ "ล่าสุด" = รหัส hex มากสุด (f2… c7… c6… bd… ab…)

แก้: เรียง _docs ตาม indexed_at / เวลาสร้างไฟล์บน GCS หลังโหลด
ข้อมูลทั้งหมดเป็นค่าสังเคราะห์ · import server แบบ lazy
"""
import json
import unittest
from datetime import datetime, timedelta, timezone


def _server():
    import server as _s
    return _s


class _Blob:
    def __init__(self, name, data, created):
        self.name = name
        self._data = data
        self.time_created = created

    def exists(self):
        return True

    def download_as_text(self, encoding="utf-8"):
        return self._data


class _LegacyMissing:
    def exists(self):
        return False


class _Bucket:
    def __init__(self, blobs):
        self._blobs = blobs

    def blob(self, name):
        return _LegacyMissing()


class _Client:
    def __init__(self, blobs):
        self._blobs = blobs

    def list_blobs(self, bucket_name, prefix="", max_results=None):
        # GCS คืนตามชื่อ (lexicographic) — จำลองให้เหมือนจริง
        return sorted(
            (b for b in self._blobs if b.name.startswith(prefix)),
            key=lambda b: b.name,
        )


class _Storage:
    is_ready = True
    bucket_name = "test"

    def __init__(self, blobs):
        self._client = _Client(blobs)
        self._bucket = _Bucket(blobs)


OWNER = "owner-A"


def _doc(doc_id, created, **extra):
    d = {"document_id": doc_id, "category": "dialysis",
         "upload_date": created.strftime("%Y-%m-%d"),
         "owner_hash": OWNER, "extracted_text": doc_id}
    d.update(extra)
    return d


class TestRecentOrder(unittest.TestCase):

    def setUp(self):
        self.s = _server()
        self.prefix = self.s.INDEX_DOC_PREFIX
        self.base = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)

    def _index_with(self, blobs):
        idx = self.s.DocumentIndex(_Storage(blobs))
        idx._blob_cache = {}
        ok = idx._reload_from_gcs()
        self.assertTrue(ok)
        return idx

    def test_recent_follows_gcs_creation_time_not_hex_name(self):
        # ชื่อไฟล์เรียง: aaaa < bbbb < ffff  แต่ ffff ถูกสร้างก่อนสุด
        t = self.base
        blobs = [
            _Blob(f"{self.prefix}ffff0001.json",
                  json.dumps(_doc("ffff0001", t)), t),                       # เก่าสุด
            _Blob(f"{self.prefix}aaaa0002.json",
                  json.dumps(_doc("aaaa0002", t + timedelta(hours=1))),
                  t + timedelta(hours=1)),
            _Blob(f"{self.prefix}bbbb0003.json",
                  json.dumps(_doc("bbbb0003", t + timedelta(days=1))),
                  t + timedelta(days=1)),                                    # ใหม่สุด
        ]
        idx = self._index_with(blobs)
        recent = [d["document_id"] for d in idx.recent_by_owner(OWNER, 5)]
        self.assertEqual(recent, ["bbbb0003", "aaaa0002", "ffff0001"])

    def test_indexed_at_field_wins_over_blob_time(self):
        t = self.base
        blobs = [
            _Blob(f"{self.prefix}a1.json",
                  json.dumps(_doc("a1", t, indexed_at=(t + timedelta(days=2)).isoformat())),
                  t),
            _Blob(f"{self.prefix}b2.json",
                  json.dumps(_doc("b2", t + timedelta(days=1))),
                  t + timedelta(days=1)),
        ]
        idx = self._index_with(blobs)
        recent = [d["document_id"] for d in idx.recent_by_owner(OWNER, 5)]
        self.assertEqual(recent, ["a1", "b2"])

    def test_versioned_update_keeps_original_entry_time(self):
        """update_fields เขียน <id>_v<ts>.json ทีหลัง — ต้องไม่ทำให้
        เอกสารเก่ากระโดดขึ้นมาเป็น 'ล่าสุด'"""
        t = self.base
        blobs = [
            _Blob(f"{self.prefix}old1.json",
                  json.dumps(_doc("old1", t)), t),
            _Blob(f"{self.prefix}old1_v9999.json",          # แก้ index ทีหลัง
                  json.dumps(_doc("old1", t, patient_hn_folder="X")),
                  t + timedelta(days=3)),
            _Blob(f"{self.prefix}new2.json",
                  json.dumps(_doc("new2", t + timedelta(days=1))),
                  t + timedelta(days=1)),
        ]
        idx = self._index_with(blobs)
        recent = [d["document_id"] for d in idx.recent_by_owner(OWNER, 5)]
        self.assertEqual(recent, ["new2", "old1"])
        self.assertEqual(idx.get("old1").get("patient_hn_folder"), "X")

    def test_search_results_newest_first(self):
        t = self.base
        blobs = [
            _Blob(f"{self.prefix}zz.json",
                  json.dumps(_doc("zz", t, extracted_text="ฟอกเลือด")), t),
            _Blob(f"{self.prefix}aa.json",
                  json.dumps(_doc("aa", t + timedelta(hours=2),
                                  extracted_text="ฟอกเลือด")),
                  t + timedelta(hours=2)),
        ]
        idx = self._index_with(blobs)
        got = [d["document_id"] for d in idx.search_by_owner("ฟอกเลือด", OWNER)]
        self.assertEqual(got[:2], ["aa", "zz"])


if __name__ == "__main__":
    unittest.main()
