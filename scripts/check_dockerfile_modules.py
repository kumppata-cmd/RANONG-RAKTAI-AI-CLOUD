#!/usr/bin/env python3
"""ตรวจว่า Dockerfile คัดลอก module ทุกตัวที่แอปต้องใช้

เหตุผลที่ต้องมีสคริปต์นี้
─────────────────────────
Dockerfile ใช้ COPY รายไฟล์ทีละบรรทัด (30 บรรทัด) รายการนี้ต้องแก้มือ
ทุกครั้งที่เพิ่ม module ใหม่ ถ้าลืม — ทุกอย่างยังเขียวหมด:
  py_compile ผ่าน       (คอมไพล์ในโฟลเดอร์ repo ที่มีไฟล์อยู่)
  unittest ผ่าน         (import จาก repo เหมือนกัน)
  flake8 ผ่าน
แต่ container ที่ production ไม่มีไฟล์นั้น gunicorn จึง boot ไม่ขึ้น

เกิดขึ้นจริงแล้ว 26 ต.ค. 2026 (deploy dep-dbfmtvbphbkc73ajme0g):
    File "/app/server.py", line 57, in <module>
      from vendor_registry import (
    ModuleNotFoundError: No module named 'vendor_registry'
    Reason: Worker failed to boot.
CI เขียวทั้งหมด แต่ deploy ล้ม — production รอดเพราะ Render คง build
เดิมไว้ ไม่ใช่เพราะมีด่านไหนจับได้

สคริปต์นี้ไล่ import จาก entrypoint แบบ transitive แล้วเทียบกับรายการ
COPY ใน Dockerfile ใช้เวลาไม่ถึงวินาที จึงรันได้ทุก push ต่างจากการ
build image จริงที่ใช้เวลาหลายนาที (ยังควรมีไว้เป็นด่านสุดท้าย)

ไม่อ่านไฟล์ข้อมูลใด ๆ อ่านแค่โค้ดกับ Dockerfile
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

ENTRYPOINTS = ["server.py"]

# ไฟล์ที่ไม่ต้องอยู่ใน image
_SKIP_PREFIXES = ("test_", "conftest")


def _repo_modules(root: pathlib.Path) -> set:
    """ชื่อ module ของไฟล์ .py ที่อยู่ระดับบนสุดของ repo"""
    return {
        p.stem for p in root.glob("*.py")
        if not p.stem.startswith(_SKIP_PREFIXES)
    }


def _imports_of(path: pathlib.Path) -> set:
    """ชื่อ module ที่ไฟล์นี้ import — รวม import ที่อยู่ในฟังก์ชัน

    ต้องไล่ทั้งไฟล์ ไม่ใช่แค่ระดับบนสุด เพราะโค้ดนี้มี import แบบ lazy
    อยู่ในฟังก์ชันหลายจุด (เช่น `from lab_extractor import ...` ใน
    endpoint) ถ้าไฟล์นั้นไม่อยู่ใน image จะล้มตอนมีคนเรียกใช้ ซึ่ง
    ตรวจยากกว่าล้มตอน boot
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as e:
        print(f"ERROR: อ่าน {path.name} ไม่ได้: {type(e).__name__}")
        raise

    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    return found


def _dockerfile_copies(dockerfile: pathlib.Path) -> tuple:
    """คืน (ชุดไฟล์ที่ COPY, มี COPY แบบ glob หรือไม่)"""
    copied, globbed = set(), False
    for raw in dockerfile.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not re.match(r"^COPY\b", line, re.IGNORECASE):
            continue
        parts = line.split()[1:]
        if not parts:
            continue
        for src in parts[:-1]:            # ตัวสุดท้ายคือ destination
            if "*" in src or "?" in src:
                globbed = True
            copied.add(src)
    return copied, globbed


def main() -> int:
    root = pathlib.Path(__file__).resolve().parent.parent
    dockerfile = root / "Dockerfile"
    if not dockerfile.exists():
        print("ERROR: ไม่พบ Dockerfile")
        return 1

    local_modules = _repo_modules(root)

    # ไล่ import แบบ transitive จาก entrypoint
    needed: set = set()
    queue = [root / e for e in ENTRYPOINTS]
    visited = set()
    while queue:
        path = queue.pop()
        if path.name in visited or not path.exists():
            continue
        visited.add(path.name)
        for mod in _imports_of(path):
            if mod in local_modules and mod not in needed:
                needed.add(mod)
                queue.append(root / f"{mod}.py")

    copied, globbed = _dockerfile_copies(dockerfile)

    if globbed:
        print("OK: Dockerfile ใช้ COPY แบบ glob — ไม่มีรายการที่ต้องแก้มือ")
        print(f"    module ที่แอปต้องใช้: {len(needed)} ตัว")
        return 0

    missing = sorted(
        m for m in needed
        if f"{m}.py" not in copied and m not in ENTRYPOINTS
    )
    for e in ENTRYPOINTS:
        if e not in copied:
            missing.append(e)

    # ไฟล์ที่ COPY ไว้แต่ไม่มีอยู่จริง → build ล้มทันที
    ghosts = sorted(
        src for src in copied
        if src.endswith(".py") and not (root / src).exists()
    )

    print(f"module ที่แอปต้องใช้ (ไล่ import จาก {', '.join(ENTRYPOINTS)}): "
          f"{len(needed)} ตัว")
    print(f"ไฟล์ .py ที่ Dockerfile COPY: "
          f"{len([c for c in copied if c.endswith('.py')])} ไฟล์")

    rc = 0
    if missing:
        rc = 1
        print()
        print("FAIL: Dockerfile ไม่ได้คัดลอก module ที่แอปต้องใช้")
        for m in missing:
            name = m if m.endswith(".py") else f"{m}.py"
            print(f"  ขาด: {name}")
        print()
        print("แก้โดยเพิ่มบรรทัดนี้ใน Dockerfile (ก่อน USER appuser):")
        for m in missing:
            name = m if m.endswith(".py") else f"{m}.py"
            print(f"  COPY {name} .")
        print()
        print("ถ้าไม่เพิ่ม: CI จะเขียวทั้งหมด แต่ gunicorn จะ boot ไม่ขึ้น")
        print("ด้วย ModuleNotFoundError ตอน deploy")

    if ghosts:
        rc = 1
        print()
        print("FAIL: Dockerfile COPY ไฟล์ที่ไม่มีอยู่จริง — docker build จะล้ม")
        for g in ghosts:
            print(f"  ไม่พบ: {g}")

    if rc == 0:
        print()
        print("OK: Dockerfile คัดลอก module ที่แอปต้องใช้ครบทุกตัว")
    return rc


if __name__ == "__main__":
    sys.exit(main())
