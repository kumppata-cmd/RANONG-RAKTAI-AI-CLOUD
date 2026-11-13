"""DB backup archive format, verify(), and restore plumbing — fake cursor, no PostgreSQL."""
import io
import json
import tarfile
import unittest

from hd_module import db_backup


class FakeCur:
    """Two tables; answers the information_schema queries and COPY TO/FROM."""
    TABLES = {"hd_a": (["id", "name"], [[1, "x"], [2, "y,z"]]), "hd_b": (["id"], [])}
    def __init__(self): self.rows = []; self.copied_in = {}; self.last = ""
    def execute(self, sql, params=None):
        self.last = sql
        if "information_schema.tables" in sql:
            self.rows = [(t,) for t in self.TABLES]
        elif "information_schema.columns" in sql:
            cols = self.TABLES[params[0]][0]
            self.rows = [(c, "integer" if c == "id" else "character varying", None if c == "id" else 100, None, None, "NO" if c == "id" else "YES",
                          "nextval('x')" if c == "id" else None) for c in cols]
        elif "PRIMARY KEY" in sql and "key_column_usage" in sql:
            self.rows = [("id",)]
        elif sql.startswith("SELECT current_database"):
            self.rows = [("testdb",)]
        elif sql.startswith("SELECT COUNT(*)"):
            q = sql.split('"'); t = q[-2] if '"' in sql else None      # last quoted token = table
            self.rows = [(len(self.copied_in.get(t, self.TABLES.get(t, ([], []))[1])),)]
        else:
            self.rows = []
    def fetchone(self): return self.rows[0] if self.rows else None
    def fetchall(self): return self.rows
    def copy_expert(self, sql, f):
        if "TO STDOUT" in sql:
            t = sql.split('"')[1]; cols, data = self.TABLES[t]
            f.write(",".join(cols) + "\n")
            for r in data:
                f.write(",".join('"%s"' % v if "," in str(v) else str(v) for v in r) + "\n")
        else:
            t = sql.split('"')[3]
            body = f.read().splitlines()[1:]
            self.copied_in[t] = body
    def __enter__(self): return self
    def __exit__(self, *a): return False


class FakeConn:
    def __init__(self): self.c = FakeCur()
    def cursor(self): return self.c
    def commit(self): pass
    def rollback(self): pass


class BackupTests(unittest.TestCase):
    def test_dump_verify_restore(self):
        conn = FakeConn()
        blob, man = db_backup.dump(conn, app_version="test")
        self.assertEqual(man["table_count"], 2); self.assertEqual(man["row_total"], 2)
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            names = tar.getnames()
            self.assertIn("data/hd_a.csv", names); self.assertIn("schema.sql", names)
            ddl = tar.extractfile("schema.sql").read().decode()
            self.assertIn('CREATE TABLE IF NOT EXISTS "hd_a"', ddl); self.assertIn("SERIAL", ddl); self.assertIn("PRIMARY KEY", ddl)
        ok, problems = db_backup.verify(blob)
        self.assertTrue(ok, problems)
        r = db_backup.restore_to_schema(conn, blob, schema="restore_test")
        self.assertTrue(r["ok"], r); self.assertEqual(r["restored"]["hd_a"], 2)
        self.assertEqual(conn.c.copied_in["hd_a"][1], '2,"y,z"')

    def test_verify_detects_corruption(self):
        blob, _ = db_backup.dump(FakeConn())
        # rebuild the archive with a tampered manifest
        src = tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz"); out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w:gz") as dst:
            for m in src.getmembers():
                data = src.extractfile(m).read()
                if m.name == "manifest.json":
                    j = json.loads(data); j["tables"]["hd_a"]["rows"] = 99; data = json.dumps(j).encode()
                    m.size = len(data)
                dst.addfile(m, io.BytesIO(data))
        ok, problems = db_backup.verify(out.getvalue())
        self.assertFalse(ok); self.assertIn("hd_a", problems[0])

    def test_refuses_non_restore_schema(self):
        with self.assertRaises(ValueError):
            db_backup.drop_restore_schema(FakeConn(), "public")

    def test_line_text(self):
        t = db_backup.line_text({"created_at": "2026-11-13T23:00:00", "table_count": 40, "row_total": 12345, "archive_bytes": 2_500_000},
                                "backups/db/x", True, True, restore={"ok": True, "restored": {"a": 1}})
        self.assertIn("40 ตาราง", t); self.assertIn("12,345", t); self.assertIn("✅ ผ่าน", t)


if __name__ == "__main__":
    unittest.main()
