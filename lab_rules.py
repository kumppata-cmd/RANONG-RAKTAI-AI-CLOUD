#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Clinical Rule Engine — Kidney Clinical Intelligence Platform 2.0

Rule Engine แยกจาก AI — ตรวจสอบค่า Lab ด้วย deterministic rules:
  1. Unit validation — ตรวจสอบว่าหน่วยถูกต้องสำหรับ analyte นั้น
  2. Critical values — ค่าวิกฤตที่ต้องแจ้งทันที
  3. Physiologic range — ค่าที่เป็นไปไม่ได้ทางสรีรวิทยา (extraction error)
  4. Reference ranges — ค่าปกติมาตรฐาน

กฎความปลอดภัย:
  - Rule Engine ไม่ทำการวินิจฉัย — แจ้งเตือนเท่านั้น
  - ผลทั้งหมดต้องผ่าน Human Review
  - ค่าที่ flag ว่า physiologic_impossible ต้องตรวจสอบซ้ำ
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class AnalyteSpec:
    """ข้อกำหนดของ analyte."""
    name: str                          # ชื่อ analyte
    valid_units: list[str]             # หน่วยที่ยอมรับ
    reference_range: tuple[float, float]  # (min, max) ค่าปกติ
    critical_low: Optional[float] = None   # ค่าวิกฤตต่ำ
    critical_high: Optional[float] = None  # ค่าวิกฤตสูง
    physiologic_min: float = 0.0       # ค่าต่ำสุดที่เป็นไปได้
    physiologic_max: float = 9999.0    # ค่าสูงสุดที่เป็นไปได้
    primary_unit: str = ""             # หน่วยหลักสำหรับ reference


# ═══════════════════════════════════════════════════════════
# Analyte Specifications — Kidney-relevant analytes
# Reference ranges based on standard clinical laboratory values
# ═══════════════════════════════════════════════════════════

