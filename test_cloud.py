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
        assert "cloud-1." in data["version"]
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
    """ทดสอบ webhook ปฏิเสธ signature ผิด (production mode)."""
    original = server.IS_TESTING
    try:
        server.IS_TESTING = False
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
            print("PASS: webhook rejects invalid signature (production)")
    finally:
        server.IS_TESTING = original


def test_webhook_valid_signature():
    """ทดสอบ webhook รับ signature ถูกต้อง (production mode)."""
    original = server.IS_TESTING
    try:
        server.IS_TESTING = False
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
            print("PASS: webhook accepts valid signature (production)")
    finally:
        server.IS_TESTING = original


def test_webhook_text_message():
    """ทดสอบ webhook รับข้อความ 'สถานะ' (production mode)."""
    original = server.IS_TESTING
    try:
        server.IS_TESTING = False
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
            print("PASS: webhook handles text message 'สถานะ' (production)")
    finally:
        server.IS_TESTING = original


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


# ═══════════════════════════════════════════════════════════
# Testing Mode Tests (APP_ENV=testing)
# ═══════════════════════════════════════════════════════════


def test_testing_mode_webhook_rejected():
    """ทดสอบ: testing mode รับ webhook แต่ไม่ประมวลผลเอกสาร — verify signature + return testing status."""
    original = server.IS_TESTING
    try:
        server.IS_TESTING = True
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
            assert data["status"] == "testing"
            assert data["accepted"] is True
            print("PASS: testing mode accepts webhook with valid signature (no document processing)")
    finally:
        server.IS_TESTING = original


def test_testing_mode_webhook_invalid_sig():
    """ทดสอบ: testing mode ปฏิเสธ webhook ที่ signature ไม่ถูกต้อง."""
    original = server.IS_TESTING
    try:
        server.IS_TESTING = True
        with server.app.test_client() as client:
            body = json.dumps({"events": []}).encode("utf-8")
            resp = client.post(
                "/webhook",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Line-Signature": "invalid_signature_value",
                },
            )
            assert resp.status_code == 403
            print("PASS: testing mode rejects webhook with invalid signature (403)")
    finally:
        server.IS_TESTING = original


def test_testing_mode_health_works():
    """ทดสอบ: /health ทำงานได้ในโหมด testing โดยไม่ต้องใช้ API Key."""
    original = server.IS_TESTING
    try:
        server.IS_TESTING = True
        with server.app.test_client() as client:
            resp = client.get("/health")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["status"] == "running"
            assert data["testing_mode"] is True
            assert data["ai_classify"] is False  # ต้องปิดเมื่อ testing
            print("PASS: /health works in testing mode (no API Key needed)")
    finally:
        server.IS_TESTING = original


def test_testing_mode_no_api_calls():
    """ทดสอบ: classify_with_claude ไม่เรียก API จริงในโหมด testing."""
    original = server.IS_TESTING
    try:
        server.IS_TESTING = True
        # สร้างไฟล์ทดสอบ
        text_file = os.path.join(os.environ["BASE_DIR"], "api_test.txt")
        with open(text_file, "w", encoding="utf-8") as f:
            f.write("ผลตรวจเลือด BUN creatinine eGFR")
        result = server.classify_with_claude(text_file)
        # ต้อง fallback เป็น keyword classification โดยไม่เรียก API
        assert "category" in result
        assert result["category"] in server.FOLDERS
        print("PASS: classify_with_claude uses keyword fallback in testing mode")
    finally:
        server.IS_TESTING = original


def test_testing_mode_no_line_reply():
    """ทดสอบ: reply_message ไม่ส่งข้อความจริงในโหมด testing."""
    original = server.IS_TESTING
    try:
        server.IS_TESTING = True
        # เรียก reply_message — ต้องไม่ error และไม่เรียก HTTP จริง
        server.reply_message("fake_token", "test message")
        print("PASS: reply_message blocked in testing mode (no LINE API call)")
    finally:
        server.IS_TESTING = original


def test_testing_mode_no_download():
    """ทดสอบ: download_line_content ไม่ดาวน์โหลดไฟล์ในโหมด testing."""
    original = server.IS_TESTING
    try:
        server.IS_TESTING = True
        result = server.download_line_content("fake_message_id")
        assert result is None
        print("PASS: download_line_content blocked in testing mode (no file download)")
    finally:
        server.IS_TESTING = original


