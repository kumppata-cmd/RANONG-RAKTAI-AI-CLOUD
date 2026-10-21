#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""สร้าง TEST-CKD-001.pdf — เอกสารจำลองสำหรับทดสอบ Lab Extraction.

ข้อมูลทั้งหมดเป็นข้อมูลจำลอง ไม่ใช่ข้อมูลผู้ป่วยจริง
"""

import os
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


def create_test_pdf():
    output_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "test_data",
        "TEST-CKD-001.pdf",
    )

    c = canvas.Canvas(output_path, pagesize=A4)
    w, h = A4

    y = h - 50
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, y, "LABORATORY REPORT")

    y -= 20
    c.setFont("Helvetica", 10)
    c.drawString(50, y, "TEST DOCUMENT - NOT REAL PATIENT DATA")

    y -= 15
    c.drawString(50, y, "Document ID: TEST-CKD-001")

    y -= 15
    c.drawString(50, y, "Specimen Date: 2026-01-15")

    y -= 15
    c.setFont("Helvetica-Bold", 12)
    c.drawString(50, y, "Lab Report - Kidney Function Panel")

    y -= 20
    c.setFont("Helvetica", 8)
    c.drawString(50, y, "=" * 80)

    # Header
    y -= 18
    c.setFont("Helvetica-Bold", 9)
    c.drawString(50, y, "Test")
    c.drawString(220, y, "Result")
    c.drawString(300, y, "Unit")
    c.drawString(420, y, "Reference Range")

    y -= 5
    c.setFont("Helvetica", 8)
    c.drawString(50, y, "-" * 80)

    # Lab results
    results = [
        ("Creatinine", "5.2", "mg/dL", "0.6-1.2"),
        ("BUN", "45", "mg/dL", "7-20"),
        ("eGFR", "10", "mL/min/1.73m2", ">90"),
        ("Sodium (Na)", "138", "mmol/L", "136-145"),
        ("Potassium (K)", "4.5", "mmol/L", "3.5-5.0"),
        ("Chloride (Cl)", "102", "mmol/L", "98-106"),
        ("Bicarbonate (HCO3)", "18", "mmol/L", "22-29"),
        ("Calcium (Ca)", "8.8", "mg/dL", "8.5-10.5"),
        ("Phosphate (PO4)", "6.5", "mg/dL", "2.5-4.5"),
        ("Albumin", "3.2", "g/dL", "3.5-5.0"),
        ("FBS (Glucose)", "110", "mg/dL", "70-100"),
        ("Hemoglobin (Hb)", "10.5", "g/dL", "12.0-17.0"),
        ("Hematocrit (Hct)", "32", "%", "36-50"),
        ("WBC", "7.5", "x10^3/uL", "4.5-11.0"),
        ("Platelet", "220", "x10^3/uL", "150-400"),
        ("PTH", "350", "pg/mL", "15-65"),
        ("Ferritin", "180", "ng/mL", "20-250"),
        ("TSAT", "25", "%", "20-50"),
    ]

    c.setFont("Helvetica", 9)
    for test, result, unit, ref in results:
        y -= 15
        c.drawString(50, y, test)
        c.drawString(220, y, result)
        c.drawString(300, y, unit)
        c.drawString(420, y, ref)

    y -= 30
    c.setFont("Helvetica", 8)
    c.drawString(50, y, "Note: This is a TEST document with SIMULATED data.")
    y -= 12
    c.drawString(50, y, "Patient identity information has been excluded for privacy.")

    c.save()
    print(f"Created: {output_path}")
    return output_path


if __name__ == "__main__":
    create_test_pdf()
