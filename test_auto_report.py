#!/usr/bin/env python3
"""Test suite for auto_report.py — Phase C."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime
from auto_report import (
    generate_monthly_report, report_to_line_text,
    report_to_flex_message, KPIMetric, MonthlyReport,
    _thai_month, _thai_quarter,
)


def test_thai_month():
    """ทดสอบชื่อเดือนภาษาไทย."""
    dt = datetime(2026, 10, 15)
    result = _thai_month(dt)
    assert "ตุลาคม" in result
    assert "2569" in result
    print(f"✅ Thai month: {result}")


def test_thai_quarter():
    """ทดสอบไตรมาสภาษาไทย."""
    dt = datetime(2026, 10, 1)
    result = _thai_quarter(dt)
    assert "ไตรมาส 4" in result
    print(f"✅ Thai quarter: {result}")


def test_kpi_metric():
    """ทดสอบ KPIMetric dataclass."""
    m = KPIMetric("Anemia", 85.0, 80.0, on_target=True)
    d = m.to_dict()
    assert d["value"] == 85.0
    assert d["on_target"] is True
    print(f"✅ KPIMetric: {d}")


def test_generate_monthly_report_no_storage():
    """ทดสอบสร้างรายงานโดยไม่มี storage."""
    report = generate_monthly_report(
        storage=None,
        patient_registry=None,
        reference_date=datetime(2026, 9, 1),
    )
    assert report.period == "2026-09"
    assert "กันยายน" in report.period_th
    assert report.total_patients == 0
    assert len(report.metrics) == 3
    print(f"✅ Monthly report (no storage): period={report.period_th}")


def test_generate_monthly_report_with_registry():
    """ทดสอบรายงานกับ mock patient registry."""

    class MockRegistry:
        def list_patients(self):
            return [
                {"ckd_stage": "G3a", "registered_date": "2026-09-05"},
                {"ckd_stage": "G3b", "registered_date": "2026-09-12"},
                {"ckd_stage": "G4", "registered_date": "2026-08-01"},
                {"ckd_stage": "G5D", "registered_date": "2026-09-20"},
            ]

    report = generate_monthly_report(
        storage=None,
        patient_registry=MockRegistry(),
        reference_date=datetime(2026, 9, 1),
    )
    assert report.total_patients == 4
    assert report.new_patients == 3  # 3 registered in 2026-09
    assert report.ckd_stage_counts.get("G3a") == 1
    assert report.ckd_stage_counts.get("G4") == 1
    print(f"✅ Monthly report with registry: {report.total_patients} patients, {report.new_patients} new")


def test_report_to_line_text():
    """ทดสอบแปลงรายงานเป็นข้อความ LINE."""
    report = MonthlyReport(
        period="2026-09",
        period_th="กันยายน 2569",
        generated_at="2026-10-01T08:00:00",
        total_patients=45,
        new_patients=3,
        ckd_stage_counts={"G3a": 10, "G3b": 15, "G4": 12, "G5": 5, "G5D": 3},
        metrics=[
            KPIMetric("Anemia On-Target", 82.5, 80.0, on_target=True),
            KPIMetric("Dialysis Adequate", 75.0, 80.0, on_target=False),
        ],
        critical_events=2,
        highlights_th=["ผู้ป่วยใหม่ 3 ราย", "✅ Anemia on-target ≥80%"],
    )
    text = report_to_line_text(report)
    assert "รายงาน KPI" in text
    assert "กันยายน 2569" in text
    assert "45 ราย" in text
    assert "+3 ราย" in text
    assert "G3a: 10" in text
    assert "✅" in text
    assert "⚠️" in text
    assert "ห้ามใช้แทน" in text
    print(f"✅ LINE text: {len(text)} chars")
    print(f"{'='*50}")
    print(text[:300])
    print(f"{'='*50}")


def test_report_to_flex_message():
    """ทดสอบแปลงรายงานเป็น LINE Flex Message."""
    report = MonthlyReport(
        period="2026-09",
        period_th="กันยายน 2569",
        generated_at="2026-10-01T08:00:00",
        total_patients=45,
        ckd_stage_counts={"G3a": 10, "G4": 12},
        metrics=[
            KPIMetric("Anemia", 85.0, 80.0, on_target=True),
            KPIMetric("Dialysis", 70.0, 80.0, on_target=False),
        ],
    )
    flex = report_to_flex_message(report)
    assert flex["type"] == "flex"
    assert "contents" in flex
    assert flex["contents"]["type"] == "bubble"
    assert "header" in flex["contents"]
    assert "body" in flex["contents"]
    assert "footer" in flex["contents"]
    print(f"✅ Flex Message: bubble with header/body/footer")


def test_report_to_dict():
    """ทดสอบ serialization."""
    report = MonthlyReport(
        period="2026-09",
        period_th="กันยายน 2569",
        generated_at="2026-10-01T08:00:00",
        total_patients=20,
        metrics=[KPIMetric("Test", 50.0, 80.0)],
    )
    d = report.to_dict()
    assert "period" in d
    assert "metrics" in d
    assert len(d["metrics"]) == 1
    print(f"✅ Serialization: {len(d)} keys")


def test_default_reference_date():
    """ทดสอบ default reference date = last month."""
    report = generate_monthly_report(storage=None)
    now = datetime.now()
    if now.month == 1:
        expected_month = 12
    else:
        expected_month = now.month - 1
    assert str(expected_month).zfill(2) in report.period
    print(f"✅ Default reference: {report.period}")


def test_safety_no_patient_ids():
    """ทดสอบว่ารายงานไม่มีข้อมูลระบุตัวผู้ป่วย."""
    report = MonthlyReport(
        period="2026-09",
        period_th="กันยายน 2569",
        generated_at="2026-10-01T08:00:00",
        total_patients=10,
        metrics=[],
    )
    text = report_to_line_text(report)
    # Must not contain HN, name patterns
    assert "HN" not in text
    assert "ชื่อ-สกุล" not in text
    print(f"✅ Safety: no patient identifiers in report text")


if __name__ == "__main__":
    tests = [
        test_thai_month,
        test_thai_quarter,
        test_kpi_metric,
        test_generate_monthly_report_no_storage,
        test_generate_monthly_report_with_registry,
        test_report_to_line_text,
        test_report_to_flex_message,
        test_report_to_dict,
        test_default_reference_date,
        test_safety_no_patient_ids,
    ]

    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"❌ FAIL {t.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed, {passed+failed} total")
    if failed == 0:
        print("🎉 ALL TESTS PASSED — Phase C Auto Report verified!")
    else:
        print(f"⚠️ {failed} test(s) failed")
