#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared utility functions for RANONG-RAKTAI-AI

Phase 1 Consolidation — รวมฟังก์ชันที่ซ้ำกันใน 4+ ไฟล์มาไว้ที่เดียว
"""

import re
from typing import Any, Optional


def safe_float(val: Any) -> Optional[float]:
    """แปลงค่าเป็น float อย่างปลอดภัย

    รองรับ: None, int, float, string (รวมถึง ">10.0", "<0.5", "H 5.2")
    เดิมถูกเขียนซ้ำใน: ckd_staging, epo_response_tracker,
    dialysis_adequacy, quarterly_compare
    """
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace(",", "")
    # ตัด prefix เช่น ">", "<", "H ", "L "
    s = re.sub(r'^[><HL]\s*', '', s)
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def validate_hn(hn: str) -> bool:
    """ตรวจรูปแบบ HN — ยอมรับตัวเลข ตัวอักษร ขีด

    เดิมถูกเขียนซ้ำใน: patient_hn_registry, patient_master_registry
    """
    if not hn or not isinstance(hn, str):
        return False
    return bool(re.match(r'^[A-Za-z0-9\-/]{1,20}$', hn.strip()))
