"""Field Encryption — V4.0 Phase 3.4

เข้ารหัส sensitive fields ใน GCS JSON files
ใช้ Fernet (AES-128-CBC + HMAC-SHA256)

ENV VARS:
- ENCRYPTION_KEY: Fernet key (base64, 32 bytes)
  สร้างด้วย: python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
- ถ้าไม่ตั้ง → ไม่เข้ารหัส (backward compatible)

ไม่เข้ารหัสทุกอย่าง — เฉพาะ fields ที่กำหนดไว้:
- patient_name, hospital_hn, clinic_hn (ใน patient registry)
- owner_hash → ไม่เข้ารหัส (ใช้สำหรับ lookup)

Usage:
    from field_encryption import FieldEncryptor

    enc = FieldEncryptor()  # อ่าน ENCRYPTION_KEY จาก ENV
    encrypted = enc.encrypt("ชื่อผู้ป่วย")
    decrypted = enc.decrypt(encrypted)
"""

from __future__ import annotations

import base64
import logging
import os

logger = logging.getLogger(__name__)

ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "").strip()

# Fields ที่ต้องเข้ารหัส
SENSITIVE_FIELDS = {
    "patient_name",
    "hospital_hn",
    "clinic_hn",
    "line_display_name",
}


class FieldEncryptor:
    """เข้า/ถอดรหัส fields ด้วย Fernet.

    ถ้าไม่มี ENCRYPTION_KEY → passthrough (ไม่เข้ารหัส)
    """

    PREFIX = "ENC:"  # prefix สำหรับ encrypted values

    def __init__(self, key: str = ""):
        self._fernet = None
        self._available = False
        _key = key or ENCRYPTION_KEY

        if _key:
            try:
                from cryptography.fernet import Fernet
                self._fernet = Fernet(_key.encode() if isinstance(_key, str) else _key)
                self._available = True
                logger.info("FieldEncryptor: initialized with Fernet")
            except ImportError:
                logger.warning(
                    "FieldEncryptor: cryptography package not installed — "
                    "encryption disabled"
                )
            except Exception as e:
                logger.warning(
                    "FieldEncryptor: invalid key (%s) — encryption disabled",
                    type(e).__name__,
                )

    @property
    def is_available(self) -> bool:
        return self._available

    def encrypt(self, value: str) -> str:
        """เข้ารหัส value.

        ถ้า encryption ไม่พร้อม → คืนค่าเดิม
        ถ้า value เข้ารหัสแล้ว → คืนค่าเดิม
        """
        if not self._available or not value:
            return value
        if isinstance(value, str) and value.startswith(self.PREFIX):
            return value  # เข้ารหัสแล้ว
        try:
            encrypted = self._fernet.encrypt(value.encode("utf-8"))
            return f"{self.PREFIX}{encrypted.decode('ascii')}"
        except Exception as e:
            logger.warning("Encrypt failed: %s", type(e).__name__)
            return value

    def decrypt(self, value: str) -> str:
        """ถอดรหัส value.

        ถ้า encryption ไม่พร้อม → คืนค่าเดิม
        ถ้า value ไม่ได้เข้ารหัส → คืนค่าเดิม
        """
        if not value:
            return value
        if not isinstance(value, str) or not value.startswith(self.PREFIX):
            return value  # ไม่ได้เข้ารหัส
        if not self._available:
            logger.warning("Cannot decrypt: encryption not available")
            return value
        try:
            encrypted_data = value[len(self.PREFIX):]
            return self._fernet.decrypt(
                encrypted_data.encode("ascii")
            ).decode("utf-8")
        except Exception as e:
            logger.warning("Decrypt failed: %s", type(e).__name__)
            return value

    def encrypt_dict(
        self,
        data: dict,
        fields: set[str] | None = None,
    ) -> dict:
        """เข้ารหัส sensitive fields ใน dict.

        Args:
            data: dict ที่ต้องการเข้ารหัส
            fields: fields ที่ต้องเข้ารหัส (default: SENSITIVE_FIELDS)
        """
        if not self._available:
            return data
        target_fields = fields or SENSITIVE_FIELDS
        result = dict(data)
        for key in target_fields:
            if key in result and isinstance(result[key], str):
                result[key] = self.encrypt(result[key])
        return result

    def decrypt_dict(
        self,
        data: dict,
        fields: set[str] | None = None,
    ) -> dict:
        """ถอดรหัส sensitive fields ใน dict."""
        target_fields = fields or SENSITIVE_FIELDS
        result = dict(data)
        for key in target_fields:
            if key in result and isinstance(result[key], str):
                result[key] = self.decrypt(result[key])
        return result

    def get_status(self) -> dict:
        """สถานะ encryption."""
        return {
            "available": self._available,
            "method": "Fernet (AES-128-CBC + HMAC-SHA256)" if self._available else "disabled",
            "sensitive_fields": sorted(SENSITIVE_FIELDS),
            "key_configured": bool(ENCRYPTION_KEY),
        }
