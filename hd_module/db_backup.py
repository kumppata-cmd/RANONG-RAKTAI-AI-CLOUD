"""Full PostgreSQL backup → GCS + Google Drive, with verify and restore (Go-Live R1).

Why not pg_dump: the Render web container has no pg_dump binary and the DB
user may be on another major version. This dumps every table in the public
schema through COPY ... TO STDOUT (CSV, header) — exact, fast, restorable
with COPY ... FROM — plus the DDL we need to recreate tables (columns/types,
primary keys) so a restore to an EMPTY database works without the app.

Archive layout (tar.gz):
    manifest.json      {created_at, db, tables: {name: {rows, bytes, columns}}, app_version}
    schema.sql         CREATE TABLE statements (from information_schema)
    data/<table>.csv   COPY CSV with header

Retention: the GCS service account is Object Creator only (cannot delete),
so old archives are kept; Drive keeps them in สำรองฐานข้อมูล/YYYY/MM/.
restore_to_schema() restores an archive into a *separate* schema
(restore_YYYYMMDD) so a restore can be tested without touching live tables.
"""
import csv
import gzip
import io
import json
import logging
import tarfile
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

GCS_PREFIX = "backups/db"
DRIVE_DIR = "สำรองฐานข้อมูล"


def _now():
    return datetime.now(timezone.utc) + timedelta(hours=7)


def list_tables(cur):
    cur.execute("""SELECT table_name FROM information_schema.tables
                   WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name""")
    return [r[0] for r in cur.fetchall()]


def table_ddl(cur, table):
    cur.execute("""SELECT column_name, data_type, character_maximum_length, numeric_precision, numeric_scale,
                          is_nullable, column_default
                   FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s
                   ORDER BY ordinal_position""", (table,))
    cols = []
    names = []
    for name, dtype, clen, nprec, nscale, nullable, default in cur.fetchall():
        names.append(name)
        t = dtype.upper()
        if dtype == "character varying":
            t = f"VARCHAR({clen})" if clen else "TEXT"
        elif dtype == "numeric" and nprec:
            t = f"NUMERIC({nprec},{nscale or 0})"
        elif dtype == "USER-DEFINED":
            t = "TEXT"          # enums → text in a cold restore
        elif dtype == "timestamp without time zone":
            t = "TIMESTAMP"
        elif dtype == "ARRAY":
            t = "TEXT[]"
        d = ""
        if default and "nextval" in default:
            t = "BIGSERIAL" if t == "BIGINT" else "SERIAL"
        elif default and not default.startswith("nextval"):
            d = f" DEFAULT {default}"
        cols.append(f'    "{name}" {t}{"" if nullable == "YES" else " NOT NULL"}{d}')
    cur.execute("""SELECT kcu.column_name FROM information_schema.table_constraints tc
                   JOIN information_schema.key_column_usage kcu ON kcu.constraint_name = tc.constraint_name
                   WHERE tc.table_schema = 'public' AND tc.table_name = %s AND tc.constraint_type = 'PRIMARY KEY'
                   ORDER BY kcu.ordinal_position""", (table,))
    pk = [r[0] for r in cur.fetchall()]
    if pk:
        cols.append("    PRIMARY KEY (" + ", ".join(f'"{c}"' for c in pk) + ")")
    return f'CREATE TABLE IF NOT EXISTS "{table}" (\n' + ",\n".join(cols) + "\n);", names


