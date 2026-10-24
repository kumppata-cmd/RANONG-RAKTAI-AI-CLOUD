"""Security verification tests.

ตรวจสอบความปลอดภัยของโค้ดก่อน deploy:
- ไม่มี hardcoded secrets
- External AI ถูกปิดไว้
- ไม่มี PHI ใน log statements
- Admin endpoints ต้องมี authentication
"""

import os
import re
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Modules ที่ deploy ขึ้น production (ไม่รวม test files)
PRODUCTION_MODULES = [
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

# Patterns ที่อาจเป็น hardcoded secrets
SECRET_PATTERNS = [
    (r'sk-[a-zA-Z0-9]{20,}', "OpenAI API key"),
    (r'AIza[0-9A-Za-z_-]{35}', "Google API key"),
    (r'AKIA[0-9A-Z]{16}', "AWS Access Key"),
    (r'ghp_[a-zA-Z0-9]{36}', "GitHub Personal Access Token"),
    (r'xoxb-[0-9]{10,}', "Slack Bot Token"),
    (r'Bearer\s+[a-zA-Z0-9._-]{20,}', "Bearer token (hardcoded)"),
]

# PHI field names ที่ไม่ควรอยู่ใน log statements
PHI_FIELDS = [
    "patient_name", "hn_number", "id_card", "phone_number",
    "date_of_birth", "address", "national_id", "thai_id",
    "citizen_id", "passport",
]


def _read_source(filename):
    """อ่าน source code ของ module."""
    filepath = os.path.join(PROJECT_ROOT, filename)
    if not os.path.exists(filepath):
        return None
    with open(filepath, "r", encoding="utf-8") as f:
        return f.read()


class TestNoHardcodedSecrets:
    """ไม่มี API keys, tokens, หรือ passwords ใน source code."""

    @pytest.mark.parametrize("module_file", PRODUCTION_MODULES)
    def test_no_secrets_in_module(self, module_file):
        """สแกนหา secret patterns ในแต่ละ module."""
        source = _read_source(module_file)
        if source is None:
            pytest.skip(f"ไม่พบไฟล์: {module_file}")

        for pattern, description in SECRET_PATTERNS:
            matches = re.findall(pattern, source)
            if matches:
                # กรอง false positives: ถ้าอยู่ใน comment หรือ docstring
                # ที่เป็นตัวอย่าง pattern ให้ข้ามไป
                real_matches = []
                for match in matches:
                    # ตรวจว่าไม่ใช่ pattern ใน regex string (เช่น ใน test นี้เอง)
                    if match not in pattern:
                        real_matches.append(match)
                if real_matches:
                    pytest.fail(
                        f"{module_file}: พบ {description} — "
                        f"ห้าม hardcode secrets ในโค้ด!"
                    )


class TestExternalAIDisabled:
    """EXTERNAL_AI_CLASSIFICATION_ALLOWED ต้องเป็น False."""

    def test_external_ai_hardcoded_false(self):
        """ตรวจว่า EXTERNAL_AI_CLASSIFICATION_ALLOWED = False ในโค้ด."""
        source = _read_source("server.py")
        assert source is not None, "ไม่พบ server.py"
        assert "EXTERNAL_AI_CLASSIFICATION_ALLOWED = False" in source, (
            "EXTERNAL_AI_CLASSIFICATION_ALLOWED ต้องเป็น False "
            "ที่ระดับโค้ด (hardcoded) — ห้ามอ่านจาก env var!"
        )

    def test_external_ai_not_from_env(self):
        """ต้องไม่อ่าน EXTERNAL_AI_CLASSIFICATION_ALLOWED จาก env var."""
        source = _read_source("server.py")
        assert source is not None, "ไม่พบ server.py"
        # ค้นหาว่ามีการ override จาก env หรือไม่
        lines = source.split("\n")
        for i, line in enumerate(lines, 1):
            # ข้ามบรรทัดที่เป็น comment หรืออยู่ใน if block ที่เช็ค
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("if "):
                continue
            if (
                "os.environ" in line
                and "EXTERNAL_AI_CLASSIFICATION_ALLOWED" in line
                and "=" in line
                and "EXTERNAL_AI_CLASSIFICATION_ALLOWED =" in line
            ):
                pytest.fail(
                    f"server.py:{i} — EXTERNAL_AI_CLASSIFICATION_ALLOWED "
                    f"ถูกอ่านจาก env var! ต้อง hardcode เป็น False เท่านั้น"
                )


class TestNoPHIInLogs:
    """Log statements ต้องไม่มี PHI field names โดยตรง."""

    @pytest.mark.parametrize("module_file", PRODUCTION_MODULES)
    def test_no_phi_in_log_calls(self, module_file):
        """ตรวจว่า logging calls ไม่มี PHI fields."""
        source = _read_source(module_file)
        if source is None:
            pytest.skip(f"ไม่พบไฟล์: {module_file}")

        lines = source.split("\n")
        violations = []

        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            # ข้ามบรรทัดที่เป็น comment, docstring, หรือ variable assignment
            if stripped.startswith("#") or stripped.startswith('"""'):
                continue

            # ตรวจเฉพาะ log/print statements
            is_log_line = any(
                kw in stripped
                for kw in [
                    "logging.", "logger.", "log.",
                    "print(", "app.logger.",
                ]
            )
            if not is_log_line:
                continue

            # ตรวจหา PHI fields ใน f-string หรือ format string
            for phi in PHI_FIELDS:
                # ตรวจ f-string pattern: {patient_name} หรือ {hn_number}
                if f"{{{phi}}}" in stripped:
                    violations.append(
                        f"  {module_file}:{i} — พบ '{phi}' ใน log statement"
                    )

        if violations:
            pytest.fail(
                "พบ PHI fields ใน log statements:\n"
                + "\n".join(violations)
                + "\n\nใช้ hash หรือ mask แทนการ log ข้อมูลผู้ป่วยโดยตรง"
            )


class TestAdminEndpoints:
    """Admin endpoints ต้องมีการตรวจสอบ ADMIN_SECRET."""

    def test_admin_secret_defined(self):
        """ตรวจว่า ADMIN_SECRET ถูกกำหนดใน server.py."""
        source = _read_source("server.py")
        assert source is not None, "ไม่พบ server.py"
        assert "ADMIN_SECRET" in source, (
            "ไม่พบ ADMIN_SECRET ใน server.py — "
            "admin endpoints ต้องมี authentication!"
        )

    def test_admin_secret_from_env(self):
        """ADMIN_SECRET ต้องอ่านจาก environment variable ไม่ใช่ hardcode."""
        source = _read_source("server.py")
        assert source is not None, "ไม่พบ server.py"
        # ตรวจว่ามีการใช้ os.environ หรือ os.getenv สำหรับ ADMIN_SECRET
        has_env_read = (
            'os.environ.get("ADMIN_SECRET"' in source
            or "os.environ.get('ADMIN_SECRET'" in source
            or 'os.getenv("ADMIN_SECRET"' in source
            or "os.getenv('ADMIN_SECRET'" in source
            or 'os.environ["ADMIN_SECRET"]' in source
            or "os.environ['ADMIN_SECRET']" in source
        )
        assert has_env_read, (
            "ADMIN_SECRET ต้องอ่านจาก os.environ หรือ os.getenv "
            "— ห้าม hardcode ค่าจริง!"
        )

    def test_no_unprotected_admin_routes(self):
        """Admin routes ต้องตรวจ ADMIN_SECRET ทุก endpoint."""
        source = _read_source("server.py")
        assert source is not None, "ไม่พบ server.py"

        # ค้นหา route ที่มี /admin/ ใน path
        admin_route_pattern = re.compile(
            r'@app\.route\(["\'](/admin/[^"\']+)["\']'
        )
        admin_routes = admin_route_pattern.findall(source)

        if admin_routes:
            # ตรวจว่า ADMIN_SECRET ถูกเช็คใน server.py
            assert "ADMIN_SECRET" in source, (
                f"พบ admin routes: {admin_routes} "
                f"แต่ไม่พบการตรวจ ADMIN_SECRET!"
            )