ANALYTE_SPECS: dict[str, AnalyteSpec] = {
    # ── Renal Function ──
    "Creatinine": AnalyteSpec(
        name="Creatinine",
        valid_units=["mg/dL", "mg/dl"],
        reference_range=(0.6, 1.2),
        critical_low=None,
        critical_high=10.0,
        physiologic_min=0.1,
        physiologic_max=30.0,
        primary_unit="mg/dL",
    ),
    "BUN": AnalyteSpec(
        name="BUN",
        valid_units=["mg/dL", "mg/dl"],
        reference_range=(7.0, 20.0),
        critical_low=None,
        critical_high=100.0,
        physiologic_min=1.0,
        physiologic_max=300.0,
        primary_unit="mg/dL",
    ),
    "eGFR": AnalyteSpec(
        name="eGFR",
        valid_units=["mL/min/1.73m2", "mL/min", "ml/min/1.73m2", "ml/min"],
        reference_range=(90.0, 120.0),
        critical_low=15.0,
        critical_high=None,
        physiologic_min=0.0,
        physiologic_max=200.0,
        primary_unit="mL/min/1.73m2",
    ),
    # ── Electrolytes ──
    "Sodium": AnalyteSpec(
        name="Sodium",
        valid_units=["mmol/L", "mEq/L", "mmol/l", "mEq/l"],
        reference_range=(136.0, 145.0),
        critical_low=120.0,
        critical_high=160.0,
        physiologic_min=100.0,
        physiologic_max=200.0,
        primary_unit="mmol/L",
    ),
    "Potassium": AnalyteSpec(
        name="Potassium",
        valid_units=["mmol/L", "mEq/L", "mmol/l", "mEq/l"],
        reference_range=(3.5, 5.0),
        critical_low=2.5,
        critical_high=6.5,
        physiologic_min=1.0,
        physiologic_max=12.0,
        primary_unit="mmol/L",
    ),
    "Chloride": AnalyteSpec(
        name="Chloride",
        valid_units=["mmol/L", "mEq/L", "mmol/l", "mEq/l"],
        reference_range=(98.0, 106.0),
        critical_low=80.0,
        critical_high=120.0,
        physiologic_min=60.0,
        physiologic_max=150.0,
        primary_unit="mmol/L",
    ),
    "Bicarbonate": AnalyteSpec(
        name="Bicarbonate",
        valid_units=["mmol/L", "mEq/L", "mmol/l", "mEq/l"],
        reference_range=(22.0, 29.0),
        critical_low=10.0,
        critical_high=40.0,
        physiologic_min=5.0,
        physiologic_max=50.0,
        primary_unit="mmol/L",
    ),
    # ── Minerals ──
    "Calcium": AnalyteSpec(
        name="Calcium",
        valid_units=["mg/dL", "mg/dl"],
        reference_range=(8.5, 10.5),
        critical_low=6.0,
        critical_high=13.0,
        physiologic_min=3.0,
        physiologic_max=20.0,
        primary_unit="mg/dL",
    ),
    "Phosphate": AnalyteSpec(
        name="Phosphate",
        valid_units=["mg/dL", "mg/dl"],
        reference_range=(2.5, 4.5),
        critical_low=1.0,
        critical_high=9.0,
        physiologic_min=0.5,
        physiologic_max=20.0,
        primary_unit="mg/dL",
    ),
    # ── Proteins ──
    "Albumin": AnalyteSpec(
        name="Albumin",
        valid_units=["g/dL", "g/dl"],
        reference_range=(3.5, 5.0),
        critical_low=1.5,
        critical_high=None,
        physiologic_min=0.5,
        physiologic_max=7.0,
        primary_unit="g/dL",
    ),
    # ── Glucose ──
    "FBS": AnalyteSpec(
        name="FBS",
        valid_units=["mg/dL", "mg/dl"],
        reference_range=(70.0, 100.0),
        critical_low=40.0,
        critical_high=500.0,
        physiologic_min=10.0,
        physiologic_max=2000.0,
        primary_unit="mg/dL",
    ),
    "Glucose": AnalyteSpec(
        name="Glucose",
        valid_units=["mg/dL", "mg/dl"],
        reference_range=(70.0, 100.0),
        critical_low=40.0,
        critical_high=500.0,
        physiologic_min=10.0,
        physiologic_max=2000.0,
        primary_unit="mg/dL",
    ),
    # ── CBC ──
    "Hemoglobin": AnalyteSpec(
        name="Hemoglobin",
        valid_units=["g/dL", "g/dl"],
        reference_range=(12.0, 17.0),
        critical_low=7.0,
        critical_high=20.0,
        physiologic_min=2.0,
        physiologic_max=25.0,
        primary_unit="g/dL",
    ),
    "Hematocrit": AnalyteSpec(
        name="Hematocrit",
        valid_units=["%"],
        reference_range=(36.0, 50.0),
        critical_low=20.0,
        critical_high=60.0,
        physiologic_min=10.0,
        physiologic_max=70.0,
        primary_unit="%",
    ),
    "WBC": AnalyteSpec(
        name="WBC",
        valid_units=["x10^3/uL", "x10^3/µL", "10^3/uL", "10^3/µL",
                     "cells/uL", "cells/µL", "x10^9/L"],
        reference_range=(4.5, 11.0),
        critical_low=2.0,
        critical_high=30.0,
        physiologic_min=0.1,
        physiologic_max=500.0,
        primary_unit="x10^3/uL",
    ),
    "Platelet": AnalyteSpec(
        name="Platelet",
        valid_units=["x10^3/uL", "x10^3/µL", "10^3/uL", "10^3/µL",
                     "x10^9/L"],
        reference_range=(150.0, 400.0),
        critical_low=50.0,
        critical_high=1000.0,
        physiologic_min=5.0,
        physiologic_max=2000.0,
        primary_unit="x10^3/uL",
    ),
    # ── Special ──
    "PTH": AnalyteSpec(
        name="PTH",
        valid_units=["pg/mL", "pg/ml", "pmol/L", "pmol/l"],
        reference_range=(15.0, 65.0),
        critical_low=None,
        critical_high=600.0,
        physiologic_min=0.0,
        physiologic_max=5000.0,
        primary_unit="pg/mL",
    ),
    "Ferritin": AnalyteSpec(
        name="Ferritin",
        valid_units=["ng/mL", "ng/ml", "µg/L", "ug/L"],
        reference_range=(20.0, 250.0),
        critical_low=None,
        critical_high=2000.0,
        physiologic_min=0.0,
        physiologic_max=100000.0,
        primary_unit="ng/mL",
    ),
    "TSAT": AnalyteSpec(
        name="TSAT",
        valid_units=["%"],
        reference_range=(20.0, 50.0),
        critical_low=None,
        critical_high=None,
        physiologic_min=0.0,
        physiologic_max=100.0,
        primary_unit="%",
    ),
    "Kt/V": AnalyteSpec(
        name="Kt/V",
        valid_units=["", "ratio"],
        reference_range=(1.2, 2.0),
        critical_low=0.8,
        critical_high=None,
        physiologic_min=0.0,
        physiologic_max=5.0,
        primary_unit="",
    ),
}


