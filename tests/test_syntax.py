"""Syntax and import verification for all modules.

ตรวจสอบว่าทุก module ใน production สามารถ compile ได้
ไม่มี syntax error — เป็น gate แรกก่อน deploy
"""

import os
import py_compile
import pytest

# รายชื่อ modules ทั้งหมดที่ deploy ขึ้น production (ตาม Dockerfile)
MODULE_FILES = [
    "server.py",
    "dashboard.py",
    "ocr_review.py",
    "lab_dashboard.py",
    "line_patient_assistant.py",
    "audit_trail.py",
    "lab_models.py",
    "lab_extractor.py",
    "lab_rules.py",
    "clinical_analyzer.py",
    "lab_tracker.py",
    "clinical_reports.py",
    "medication_review.py",
    "line_bot_enhancer.py",
    "lab_alert.py",
    "physician_approval.py",
    "alert_delivery.py",
    "patient_identity.py",
    "ocr_safety.py",
    "health_check.py",
]

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.mark.parametrize("module_file", MODULE_FILES)
def test_syntax_compile(module_file):
    """แต่ละ module ต้อง compile ได้โดยไม่มี SyntaxError."""
    filepath = os.path.join(PROJECT_ROOT, module_file)
    if os.path.exists(filepath):
        py_compile.compile(filepath, doraise=True)
    else:
        pytest.skip(f"ไม่พบไฟล์: {module_file}")


def test_all_modules_exist():
    """ตรวจสอบว่า modules ทั้งหมดที่ระบุใน Dockerfile มีอยู่จริง."""
    missing = []
    for module_file in MODULE_FILES:
        filepath = os.path.join(PROJECT_ROOT, module_file)
        if not os.path.exists(filepath):
            missing.append(module_file)

    if missing:
        pytest.fail(
            f"Modules ที่หายไป (ต้องมีตาม Dockerfile): {', '.join(missing)}"
        )


def test_no_tabs_in_source():
    """ตรวจว่าไม่มีการใช้ tab (\\t) ผสมกับ spaces ในโค้ด.

    Python 3 ไม่ยอมรับ mixed indentation
    """
    for module_file in MODULE_FILES:
        filepath = os.path.join(PROJECT_ROOT, module_file)
        if not os.path.exists(filepath):
            continue
        with open(filepath, "r", encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                if "\t" in line and not line.strip().startswith("#"):
                    # อนุญาต tab ใน string literals (เช่น \t ใน print)
                    # เช็คว่ามี tab ที่ indent หรือเปล่า
                    stripped = line.lstrip()
                    indent = line[: len(line) - len(stripped)]
                    if "\t" in indent:
                        pytest.fail(
                            f"{module_file}:{i} — มี tab ใน indentation"
                        )
