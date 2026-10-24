"""RANONG RAKTAI AI — pytest configuration.

กำหนดค่าพื้นฐานสำหรับ test suite:
- ตั้ง environment variables ขั้นต่ำเพื่อให้ import modules ได้
- fixtures สำหรับใช้ร่วมกันระหว่าง test files
"""

import os
import sys
import pytest

# เพิ่ม project root ไปยัง sys.path เพื่อให้ import modules ได้
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture(autouse=True)
def ci_env_vars(monkeypatch):
    """ตั้ง environment variables ขั้นต่ำสำหรับ CI.

    ค่าเหล่านี้เป็น dummy — ใช้แค่ให้ modules import ได้โดยไม่ crash
    ไม่มีค่าจริง, ไม่มี secrets จริง
    """
    env_defaults = {
        "LINE_CHANNEL_ACCESS_TOKEN": "ci-dummy-token",
        "LINE_CHANNEL_SECRET": "ci-dummy-secret",
        "ADMIN_SECRET": "ci-dummy-admin",
        "GCS_BUCKET_NAME": "ci-dummy-bucket",
    }
    for key, value in env_defaults.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def project_root():
    """คืนค่า path ไปยัง project root."""
    return PROJECT_ROOT