def test_testing_mode_is_default():
    """ทดสอบ: ค่าเริ่มต้น APP_ENV คือ testing เมื่อไม่ได้กำหนด."""
    # server.APP_ENV ถูกตั้งตอน import — ตรวจว่า IS_TESTING เป็น True ตามค่าเริ่มต้น
    assert server.IS_TESTING is True, "IS_TESTING should default to True"
    assert server.APP_ENV == "testing", f"APP_ENV should be 'testing', got '{server.APP_ENV}'"
    print("PASS: APP_ENV defaults to 'testing' (IS_TESTING=True)")


def test_testing_mode_webhook_test03():
    """ทดสอบ: testing mode รับข้อความ TEST-03 ผ่าน webhook อย่างปลอดภัย."""
    original = server.IS_TESTING
    try:
        server.IS_TESTING = True
        with server.app.test_client() as client:
            body = json.dumps({
                "events": [{
                    "type": "message",
                    "replyToken": "test_reply_token",
                    "message": {"type": "text", "text": "TEST-03"},
                }]
            }).encode("utf-8")
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
            assert data["status"] == "testing"
            assert data["accepted"] is True
            print("PASS: TEST-03 message accepted in testing mode (no document processing)")
    finally:
        server.IS_TESTING = original


# ═══════════════════════════════════════════════════════════
# File Processing Pipeline Tests (Step 8)
# ═══════════════════════════════════════════════════════════


def test_save_to_inbox_mock_jpg():
    """ทดสอบ save_to_inbox กับ mock JPG binary."""
    # JPEG magic bytes + padding (ไม่ใช่ภาพจริง ไม่มีข้อมูลผู้ป่วย)
    mock_jpg = b"\xff\xd8\xff\xe0" + b"\x00" * 200
    filepath = server.save_to_inbox(mock_jpg, "test_mock.jpg")
    assert os.path.exists(filepath), f"ไฟล์ไม่ถูกสร้าง: {filepath}"
    assert os.path.basename(filepath).endswith(".jpg")
    with open(filepath, "rb") as f:
        assert f.read() == mock_jpg
    print("PASS: save_to_inbox mock JPG")


def test_save_to_inbox_mock_png():
    """ทดสอบ save_to_inbox กับ mock PNG binary."""
    mock_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
    filepath = server.save_to_inbox(mock_png, "test_mock.png")
    assert os.path.exists(filepath), f"ไฟล์ไม่ถูกสร้าง: {filepath}"
    assert os.path.basename(filepath).endswith(".png")
    with open(filepath, "rb") as f:
        assert f.read() == mock_png
    print("PASS: save_to_inbox mock PNG")


def test_save_to_inbox_mock_pdf():
    """ทดสอบ save_to_inbox กับ mock PDF binary."""
    mock_pdf = b"%PDF-1.4 mock document" + b"\x00" * 200
    filepath = server.save_to_inbox(mock_pdf, "test_mock.pdf")
    assert os.path.exists(filepath), f"ไฟล์ไม่ถูกสร้าง: {filepath}"
    assert os.path.basename(filepath).endswith(".pdf")
    with open(filepath, "rb") as f:
        assert f.read() == mock_pdf
    print("PASS: save_to_inbox mock PDF")


def test_save_to_inbox_duplicate():
    """ทดสอบ save_to_inbox ไม่ทับไฟล์ที่มีชื่อซ้ำ."""
    data1 = b"first file content"
    data2 = b"second file content"
    path1 = server.save_to_inbox(data1, "duplicate_test.txt")
    path2 = server.save_to_inbox(data2, "duplicate_test.txt")
    assert os.path.exists(path1)
    assert os.path.exists(path2)
    assert path1 != path2, "ไฟล์ซ้ำต้องได้ path ต่างกัน"
    with open(path1, "rb") as f:
        assert f.read() == data1
    with open(path2, "rb") as f:
        assert f.read() == data2
    print("PASS: save_to_inbox duplicate filename protection")


