#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RANONG RAKTAI AI — Cloud Deployment Tests
ทดสอบด้วยข้อมูลจำลอง (ไม่ใช้ API จริง, ไม่ใช้ข้อมูลผู้ป่วยจริง)
"""

import json
import hmac
import hashlib
import base64
import os
import sys
import tempfile

# ตั้ง ENV จำลองก่อน import server
os.environ["LINE_CHANNEL_SECRET"] = "test_secret_for_unit_test"
os.environ["LINE_CHANNEL_ACCESS_TOKEN"] = "test_token_for_unit_test"
os.environ["ANTHROPIC_API_KEY"] = ""  # ปิด Claude API ระหว่างทดสอบ
os.environ["ENABLE_AI_CLASSIFY"] = "false"  # ใช้ keyword เท่านั้น
os.environ["BASE_DIR"] = tempfile.mkdtemp(prefix="ranong_test_")
os.environ["STORAGE_BACKEND"] = "local"

import server


def make_signature(body: bytes, secret: str = "test_secret_for_unit_test") -> str:
    """สร้าง LINE Webhook Signature จำลอง."""
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("utf-8")


def test_health():
    """ทดสอบ /health endpoint."""
    with server.app.test_client() as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "running"
        assert data["version"] == "cloud-1.0"
        assert "ocr_ready" in data
        print("PASS: /health endpoint")


def test_root():
    """ทดสอบ / endpoint."""
    with server.app.test_client() as client:
        resp = client.get("/")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "running"
        assert "webhook" in data
        print("PASS: / root endpoint")


def test_webhook_invalid_signature():
    """ทดสอบ webhook ปฏิเสธ signature ผิด."""
    with server.app.test_client() as client:
        body = json.dumps({"events": []}).encode("utf-8")
        resp = client.post(
            "/webhook",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Line-Signature": "invalid_signature",
            },
        )
        assert resp.status_code == 403
        print("PASS: webhook rejects invalid signature")


def test_webhook_valid_signature():
    """ทดสอบ webhook รับ signature ถูกต้อง."""
    with server.app.test_client() as client:
        body = json.dumps({"events": []}).encode("utf-8")
        sig = make_signature(body)
        resp = client.post(
            "/webhook",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Line-Signature": sig,
            },
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        print("PASS: webhook accepts valid signature")


def test_webhook_text_message():
    """ทดสอบ webhook รับข้อความ 'สถานะ'."""
    with server.app.test_client() as client:
        payload = {
            "events": [
                {
                    "type": "message",
                    "replyToken": "test_reply_token",
                    "message": {
                        "type": "text",
                        "id": "12345",
                        "text": "สถานะ",
                    },
                }
            ]
        }
        body = json.dumps(payload).encode("utf-8")
        sig = make_signature(body)
        resp = client.post(
            "/webhook",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Line-Signature": sig,
            },
        )
        assert resp.status_code == 200
        print("PASS: webhook handles text message 'สถานะ'")


def test_verify_signature():
    """ทดสอบ verify_signature function."""
    body = b"test body"
    sig = make_signature(body)
    assert server.verify_signature(body, sig) is True
    assert server.verify_signature(body, "wrong_sig") is False
    print("PASS: verify_signature")


def test_validate_file():
    """ทดสอบ validate_file กับไฟล์จำลอง."""
    # ไฟล์ว่าง
    empty_file = os.path.join(os.environ["BASE_DIR"], "empty.txt")
    with open(empty_file, "w") as f:
        pass
    valid, reason = server.validate_file(empty_file)
    assert valid is False
    assert "ว่างเปล่า" in reason

    # ไฟล์ text ปกติ
    text_file = os.path.join(os.environ["BASE_DIR"], "test.txt")
    with open(text_file, "w", encoding="utf-8") as f:
        f.write("ผลตรวจเลือด BUN creatinine eGFR hemoglobin CBC")
    valid, reason = server.validate_file(text_file)
    assert valid is True

    # นามสกุลไม่รองรับ
    bad_file = os.path.join(os.environ["BASE_DIR"], "test.exe")
    with open(bad_file, "w") as f:
        f.write("test")
    valid, reason = server.validate_file(bad_file)
    assert valid is False

    print("PASS: validate_file")


def test_classify_keywords_lab():
    """ทดสอบ keyword classification — ผลแล็บ."""
    text_file = os.path.join(os.environ["BASE_DIR"], "lab_test.txt")
    with open(text_file, "w", encoding="utf-8") as f:
        f.write(
            "ผลตรวจทางห้องปฏิบัติการ\n"
            "BUN: 25 mg/dL\n"
            "Creatinine: 1.2 mg/dL\n"
            "eGFR: 65 mL/min\n"
            "Hemoglobin: 12.5 g/dL\n"
            "CBC: WBC 7500 RBC 4.5\n"
        )
    result = server.classify_with_keywords(text_file)
    assert result["category"] == "lab", f"Expected 'lab', got '{result['category']}'"
    print(f"PASS: keyword classification → lab ({result['reason']})")


def test_classify_keywords_bills():
    """ทดสอบ keyword classification — ใบเสร็จ."""
    text_file = os.path.join(os.environ["BASE_DIR"], "bill_test.txt")
    with open(text_file, "w", encoding="utf-8") as f:
        f.write(
            "ใบเสร็จรับเงิน\n"
            "คลินิกระนอง รักษ์ไต\n"
            "ค่ารักษาพยาบาล: 1,500 บาท\n"
            "ค่ายา: 800 บาท\n"
            "ยอดรวม: 2,300 บาท\n"
            "ชำระเงินเรียบร้อย\n"
        )
    result = server.classify_with_keywords(text_file)
    assert result["category"] == "bills", f"Expected 'bills', got '{result['category']}'"
    print(f"PASS: keyword classification → bills ({result['reason']})")


def test_classify_keywords_rx():
    """ทดสอบ keyword classification — ใบสั่งยา."""
    text_file = os.path.join(os.environ["BASE_DIR"], "rx_test.txt")
    with open(text_file, "w", encoding="utf-8") as f:
        f.write(
            "ใบสั่งยา\n"
            "1. Amlodipine 5 mg 1x1 หลังอาหาร เช้า\n"
            "2. Losartan 50 mg 1x1 หลังอาหาร เช้า\n"
            "3. Calcium Carbonate 1500 mg วันละ 1 เม็ด\n"
            "เภสัชกรจ่ายยาเรียบร้อย\n"
        )
    result = server.classify_with_keywords(text_file)
    assert result["category"] == "rx", f"Expected 'rx', got '{result['category']}'"
    print(f"PASS: keyword classification → rx ({result['reason']})")


def test_classify_keywords_dialysis():
    """ทดสอบ keyword classification — บันทึกฟอกเลือด."""
    text_file = os.path.join(os.environ["BASE_DIR"], "dialysis_test.txt")
    with open(text_file, "w", encoding="utf-8") as f:
        f.write(
            "บันทึกการฟอกเลือด (Hemodialysis)\n"
            "น้ำหนักแห้ง: 55 kg\n"
            "Blood flow QB: 250 mL/min\n"
            "Dialysate QD: 500 mL/min\n"
            "UF goal: 2.0 L\n"
            "Dialyzer: F60\n"
        )
    result = server.classify_with_keywords(text_file)
    assert result["category"] == "dialysis", f"Expected 'dialysis', got '{result['category']}'"
    print(f"PASS: keyword classification → dialysis ({result['reason']})")


def test_storage_local():
    """ทดสอบ StorageLocal."""
    st = server.StorageLocal()
    data = b"test document content"
    path = st.save(data, "bills", "test_bill.txt")
    assert os.path.exists(path)
    with open(path, "rb") as f:
        assert f.read() == data
    print(f"PASS: StorageLocal save → {path}")


def test_detect_file_type():
    """ทดสอบ detect_file_type."""
    # JPEG
    jpg_file = os.path.join(os.environ["BASE_DIR"], "test.jpg")
    with open(jpg_file, "wb") as f:
        f.write(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
    assert server.detect_file_type(jpg_file) == ".jpg"

    # PNG
    png_file = os.path.join(os.environ["BASE_DIR"], "test.png")
    with open(png_file, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    assert server.detect_file_type(png_file) == ".png"

    # PDF
    pdf_file = os.path.join(os.environ["BASE_DIR"], "test.pdf")
    with open(pdf_file, "wb") as f:
        f.write(b"%PDF-1.4" + b"\x00" * 100)
    assert server.detect_file_type(pdf_file) == ".pdf"

    # Text
    txt_file = os.path.join(os.environ["BASE_DIR"], "test2.txt")
    with open(txt_file, "w") as f:
        f.write("Hello world")
    assert server.detect_file_type(txt_file) == ".txt"

    print("PASS: detect_file_type")


def main():
    """รันการทดสอบทั้งหมด."""
    print("=" * 60)
    print("RANONG RAKTAI AI — Cloud Deployment Tests")
    print("=" * 60)
    print(f"BASE_DIR: {os.environ['BASE_DIR']}")
    print(f"Python: {sys.version}")
    print()

    tests = [
        test_health,
        test_root,
        test_webhook_invalid_signature,
        test_webhook_valid_signature,
        test_webhook_text_message,
        test_verify_signature,
        test_validate_file,
        test_classify_keywords_lab,
        test_classify_keywords_bills,
        test_classify_keywords_rx,
        test_classify_keywords_dialysis,
        test_storage_local,
        test_detect_file_type,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"FAIL: {test.__name__} — {e}")
            failed += 1

    print()
    print("=" * 60)
    print(f"Results: {passed} PASS / {failed} FAIL / {len(tests)} TOTAL")
    print("=" * 60)

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
