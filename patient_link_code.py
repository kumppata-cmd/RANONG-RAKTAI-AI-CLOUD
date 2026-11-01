"""รหัสผูกบัญชีผู้ป่วย — ผูก LINE ของผู้ป่วยกับ HN อย่างปลอดภัย

ปัญหาที่แก้ (2026-11-01):
  ผู้ป่วยไม่รู้ HN ของตัวเอง และชื่อ LINE ไม่ใช่ชื่อจริง ระบบจึงไม่มีทาง
  รู้ว่า LINE นี้คือผู้ป่วยคนไหน  การให้ผู้ป่วยพิมพ์ "ลงทะเบียนHN <เลข>"
  เองก็อันตราย — ใครพิมพ์ HN ของคนอื่นก่อนก็ได้เห็นเวชระเบียนของคนนั้น

วิธีใหม่ (เหมือนธนาคาร/รพ.ทั่วไป):
  1. เจ้าหน้าที่/แพทย์ ยืนยันตัวผู้ป่วยที่หน้าเคาน์เตอร์ แล้วพิมพ์ใน LINE
     "ออกรหัส <HN>" → ระบบให้รหัส 6 หลัก ใช้ครั้งเดียว หมดอายุ 48 ชม.
  2. ผู้ป่วยเพิ่มเพื่อน LINE OA แล้วพิมพ์ "ผูก <รหัส>"
  3. ระบบผูก LINE นั้นกับ HN ของผู้ป่วย — ตั้งแต่นี้ LIFF "ดูผลแล็บ"
     แสดงเฉพาะเอกสารของ HN นี้เท่านั้น

ความปลอดภัย:
  - รหัสสุ่มด้วย secrets (1 ในล้าน) + จำกัดการเดา 5 ครั้ง/ชม./บัญชี
  - ใช้ได้ครั้งเดียว (marker ไฟล์ .used) และหมดอายุ
  - เก็บใน GCS แบบ create-only (Service Account เขียนทับ/ลบไม่ได้)
  - ไม่เก็บชื่อผู้ป่วยในไฟล์รหัส — เก็บแค่ HN และผู้ออก (hash)
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

LINK_CODE_PREFIX = "_state/link_codes/"
LINK_CODE_TTL_SEC = 48 * 3600
LINK_CODE_MAX_ATTEMPTS_PER_HOUR = 5

_CODE_RE = re.compile(r"^\d{6}$")


class LinkCodeStore:
    """ออก/ตรวจ/ใช้ รหัสผูกบัญชี — เก็บบน GCS (create-only)"""

    def __init__(self, storage, state_store=None):
        self._storage = storage
        self._state = state_store

    # ── helpers ─────────────────────────────────────────────
    def _bucket(self):
        if self._storage is None or not getattr(self._storage, "is_ready", False):
            return None
        return getattr(self._storage, "_bucket", None)

    @staticmethod
    def _path(code: str, used: bool = False) -> str:
        return f"{LINK_CODE_PREFIX}{code}{'.used' if used else ''}.json"

    # ── ออกรหัส ─────────────────────────────────────────────
    def issue(self, hn: str, issued_by_hash: str) -> tuple[bool, str]:
        """คืน (ok, code หรือข้อความผิดพลาด)"""
        bucket = self._bucket()
        if bucket is None:
            return False, "ระบบจัดเก็บยังไม่พร้อม"
        hn = (hn or "").strip().upper()
        if not hn:
            return False, "ต้องระบุ HN"
        for _attempt in range(5):
            code = f"{secrets.randbelow(1_000_000):06d}"
            blob = bucket.blob(self._path(code))
            try:
                if blob.exists():
                    continue
                blob.upload_from_string(
                    json.dumps({
                        "hn": hn,
                        "issued_by": (issued_by_hash or "")[:16],
                        "issued_at": datetime.now(timezone.utc).isoformat(),
                        "expires_at": time.time() + LINK_CODE_TTL_SEC,
                    }),
                    content_type="application/json",
                    if_generation_match=0,
                )
                return True, code
            except Exception as e:
                logger.warning("ออกรหัสผูกบัญชีไม่สำเร็จ: %s", type(e).__name__)
                continue
        return False, "ออกรหัสไม่สำเร็จ กรุณาลองใหม่"

    # ── ยกเลิกรหัส (เจ้าหน้าที่) ────────────────────────────
    def revoke(self, code: str, by_hash: str) -> bool:
        bucket = self._bucket()
        if bucket is None or not _CODE_RE.match(code or ""):
            return False
        try:
            bucket.blob(self._path(code, used=True)).upload_from_string(
                json.dumps({"revoked_by": (by_hash or "")[:16],
                            "at": datetime.now(timezone.utc).isoformat()}),
                content_type="application/json",
                if_generation_match=0,
            )
            return True
        except Exception:
            return False

    # ── ผู้ป่วยใช้รหัส ───────────────────────────────────────
    def consume(self, code: str, user_hash: str) -> tuple[str, str]:
        """คืน (status, hn)

        status: VALID / INVALID / EXPIRED / USED / RATE_LIMITED / UNAVAILABLE
        """
        code = (code or "").strip()
        if not _CODE_RE.match(code):
            return "INVALID", ""
        # จำกัดการเดา — นับทุกความพยายาม (ถูกหรือผิด) ต่อบัญชี/ชั่วโมง
        if self._state is not None:
            try:
                n = self._state.incr(f"linkcode:{user_hash}", 3600)
                if n and n > LINK_CODE_MAX_ATTEMPTS_PER_HOUR:
                    return "RATE_LIMITED", ""
            except Exception:
                pass
        bucket = self._bucket()
        if bucket is None:
            return "UNAVAILABLE", ""
        try:
            blob = bucket.blob(self._path(code))
            if not blob.exists():
                return "INVALID", ""
            data = json.loads(blob.download_as_text())
            if bucket.blob(self._path(code, used=True)).exists():
                return "USED", ""
            if time.time() > float(data.get("expires_at", 0)):
                return "EXPIRED", ""
            # ทำเครื่องหมายว่าใช้แล้ว (create-only → ใช้ซ้ำไม่ได้แม้แข่งกัน)
            try:
                bucket.blob(self._path(code, used=True)).upload_from_string(
                    json.dumps({"used_by": (user_hash or "")[:16],
                                "at": datetime.now(timezone.utc).isoformat()}),
                    content_type="application/json",
                    if_generation_match=0,
                )
            except Exception:
                return "USED", ""
            return "VALID", str(data.get("hn", "")).upper()
        except Exception as e:
            logger.warning("ตรวจรหัสผูกบัญชีไม่สำเร็จ: %s", type(e).__name__)
            return "UNAVAILABLE", ""


def parse_issue_command(text: str):
    """"ออกรหัส <HN>" / "รหัสผูก <HN>" / "issue code <HN>" → HN หรือ None"""
    m = re.match(
        r"^\s*(?:ออกรหัส|ออกรหัสผูก|รหัสผูก|issue\s*code)\s*(?:HN|hn)?\s*"
        r"([A-Za-z0-9-]{3,20})\s*$",
        text or "",
    )
    return m.group(1).upper() if m else None


def parse_link_command(text: str):
    """"ผูก <รหัส>" / "ผูกบัญชี <รหัส>" / "link <รหัส>" → รหัส 6 หลัก หรือ None"""
    m = re.match(
        r"^\s*(?:ผูก|ผูกบัญชี|ผูกHN|ผูก HN|link)\s*[:：]?\s*(\d{6})\s*$",
        text or "",
    )
    return m.group(1) if m else None


def parse_revoke_command(text: str):
    m = re.match(r"^\s*(?:ยกเลิกรหัส|revoke\s*code)\s*(\d{6})\s*$", text or "")
    return m.group(1) if m else None