def test_copy_to_category_lab():
    """ทดสอบ copy_to_category คัดลอกไปโฟลเดอร์ lab ถูกต้อง."""
    # สร้างไฟล์ต้นทาง
    src_file = os.path.join(os.environ["BASE_DIR"], "copy_test_lab.txt")
    with open(src_file, "w", encoding="utf-8") as f:
        f.write("mock lab result for testing")
    dest = server.copy_to_category(src_file, "lab")
    assert os.path.exists(dest), f"ไฟล์ไม่ถูกคัดลอก: {dest}"
    assert server.FOLDERS["lab"] in dest, f"ต้องอยู่ในโฟลเดอร์ {server.FOLDERS['lab']}"
    # ต้นฉบับยังอยู่
    assert os.path.exists(src_file), "ไฟล์ต้นฉบับถูกลบ (ต้องคัดลอก ไม่ใช่ย้าย)"
    print(f"PASS: copy_to_category → lab ({dest})")


def test_path_traversal_blocked():
    """ทดสอบ save_to_inbox ป้องกัน path traversal."""
    malicious_name = "../../etc/passwd.txt"
    data = b"path traversal test"
    filepath = server.save_to_inbox(data, malicious_name)
    # ไฟล์ต้องอยู่ใน inbox เท่านั้น ไม่ใช่ ../../etc/
    inbox_dir = os.path.join(os.environ["BASE_DIR"], server.FOLDERS["inbox"])
    assert filepath.startswith(inbox_dir), (
        f"Path traversal! ไฟล์หลุดออกนอก inbox: {filepath}"
    )
    assert os.path.basename(filepath) == "passwd.txt" or "passwd" in os.path.basename(filepath)
    assert os.path.exists(filepath)
    print("PASS: path traversal blocked — file stays in inbox")


def test_oversized_file_rejected():
    """ทดสอบ validate_file ปฏิเสธไฟล์เกิน 20 MB."""
    big_file = os.path.join(os.environ["BASE_DIR"], "big_test.txt")
    with open(big_file, "wb") as f:
        # เขียน 21 MB (เกิน MAX_FILE_SIZE = 20 MB)
        f.write(b"x" * (21 * 1024 * 1024))
    valid, reason = server.validate_file(big_file)
    assert valid is False, "ไฟล์เกิน 20 MB ต้องถูกปฏิเสธ"
    assert "MB" in reason or "ใหญ่เกิน" in reason
    # ล้าง — ลบไฟล์ใหญ่ทิ้ง
    os.remove(big_file)
    print("PASS: oversized file rejected (>20 MB)")


def test_storage_s3_fallback():
    """ทดสอบ StorageS3 fallback ไป local เมื่อยังไม่เชื่อมต่อ."""
    s3 = server.StorageS3()
    data = b"s3 fallback test content"
    path = s3.save(data, "bills", "s3_test_bill.txt")
    # ต้อง fallback ไป local — ไฟล์ต้องอยู่บน disk
    assert os.path.exists(path), f"S3 fallback ไม่ได้เก็บไฟล์: {path}"
    with open(path, "rb") as f:
        assert f.read() == data
    # list_files ต้อง return empty list (ยังไม่เชื่อมต่อ S3)
    files = s3.list_files("bills")
    assert isinstance(files, list)
    print(f"PASS: StorageS3 fallback to local ({path})")


# ═══════════════════════════════════════════════════════════
# GCS Storage Tests (Phase B — mock, ไม่เรียก GCS จริง)
# ═══════════════════════════════════════════════════════════

def test_gcs_no_credentials_fallback():
    """GCS ไม่มี credentials → fallback ไป local."""
    gcs = server.StorageGCS()
    assert not gcs.is_ready, "GCS ไม่ควร ready เมื่อไม่มี credentials"
    assert gcs._init_error is not None
    # save ควร fallback ไป local
    test_data = b"test content for fallback"
    result = gcs.save(test_data, "lab", "fallback_test.pdf")
    assert "gs://" not in result, "ควร fallback ไม่ใช่ GCS"
    assert os.path.exists(result), f"ไฟล์ fallback ไม่มี: {result}"
    os.unlink(result)
    print(f"PASS: test_gcs_no_credentials_fallback")


