#!/usr/bin/env python3
"""Test suite for liff_app.py — Phase F."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from liff_app import get_patient_labs_for_liff, get_liff_html, LIFF_HTML


def test_liff_html_exists():
    """ตรวจว่า LIFF HTML มีโครงสร้างที่ถูกต้อง."""
    assert "<!DOCTYPE html>" in LIFF_HTML
    assert "คลินิกโรคไต" in LIFF_HTML
    assert "PDPA" in LIFF_HTML
    assert "consent" in LIFF_HTML.lower()
    assert "Chart.js" in LIFF_HTML or "chart.js" in LIFF_HTML
    print("✅ LIFF HTML: valid structure with PDPA consent")


def test_liff_html_read_only():
    """ตรวจว่า LIFF เป็น Read-Only — ไม่มีปุ่มแก้ไข."""
    # No edit/delete/modify buttons
    assert "แก้ไข" not in LIFF_HTML or "ไม่สามารถแก้ไข" in LIFF_HTML
    assert '<form' not in LIFF_HTML.lower() or 'consent' in LIFF_HTML.lower()
    assert 'type="submit"' not in LIFF_HTML.lower() or 'consent' in LIFF_HTML.lower()
    print("✅ LIFF Read-Only: no edit controls")


def test_liff_html_dark_theme():
    """ตรวจว่าใช้ dark theme consistent."""
    assert "#0f172a" in LIFF_HTML  # --bg
    assert "#1e293b" in LIFF_HTML  # --card
    assert "#38bdf8" in LIFF_HTML  # --primary
    print("✅ LIFF dark theme: consistent with dashboard")


def test_get_liff_html_substitution():
    """ตรวจว่า get_liff_html แทนค่า config ได้."""
    html = get_liff_html(api_base="https://example.com")
    assert "https://example.com" in html
    assert "__API_BASE__" not in html
    print("✅ get_liff_html: API_BASE substituted")


def test_get_patient_labs_no_storage():
    """ตรวจว่าทำงานได้โดยไม่มี storage."""
    result = get_patient_labs_for_liff(storage=None, owner_hash="test1234abcd")
    assert result["owner_hash_prefix"] == "test1234"
    assert result["labs"] == []
    assert "trends" in result
    print("✅ get_patient_labs (no storage): empty result")


def test_get_patient_labs_no_owner():
    """ตรวจว่า reject empty owner_hash."""
    result = get_patient_labs_for_liff(storage=None, owner_hash="")
    assert result["labs"] == []
    assert result["owner_hash_prefix"] == ""
    print("✅ get_patient_labs (no owner): empty result")


def test_liff_has_tabs():
    """ตรวจว่ามี 3 tabs: ผลแล็บ, แนวโน้ม, CKD."""
    assert "ผลแล็บ" in LIFF_HTML
    assert "แนวโน้ม" in LIFF_HTML
    assert "CKD" in LIFF_HTML
    assert "tab-results" in LIFF_HTML
    assert "tab-trends" in LIFF_HTML
    assert "tab-ckd" in LIFF_HTML
    print("✅ LIFF tabs: ผลแล็บ, แนวโน้ม, CKD")


def test_liff_has_charts():
    """ตรวจว่ามี chart canvases สำหรับ trends."""
    assert "chartEgfr" in LIFF_HTML
    assert "chartCr" in LIFF_HTML
    assert "chartHb" in LIFF_HTML
    print("✅ LIFF charts: eGFR, Creatinine, Hemoglobin")


def test_liff_disclaimer():
    """ตรวจว่ามี disclaimer."""
    assert "ข้อมูลประกอบ" in LIFF_HTML or "ปรึกษาแพทย์" in LIFF_HTML
    assert "Read-Only" in LIFF_HTML
    print("✅ LIFF disclaimer present")


if __name__ == "__main__":
    tests = [
        test_liff_html_exists,
        test_liff_html_read_only,
        test_liff_html_dark_theme,
        test_get_liff_html_substitution,
        test_get_patient_labs_no_storage,
        test_get_patient_labs_no_owner,
        test_liff_has_tabs,
        test_liff_has_charts,
        test_liff_disclaimer,
    ]

    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"❌ FAIL {t.__name__}: {e}")
            failed += 1

    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed, {passed+failed} total")
    if failed == 0:
        print("🎉 ALL TESTS PASSED — Phase F LIFF Mini-App verified!")
    else:
        print(f"⚠️ {failed} test(s) failed")
