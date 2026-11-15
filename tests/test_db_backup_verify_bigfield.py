"""verify() must accept CSV cells larger than Python's default 128 KB csv limit.

15 Nov 2026: nightly backup reported "archive unreadable: field larger than field
limit (131072)" although the archive was fine (a table stores big text/JSON cells)."""
import csv
import io
import json
import tarfile
import unittest

from hd_module import db_backup


def _archive(rows):
    buf = io.BytesIO()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["id", "payload"])
    for r in rows:
        w.writerow(r)
    data = out.getvalue().encode()
    man = {"tables": {"t": {"rows": len(rows)}}}
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, blob in (("data/t.csv", data), ("schema.sql", b"--"), ("manifest.json", json.dumps(man).encode())):
            ti = tarfile.TarInfo(name)
            ti.size = len(blob)
            tar.addfile(ti, io.BytesIO(blob))
    return buf.getvalue()


class VerifyBigFieldTests(unittest.TestCase):
    def test_cell_over_128kb_verifies(self):
        ok, problems = db_backup.verify(_archive([[1, "x" * 400_000], [2, "small"]]))
        self.assertTrue(ok, problems)

    def test_row_count_mismatch_still_detected(self):
        blob = _archive([[1, "a"]])
        # manifest claims 1 row; tamper by rebuilding with a wrong manifest
        buf = io.BytesIO()
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as src, tarfile.open(fileobj=buf, mode="w:gz") as dst:
            for m in src.getmembers():
                data = src.extractfile(m).read()
                if m.name == "manifest.json":
                    data = json.dumps({"tables": {"t": {"rows": 5}}}).encode()
                    m.size = len(data)
                dst.addfile(m, io.BytesIO(data))
        ok, problems = db_backup.verify(buf.getvalue())
        self.assertFalse(ok)
        self.assertIn("t: csv 1 rows", problems[0])


if __name__ == "__main__":
    unittest.main()