def test_gcs_invalid_base64_credentials():
    """GCS credentials Base64 ไม่ถูกต้อง → fallback."""
    orig = os.environ.get("GCS_CREDENTIALS_BASE64", "")
    os.environ["GCS_CREDENTIALS_BASE64"] = "not_valid_base64!!!"
    os.environ["STORAGE_BUCKET"] = "test-bucket"
    try:
        gcs = server.StorageGCS()
        assert not gcs.is_ready, "GCS ไม่ควร ready เมื่อ credentials ไม่ถูกต้อง"
    finally:
        if orig:
            os.environ["GCS_CREDENTIALS_BASE64"] = orig
        else:
            os.environ.pop("GCS_CREDENTIALS_BASE64", None)
        os.environ.pop("STORAGE_BUCKET", None)
    print(f"PASS: test_gcs_invalid_base64_credentials")


def test_gcs_content_type_detection():
    """ตรวจสอบ Content-Type mapping ถูกต้อง."""
    gcs = server.StorageGCS()
    assert gcs._get_content_type("report.pdf") == "application/pdf"
    assert gcs._get_content_type("scan.jpg") == "image/jpeg"
    assert gcs._get_content_type("scan.jpeg") == "image/jpeg"
    assert gcs._get_content_type("photo.png") == "image/png"
    assert gcs._get_content_type("notes.txt") == "text/plain; charset=utf-8"
    assert gcs._get_content_type("unknown.xyz") == "application/octet-stream"
    print(f"PASS: test_gcs_content_type_detection")


def test_gcs_path_traversal_in_save():
    """GCS save ป้องกัน path traversal ในชื่อไฟล์."""
    gcs = server.StorageGCS()
    # ไม่มี credentials → fallback ไป local แต่ยังตรวจ path traversal
    result = gcs.save(b"test", "lab", "../../etc/passwd.pdf")
    # ไฟล์ต้องอยู่ในโฟลเดอร์ที่ถูกต้อง ไม่ใช่ /etc/
    assert "/etc/" not in result, f"Path traversal ไม่ถูกป้องกัน: {result}"
    if os.path.exists(result):
        os.unlink(result)
    print(f"PASS: test_gcs_path_traversal_in_save")


def test_gcs_empty_filename_handling():
    """GCS save ชื่อไฟล์ว่าง → สร้างชื่อแบบอัตโนมัติ."""
    gcs = server.StorageGCS()
    result = gcs.save(b"test data", "review", "")
    assert os.path.exists(result), f"ไฟล์ไม่ถูกสร้าง: {result}"
    basename = os.path.basename(result)
    assert "unnamed_" in basename, f"ชื่อไฟล์ไม่ถูกต้อง: {basename}"
    os.unlink(result)
    print(f"PASS: test_gcs_empty_filename_handling")


def test_gcs_list_files_not_ready():
    """GCS list_files เมื่อไม่มี credentials → return []."""
    gcs = server.StorageGCS()
    result = gcs.list_files("lab")
    assert result == [], f"ควร return [] แต่ได้: {result}"
    print(f"PASS: test_gcs_list_files_not_ready")


def test_gcs_check_connection_not_ready():
    """GCS check_connection เมื่อไม่มี credentials → status=not_ready."""
    gcs = server.StorageGCS()
    result = gcs.check_connection()
    assert result["status"] == "not_ready", f"ควร not_ready แต่ได้: {result}"
    assert "error" in result
    print(f"PASS: test_gcs_check_connection_not_ready")


def _inject_google_mock_modules():
    """สร้าง mock google modules ใน sys.modules (ไม่ต้องติดตั้ง google-cloud-storage)."""
    import types
    import unittest.mock as mock

    # สร้าง mock module hierarchy
    google_mod = types.ModuleType("google")
    google_cloud_mod = types.ModuleType("google.cloud")
    google_cloud_storage_mod = types.ModuleType("google.cloud.storage")
    google_oauth2_mod = types.ModuleType("google.oauth2")
    google_oauth2_sa_mod = types.ModuleType("google.oauth2.service_account")

    # mock Client และ Credentials
    mock_client_cls = mock.MagicMock()
    mock_creds_cls = mock.MagicMock()

    google_cloud_storage_mod.Client = mock_client_cls
    google_oauth2_sa_mod.Credentials = mock_creds_cls

    google_mod.cloud = google_cloud_mod
    google_mod.oauth2 = google_oauth2_mod
    google_cloud_mod.storage = google_cloud_storage_mod
    google_oauth2_mod.service_account = google_oauth2_sa_mod

    # ใส่ใน sys.modules
    modules = {
        "google": google_mod,
        "google.cloud": google_cloud_mod,
        "google.cloud.storage": google_cloud_storage_mod,
        "google.oauth2": google_oauth2_mod,
        "google.oauth2.service_account": google_oauth2_sa_mod,
    }
    return modules, mock_client_cls, mock_creds_cls