def dump(conn, app_version=""):
    """→ (archive_bytes, manifest)"""
    buf = io.BytesIO()
    manifest = {"created_at": _now().isoformat(timespec="seconds"), "app_version": app_version, "tables": {}}
    ddl = []
    with conn.cursor() as cur:
        cur.execute("SELECT current_database()")
        manifest["db"] = cur.fetchone()[0]
        tables = list_tables(cur)
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for t in tables:
                try:
                    stmt, cols = table_ddl(cur, t)
                    ddl.append(stmt)
                    out = io.StringIO()
                    cur.copy_expert(f'COPY "{t}" TO STDOUT WITH (FORMAT csv, HEADER true)', out)
                    data = out.getvalue().encode("utf-8")
                    cur.execute(f'SELECT COUNT(*) FROM "{t}"')
                    n = cur.fetchone()[0]
                    info = tarfile.TarInfo(f"data/{t}.csv")
                    info.size = len(data)
                    info.mtime = int(datetime.now(timezone.utc).timestamp())
                    tar.addfile(info, io.BytesIO(data))
                    manifest["tables"][t] = {"rows": n, "bytes": len(data), "columns": cols}
                except Exception as e:
                    logger.warning("backup table %s skipped: %s", t, str(e)[:100])
                    conn.rollback()
                    manifest["tables"][t] = {"error": str(e)[:100]}
            sch = "\n\n".join(ddl).encode("utf-8")
            for name, blob in (("schema.sql", sch), ("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1, default=str).encode("utf-8"))):
                info = tarfile.TarInfo(name)
                info.size = len(blob)
                info.mtime = int(datetime.now(timezone.utc).timestamp())
                tar.addfile(info, io.BytesIO(blob))
    manifest["archive_bytes"] = buf.tell()
    manifest["table_count"] = sum(1 for v in manifest["tables"].values() if "rows" in v)
    manifest["row_total"] = sum(v.get("rows", 0) for v in manifest["tables"].values())
    return buf.getvalue(), manifest


def verify(archive_bytes):
    """Read the archive back: every table in the manifest has a CSV whose row count matches. → (ok, problems)"""
    problems = []
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
            names = tar.getnames()
            if "manifest.json" not in names or "schema.sql" not in names:
                return False, ["manifest/schema missing"]
            man = json.loads(tar.extractfile("manifest.json").read().decode("utf-8"))
            for t, info in man["tables"].items():
                if "rows" not in info:
                    continue
                m = tar.extractfile(f"data/{t}.csv")
                if m is None:
                    problems.append(f"{t}: csv missing")
                    continue
                txt = io.StringIO(m.read().decode("utf-8"))
                n = sum(1 for _ in csv.reader(txt)) - 1
                if n != info["rows"]:
                    problems.append(f"{t}: csv {n} rows ≠ manifest {info['rows']}")
    except Exception as e:
        return False, [f"archive unreadable: {e}"]
    return (not problems), problems


def restore_to_schema(conn, archive_bytes, schema=None, tables=None):
    """Restore into a separate schema (default restore_YYYYMMDD) — never touches public.*.
    → {"schema", "restored": {table: rows}, "errors": {...}}"""
    schema = schema or f"restore_{_now().strftime('%Y%m%d_%H%M')}"
    if not schema.startswith("restore_"):
        raise ValueError("restore schema must start with restore_")
    out = {"schema": schema, "restored": {}, "errors": {}}
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
        man = json.loads(tar.extractfile("manifest.json").read().decode("utf-8"))
        ddl = tar.extractfile("schema.sql").read().decode("utf-8")
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            cur.execute(f'SET search_path TO "{schema}"')
            for stmt in ddl.split("\n\n"):
                if not stmt.strip():
                    continue
                # strip FK-less; PK stays. Serial columns become plain in restore schema.
                stmt = stmt.replace(" SERIAL", " INTEGER").replace(" BIGSERIAL", " BIGINT")
                try:
                    cur.execute(stmt)
                except Exception as e:
                    conn.rollback()
                    cur.execute(f'SET search_path TO "{schema}"')
                    out["errors"][stmt[:40]] = str(e)[:100]
            for t, info in man["tables"].items():
                if "rows" not in info or (tables and t not in tables):
                    continue
                try:
                    m = tar.extractfile(f"data/{t}.csv")
                    cur.execute(f'TRUNCATE "{schema}"."{t}"')
                    cur.copy_expert(f'COPY "{schema}"."{t}" FROM STDIN WITH (FORMAT csv, HEADER true)', io.StringIO(m.read().decode("utf-8")))
                    cur.execute(f'SELECT COUNT(*) FROM "{schema}"."{t}"')
                    out["restored"][t] = cur.fetchone()[0]
                except Exception as e:
                    conn.rollback()
                    cur.execute(f'SET search_path TO "{schema}"')
                    out["errors"][t] = str(e)[:120]
            cur.execute("SET search_path TO public")
        conn.commit()
    out["ok"] = not out["errors"] and all(out["restored"].get(t) == info.get("rows") for t, info in man["tables"].items() if "rows" in info and (not tables or t in tables))
    return out


def drop_restore_schema(conn, schema):
    if not schema.startswith("restore_"):
        raise ValueError("refusing to drop a non-restore schema")
    with conn.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    conn.commit()


def archive_name(ts=None):
    ts = ts or _now()
    return f"hd_db_{ts.strftime('%Y-%m-%d_%H%M')}.tar.gz"


def line_text(man, gcs_path, drive_ok, verify_ok, problems=None, restore=None):
    mb = man.get("archive_bytes", 0) / 1024 / 1024
    L = [f"💾 สำรองฐานข้อมูล {man['created_at'][:16].replace('T', ' ')}",
         f"{man.get('table_count', 0)} ตาราง · {man.get('row_total', 0):,} แถว · {mb:.1f} MB",
         f"GCS {'✅' if gcs_path else '❌'} · Drive {'✅' if drive_ok else '❌'} · ตรวจสอบไฟล์ {'✅' if verify_ok else '❌'}"]
    if problems:
        L.append("⚠️ " + "; ".join(problems[:3]))
    if restore:
        L.append(f"ทดสอบกู้คืน: {'✅ ผ่าน' if restore.get('ok') else '❌'} ({len(restore.get('restored', {}))} ตาราง)")
    return "\n".join(L)
