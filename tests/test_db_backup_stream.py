"""25 Nov 2026 — the 23:00 DB backup must not hold whole tables / the whole archive in RAM.

dump(out=file) streams the archive to a file; verify()/restore_to_schema() accept a
file-like archive; the per-table spool accepts bytes (real psycopg2) and str (the
test fake) alike.
"""
import io
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hd_module import db_backup  # noqa: E402
from test_db_backup import FakeConn, FakeCur  # noqa: E402


class BytesCur(FakeCur):
    """Like psycopg2: COPY TO writes *bytes* to a non-text file object."""

    def copy_expert(self, sql, f):
        if "TO STDOUT" in sql:
            t = sql.split('"')[1]
            cols, data = self.TABLES[t]
            f.write((",".join(cols) + "\n").encode("utf-8"))
            for r in data:
                f.write((",".join(str(v) for v in r) + "\n").encode("utf-8"))
        else:
            super().copy_expert(sql, f)


class BytesConn(FakeConn):
    def cursor(self):
        return BytesCur()


class StreamBackupTest(unittest.TestCase):
    def test_dump_to_file_matches_in_memory_dump(self):
        blob, man1 = db_backup.dump(FakeConn(), app_version="t")
        with tempfile.TemporaryFile() as spool:
            empty, man2 = db_backup.dump(FakeConn(), app_version="t", out=spool)
            self.assertEqual(empty, b"")
            self.assertEqual(man1["row_total"], man2["row_total"])
            self.assertEqual(man1["table_count"], man2["table_count"])
            self.assertGreater(man2["archive_bytes"], 0)
            # verify/restore accept the file object directly (rewound internally)
            spool.seek(0, 2)
            ok, problems = db_backup.verify(spool)
            self.assertTrue(ok, problems)
            r = db_backup.restore_to_schema(FakeConn(), spool, schema="restore_stream")
            self.assertEqual(sum(r["restored"].values()), man2["row_total"])
            spool.seek(0)
            with tarfile.open(fileobj=spool, mode="r:gz") as tar:
                self.assertIn("manifest.json", tar.getnames())

    def test_spool_accepts_bytes_from_psycopg2_style_cursor(self):
        blob, man = db_backup.dump(BytesConn(), app_version="t")
        ok, problems = db_backup.verify(blob)
        self.assertTrue(ok, problems)
        self.assertEqual(man["row_total"], db_backup.dump(FakeConn())[1]["row_total"])

    def test_csv_spool_counts_both_str_and_bytes(self):
        s = db_backup._CsvSpool()
        try:
            s.write("กข\n")
            s.write(b"ab\n")
            self.assertEqual(s.size, len("กข\n".encode("utf-8")) + 3)
            s.f.seek(0)
            self.assertEqual(s.f.read(), "กข\n".encode("utf-8") + b"ab\n")
        finally:
            s.close()

    def test_verify_streams_large_field(self):
        buf = io.BytesIO()
        big = "x" * 400_000
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            def add(name, data):
                b = data.encode("utf-8")
                i = tarfile.TarInfo(name)
                i.size = len(b)
                tar.addfile(i, io.BytesIO(b))
            add("manifest.json", '{"tables": {"t": {"rows": 2}}}')
            add("schema.sql", "")
            add("data/t.csv", f'id,v\n1,"{big}"\n2,small\n')
        ok, problems = db_backup.verify(buf.getvalue())
        self.assertTrue(ok, problems)


if __name__ == "__main__":
    unittest.main()