def test_gcs_valid_credentials_mock():
    """GCS ด้วย credentials จำลอง — ตรวจ init flow (mock google libs)."""
    import unittest.mock as mock

    # สร้าง service account JSON จำลอง
    fake_sa = {
        "type": "service_account",
        "project_id": "test-project",
        "private_key_id": "fake-key-id",
        "private_key": "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----\n",
        "client_email": "test@test-project.iam.gserviceaccount.com",
        "client_id": "123456",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    fake_b64 = base64.b64encode(json.dumps(fake_sa).encode()).decode()

    orig_bucket = os.environ.get("STORAGE_BUCKET", "")
    orig_creds = os.environ.get("GCS_CREDENTIALS_BASE64", "")
    os.environ["STORAGE_BUCKET"] = "test-bucket-mock"
    os.environ["GCS_CREDENTIALS_BASE64"] = fake_b64

    # เก็บ original modules
    orig_modules = {}
    for key in ["google", "google.cloud", "google.cloud.storage", "google.oauth2", "google.oauth2.service_account"]:
        orig_modules[key] = sys.modules.get(key)

    orig_storage_bucket = server.STORAGE_BUCKET
    try:
        server.STORAGE_BUCKET = "test-bucket-mock"
        modules, mock_client_cls, mock_creds_cls = _inject_google_mock_modules()
        sys.modules.update(modules)

        mock_client = mock.MagicMock()
        mock_bucket = mock.MagicMock()
        mock_client.bucket.return_value = mock_bucket
        mock_client_cls.return_value = mock_client

        gcs = server.StorageGCS()
        assert gcs.is_ready, "GCS ควร ready เมื่อ mock credentials สำเร็จ"
        assert gcs.bucket_name == "test-bucket-mock"

        # ทดสอบ upload
        mock_blob = mock.MagicMock()
        mock_bucket.blob.return_value = mock_blob

        result = gcs.save(b"\xff\xd8\xfftest jpg data", "lab", "test_scan.jpg")
        assert result.startswith("gs://"), f"ผลลัพธ์ควรเป็น gs:// แต่ได้: {result}"
        mock_blob.upload_from_string.assert_called_once()
        call_args = mock_blob.upload_from_string.call_args
        assert call_args[1]["content_type"] == "image/jpeg"

        # ทดสอบ list_files
        mock_blob1 = mock.MagicMock()
        mock_blob1.name = "04_LAB_RESULTS/file1.pdf"
        mock_blob2 = mock.MagicMock()
        mock_blob2.name = "04_LAB_RESULTS/file2.jpg"
        mock_client.list_blobs.return_value = [mock_blob1, mock_blob2]

        files = gcs.list_files("lab")
        assert len(files) == 2
        assert "file1.pdf" in files
        assert "file2.jpg" in files
    finally:
        server.STORAGE_BUCKET = orig_storage_bucket
        # คืนค่า modules เดิม
        for key, val in orig_modules.items():
            if val is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = val
        if orig_bucket:
            os.environ["STORAGE_BUCKET"] = orig_bucket
        else:
            os.environ.pop("STORAGE_BUCKET", None)
        if orig_creds:
            os.environ["GCS_CREDENTIALS_BASE64"] = orig_creds
        else:
            os.environ.pop("GCS_CREDENTIALS_BASE64", None)

    print(f"PASS: test_gcs_valid_credentials_mock")


def test_gcs_upload_error_fallback():
    """GCS upload ล้มเหลว → fallback ไป local."""
    import unittest.mock as mock

    fake_sa = {
        "type": "service_account",
        "project_id": "test-project",
        "private_key_id": "fake",
        "private_key": "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----\n",
        "client_email": "test@test.iam.gserviceaccount.com",
        "client_id": "123",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    fake_b64 = base64.b64encode(json.dumps(fake_sa).encode()).decode()

    orig_bucket = os.environ.get("STORAGE_BUCKET", "")
    orig_creds = os.environ.get("GCS_CREDENTIALS_BASE64", "")
    os.environ["STORAGE_BUCKET"] = "test-bucket"
    os.environ["GCS_CREDENTIALS_BASE64"] = fake_b64

    # เก็บ original modules
    orig_modules = {}
    for key in ["google", "google.cloud", "google.cloud.storage", "google.oauth2", "google.oauth2.service_account"]:
        orig_modules[key] = sys.modules.get(key)

    orig_storage_bucket = server.STORAGE_BUCKET
    try:
        server.STORAGE_BUCKET = "test-bucket"
        modules, mock_client_cls, mock_creds_cls = _inject_google_mock_modules()
        sys.modules.update(modules)

        mock_client = mock.MagicMock()
        mock_bucket = mock.MagicMock()
        mock_client.bucket.return_value = mock_bucket
        mock_blob = mock.MagicMock()
        mock_blob.upload_from_string.side_effect = Exception("Network error")
        mock_bucket.blob.return_value = mock_blob
        mock_client_cls.return_value = mock_client

        gcs = server.StorageGCS()
        assert gcs.is_ready

        result = gcs.save(b"test data", "lab", "error_test.pdf")
        # ควร fallback ไป local
        assert "gs://" not in result, f"ควร fallback แต่ได้: {result}"
        assert os.path.exists(result)
        os.unlink(result)
    finally:
        server.STORAGE_BUCKET = orig_storage_bucket
        # คืนค่า modules เดิม
        for key, val in orig_modules.items():
            if val is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = val
        if orig_bucket:
            os.environ["STORAGE_BUCKET"] = orig_bucket
        else:
            os.environ.pop("STORAGE_BUCKET", None)
        if orig_creds:
            os.environ["GCS_CREDENTIALS_BASE64"] = orig_creds
        else:
            os.environ.pop("GCS_CREDENTIALS_BASE64", None)

    print(f"PASS: test_gcs_upload_error_fallback")


def test_testing_docs_mode():
    """testing-docs mode: IS_TESTING=True, IS_TESTING_DOCS=True."""
    # ตรวจสอบค่า IS_TESTING_DOCS ปัจจุบัน (ควรเป็น False เพราะ APP_ENV=testing)
    assert server.IS_TESTING is True, "IS_TESTING ควรเป็น True"
    assert server.IS_TESTING_DOCS is False, "IS_TESTING_DOCS ควรเป็น False เพราะ APP_ENV=testing"
    print(f"PASS: test_testing_docs_mode")


def test_health_version_updated():
    """Health endpoint แสดง version cloud-1.1-gcs."""
    with server.app.test_client() as client:
        resp = client.get("/health")
        data = resp.get_json()
        assert "1.1-gcs" in data["version"], f"version ผิด: {data['version']}"
    print(f"PASS: test_health_version_updated")


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
        # Testing mode tests
        test_testing_mode_webhook_rejected,
        test_testing_mode_webhook_invalid_sig,
        test_testing_mode_health_works,
        test_testing_mode_no_api_calls,
        test_testing_mode_no_line_reply,
        test_testing_mode_no_download,
        test_testing_mode_is_default,
        test_testing_mode_webhook_test03,
        # File processing pipeline tests (Step 8)
        test_save_to_inbox_mock_jpg,
        test_save_to_inbox_mock_png,
        test_save_to_inbox_mock_pdf,
        test_save_to_inbox_duplicate,
        test_copy_to_category_lab,
        test_path_traversal_blocked,
        test_oversized_file_rejected,
        test_storage_s3_fallback,
        # GCS Storage tests (Phase B)
        test_gcs_no_credentials_fallback,
        test_gcs_invalid_base64_credentials,
        test_gcs_content_type_detection,
        test_gcs_path_traversal_in_save,
        test_gcs_empty_filename_handling,
        test_gcs_list_files_not_ready,
        test_gcs_check_connection_not_ready,
        test_gcs_valid_credentials_mock,
        test_gcs_upload_error_fallback,
        test_testing_docs_mode,
        test_health_version_updated,
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
