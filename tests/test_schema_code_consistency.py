"""Guard against SQL in hd_module referencing columns the schema never creates.

On 11 Nov 2026 several features silently failed in production because code
wrote to columns that no migration created (hd_clinical_alerts.acknowledged,
hd_dialyzer_tracking.use_count, hd_anticoagulation.drug, ...). This test
parses every CREATE TABLE / ADD COLUMN in migrations.py + __init__.py and
checks each INSERT column list and UPDATE ... SET target in hd_module/*.py
against it.
"""
import glob
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
MOD = os.path.join(HERE, "..", "hd_module")
# table-level constraints, matched as whole keywords so that columns such as
# "checked" / "checklist_date" / "primary_diagnosis" are not mistaken for them
_CONSTRAINT = re.compile(r"^(UNIQUE|PRIMARY\s+KEY|CONSTRAINT|FOREIGN\s+KEY|CHECK)\s*\(", re.I)


def _schema():
    src = open(os.path.join(MOD, "migrations.py"), encoding="utf-8").read()
    src += open(os.path.join(MOD, "__init__.py"), encoding="utf-8").read()
    tables = {}
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (hd_\w+)\s*\((.*?)\n\s*\);", src, re.S):
        cols = set()
        for line in m.group(2).splitlines():
            line = line.split("--")[0].strip()
            if not line or _CONSTRAINT.match(line):
                continue
            cols.add(line.split()[0].strip(",").lower())
        tables.setdefault(m.group(1), set()).update(cols)
    # one-line CREATE TABLE strings inside python code
    code = "".join(open(f, encoding="utf-8").read() for f in glob.glob(os.path.join(MOD, "*.py")))
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (hd_\w+)\s*\((.*?)\)\s*\"\"\"", code, re.S):
        body = re.sub(r"\s+", " ", m.group(2))
        for part in body.split(","):
            part = part.strip()
            if part and not _CONSTRAINT.match(part):
                tables.setdefault(m.group(1), set()).add(part.split()[0].strip(")").lower())
    for m in re.finditer(r"ALTER TABLE (hd_\w+) ADD COLUMN IF NOT EXISTS (\w+)", src + code):
        tables.setdefault(m.group(1), set()).add(m.group(2).lower())
    return tables, code


class SchemaCodeConsistency(unittest.TestCase):
    def test_insert_columns_exist(self):
        tables, code = _schema()
        problems = []
        for m in re.finditer(r"INSERT INTO (hd_\w+)\s*\(([^)]*)\)", code):
            table, raw = m.group(1), m.group(2)
            if "{" in raw:          # dynamically built column list (validated at runtime)
                continue
            cols = [c.strip().lower() for c in raw.replace("\n", " ").split(",") if c.strip()]
            if table not in tables:
                problems.append(f"{table}: table not defined in migrations")
                continue
            missing = [c for c in cols if c not in tables[table]]
            if missing:
                problems.append(f"{table}: INSERT uses unknown column(s) {missing}")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_update_targets_exist(self):
        tables, code = _schema()
        problems = []
        for m in re.finditer(r"UPDATE (hd_\w+)\s+SET\s+(.*?)\s+WHERE", code, re.S):
            table = m.group(1)
            if table not in tables:
                continue
            targets = re.findall(r"(?:^|,)\s*(\w+)\s*=", m.group(2).replace("\n", " "))
            missing = sorted({t for t in targets if t.lower() not in tables[table]})
            if missing:
                problems.append(f"{table}: UPDATE sets unknown column(s) {missing}")
        self.assertEqual(problems, [], "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