def validate_result(analyte: str, value: float, unit: str) -> list[str]:
    """ตรวจสอบผลแล็บด้วย Clinical Rules.

    Returns:
        list ของ RuleFlag strings ที่พบปัญหา (ว่าง = ปกติ)
    """
    flags = []
    spec = ANALYTE_SPECS.get(analyte)

    if spec is None:
        # ไม่มี spec สำหรับ analyte นี้ — ไม่สามารถตรวจสอบได้
        return flags

    # 1. Unit validation
    if unit and spec.valid_units:
        # สำหรับ Kt/V ที่ไม่มีหน่วย ยอมรับ empty string
        if unit not in spec.valid_units:
            flags.append("unit_mismatch")

    # 2. Physiologic range — ค่าที่เป็นไปไม่ได้
    if value < spec.physiologic_min or value > spec.physiologic_max:
        flags.append("physiologic_impossible")
        return flags  # ไม่ต้องตรวจต่อ — ค่าผิดปกติเกินไป

    # 3. Critical values
    if spec.critical_low is not None and value < spec.critical_low:
        flags.append("critical_low")
    elif spec.critical_high is not None and value > spec.critical_high:
        flags.append("critical_high")

    # 4. Reference range (abnormal but not critical)
    ref_min, ref_max = spec.reference_range
    if "critical_low" not in flags and "critical_high" not in flags:
        if value < ref_min:
            flags.append("abnormal_low")
        elif value > ref_max:
            flags.append("abnormal_high")

    return flags


def get_reference_range(analyte: str) -> str:
    """คืนค่าปกติเป็น string เช่น '0.6-1.2 mg/dL'."""
    spec = ANALYTE_SPECS.get(analyte)
    if spec is None:
        return "N/A"
    ref_min, ref_max = spec.reference_range
    unit = spec.primary_unit
    if unit:
        return f"{ref_min}-{ref_max} {unit}"
    return f"{ref_min}-{ref_max}"


def get_valid_units(analyte: str) -> list[str]:
    """คืน list ของหน่วยที่ยอมรับสำหรับ analyte."""
    spec = ANALYTE_SPECS.get(analyte)
    if spec is None:
        return []
    return list(spec.valid_units)


def is_critical(flags: list[str]) -> bool:
    """ตรวจว่ามี critical flag หรือไม่."""
    return any(f in ("critical_low", "critical_high") for f in flags)


def is_physiologic_impossible(flags: list[str]) -> bool:
    """ตรวจว่าค่าเป็นไปไม่ได้ทางสรีรวิทยา."""
    return "physiologic_impossible" in flags
