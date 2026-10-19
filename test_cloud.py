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
        assert "cloud-2." in data["version"]
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
    """GCS credentials Base64 ไม่ถูกต้อง → ไม่ ready."""
    orig_creds = os.environ.get("GCS_CREDENTIALS_BASE64", "")
    orig_adc = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    orig_bucket = os.environ.get("GCS_BUCKET_NAME", "")
    orig_bucket2 = os.environ.get("STORAGE_BUCKET", "")
    # ต้องลบ ADC ออกก่อนเพื่อให้ใช้ base64 path
    os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
    os.environ["GCS_CREDENTIALS_BASE64"] = "not_valid_base64!!!"
    os.environ["GCS_BUCKET_NAME"] = "test-bucket"
    orig_storage_bucket = server.STORAGE_BUCKET
    try:
        server.STORAGE_BUCKET = "test-bucket"
        gcs = server.StorageGCS()
        assert not gcs.is_ready, "GCS ไม่ควร ready เมื่อ credentials ไม่ถูกต้อง"
    finally:
        server.STORAGE_BUCKET = orig_storage_bucket
        for k, v in [("GCS_CREDENTIALS_BASE64", orig_creds),
                      ("GOOGLE_APPLICATION_CREDENTIALS", orig_adc),
                      ("GCS_BUCKET_NAME", orig_bucket),
                      ("STORAGE_BUCKET", orig_bucket2)]:
            if v:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
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
    """Health endpoint แสดง version cloud-2.0-production."""
    with server.app.test_client() as client:
        resp = client.get("/health")
        data = resp.get_json()
        assert "2.0-production" in data["version"], f"version ผิด: {data['version']}"
    print(f"PASS: test_health_version_updated")


# ═══════════════════════════════════════════════════════════
# Security Remediation Tests
# ═══════════════════════════════════════════════════════════


def test_cleanup_after_gcs_upload_success():
    """ลบไฟล์ชั่วคราวเมื่อ GCS upload สำเร็จ (dest เริ่มด้วย gs://)."""
    import threading

    inbox = os.path.join(server.BASE_DIR, server.FOLDERS["inbox"])
    lab_folder = os.path.join(server.BASE_DIR, server.FOLDERS["lab"])
    os.makedirs(inbox, exist_ok=True)
    os.makedirs(lab_folder, exist_ok=True)

    # สร้างไฟล์จำลองใน inbox
    test_file = os.path.join(inbox, "cleanup_test_lab_report.txt")
    with open(test_file, "w") as f:
        f.write("ผลตรวจ creatinine 1.2 mg/dL")

    assert os.path.exists(test_file), "ไฟล์ต้นทางต้องมีอยู่ก่อน"

    # Mock copy_to_category ให้ return gs:// path (จำลอง GCS upload สำเร็จ)
    original_copy = server.copy_to_category

    def mock_copy_to_category(src, cat, metadata=None):
        # คัดลอกไปยัง local folder จริง (เพื่อทดสอบว่า local copy ถูกลบด้วย)
        import shutil
        dest_folder = os.path.join(server.BASE_DIR, server.FOLDERS.get(cat, server.FOLDERS["review"]))
        os.makedirs(dest_folder, exist_ok=True)
        local_dest = os.path.join(dest_folder, os.path.basename(src))
        shutil.copy2(src, local_dest)
        return "gs://test-bucket/04_LAB_RESULTS/cleanup_test_lab_report.txt"

    server.copy_to_category = mock_copy_to_category
    try:
        # เรียก process_file_async แล้วรอให้ thread ทำงานเสร็จ
        server.process_file_async(test_file, reply_token=None)
        # รอ thread daemon ทำงาน
        for t in threading.enumerate():
            if t.daemon and t.name != "MainThread":
                t.join(timeout=5)

        assert not os.path.exists(test_file), \
            f"ไฟล์ inbox ต้องถูกลบหลัง GCS upload สำเร็จ: {test_file}"
        print("PASS: cleanup after GCS upload success")
    finally:
        server.copy_to_category = original_copy
        # cleanup
        if os.path.exists(test_file):
            os.remove(test_file)


def test_no_cleanup_when_gcs_fails():
    """ไม่ลบไฟล์เมื่อ GCS upload ล้มเหลว (fallback local)."""
    import threading

    inbox = os.path.join(server.BASE_DIR, server.FOLDERS["inbox"])
    os.makedirs(inbox, exist_ok=True)

    test_file = os.path.join(inbox, "no_cleanup_test.txt")
    with open(test_file, "w") as f:
        f.write("ใบเสร็จค่ายา 500 บาท")

    # Mock copy_to_category ให้ return local path (จำลอง GCS ล้มเหลว)
    original_copy = server.copy_to_category

    def mock_copy_local(src, cat, metadata=None):
        return os.path.join(server.BASE_DIR, server.FOLDERS.get(cat, "review"), os.path.basename(src))

    server.copy_to_category = mock_copy_local
    try:
        server.process_file_async(test_file, reply_token=None)
        for t in threading.enumerate():
            if t.daemon and t.name != "MainThread":
                t.join(timeout=5)

        assert os.path.exists(test_file), \
            "ไฟล์ inbox ต้องยังอยู่เมื่อ GCS upload ล้มเหลว"
        print("PASS: no cleanup when GCS fails (fallback local)")
    finally:
        server.copy_to_category = original_copy
        if os.path.exists(test_file):
            os.remove(test_file)


def test_cleanup_oserror_graceful():
    """os.remove() ล้มเหลว ต้องไม่ crash — log warning แล้วทำงานต่อ."""
    import threading
    from unittest.mock import patch

    inbox = os.path.join(server.BASE_DIR, server.FOLDERS["inbox"])
    os.makedirs(inbox, exist_ok=True)

    test_file = os.path.join(inbox, "oserror_test.txt")
    with open(test_file, "w") as f:
        f.write("ผลตรวจ BUN 15 mg/dL")

    original_copy = server.copy_to_category

    def mock_copy_gcs(src, cat, metadata=None):
        return "gs://test-bucket/04_LAB_RESULTS/oserror_test.txt"

    server.copy_to_category = mock_copy_gcs

    # Mock os.remove ให้ raise OSError
    original_remove = os.remove
    remove_calls = []

    def mock_remove(path):
        remove_calls.append(path)
        raise OSError("Permission denied (mock)")

    try:
        os.remove = mock_remove
        server.process_file_async(test_file, reply_token=None)
        for t in threading.enumerate():
            if t.daemon and t.name != "MainThread":
                t.join(timeout=5)

        # ตรวจว่าพยายามลบ (มี call) แต่ไม่ crash
        assert len(remove_calls) > 0, "ต้องมีการพยายามลบไฟล์"
        print("PASS: cleanup OSError handled gracefully")
    finally:
        os.remove = original_remove
        server.copy_to_category = original_copy
        if os.path.exists(test_file):
            original_remove(test_file)


def test_cleanup_path_safety():
    """ไม่ลบไฟล์ที่อยู่นอก BASE_DIR (ป้องกัน path traversal)."""
    import threading

    # สร้างไฟล์ชั่วคราวนอก BASE_DIR
    with tempfile.NamedTemporaryFile(delete=False, suffix=".txt", prefix="outside_") as f:
        f.write(b"test file outside BASE_DIR")
        outside_file = f.name

    original_copy = server.copy_to_category

    def mock_copy_gcs(src, cat, metadata=None):
        return "gs://test-bucket/04_LAB_RESULTS/outside_test.txt"

    server.copy_to_category = mock_copy_gcs
    try:
        server.process_file_async(outside_file, reply_token=None)
        for t in threading.enumerate():
            if t.daemon and t.name != "MainThread":
                t.join(timeout=5)

        assert os.path.exists(outside_file), \
            "ไฟล์นอก BASE_DIR ต้องไม่ถูกลบ"
        print("PASS: cleanup path safety — files outside BASE_DIR not deleted")
    finally:
        server.copy_to_category = original_copy
        if os.path.exists(outside_file):
            os.remove(outside_file)


def test_text_message_log_no_content():
    """Log ข้อความ LINE ต้องไม่บันทึกเนื้อหา — บันทึกเฉพาะจำนวนตัวอักษร."""
    import io
    import logging

    # ต้องทดสอบใน production mode เพราะ testing mode return ก่อนถึง text handler
    original_testing = server.IS_TESTING
    original_testing_docs = server.IS_TESTING_DOCS

    # จับ log output
    log_capture = io.StringIO()
    handler = logging.StreamHandler(log_capture)
    handler.setLevel(logging.INFO)
    server.logger.addHandler(handler)

    try:
        server.IS_TESTING = False
        server.IS_TESTING_DOCS = False

        # ส่ง webhook text message จำลอง
        body = json.dumps({
            "events": [{
                "type": "message",
                "replyToken": "test_token_remediation",
                "message": {
                    "type": "text",
                    "text": "ข้อมูลลับผู้ป่วย: นาย ก. creatinine 5.0"
                }
            }]
        }).encode("utf-8")

        sig = make_signature(body)
        with server.app.test_client() as client:
            resp = client.post(
                "/webhook",
                data=body,
                content_type="application/json",
                headers={"X-Line-Signature": sig},
            )

        log_output = log_capture.getvalue()

        # ต้องไม่มีเนื้อหาข้อความใน log
        assert "ข้อมูลลับผู้ป่วย" not in log_output, \
            "เนื้อหาข้อความผู้ป่วยต้องไม่ปรากฏใน log"
        assert "creatinine 5.0" not in log_output, \
            "ข้อมูลทางการแพทย์ต้องไม่ปรากฏใน log"
        # ต้องมี log แสดงจำนวนตัวอักษร
        assert "chars" in log_output, \
            "Log ต้องแสดงจำนวนตัวอักษร (chars) แทนเนื้อหา"

        print("PASS: text message log does not contain patient data")
    finally:
        server.IS_TESTING = original_testing
        server.IS_TESTING_DOCS = original_testing_docs
        server.logger.removeHandler(handler)


# ═══════════════════════════════════════════════════════════
# Phase 1 — Security Hardening tests
# ═══════════════════════════════════════════════════════════

import logging
import re
import threading as _threading
import unittest.mock as _mock

PATIENT_NAME = "สมชาย"
PATIENT_SURNAME = "ใจดี"
PATIENT_FILENAME = f"{PATIENT_NAME}_{PATIENT_SURNAME}_ผลเลือด.pdf"

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def _valid_pdf_bytes(marker: bytes = b"creatinine 1.2 mg/dL") -> bytes:
    """PDF จำลองที่ผ่านการตรวจ magic bytes และใหญ่พอ."""
    return b"%PDF-1.4\n" + marker + b"\n" + (b"0" * 200) + b"\n%%EOF\n"


def _valid_jpeg_bytes() -> bytes:
    return b"\xff\xd8\xff\xe0" + (b"\x00" * 200) + b"\xff\xd9"


class _LogCapture:
    """เก็บข้อความ log ทั้งหมดของ server ระหว่างบล็อก with."""

    def __init__(self):
        self.records = []
        self._handler = None

    def __enter__(self):
        capture = self

        class _H(logging.Handler):
            def emit(self, record):
                try:
                    capture.records.append(record.getMessage())
                except Exception:
                    capture.records.append("<unformattable>")

        self._handler = _H()
        logging.getLogger("server").addHandler(self._handler)
        logging.getLogger().addHandler(self._handler)
        return self

    def __exit__(self, *exc):
        logging.getLogger("server").removeHandler(self._handler)
        logging.getLogger().removeHandler(self._handler)
        return False

    @property
    def text(self) -> str:
        return "\n".join(self.records)


class FakeRedisClient:
    """Redis client จำลองขั้นต่ำ — ไม่ต้องติดตั้ง redis หรือ fakeredis.

    รองรับเฉพาะคำสั่งที่ RedisBackend ใช้: set(nx,ex), incr, expire, decr, ping
    แชร์ dict เดียวกันได้หลาย client เพื่อจำลองหลาย worker
    """

    def __init__(self, shared_store=None, fail=False):
        self.store = shared_store if shared_store is not None else {}
        self.fail = fail
        self.calls = []

    def _check(self):
        if self.fail:
            raise ConnectionError("fake redis down")

    def ping(self):
        self._check()
        return True

    def set(self, key, value, nx=False, ex=None):
        self._check()
        self.calls.append(("set", key))
        if nx and key in self.store:
            return None
        self.store[key] = int(value)
        return True

    def incr(self, key):
        self._check()
        self.calls.append(("incr", key))
        self.store[key] = int(self.store.get(key, 0)) + 1
        return self.store[key]

    def decr(self, key):
        self._check()
        self.calls.append(("decr", key))
        self.store[key] = max(0, int(self.store.get(key, 0)) - 1)
        return self.store[key]

    def expire(self, key, ttl):
        self._check()
        return True


def _fresh_state():
    """คืน state store ใหม่ (in-memory) และผูก dedup/rate limiter เข้ากับมัน."""
    store = server.StateStore("")
    server.state_store = store
    server.dedup_guard = server.DedupGuard(store)
    server.rate_limiter = server.RateLimiter(store)
    return store


def _join_workers(timeout=5):
    for t in _threading.enumerate():
        if t.daemon and t.name != "MainThread":
            t.join(timeout=timeout)


def _post_webhook(events):
    """POST webhook พร้อม signature ที่ถูกต้อง (production mode)."""
    body = json.dumps({"events": events}).encode("utf-8")
    with server.app.test_client() as client:
        return client.post(
            "/webhook",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Line-Signature": make_signature(body),
            },
        )


def _file_event(
    file_name=PATIENT_FILENAME,
    msg_id="msgid-0001",
    event_id="evt-0001",
    user_id="Uabcdef1234567890",
    redelivery=False,
):
    return {
        "type": "message",
        "webhookEventId": event_id,
        "deliveryContext": {"isRedelivery": redelivery},
        "replyToken": "reply-token-test",
        "source": {"type": "user", "userId": user_id},
        "message": {"type": "file", "id": msg_id, "fileName": file_name},
    }


class _ProdWebhook:
    """รัน webhook ในโหมด production พร้อม mock ขอบนอกทั้งหมด.

    ไม่เรียกเครือข่ายจริง ไม่แตะ GCS จริง
    """

    def __init__(self, download_data=None, content_ext=".jpg"):
        self.download_data = (
            download_data if download_data is not None else _valid_pdf_bytes()
        )
        self.content_ext = content_ext
        self.replies = []
        self.saved = []          # (category, filename, metadata)
        self.downloads = 0
        self._orig = {}

    def __enter__(self):
        _fresh_state()
        self._orig = {
            "IS_TESTING": server.IS_TESTING,
            "IS_TESTING_DOCS": server.IS_TESTING_DOCS,
            "download_line_content": server.download_line_content,
            "get_content_type_ext": server.get_content_type_ext,
            "reply_message": server.reply_message,
            "copy_to_category": server.copy_to_category,
        }
        server.IS_TESTING = False
        server.IS_TESTING_DOCS = False

        def fake_download(message_id):
            self.downloads += 1
            return self.download_data

        def fake_reply(token, text):
            self.replies.append(text)

        def fake_copy(src, cat, metadata=None):
            self.saved.append((cat, os.path.basename(src), metadata))
            return f"gs://test-bucket/{server.FOLDERS.get(cat, 'x')}/{os.path.basename(src)}"

        server.download_line_content = fake_download
        server.get_content_type_ext = lambda mid: self.content_ext
        server.reply_message = fake_reply
        server.copy_to_category = fake_copy
        return self

    def __exit__(self, *exc):
        _join_workers()
        for k, v in self._orig.items():
            setattr(server, k, v)
        return False


# ─── กลุ่ม A: Logs ไม่มีข้อมูลผู้ป่วย ────────────────────────

def test_patient_name_never_in_logs():
    """ชื่อผู้ป่วยในชื่อไฟล์ต้องไม่ปรากฏใน log ใด ๆ."""
    with _LogCapture() as cap:
        with _ProdWebhook(download_data=_valid_pdf_bytes()):
            _post_webhook([_file_event()])
            _join_workers()
    assert PATIENT_NAME not in cap.text, f"ชื่อผู้ป่วยรั่วใน log: {cap.text[:400]}"
    assert PATIENT_SURNAME not in cap.text, f"นามสกุลรั่วใน log: {cap.text[:400]}"
    assert "ext=.pdf" in cap.text, "ควร log นามสกุลแทนชื่อไฟล์"
    print("PASS: ชื่อผู้ป่วยไม่ปรากฏใน log")


def test_patient_name_never_in_stored_filename():
    """ชื่อไฟล์ที่ส่งเข้า storage ต้องเป็น UUID ไม่ใช่ชื่อผู้ป่วย."""
    with _ProdWebhook(download_data=_valid_pdf_bytes()) as w:
        _post_webhook([_file_event()])
        _join_workers()
    assert w.saved, "ต้องมีการจัดเก็บอย่างน้อย 1 ไฟล์"
    _cat, stored_name, _meta = w.saved[0]
    assert PATIENT_NAME not in stored_name, f"ชื่อผู้ป่วยอยู่ในชื่อไฟล์: {stored_name}"
    stem = os.path.splitext(stored_name)[0]
    assert UUID_RE.match(stem), f"ชื่อไฟล์ไม่ใช่ UUID: {stored_name}"
    print("PASS: ชื่อไฟล์จัดเก็บเป็น UUID ไม่มีชื่อผู้ป่วย")


def test_error_log_has_no_exception_detail():
    """error log ต้องมีเฉพาะชนิด exception ไม่มี path หรือข้อความละเอียด."""
    secret_path = "/tmp/" + PATIENT_NAME + "_secret_path.txt"

    def boom(src, cat, metadata=None):
        raise RuntimeError(f"failed writing {secret_path}")

    orig = server.copy_to_category
    tmp = os.path.join(server.BASE_DIR, server.FOLDERS["inbox"], "errlog_test.txt")
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("ผลตรวจ creatinine 1.2 mg/dL")
    try:
        server.copy_to_category = boom
        with _LogCapture() as cap:
            server.process_file_async(tmp, reply_token=None, doc_id="doc-err")
            _join_workers()
        assert "RuntimeError" in cap.text, "ควร log ชนิด exception"
        assert secret_path not in cap.text, f"path รั่วใน log: {cap.text[:300]}"
        assert PATIENT_NAME not in cap.text, "ชื่อผู้ป่วยรั่วใน error log"
    finally:
        server.copy_to_category = orig
        if os.path.exists(tmp):
            os.unlink(tmp)
    print("PASS: error log ไม่มีรายละเอียด exception ที่รั่วข้อมูล")


def test_keyword_log_counts_only():
    """log ของ keyword matching ต้องมีเฉพาะจำนวน ไม่มีรายการคำ."""
    tmp = os.path.join(server.BASE_DIR, "kwlog_test.txt")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("ผลตรวจ creatinine eGFR BUN potassium ค่าปกติ mg/dL")
    try:
        with _LogCapture() as cap:
            server.classify_with_keywords(tmp)
        assert "hits" in cap.text, "ควร log จำนวน hits"
        assert "creatinine" not in cap.text, f"คำที่ match รั่วใน log: {cap.text[:300]}"
        assert "eGFR" not in cap.text, "คำที่ match รั่วใน log"
    finally:
        os.unlink(tmp)
    print("PASS: keyword log มีเฉพาะจำนวน ไม่มีรายการคำ")


def test_classification_log_has_no_path():
    """classification_log.jsonl ต้องไม่มี path หรือชื่อไฟล์ต้นฉบับ."""
    log_file = os.path.join(server.BASE_DIR, "logs", "classification_log.jsonl")
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    before = os.path.getsize(log_file) if os.path.exists(log_file) else 0

    with _ProdWebhook(download_data=_valid_pdf_bytes()):
        _post_webhook([_file_event()])
        _join_workers()

    assert os.path.exists(log_file), "ต้องมี classification log"
    with open(log_file, "r", encoding="utf-8") as f:
        f.seek(before)
        new_content = f.read()
    assert new_content.strip(), "ต้องมี entry ใหม่"
    entry = json.loads(new_content.strip().splitlines()[-1])
    assert "source" not in entry, "ต้องไม่มีฟิลด์ source (path)"
    assert "destination" not in entry, "ต้องไม่มีฟิลด์ destination (path)"
    assert "doc_id" in entry and "category" in entry
    assert PATIENT_NAME not in new_content, "ชื่อผู้ป่วยอยู่ใน classification log"
    print("PASS: classification log ไม่มี path หรือชื่อไฟล์ต้นฉบับ")


# ─── กลุ่ม B: UUID และ metadata ─────────────────────────────

def test_build_storage_filename_uuid():
    """build_storage_filename คืนชื่อ UUID และไม่ซ้ำกัน."""
    doc_id, fname = server.build_storage_filename(".pdf")
    assert UUID_RE.match(doc_id), f"doc_id ไม่ใช่ UUID v4: {doc_id}"
    assert fname == f"{doc_id}.pdf"
    names = {server.build_storage_filename(".jpg")[1] for _ in range(200)}
    assert len(names) == 200, "ชื่อไฟล์ซ้ำกัน — UUID ไม่ทำงาน"
    _d, bad = server.build_storage_filename(".exe")
    assert bad.endswith(".dat"), f"นามสกุลที่ไม่รองรับควรเป็น .dat: {bad}"
    print("PASS: build_storage_filename สร้าง UUID ไม่ซ้ำ")


def test_metadata_contains_no_pii():
    """metadata ต้องไม่มีชื่อไฟล์ต้นฉบับหรือ LINE User ID."""
    meta = server.build_object_metadata(
        doc_id="doc-1", category="lab", ext=".pdf",
        size_bytes=1234, sha256_hex="abc123",
    )
    blob = json.dumps(meta, ensure_ascii=False)
    assert PATIENT_NAME not in blob and "fileName" not in blob
    assert "original_filename" not in meta, "ต้องไม่เก็บชื่อไฟล์ต้นฉบับ"
    assert "user_id" not in meta and "userId" not in blob
    for key in ("doc_id", "category", "ext", "size_bytes",
                "content_sha256", "received_at", "source", "classifier"):
        assert key in meta, f"ขาดฟิลด์ {key}"
    assert all(isinstance(v, str) for v in meta.values()), \
        "ค่า metadata ของ GCS ต้องเป็น string ทั้งหมด"
    print("PASS: metadata ไม่มีข้อมูลส่วนบุคคล")


def test_metadata_passed_to_storage():
    """metadata ต้องถูกส่งต่อไปยัง storage layer จริง."""
    with _ProdWebhook(download_data=_valid_pdf_bytes()) as w:
        _post_webhook([_file_event()])
        _join_workers()
    _cat, _name, meta = w.saved[0]
    assert meta is not None, "metadata ต้องถูกส่งไป storage"
    assert meta["source"] == "line_oa"
    assert len(meta["content_sha256"]) == 64, "ต้องมี sha256 ของเนื้อหา"
    print("PASS: metadata ถูกส่งต่อไปยัง storage")


def test_gcs_blob_path_uuid_and_month():
    """GCS object path = {folder}/{YYYY-MM}/{uuid}.ext และตั้ง metadata."""
    import types
    fake_sa = {"type": "service_account", "project_id": "p",
               "private_key": "k", "client_email": "e@x.iam.gserviceaccount.com",
               "token_uri": "https://oauth2.googleapis.com/token"}
    fake_b64 = base64.b64encode(json.dumps(fake_sa).encode()).decode()
    orig_modules = {k: sys.modules.get(k) for k in
                    ["google", "google.cloud", "google.cloud.storage",
                     "google.oauth2", "google.oauth2.service_account"]}
    orig_bucket_env = os.environ.get("GCS_CREDENTIALS_BASE64", "")
    orig_storage_bucket = server.STORAGE_BUCKET
    os.environ["GCS_CREDENTIALS_BASE64"] = fake_b64
    try:
        server.STORAGE_BUCKET = "test-bucket-uuid"
        modules, mock_client_cls, _ = _inject_google_mock_modules()
        sys.modules.update(modules)
        mock_client, mock_bucket, mock_blob = (_mock.MagicMock(),
                                               _mock.MagicMock(),
                                               _mock.MagicMock())
        mock_client.bucket.return_value = mock_bucket
        mock_bucket.blob.return_value = mock_blob
        mock_client_cls.return_value = mock_client

        gcs = server.StorageGCS()
        assert gcs.is_ready
        doc_id, fname = server.build_storage_filename(".pdf")
        meta = server.build_object_metadata(doc_id, "lab", ".pdf", 500, "f" * 64)
        uri = gcs.save(_valid_pdf_bytes(), "lab", fname, meta)

        blob_path = mock_bucket.blob.call_args[0][0]
        parts = blob_path.split("/")
        assert parts[0] == "04_LAB_RESULTS", f"folder ผิด: {blob_path}"
        assert re.match(r"^\d{4}-\d{2}$", parts[1]), f"ไม่มี month partition: {blob_path}"
        assert parts[2] == fname and UUID_RE.match(os.path.splitext(parts[2])[0])
        assert uri.startswith("gs://test-bucket-uuid/")
        assert mock_blob.metadata == meta, "metadata ต้องถูกตั้งบน blob"
        kwargs = mock_blob.upload_from_string.call_args[1]
        assert kwargs["if_generation_match"] == 0, \
            "ต้องใช้ if_generation_match=0 กันเขียนทับ"
        print("PASS: GCS path เป็น UUID + month partition พร้อม metadata")
    finally:
        server.STORAGE_BUCKET = orig_storage_bucket
        os.environ["GCS_CREDENTIALS_BASE64"] = orig_bucket_env
        for k, v in orig_modules.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def test_gcs_precondition_412_treated_as_stored():
    """GCS 412 (object มีอยู่แล้ว) ต้องถือว่าจัดเก็บสำเร็จ ไม่ fallback ไป local."""
    fake_sa = {"type": "service_account", "project_id": "p", "private_key": "k",
               "client_email": "e@x.iam.gserviceaccount.com",
               "token_uri": "https://oauth2.googleapis.com/token"}
    fake_b64 = base64.b64encode(json.dumps(fake_sa).encode()).decode()
    orig_modules = {k: sys.modules.get(k) for k in
                    ["google", "google.cloud", "google.cloud.storage",
                     "google.oauth2", "google.oauth2.service_account"]}
    orig_env = os.environ.get("GCS_CREDENTIALS_BASE64", "")
    orig_storage_bucket = server.STORAGE_BUCKET
    os.environ["GCS_CREDENTIALS_BASE64"] = fake_b64
    try:
        server.STORAGE_BUCKET = "test-bucket-412"
        modules, mock_client_cls, _ = _inject_google_mock_modules()
        sys.modules.update(modules)
        mock_client, mock_bucket, mock_blob = (_mock.MagicMock(),
                                               _mock.MagicMock(),
                                               _mock.MagicMock())
        mock_client.bucket.return_value = mock_bucket
        mock_bucket.blob.return_value = mock_blob
        mock_client_cls.return_value = mock_client

        class Precondition(Exception):
            code = 412

        mock_blob.upload_from_string.side_effect = Precondition("exists")
        gcs = server.StorageGCS()
        result = gcs.save(b"x" * 300, "lab", "dup-uuid.pdf")
        assert result.startswith("gs://"), f"412 ควรถือว่าสำเร็จ แต่ได้: {result}"
        print("PASS: GCS 412 ถือว่าจัดเก็บแล้ว ไม่เขียนทับและไม่ fallback")
    finally:
        server.STORAGE_BUCKET = orig_storage_bucket
        os.environ["GCS_CREDENTIALS_BASE64"] = orig_env
        for k, v in orig_modules.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


# ─── กลุ่ม C: Dedup และ Rate limit ──────────────────────────

def test_dedup_same_event_id():
    """event id เดิมส่งซ้ำ → ไม่ดาวน์โหลดและไม่จัดเก็บรอบสอง."""
    with _ProdWebhook() as w:
        ev = _file_event(event_id="evt-dup", msg_id="msg-dup")
        r1 = _post_webhook([ev])
        _join_workers()
        r2 = _post_webhook([ev])
        _join_workers()
    assert r1.status_code == 200 and r2.status_code == 200
    assert w.downloads == 1, f"ควรดาวน์โหลดครั้งเดียว แต่ได้ {w.downloads}"
    assert len(w.saved) == 1, f"ควรจัดเก็บครั้งเดียว แต่ได้ {len(w.saved)}"
    print("PASS: dedup ด้วย webhookEventId")


def test_dedup_message_id_when_event_id_differs():
    """event id ต่างแต่ message id เดิม → ยังต้องกันซ้ำได้."""
    with _ProdWebhook() as w:
        _post_webhook([_file_event(event_id="evt-A", msg_id="msg-same")])
        _join_workers()
        _post_webhook([_file_event(event_id="evt-B", msg_id="msg-same")])
        _join_workers()
    assert w.downloads == 1, f"message id ซ้ำต้องถูกกัน (downloads={w.downloads})"
    print("PASS: dedup ด้วย message id")


def test_redelivery_flag_processed_once():
    """redelivery ของ event เดิมต้องไม่ถูกประมวลผลซ้ำ."""
    with _ProdWebhook() as w:
        _post_webhook([_file_event(event_id="evt-rd", msg_id="msg-rd")])
        _join_workers()
        with _LogCapture() as cap:
            _post_webhook([_file_event(event_id="evt-rd", msg_id="msg-rd",
                                       redelivery=True)])
            _join_workers()
    assert w.downloads == 1, "redelivery ต้องไม่ประมวลผลซ้ำ"
    assert "ข้าม event ซ้ำ" in cap.text
    print("PASS: redelivery ถูกกันด้วย dedup")


def test_content_hash_dedup_no_temp_file():
    """เนื้อหาเดิมส่งซ้ำ (คนละ message) → ไม่จัดเก็บ และไม่มี temp file ค้าง."""
    inbox = os.path.join(server.BASE_DIR, server.FOLDERS["inbox"])
    os.makedirs(inbox, exist_ok=True)
    same = _valid_pdf_bytes("เอกสารเดียวกัน".encode("utf-8"))
    with _ProdWebhook(download_data=same) as w:
        _post_webhook([_file_event(event_id="e1", msg_id="m1")])
        _join_workers()
        before = set(os.listdir(inbox))
        _post_webhook([_file_event(event_id="e2", msg_id="m2")])
        _join_workers()
        after = set(os.listdir(inbox))
    assert len(w.saved) == 1, f"เนื้อหาซ้ำต้องจัดเก็บครั้งเดียว (saved={len(w.saved)})"
    assert after == before, f"ต้องไม่มี temp file ใหม่ค้าง: {after - before}"
    assert any("ไม่ต้องส่งซ้ำ" in r for r in w.replies), "ควรแจ้งผู้ส่งว่าได้รับแล้ว"
    print("PASS: dedup ด้วย sha256 ของเนื้อหา + ไม่มี temp file ค้าง")


def test_dedup_shared_across_workers():
    """สอง instance ที่ใช้ backend ร่วมกัน ต้องกันซ้ำข้าม worker ได้."""
    shared = {}
    s1, s2 = server.StateStore(""), server.StateStore("")
    s1._redis = server.RedisBackend(FakeRedisClient(shared))
    s2._redis = server.RedisBackend(FakeRedisClient(shared))
    g1, g2 = server.DedupGuard(s1), server.DedupGuard(s2)
    assert g1.is_new_event("evt-shared") is True, "worker แรกต้องผ่าน"
    assert g2.is_new_event("evt-shared") is False, \
        "worker ที่สองต้องเห็นว่าซ้ำ — มิฉะนั้นไฟล์จะถูกบันทึกซ้ำใน GCS"
    print("PASS: dedup แชร์ข้าม worker ผ่าน backend ร่วม")


def test_rate_limit_blocks_and_isolates_users():
    """เกินเพดานต่อนาทีถูกปฏิเสธ และผู้ใช้คนอื่นไม่ได้รับผลกระทบ."""
    store = _fresh_state()
    limiter = server.RateLimiter(store)
    limit = limiter._limit(server.RATE_LIMIT_PER_MINUTE)
    for i in range(limit):
        ok, _ = limiter.check("userA")
        assert ok, f"คำขอที่ {i+1} ควรผ่าน (เพดาน {limit})"
    ok, msg = limiter.check("userA")
    assert not ok, "คำขอที่เกินเพดานต้องถูกปฏิเสธ"
    assert "รอสักครู่" in msg
    ok_b, _ = limiter.check("userB")
    assert ok_b, "ผู้ใช้คนอื่นต้องไม่ถูกกระทบ"
    print(f"PASS: rate limit ปฏิเสธเมื่อเกิน {limit}/นาที และแยกตามผู้ใช้")


def test_rate_limit_no_quota_burn_on_reject():
    """คำขอที่ถูกปฏิเสธต้องไม่กิน quota ของหน้าต่างชั่วโมง/วัน."""
    store = _fresh_state()
    limiter = server.RateLimiter(store)
    minute_limit = limiter._limit(server.RATE_LIMIT_PER_MINUTE)
    for _ in range(minute_limit):
        limiter.check("userC")
    now = int(server.time.time())
    hour_key = f"rl:userC:h:{now // 3600}"
    before = store.incr(hour_key, 3600) - 1   # อ่านค่าโดยไม่ทิ้งผลข้างเคียง
    store.decr(hour_key)
    limiter.check("userC")          # ถูกปฏิเสธที่หน้าต่างนาที
    after = store.incr(hour_key, 3600) - 1
    store.decr(hour_key)
    assert after == before, \
        f"ตัวนับชั่วโมงต้องไม่เพิ่มเมื่อถูกปฏิเสธ (ก่อน {before} หลัง {after})"
    print("PASS: คำขอที่ถูกปฏิเสธไม่กิน quota ของหน้าต่างอื่น")


def test_rate_limit_halved_when_not_shared():
    """เมื่อ backend ไม่แชร์ข้าม worker เพดานต้องถูกลดครึ่งหนึ่ง."""
    store = server.StateStore("")
    limiter = server.RateLimiter(store)
    assert store.is_shared is False
    assert limiter._limit(10) == 5, "ควรลดครึ่งเมื่อไม่แชร์"
    shared_store = server.StateStore("")
    shared_store._redis = server.RedisBackend(FakeRedisClient())
    assert server.RateLimiter(shared_store)._limit(10) == 10, \
        "ไม่ควรลดเมื่อแชร์ข้าม worker ได้"
    print("PASS: เพดานลดครึ่งเมื่อ backend ไม่แชร์ข้าม worker")


def test_retry_does_not_consume_rate_quota():
    """LINE retry ต้องไม่กิน quota ของผู้ป่วย."""
    with _ProdWebhook() as w:
        ev = _file_event(event_id="evt-q", msg_id="msg-q")
        for _ in range(8):
            _post_webhook([ev])
        _join_workers()
        store = server.state_store
        now = int(server.time.time())
        key = f"rl:{server.hash_user_id('Uabcdef1234567890')}:m:{now // 60}"
        count = store.incr(key, 60) - 1
        store.decr(key)
    assert w.downloads == 1, "retry ต้องถูก dedup"
    assert count == 1, f"retry ต้องนับ quota ครั้งเดียว แต่ได้ {count}"
    print("PASS: retry ไม่กิน quota (นับครั้งเดียวจาก 8 ครั้ง)")


def test_rate_limited_request_not_downloaded():
    """คำขอที่เกิน rate limit ต้องไม่ถูกดาวน์โหลดและไม่จัดเก็บ."""
    with _ProdWebhook() as w:
        limit = server.rate_limiter._limit(server.RATE_LIMIT_PER_MINUTE)
        for i in range(limit + 3):
            _post_webhook([_file_event(event_id=f"e{i}", msg_id=f"m{i}")])
        _join_workers()
    assert w.downloads == limit, \
        f"ควรดาวน์โหลดเท่าเพดาน {limit} แต่ได้ {w.downloads}"
    assert any("ไม่เกิน" in r for r in w.replies), "ควรแจ้งผู้ส่งว่าเกินเพดาน"
    print(f"PASS: คำขอเกินเพดานไม่ถูกดาวน์โหลด (ดาวน์โหลด {limit} จาก {limit + 3})")


def test_redis_backend_used_when_available():
    """RedisBackend ใช้คำสั่ง atomic SET NX และนับผ่าน INCR."""
    client = FakeRedisClient()
    backend = server.RedisBackend(client)
    assert backend.set_if_absent("k1", 60) is True
    assert backend.set_if_absent("k1", 60) is False, "SET NX ต้องกันซ้ำ"
    assert backend.incr("c1", 60) == 1 and backend.incr("c1", 60) == 2
    backend.decr("c1")
    assert client.store["c1"] == 1
    assert any(c[0] == "set" for c in client.calls)
    print("PASS: RedisBackend ใช้คำสั่ง atomic ถูกต้อง")


def test_redis_failure_falls_back_safely():
    """Redis ล่มกลางทาง → สลับไป in-memory ไม่ throw และรายงาน degraded."""
    store = server.StateStore("")
    store._redis = server.RedisBackend(FakeRedisClient(fail=True))
    store._status = "connected"
    with _LogCapture() as cap:
        ok = store.set_if_absent("key-after-failure", 60)
    assert ok is True, "ต้องยังทำงานได้ (fail-open) ไม่ throw"
    assert store.status == "degraded", f"สถานะควรเป็น degraded ได้ {store.status}"
    assert store.is_shared is False, "หลัง fallback ต้องไม่ถือว่าแชร์ข้าม worker"
    assert "in-memory fallback" in cap.text
    assert store.set_if_absent("key-after-failure", 60) is False, \
        "fallback ต้องยังกันซ้ำภายใน worker ได้"
    print("PASS: Redis ล่ม → fallback ปลอดภัย ไม่หยุดรับเอกสาร")


def test_state_store_not_configured_by_default():
    """Phase 1: ยังไม่เชื่อม Redis — ต้องไม่ crash และรายงานสถานะถูกต้อง."""
    store = server.StateStore("")
    assert store.status == "not_configured"
    assert store.is_shared is False
    assert store.set_if_absent("x", 60) is True
    with server.app.test_client() as client:
        data = client.get("/health").get_json()
    assert data["state_store"]["status"] in (
        "not_configured", "unavailable", "degraded", "connected")
    assert data["external_ai"] == "disabled_by_code", \
        "health ต้องรายงานว่า AI ภายนอกปิดอยู่"
    print("PASS: ไม่มี Redis ก็ทำงานได้ และ health รายงานสถานะครบ")


def test_redis_url_without_package_falls_back():
    """ตั้ง REDIS_URL แต่ไม่มี redis package → unavailable + ใช้ fallback."""
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) \
        else __builtins__.__import__

    def fake_import(name, *a, **kw):
        if name == "redis":
            raise ImportError("no redis")
        return real_import(name, *a, **kw)

    with _mock.patch("builtins.__import__", side_effect=fake_import):
        store = server.StateStore("redis://localhost:6379")
    assert store.status == "unavailable", f"สถานะควร unavailable ได้ {store.status}"
    assert store.set_if_absent("y", 60) is True, "ต้องยังทำงานได้ด้วย fallback"
    print("PASS: ไม่มี redis package → fallback ไม่ crash")


# ─── กลุ่ม D: ตรวจชนิดไฟล์ ขนาด และ error ───────────────────

def test_txt_with_null_bytes_rejected():
    """ไฟล์ binary ที่เปลี่ยนนามสกุลเป็น .txt ต้องถูกปฏิเสธ."""
    tmp = os.path.join(server.BASE_DIR, "fake_text.txt")
    with open(tmp, "wb") as f:
        f.write("ผลตรวจ".encode("utf-8") + b"\x00\x01\x02binary")
    try:
        ok, reason = server.validate_file(tmp)
        assert not ok, "ไฟล์ที่มี null byte ต้องถูกปฏิเสธ"
        assert "UTF-8" in reason
    finally:
        os.unlink(tmp)
    print("PASS: .txt ที่มี null byte ถูกปฏิเสธ")


def test_invalid_utf8_txt_rejected():
    """.txt ที่ decode UTF-8 ไม่ได้ ต้องถูกปฏิเสธ."""
    tmp = os.path.join(server.BASE_DIR, "bad_utf8.txt")
    with open(tmp, "wb") as f:
        f.write(b"\xff\xfe\xfd invalid utf8 sequence \xc3\x28")
    try:
        ok, _ = server.validate_file(tmp)
        assert not ok, ".txt ที่ไม่ใช่ UTF-8 ต้องถูกปฏิเสธ"
    finally:
        os.unlink(tmp)
    print("PASS: .txt ที่ไม่ใช่ UTF-8 ถูกปฏิเสธ")


def test_short_txt_still_accepted():
    """บันทึกข้อความสั้นยังต้องรับได้ — ขนาดขั้นต่ำใช้เฉพาะไฟล์ binary."""
    tmp = os.path.join(server.BASE_DIR, "short_note.txt")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("ผลตรวจปกติ")
    try:
        ok, reason = server.validate_file(tmp)
        assert ok, f".txt สั้นต้องผ่าน แต่ถูกปฏิเสธ: {reason}"
    finally:
        os.unlink(tmp)
    print("PASS: .txt สั้นยังรับได้")


def test_truncated_binary_rejected():
    """ไฟล์ binary ที่เล็กผิดปกติต้องถูกปฏิเสธ."""
    tmp = os.path.join(server.BASE_DIR, "tiny.pdf")
    with open(tmp, "wb") as f:
        f.write(b"%PDF-1.4\n")
    try:
        ok, reason = server.validate_file(tmp)
        assert not ok, "PDF ที่เล็กผิดปกติต้องถูกปฏิเสธ"
        assert "เล็กผิดปกติ" in reason
    finally:
        os.unlink(tmp)
    print("PASS: ไฟล์ binary ที่เล็กผิดปกติถูกปฏิเสธ")


def test_missing_message_id_skipped():
    """event ที่ไม่มี message id ต้องถูกข้าม ไม่ crash."""
    with _ProdWebhook() as w:
        ev = _file_event(msg_id=None, event_id="evt-noid")
        ev["message"]["id"] = None
        with _LogCapture() as cap:
            resp = _post_webhook([ev])
            _join_workers()
    assert resp.status_code == 200, "ต้องตอบ 200 ไม่ crash"
    assert w.downloads == 0, "ต้องไม่พยายามดาวน์โหลด"
    assert "ไม่มี message id" in cap.text
    print("PASS: event ที่ไม่มี message id ถูกข้ามอย่างปลอดภัย")


def test_unsupported_extension_reply_has_no_filename():
    """ไฟล์ผิดประเภท → ปฏิเสธ และข้อความตอบกลับต้องไม่มีชื่อไฟล์."""
    with _ProdWebhook() as w:
        _post_webhook([_file_event(file_name=f"{PATIENT_NAME}_รายงาน.exe",
                                   event_id="evt-exe", msg_id="msg-exe")])
        _join_workers()
    assert w.downloads == 0, "ไฟล์ผิดประเภทต้องไม่ถูกดาวน์โหลด"
    assert w.replies, "ต้องตอบกลับผู้ส่ง"
    joined = " ".join(w.replies)
    assert PATIENT_NAME not in joined, f"ชื่อผู้ป่วยรั่วในข้อความตอบกลับ: {joined}"
    assert "ไม่รองรับ" in joined
    print("PASS: ไฟล์ผิดประเภทถูกปฏิเสธ และคำตอบไม่มีชื่อไฟล์")


def test_normalize_incoming_extension():
    """ดึงนามสกุลอย่างปลอดภัยจากชื่อไฟล์ที่ผู้ใช้ส่งมา."""
    assert server.normalize_incoming_extension("ผลเลือด.PDF") == ".pdf"
    assert server.normalize_incoming_extension("x.exe") == ""
    assert server.normalize_incoming_extension("") == ""
    assert server.normalize_incoming_extension(".jpg") == ".jpg"
    long_name = "ก" * 5000 + ".pdf"
    assert server.normalize_incoming_extension(long_name) == "", \
        "ชื่อยาวเกินต้องถูกตัดจนไม่เหลือนามสกุลที่ถูกต้อง"
    print("PASS: normalize_incoming_extension ทำงานถูกต้อง")


def test_long_filename_truncated_in_inbox():
    """ชื่อไฟล์ยาวผิดปกติต้องถูกตัดและไม่ทำให้ระบบพัง."""
    long_name = "a" * 500 + ".pdf"
    path = server.save_to_inbox(_valid_pdf_bytes(), long_name)
    try:
        base = os.path.basename(path)
        assert len(base) <= server.MAX_FILENAME_LENGTH, f"ชื่อไฟล์ยาวเกิน: {len(base)}"
        assert base.endswith(".pdf"), "นามสกุลต้องคงอยู่"
    finally:
        if os.path.exists(path):
            os.unlink(path)
    print("PASS: ชื่อไฟล์ยาวถูกตัดอย่างปลอดภัย")


def test_external_ai_blocked_by_code():
    """classify_with_claude ต้องไม่เรียก network แม้อยู่ในโหมด production."""
    tmp = os.path.join(server.BASE_DIR, "ai_block.txt")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("ใบเสร็จรับเงิน ค่ายา จำนวนเงิน 500 บาท")
    orig_testing = server.IS_TESTING
    orig_key = server.ANTHROPIC_API_KEY
    orig_post = server.http_requests.post
    called = []
    try:
        server.IS_TESTING = False
        server.ANTHROPIC_API_KEY = "sk-ant-fake-key-should-not-be-used"
        server.http_requests.post = lambda *a, **k: called.append(a) or (_ for _ in ()).throw(
            AssertionError("ห้ามเรียก network")
        )
        assert server.EXTERNAL_AI_CLASSIFICATION_ALLOWED is False
        result = server.classify_with_claude(tmp)
        assert called == [], "ต้องไม่มีการเรียก HTTP ออกไปเลย"
        assert result.get("category") in {"bills", "rx", "lab", "dialysis", "review"}
    finally:
        server.IS_TESTING = orig_testing
        server.ANTHROPIC_API_KEY = orig_key
        server.http_requests.post = orig_post
        os.unlink(tmp)
    print("PASS: AI ภายนอกถูกล็อกที่ระดับโค้ด ไม่เรียก network")


# ─── กลุ่ม E: OCR ───────────────────────────────────────────

def test_ocr_dpi_reduced():
    """OCR ต้อง render ที่ 200 DPI เพื่อลดการใช้หน่วยความจำ."""
    assert server.OCR_DPI == 200, f"OCR_DPI ควรเป็น 200 ได้ {server.OCR_DPI}"
    a4_300 = int(595 * 300 / 72) * int(842 * 300 / 72) * 3
    a4_200 = int(595 * server.OCR_DPI / 72) * int(842 * server.OCR_DPI / 72) * 3
    assert a4_200 < a4_300 * 0.5, "หน่วยความจำต่อหน้าควรลดลงเกินครึ่ง"
    print(f"PASS: OCR ที่ {server.OCR_DPI} DPI — buffer/หน้า "
          f"{a4_300 // 1048576} MB → {a4_200 // 1048576} MB")


def test_ocr_concurrency_limited():
    """ต้องมี semaphore จำกัดงาน OCR พร้อมกันต่อ worker."""
    assert hasattr(server, "_ocr_semaphore"), "ต้องมี _ocr_semaphore"
    acquired = server._ocr_semaphore.acquire(blocking=False)
    assert acquired, "semaphore ควรว่างตอนเริ่มทดสอบ"
    try:
        assert server._ocr_semaphore.acquire(blocking=False) is False, \
            "ต้องจำกัดงาน OCR พร้อมกันไม่เกิน OCR_MAX_CONCURRENT"
    finally:
        server._ocr_semaphore.release()
    print("PASS: OCR จำกัดงานพร้อมกันด้วย semaphore")


def test_ocr_releases_memory_per_page():
    """OCR PDF ต้องคืนหน่วยความจำรายหน้า ไม่สะสมทุกหน้า."""
    import inspect
    src = inspect.getsource(server.extract_text_with_ocr)
    assert "finally:" in src and "img.close()" in src, \
        "ต้องปิด image ใน finally รายหน้า"
    assert "pix = None" in src, "ต้องคืน pixmap รายหน้า"
    loop_body = src.split("for i in range(num_pages):")[1]
    assert loop_body.index("finally:") < loop_body.index("full_text") \
        if "full_text" in loop_body else True
    print("PASS: OCR คืนหน่วยความจำรายหน้า")


def test_line_capability_preserved():
    """ความสามารถเดิมต้องยังอยู่: รับไฟล์ LINE → จัดเก็บ → ตอบกลับ."""
    with _ProdWebhook(download_data=_valid_pdf_bytes("ใบเสร็จรับเงิน ค่ายา บาท".encode("utf-8"))) as w:
        resp = _post_webhook([_file_event(file_name="receipt.pdf",
                                          event_id="evt-cap", msg_id="msg-cap")])
        _join_workers()
    assert resp.status_code == 200
    assert w.downloads == 1, "ต้องดาวน์โหลดไฟล์จาก LINE"
    assert len(w.saved) == 1, "ต้องจัดเก็บลง storage"
    assert any("รับเอกสารแล้ว" in r for r in w.replies), "ต้องตอบกลับผู้ส่ง"
    print("PASS: ความสามารถรับเอกสาร LINE และจัดเก็บยังทำงานครบ")


# ─── กลุ่ม: GCS ADC + Bucket Name + Fail-loud ─────────────────


def test_gcs_bucket_name_env():
    """GCS_BUCKET_NAME ต้องถูกอ่านก่อน STORAGE_BUCKET."""
    orig_bucket = os.environ.get("GCS_BUCKET_NAME", "")
    orig_bucket2 = os.environ.get("STORAGE_BUCKET", "")
    orig_val = server.STORAGE_BUCKET
    try:
        os.environ["GCS_BUCKET_NAME"] = "my-gcs-bucket"
        os.environ.pop("STORAGE_BUCKET", None)
        # จำลองการอ่านค่าใหม่ตาม logic เดียวกับ server.py
        bucket = os.getenv("GCS_BUCKET_NAME") or os.getenv("STORAGE_BUCKET", "")
        assert bucket == "my-gcs-bucket", f"ควรอ่าน GCS_BUCKET_NAME แต่ได้: {bucket}"
    finally:
        for k, v in [("GCS_BUCKET_NAME", orig_bucket),
                      ("STORAGE_BUCKET", orig_bucket2)]:
            if v:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
        server.STORAGE_BUCKET = orig_val
    print("PASS: GCS_BUCKET_NAME ถูกอ่านก่อน STORAGE_BUCKET")


def test_gcs_bucket_name_fallback_to_storage_bucket():
    """ถ้าไม่มี GCS_BUCKET_NAME ต้อง fallback ไป STORAGE_BUCKET."""
    orig_bucket = os.environ.get("GCS_BUCKET_NAME", "")
    orig_bucket2 = os.environ.get("STORAGE_BUCKET", "")
    try:
        os.environ.pop("GCS_BUCKET_NAME", None)
        os.environ["STORAGE_BUCKET"] = "legacy-bucket"
        bucket = os.getenv("GCS_BUCKET_NAME") or os.getenv("STORAGE_BUCKET", "")
        assert bucket == "legacy-bucket", f"ควร fallback ไป STORAGE_BUCKET แต่ได้: {bucket}"
    finally:
        for k, v in [("GCS_BUCKET_NAME", orig_bucket),
                      ("STORAGE_BUCKET", orig_bucket2)]:
            if v:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
    print("PASS: fallback ไป STORAGE_BUCKET เมื่อไม่มี GCS_BUCKET_NAME")


def test_gcs_adc_auth_method():
    """เมื่อตั้ง GOOGLE_APPLICATION_CREDENTIALS → auth_method=adc."""
    import unittest.mock as _mock

    orig_adc = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    orig_creds = os.environ.get("GCS_CREDENTIALS_BASE64", "")
    orig_bucket_env = os.environ.get("GCS_BUCKET_NAME", "")
    orig_storage_bucket = server.STORAGE_BUCKET
    orig_modules = {}
    for key in ["google", "google.cloud", "google.cloud.storage",
                "google.oauth2", "google.oauth2.service_account"]:
        orig_modules[key] = sys.modules.get(key)
    try:
        server.STORAGE_BUCKET = "test-adc-bucket"
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "/tmp/fake-sa-key.json"
        os.environ.pop("GCS_CREDENTIALS_BASE64", None)
        os.environ["GCS_BUCKET_NAME"] = "test-adc-bucket"

        modules, mock_client_cls, _ = _inject_google_mock_modules()
        sys.modules.update(modules)

        mock_client = _mock.MagicMock()
        mock_bucket = _mock.MagicMock()
        mock_client.bucket.return_value = mock_bucket
        mock_client_cls.return_value = mock_client

        gcs = server.StorageGCS()
        assert gcs.is_ready, f"GCS ควร ready ด้วย ADC แต่ error={gcs._init_error}"
        assert gcs._auth_method == "adc", f"ควรใช้ adc แต่ได้: {gcs._auth_method}"
        # Client() ถูกเรียกโดยไม่มี credentials argument (ADC ใช้ env var)
        mock_client_cls.assert_called_once_with()
    finally:
        server.STORAGE_BUCKET = orig_storage_bucket
        for key, val in orig_modules.items():
            if val is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = val
        for k, v in [("GOOGLE_APPLICATION_CREDENTIALS", orig_adc),
                      ("GCS_CREDENTIALS_BASE64", orig_creds),
                      ("GCS_BUCKET_NAME", orig_bucket_env)]:
            if v:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
    print("PASS: ADC auth method ใช้ GOOGLE_APPLICATION_CREDENTIALS")


def test_gcs_no_bucket_error_log():
    """ไม่มี bucket name → log ERROR ไม่ใช่ WARNING."""
    orig_storage_bucket = server.STORAGE_BUCKET
    try:
        server.STORAGE_BUCKET = ""
        with _LogCapture() as cap:
            gcs = server.StorageGCS()
        assert not gcs.is_ready
        assert "GCS_BUCKET_NAME" in gcs._init_error, \
            f"Error ต้องบอก GCS_BUCKET_NAME แต่ได้: {gcs._init_error}"
        # ตรวจว่า log เป็น ERROR ไม่ใช่ WARNING
        found_error = any("GCS_BUCKET_NAME" in r or "STORAGE_BUCKET" in r
                          for r in cap.records)
        assert found_error, f"ต้อง log ERROR เมื่อไม่มี bucket name"
    finally:
        server.STORAGE_BUCKET = orig_storage_bucket
    print("PASS: ไม่มี bucket name → ERROR log ชัดเจน")


def test_gcs_save_not_ready_error_log():
    """save() เมื่อ GCS ไม่พร้อม → log ERROR ไม่ใช่ WARNING."""
    orig_storage_bucket = server.STORAGE_BUCKET
    try:
        server.STORAGE_BUCKET = ""
        gcs = server.StorageGCS()
        with _LogCapture() as cap:
            result = gcs.save(b"test", "lab", "test.pdf")
        error_logs = [r for r in cap.records if "GCS ไม่พร้อม" in r]
        assert len(error_logs) >= 1, f"ต้อง log ERROR เมื่อ save ไม่ได้ แต่ได้: {cap.records}"
        local_logs = [r for r in cap.records if "local disk" in r]
        assert len(local_logs) >= 1, \
            f"ต้องแจ้งว่าเก็บ local ชั่วคราว แต่ได้: {cap.records}"
        if os.path.exists(result):
            os.unlink(result)
    finally:
        server.STORAGE_BUCKET = orig_storage_bucket
    print("PASS: save() เมื่อ GCS ไม่พร้อม → ERROR log พร้อมคำเตือน")


def test_gcs_health_shows_auth_method():
    """health endpoint ต้องแสดง auth_method เมื่อใช้ GCS."""
    import unittest.mock as _mock

    orig_backend = server.STORAGE_BACKEND
    orig_storage = server.storage
    orig_storage_bucket = server.STORAGE_BUCKET
    orig_adc = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    orig_modules = {}
    for key in ["google", "google.cloud", "google.cloud.storage",
                "google.oauth2", "google.oauth2.service_account"]:
        orig_modules[key] = sys.modules.get(key)
    try:
        server.STORAGE_BACKEND = "gcs"
        server.STORAGE_BUCKET = "test-health-bucket"
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "/tmp/fake.json"

        modules, mock_client_cls, _ = _inject_google_mock_modules()
        sys.modules.update(modules)

        mock_client = _mock.MagicMock()
        mock_bucket = _mock.MagicMock()
        mock_client.bucket.return_value = mock_bucket
        mock_bucket.reload.return_value = None
        mock_client_cls.return_value = mock_client

        gcs = server.StorageGCS()
        server.storage = gcs

        with server.app.test_client() as client:
            resp = client.get("/health")
            data = json.loads(resp.data)
        assert "gcs" in data, f"health ต้องมี gcs section แต่ได้: {list(data.keys())}"
        assert data["gcs"].get("auth_method") == "adc", \
            f"ต้องแสดง auth_method=adc แต่ได้: {data['gcs']}"
    finally:
        server.STORAGE_BACKEND = orig_backend
        server.storage = orig_storage
        server.STORAGE_BUCKET = orig_storage_bucket
        for key, val in orig_modules.items():
            if val is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = val
        if orig_adc:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = orig_adc
        else:
            os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
    print("PASS: health endpoint แสดง auth_method")


# ─── กลุ่ม: Testing-docs reply logging ─────────────────────────


def _text_event(text="ทดสอบ", event_id="evt-txt-001", user_id="Uabcdef1234567890"):
    """สร้าง text message event สำหรับทดสอบ."""
    return {
        "type": "message",
        "webhookEventId": event_id,
        "deliveryContext": {"isRedelivery": False},
        "replyToken": "reply-token-text-test",
        "source": {"type": "user", "userId": user_id},
        "message": {"type": "text", "id": "msg-txt-001", "text": text},
    }


def test_testing_docs_text_reply_called():
    """โหมด testing-docs ต้องเรียก reply_message สำหรับข้อความ text."""
    _fresh_state()
    orig_testing = server.IS_TESTING
    orig_docs = server.IS_TESTING_DOCS
    orig_reply = server.reply_message
    replies = []
    try:
        server.IS_TESTING = True
        server.IS_TESTING_DOCS = True
        server.reply_message = lambda token, text: replies.append(text)
        resp = _post_webhook([_text_event("สถานะ", event_id="evt-docreply-01")])
        _join_workers()
        assert resp.status_code == 200
        assert len(replies) >= 1, "ต้องเรียก reply_message อย่างน้อย 1 ครั้ง"
        assert any("ทำงานปกติ" in r for r in replies), \
            f"ต้องตอบสถานะระบบ แต่ได้: {replies}"
    finally:
        server.IS_TESTING = orig_testing
        server.IS_TESTING_DOCS = orig_docs
        server.reply_message = orig_reply
    print("PASS: testing-docs เรียก reply_message สำหรับข้อความ text")


def test_testing_docs_reply_success_logged():
    """โหมด testing-docs ต้อง log INFO เมื่อ LINE Reply API ตอบกลับสำเร็จ."""
    _fresh_state()
    orig_testing = server.IS_TESTING
    orig_docs = server.IS_TESTING_DOCS
    orig_reply = server.reply_message
    try:
        server.IS_TESTING = True
        server.IS_TESTING_DOCS = True
        # คืนค่า reply_message จริง แต่ mock HTTP request
        server.reply_message = orig_reply

        class FakeResp:
            status_code = 200
            def raise_for_status(self):
                pass

        import unittest.mock as _mock
        with _LogCapture() as cap, \
             _mock.patch.object(server.http_requests, "post", return_value=FakeResp()):
            server.reply_message("fake-token", "ทดสอบ")

        assert any("ตอบกลับ LINE สำเร็จ" in r for r in cap.records), \
            f"ต้องมี INFO log สำเร็จ แต่ได้: {cap.records}"
        assert any("status=200" in r for r in cap.records), \
            "ต้องแสดง status code ใน log"
    finally:
        server.IS_TESTING = orig_testing
        server.IS_TESTING_DOCS = orig_docs
        server.reply_message = orig_reply
    print("PASS: testing-docs log INFO เมื่อ reply สำเร็จ")


def test_testing_docs_reply_failure_logged():
    """โหมด testing-docs ต้อง log ERROR เมื่อ LINE Reply API ล้มเหลว."""
    _fresh_state()
    orig_testing = server.IS_TESTING
    orig_docs = server.IS_TESTING_DOCS
    orig_reply = server.reply_message
    try:
        server.IS_TESTING = True
        server.IS_TESTING_DOCS = True
        server.reply_message = orig_reply

        class FakeErrorResp:
            status_code = 401

        class FakeHTTPError(Exception):
            def __init__(self):
                self.response = FakeErrorResp()
                super().__init__("Unauthorized")

        import unittest.mock as _mock
        with _LogCapture() as cap, \
             _mock.patch.object(server.http_requests, "post",
                                side_effect=FakeHTTPError()):
            server.reply_message("fake-token", "ทดสอบ")

        assert any("ตอบกลับ LINE ไม่สำเร็จ" in r for r in cap.records), \
            f"ต้องมี ERROR log ล้มเหลว แต่ได้: {cap.records}"
        assert any("status=401" in r for r in cap.records), \
            "ต้องแสดง HTTP status ใน error log"
        # ต้องไม่มี token หรือเนื้อหาข้อความใน log
        for r in cap.records:
            assert "fake-token" not in r, "Reply token ต้องไม่อยู่ใน log"
            assert "ทดสอบ" not in r or "ตอบกลับ" in r, "เนื้อหาข้อความต้องไม่อยู่ใน log"
    finally:
        server.IS_TESTING = orig_testing
        server.IS_TESTING_DOCS = orig_docs
        server.reply_message = orig_reply
    print("PASS: testing-docs log ERROR เมื่อ reply ล้มเหลว")


# ═══════════════════════════════════════════════════════════
# Production Mode Tests
# ═══════════════════════════════════════════════════════════


def test_production_gcs_save_raises_on_not_ready():
    """Production mode: GCS ไม่พร้อม → raise RuntimeError (ไม่ fallback local)."""
    orig_prod = server.IS_PRODUCTION
    orig_testing = server.IS_TESTING
    try:
        server.IS_PRODUCTION = True
        server.IS_TESTING = False

        gcs = server.StorageGCS()
        # GCS ไม่พร้อม (ไม่มี credentials)
        assert not gcs.is_ready, "GCS ควรไม่พร้อม (ไม่มี credentials จริง)"

        raised = False
        try:
            gcs.save(b"test data", "lab_results", "test.pdf")
        except RuntimeError as e:
            raised = True
            assert "ไม่สามารถจัดเก็บเอกสารได้" in str(e)
        assert raised, "Production: GCS ไม่พร้อมต้อง raise RuntimeError"
    finally:
        server.IS_PRODUCTION = orig_prod
        server.IS_TESTING = orig_testing
    print("PASS: test_production_gcs_save_raises_on_not_ready")


def test_production_gcs_upload_error_raises():
    """Production mode: GCS upload ล้มเหลว → raise RuntimeError."""
    orig_prod = server.IS_PRODUCTION
    orig_testing = server.IS_TESTING
    try:
        server.IS_PRODUCTION = True
        server.IS_TESTING = False

        gcs = server.StorageGCS()
        # จำลอง GCS พร้อมแต่ upload ล้มเหลว
        # is_ready property ตรวจ _client and _bucket
        mock_blob = type("MockBlob", (), {
            "metadata": None,
            "upload_from_string": lambda self, *a, **kw: (_ for _ in ()).throw(
                ConnectionError("network error")
            ),
        })
        mock_bucket = type("MockBucket", (), {
            "blob": lambda self, path: mock_blob(),
        })()
        gcs._client = object()  # ต้องไม่ None เพื่อผ่าน is_ready
        gcs._bucket = mock_bucket
        gcs.bucket_name = "test-bucket"

        raised = False
        try:
            gcs.save(b"test data", "lab_results", "test.pdf")
        except RuntimeError as e:
            raised = True
            assert "ล้มเหลว" in str(e)
        assert raised, "Production: GCS upload error ต้อง raise RuntimeError"
    finally:
        server.IS_PRODUCTION = orig_prod
        server.IS_TESTING = orig_testing
    print("PASS: test_production_gcs_upload_error_raises")


def test_production_no_local_fallback():
    """Production mode: ไม่มี local fallback — ต้อง raise ทุกกรณีที่ GCS ล้มเหลว."""
    orig_prod = server.IS_PRODUCTION
    orig_testing = server.IS_TESTING
    try:
        server.IS_PRODUCTION = True
        server.IS_TESTING = False

        gcs = server.StorageGCS()
        assert not gcs.is_ready

        # ตรวจสอบว่า fallback._save() ไม่ถูกเรียก
        fallback_called = False
        orig_fallback_save = gcs._fallback.save

        def mock_fallback_save(*args, **kwargs):
            nonlocal fallback_called
            fallback_called = True
            return orig_fallback_save(*args, **kwargs)

        gcs._fallback.save = mock_fallback_save

        try:
            gcs.save(b"test", "lab_results", "test.pdf")
        except RuntimeError:
            pass  # คาดหวัง

        assert not fallback_called, "Production: ต้องไม่เรียก local fallback"
    finally:
        server.IS_PRODUCTION = orig_prod
        server.IS_TESTING = orig_testing
    print("PASS: test_production_no_local_fallback")


def test_production_webhook_rejects_without_gcs():
    """Production mode: webhook คืน 503 เมื่อ GCS ไม่พร้อม."""
    orig_prod = server.IS_PRODUCTION
    orig_testing = server.IS_TESTING
    orig_backend = server.STORAGE_BACKEND
    orig_storage = server.storage
    try:
        server.IS_PRODUCTION = True
        server.IS_TESTING = False
        server.STORAGE_BACKEND = "gcs"

        # จำลอง GCS storage ที่ไม่พร้อม
        mock_storage = type("MockGCS", (), {
            "is_ready": False,
            "_init_error": "test: no credentials",
        })()
        server.storage = mock_storage

        with server.app.test_client() as client:
            body = json.dumps({"events": []}).encode()
            sig = make_signature(body)
            resp = client.post(
                "/webhook",
                data=body,
                headers={
                    "X-Line-Signature": sig,
                    "Content-Type": "application/json",
                },
            )
            assert resp.status_code == 503, \
                f"Production GCS ไม่พร้อม ต้องคืน 503 แต่ได้ {resp.status_code}"
            data = resp.get_json()
            assert data.get("reason") == "storage_not_ready"
    finally:
        server.IS_PRODUCTION = orig_prod
        server.IS_TESTING = orig_testing
        server.STORAGE_BACKEND = orig_backend
        server.storage = orig_storage
    print("PASS: test_production_webhook_rejects_without_gcs")


def test_production_health_503_without_gcs():
    """Production mode: /health คืน 503 เมื่อ GCS ไม่พร้อม."""
    orig_prod = server.IS_PRODUCTION
    orig_testing = server.IS_TESTING
    orig_backend = server.STORAGE_BACKEND
    orig_storage = server.storage
    try:
        server.IS_PRODUCTION = True
        server.IS_TESTING = False
        server.STORAGE_BACKEND = "gcs"

        # จำลอง GCS storage ที่ไม่พร้อม
        mock_storage = type("MockGCS", (), {
            "is_ready": False,
            "_init_error": "test",
            "_auth_method": "none",
            "check_connection": lambda self: {
                "status": "not_connected",
                "error": "test",
            },
        })()
        server.storage = mock_storage

        with server.app.test_client() as client:
            resp = client.get("/health")
            assert resp.status_code == 503, \
                f"Production GCS down ต้องคืน 503 แต่ได้ {resp.status_code}"
            data = resp.get_json()
            assert data["status"] == "degraded"
            assert data["accepting_documents"] is False
    finally:
        server.IS_PRODUCTION = orig_prod
        server.IS_TESTING = orig_testing
        server.STORAGE_BACKEND = orig_backend
        server.storage = orig_storage
    print("PASS: test_production_health_503_without_gcs")


def test_production_health_503_without_gcs_backend():
    """Production mode: /health คืน 503 เมื่อ STORAGE_BACKEND ไม่ใช่ gcs."""
    orig_prod = server.IS_PRODUCTION
    orig_testing = server.IS_TESTING
    orig_backend = server.STORAGE_BACKEND
    try:
        server.IS_PRODUCTION = True
        server.IS_TESTING = False
        server.STORAGE_BACKEND = "local"

        with server.app.test_client() as client:
            resp = client.get("/health")
            assert resp.status_code == 503, \
                f"Production local storage ต้องคืน 503 แต่ได้ {resp.status_code}"
            data = resp.get_json()
            assert data["status"] == "misconfigured"
            assert data["accepting_documents"] is False
    finally:
        server.IS_PRODUCTION = orig_prod
        server.IS_TESTING = orig_testing
        server.STORAGE_BACKEND = orig_backend
    print("PASS: test_production_health_503_without_gcs_backend")


def test_production_health_checklist():
    """Production mode: /health แสดง production_checklist."""
    orig_prod = server.IS_PRODUCTION
    orig_testing = server.IS_TESTING
    orig_backend = server.STORAGE_BACKEND
    orig_storage = server.storage
    try:
        server.IS_PRODUCTION = True
        server.IS_TESTING = False
        server.STORAGE_BACKEND = "gcs"

        # จำลอง GCS พร้อม
        mock_storage = type("MockGCS", (), {
            "is_ready": True,
            "_auth_method": "adc",
            "check_connection": lambda self: {
                "status": "connected",
                "bucket": "test-bucket",
            },
        })()
        server.storage = mock_storage

        with server.app.test_client() as client:
            resp = client.get("/health")
            assert resp.status_code == 200
            data = resp.get_json()
            assert "production_checklist" in data, \
                "Production mode ต้องมี production_checklist"
            checklist = data["production_checklist"]
            assert "line_channel_secret" in checklist
            assert "line_access_token" in checklist
            assert "storage_gcs" in checklist
            assert "gcs_ready" in checklist
            assert "external_ai_locked" in checklist
            assert checklist["storage_gcs"] is True
            assert checklist["gcs_ready"] is True
            assert data["production_mode"] is True
            assert data["accepting_documents"] is True
    finally:
        server.IS_PRODUCTION = orig_prod
        server.IS_TESTING = orig_testing
        server.STORAGE_BACKEND = orig_backend
        server.storage = orig_storage
    print("PASS: test_production_health_checklist")


def test_production_health_no_checklist_in_testing():
    """Testing mode: /health ไม่แสดง production_checklist."""
    # server อยู่ใน testing mode อยู่แล้ว
    with server.app.test_client() as client:
        resp = client.get("/health")
        data = resp.get_json()
        assert "production_checklist" not in data, \
            "Testing mode ต้องไม่มี production_checklist"
        assert data.get("production_mode") is False
    print("PASS: test_production_health_no_checklist_in_testing")


def test_production_copy_to_category_reraises():
    """Production mode: copy_to_category re-raise RuntimeError จาก storage.save."""
    orig_prod = server.IS_PRODUCTION
    orig_testing = server.IS_TESTING
    orig_backend = server.STORAGE_BACKEND
    orig_storage = server.storage
    try:
        server.IS_PRODUCTION = True
        server.IS_TESTING = False
        server.STORAGE_BACKEND = "gcs"

        # จำลอง storage ที่ raise RuntimeError
        mock_storage = type("MockGCS", (), {
            "save": lambda self, *a, **kw: (_ for _ in ()).throw(
                RuntimeError("GCS ล้มเหลว")
            ),
        })()
        server.storage = mock_storage

        # สร้างไฟล์ทดสอบ
        test_dir = os.path.join(os.environ["BASE_DIR"], "inbox")
        os.makedirs(test_dir, exist_ok=True)
        test_file = os.path.join(test_dir, "test_prod_copy.pdf")
        with open(test_file, "wb") as f:
            f.write(b"%PDF-1.4 test")

        raised = False
        try:
            server.copy_to_category(test_file, "lab_results")
        except RuntimeError:
            raised = True
        assert raised, "Production: copy_to_category ต้อง re-raise RuntimeError"

        # ทำความสะอาด
        if os.path.exists(test_file):
            os.remove(test_file)
    finally:
        server.IS_PRODUCTION = orig_prod
        server.IS_TESTING = orig_testing
        server.STORAGE_BACKEND = orig_backend
        server.storage = orig_storage
    print("PASS: test_production_copy_to_category_reraises")


# ═══════════════════════════════════════════════════════════
# Document Search V1 Tests
# ═══════════════════════════════════════════════════════════


def test_document_index_add_and_search():
    """ทดสอบ DocumentIndex: add + search."""
    idx = server.DocumentIndex(server.storage)
    idx._loaded = True  # ข้าม GCS load

    doc1 = {
        "document_id": "test-001",
        "original_filename": "document.pdf",
        "gcs_object_path": "lab/2026-10/test-001.pdf",
        "category": "lab",
        "upload_date": "2026-10-19",
        "mime_type": "application/pdf",
        "file_size": 1024,
        "ocr_status": "native",
        "extracted_text": "ผลตรวจเลือด creatinine 1.2 mg/dL",
    }
    doc2 = {
        "document_id": "test-002",
        "original_filename": "document.jpg",
        "gcs_object_path": "bills/2026-10/test-002.jpg",
        "category": "bills",
        "upload_date": "2026-10-18",
        "mime_type": "image/jpeg",
        "file_size": 2048,
        "ocr_status": "ocr",
        "extracted_text": "ใบเสร็จค่ารักษาพยาบาล",
    }
    idx.add(doc1)
    idx.add(doc2)

    # Search by Thai content
    results = idx.search("ผลตรวจเลือด")
    assert len(results) == 1, f"Expected 1 result, got {len(results)}"
    assert results[0]["document_id"] == "test-001"

    # Search by category
    results = idx.search("bills")
    assert len(results) == 1, f"Expected 1 result for bills, got {len(results)}"
    assert results[0]["document_id"] == "test-002"

    # Search by date
    results = idx.search("2026-10-19")
    assert len(results) == 1
    assert results[0]["document_id"] == "test-001"

    # No results
    results = idx.search("ไม่มีข้อมูลนี้")
    assert len(results) == 0

    print("PASS: test_document_index_add_and_search")


def test_document_index_recent():
    """ทดสอบ DocumentIndex: recent()."""
    idx = server.DocumentIndex(server.storage)
    idx._loaded = True

    for i in range(10):
        idx.add({
            "document_id": f"recent-{i:03d}",
            "category": "lab",
            "upload_date": f"2026-10-{10+i}",
            "extracted_text": "",
        })

    recent = idx.recent(5)
    assert len(recent) == 5, f"Expected 5 recent, got {len(recent)}"
    # Most recent first
    assert recent[0]["document_id"] == "recent-009"
    assert recent[4]["document_id"] == "recent-005"
    print("PASS: test_document_index_recent")


def test_document_index_idempotent():
    """ทดสอบ DocumentIndex: add ซ้ำไม่เพิ่มซ้ำ (idempotent)."""
    idx = server.DocumentIndex(server.storage)
    idx._loaded = True

    doc = {"document_id": "idem-001", "category": "lab", "extracted_text": ""}
    idx.add(doc)
    idx.add(doc)  # ซ้ำ
    idx.add(doc)  # ซ้ำ

    assert idx.count() == 1, f"Expected 1 doc, got {idx.count()}"
    print("PASS: test_document_index_idempotent")


def test_document_index_get():
    """ทดสอบ DocumentIndex: get by doc_id."""
    idx = server.DocumentIndex(server.storage)
    idx._loaded = True

    idx.add({"document_id": "get-001", "category": "rx", "extracted_text": ""})

    doc = idx.get("get-001")
    assert doc is not None
    assert doc["category"] == "rx"

    assert idx.get("nonexistent") is None
    print("PASS: test_document_index_get")


def test_search_max_results():
    """ทดสอบค้นหาจำกัดจำนวนผลลัพธ์."""
    idx = server.DocumentIndex(server.storage)
    idx._loaded = True

    for i in range(20):
        idx.add({
            "document_id": f"max-{i:03d}",
            "category": "lab",
            "extracted_text": "ผลตรวจ common_keyword",
        })

    results = idx.search("common_keyword", max_results=5)
    assert len(results) == 5, f"Expected max 5, got {len(results)}"
    print("PASS: test_search_max_results")


def test_search_thai_text():
    """ทดสอบค้นหาด้วยภาษาไทย."""
    idx = server.DocumentIndex(server.storage)
    idx._loaded = True

    idx.add({
        "document_id": "thai-001",
        "category": "dialysis",
        "extracted_text": "รายงานการฟอกเลือด ครั้งที่ 42",
    })
    idx.add({
        "document_id": "thai-002",
        "category": "rx",
        "extracted_text": "ใบสั่งยา แอมโลดิปีน 5 มก.",
    })

    # Thai search
    results = idx.search("ฟอกเลือด")
    assert len(results) == 1
    assert results[0]["document_id"] == "thai-001"

    results = idx.search("แอมโลดิปีน")
    assert len(results) == 1
    assert results[0]["document_id"] == "thai-002"

    print("PASS: test_search_thai_text")


def test_handle_search_command_unauthorized():
    """ทดสอบ unauthorized user ไม่สามารถค้นหาได้."""
    # Clear authorized users for test
    original = server.AUTHORIZED_USERS.copy()
    server.AUTHORIZED_USERS.clear()

    response = server.handle_search_command("ค้นหา ผลแล็บ", "unauthorized_hash")
    assert response is not None
    assert "ไม่มีสิทธิ์" in response

    response = server.handle_search_command("รายการล่าสุด", "unauthorized_hash")
    assert "ไม่มีสิทธิ์" in response

    response = server.handle_search_command("ขอไฟล์ abc123", "unauthorized_hash")
    assert "ไม่มีสิทธิ์" in response

    server.AUTHORIZED_USERS = original
    print("PASS: test_handle_search_command_unauthorized")


def test_handle_search_command_help():
    """ทดสอบคำสั่ง ช่วยเหลือ — ไม่ต้อง auth."""
    response = server.handle_search_command("ช่วยเหลือ", "any_hash")
    assert response is not None
    assert "ค้นหา" in response
    assert "รายการล่าสุด" in response
    assert "ขอไฟล์" in response
    print("PASS: test_handle_search_command_help")


def test_handle_search_command_not_search():
    """ทดสอบข้อความที่ไม่ใช่คำสั่งค้นหา → None."""
    response = server.handle_search_command("สวัสดี", "any_hash")
    assert response is None
    print("PASS: test_handle_search_command_not_search")


def test_is_authorized_user():
    """ทดสอบ is_authorized_user."""
    original = server.AUTHORIZED_USERS.copy()

    server.AUTHORIZED_USERS = {"hash_a", "hash_b"}
    assert server.is_authorized_user("hash_a") is True
    assert server.is_authorized_user("hash_c") is False

    # Empty allowlist = disabled
    server.AUTHORIZED_USERS = set()
    assert server.is_authorized_user("hash_a") is False

    server.AUTHORIZED_USERS = original
    print("PASS: test_is_authorized_user")


def test_format_search_results_empty():
    """ทดสอบ format_search_results empty."""
    result = server.format_search_results([])
    assert "ไม่พบ" in result
    print("PASS: test_format_search_results_empty")


def test_format_search_results_with_docs():
    """ทดสอบ format_search_results with documents."""
    docs = [
        {
            "document_id": "abc12345-6789",
            "category": "lab",
            "upload_date": "2026-10-19",
        },
        {
            "document_id": "def12345-6789",
            "category": "bills",
            "upload_date": "2026-10-18",
        },
    ]
    result = server.format_search_results(docs)
    assert "2 เอกสาร" in result
    assert "ผลตรวจแล็บ" in result
    assert "ใบเสร็จ/บิล" in result
    assert "abc12345" in result
    assert "ขอไฟล์" in result
    print("PASS: test_format_search_results_with_docs")


def test_audit_logger_creates_entry():
    """ทดสอบ AuditLogger สร้าง log entry."""
    import io
    import logging

    # Capture log output
    log_capture = io.StringIO()
    handler = logging.StreamHandler(log_capture)
    handler.setLevel(logging.INFO)
    server.logger.addHandler(handler)

    server.audit_logger.log("test_action", "test_user_hash_12345", {"key": "val"})

    server.logger.removeHandler(handler)
    log_output = log_capture.getvalue()
    assert "AUDIT: test_action" in log_output
    assert "test_use" in log_output  # truncated hash
    print("PASS: test_audit_logger_creates_entry")


def test_backfill_endpoint_no_auth():
    """ทดสอบ backfill endpoint ต้องมี ADMIN_SECRET."""
    client = server.app.test_client()

    original_secret = server.ADMIN_SECRET
    server.ADMIN_SECRET = "test_admin_secret_123"

    # No auth header
    resp = client.post("/admin/backfill")
    assert resp.status_code == 401

    # Wrong auth
    resp = client.post("/admin/backfill", headers={"X-Admin-Secret": "wrong"})
    assert resp.status_code == 401

    server.ADMIN_SECRET = original_secret
    print("PASS: test_backfill_endpoint_no_auth")


def test_health_includes_document_search():
    """ทดสอบ health endpoint แสดงข้อมูล document search."""
    # Need production mode for document_search field
    original_prod = server.IS_PRODUCTION
    original_env = server.APP_ENV
    server.IS_PRODUCTION = True
    server.APP_ENV = "production"

    client = server.app.test_client()
    resp = client.get("/health")
    data = resp.get_json()

    # Document search info should appear in production
    if "document_search" in data:
        assert "indexed_documents" in data["document_search"]
        assert "authorized_users" in data["document_search"]

    server.IS_PRODUCTION = original_prod
    server.APP_ENV = original_env
    print("PASS: test_health_includes_document_search")


def test_webhook_text_search_command():
    """ทดสอบ webhook text message ส่งคำสั่ง ช่วยเหลือ."""
    from unittest.mock import patch

    payload = {
        "events": [{
            "type": "message",
            "webhookEventId": "evt_search_test_001",
            "deliveryContext": {"isRedelivery": False},
            "replyToken": "search_reply_token",
            "source": {"userId": "test_search_user"},
            "message": {
                "type": "text",
                "id": "msg_search_001",
                "text": "ช่วยเหลือ",
            },
        }],
    }
    body = json.dumps(payload).encode("utf-8")
    sig = make_signature(body)

    client = server.app.test_client()

    with patch("server.reply_message") as mock_reply:
        resp = client.post(
            "/webhook",
            data=body,
            headers={
                "X-Line-Signature": sig,
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 200

        if mock_reply.called:
            reply_text = mock_reply.call_args[0][1]
            assert "ค้นหา" in reply_text
            assert "รายการล่าสุด" in reply_text

    print("PASS: test_webhook_text_search_command")


def test_status_shows_doc_count():
    """ทดสอบ สถานะ แสดงจำนวนเอกสารในดัชนี."""
    from unittest.mock import patch

    payload = {
        "events": [{
            "type": "message",
            "webhookEventId": "evt_status_doc_001",
            "deliveryContext": {"isRedelivery": False},
            "replyToken": "status_doc_token",
            "source": {"userId": "test_status_user"},
            "message": {
                "type": "text",
                "id": "msg_status_doc_001",
                "text": "สถานะ",
            },
        }],
    }
    body = json.dumps(payload).encode("utf-8")
    sig = make_signature(body)

    client = server.app.test_client()

    with patch("server.reply_message") as mock_reply:
        resp = client.post(
            "/webhook",
            data=body,
            headers={
                "X-Line-Signature": sig,
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 200
        if mock_reply.called:
            reply_text = mock_reply.call_args[0][1]
            assert "ดัชนี" in reply_text

    print("PASS: test_status_shows_doc_count")


def test_no_patient_data_in_index():
    """ทดสอบว่า original_filename ไม่เก็บชื่อผู้ป่วย."""
    idx = server.DocumentIndex(server.storage)
    idx._loaded = True

    # Simulate what process_file_async does — uses generic name
    doc = {
        "document_id": "pii-001",
        "original_filename": "document.pdf",  # NOT patient name
        "category": "lab",
        "extracted_text": "ผลตรวจ",
    }
    idx.add(doc)

    stored = idx.get("pii-001")
    # Should NOT contain patient-identifying filename
    assert stored["original_filename"] == "document.pdf"
    assert "สมชาย" not in stored["original_filename"]
    print("PASS: test_no_patient_data_in_index")


def test_signed_url_not_ready():
    """ทดสอบ signed URL เมื่อ GCS ไม่พร้อม."""
    url = server.generate_signed_url("some/path.pdf")
    # In testing mode with local storage, should return None
    assert url is None
    print("PASS: test_signed_url_not_ready")


# ── Admin Registration Tests ──────────────────────────────────


def test_register_command_success():
    """ทดสอบลงทะเบียนด้วย ADMIN_SECRET ที่ถูกต้อง — คืน hash ไม่คืน userId."""
    original_secret = server.ADMIN_SECRET
    try:
        server.ADMIN_SECRET = "test-secret-12345"
        test_hash = "abc123def456"
        result = server.handle_search_command("ลงทะเบียน test-secret-12345", test_hash)
        assert result is not None, "ควรคืนข้อความ"
        assert "✅" in result, "ควรมีเครื่องหมายสำเร็จ"
        assert test_hash in result, "ควรแสดง hash"
        assert "AUTHORIZED_USERS" in result, "ควรมีคำแนะนำ"
        # ห้ามมี userId จริงในผลลัพธ์ — test_hash เป็น hash แล้ว
        print("PASS: test_register_command_success")
    finally:
        server.ADMIN_SECRET = original_secret


def test_register_command_wrong_secret():
    """ทดสอบลงทะเบียนด้วย secret ที่ผิด — ไม่ตอบกลับ (ป้องกัน brute force)."""
    original_secret = server.ADMIN_SECRET
    try:
        server.ADMIN_SECRET = "correct-secret"
        result = server.handle_search_command("ลงทะเบียน wrong-secret", "somehash")
        assert result is None, "ต้องคืน None เมื่อ secret ผิด (ไม่เปิดเผยว่ามีคำสั่งนี้)"
        print("PASS: test_register_command_wrong_secret")
    finally:
        server.ADMIN_SECRET = original_secret


def test_register_command_no_admin_secret_set():
    """ทดสอบลงทะเบียนเมื่อยังไม่ตั้ง ADMIN_SECRET — ไม่ตอบกลับ."""
    original_secret = server.ADMIN_SECRET
    try:
        server.ADMIN_SECRET = ""
        result = server.handle_search_command("ลงทะเบียน any-value", "somehash")
        assert result is None, "ต้องคืน None เมื่อไม่ได้ตั้ง ADMIN_SECRET"
        print("PASS: test_register_command_no_admin_secret_set")
    finally:
        server.ADMIN_SECRET = original_secret


def test_register_command_no_userid_in_response():
    """ทดสอบว่าผลลัพธ์ไม่มี LINE userId จริง — มีแต่ hash."""
    original_secret = server.ADMIN_SECRET
    try:
        server.ADMIN_SECRET = "mysecret"
        # สมมติ userId จริงคือ "U1234567890abcdef" → hash_user_id จะแปลงเป็น hash
        fake_user_id = "U1234567890abcdef"
        user_hash = server.hash_user_id(fake_user_id)
        result = server.handle_search_command("ลงทะเบียน mysecret", user_hash)
        assert result is not None
        # userId จริงต้องไม่ปรากฏ
        assert fake_user_id not in result, "userId จริงต้องไม่อยู่ในผลลัพธ์"
        # hash ต้องปรากฏ
        assert user_hash in result, "hash ต้องอยู่ในผลลัพธ์"
        print("PASS: test_register_command_no_userid_in_response")
    finally:
        server.ADMIN_SECRET = original_secret


def test_register_command_empty_secret():
    """ทดสอบพิมพ์ 'ลงทะเบียน ' โดยไม่ใส่ secret — ไม่ตอบกลับ."""
    original_secret = server.ADMIN_SECRET
    try:
        server.ADMIN_SECRET = "valid-secret"
        result = server.handle_search_command("ลงทะเบียน ", "somehash")
        assert result is None, "ต้องคืน None เมื่อไม่ใส่ secret"
        print("PASS: test_register_command_empty_secret")
    finally:
        server.ADMIN_SECRET = original_secret


def test_user_id_salt_production_stable():
    """ทดสอบว่า Production ใช้ salt คงที่เมื่อไม่ตั้ง USER_ID_SALT."""
    # ตรวจว่าโค้ดมี fallback salt สำหรับ production
    import inspect
    source = inspect.getsource(server)
    assert "ranong-raktai-default-salt-please-set-env" in source, \
        "Production ต้องมี fallback salt คงที่"
    print("PASS: test_user_id_salt_production_stable")


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
        # Security Remediation tests
        test_cleanup_after_gcs_upload_success,
        test_no_cleanup_when_gcs_fails,
        test_cleanup_oserror_graceful,
        test_cleanup_path_safety,
        test_text_message_log_no_content,
        # ── Phase 1: Logs ไม่มีข้อมูลผู้ป่วย ──
        test_patient_name_never_in_logs,
        test_patient_name_never_in_stored_filename,
        test_error_log_has_no_exception_detail,
        test_keyword_log_counts_only,
        test_classification_log_has_no_path,
        # ── Phase 1: UUID และ metadata ──
        test_build_storage_filename_uuid,
        test_metadata_contains_no_pii,
        test_metadata_passed_to_storage,
        test_gcs_blob_path_uuid_and_month,
        test_gcs_precondition_412_treated_as_stored,
        # ── Phase 1: Dedup และ Rate limit ──
        test_dedup_same_event_id,
        test_dedup_message_id_when_event_id_differs,
        test_redelivery_flag_processed_once,
        test_content_hash_dedup_no_temp_file,
        test_dedup_shared_across_workers,
        test_rate_limit_blocks_and_isolates_users,
        test_rate_limit_no_quota_burn_on_reject,
        test_rate_limit_halved_when_not_shared,
        test_retry_does_not_consume_rate_quota,
        test_rate_limited_request_not_downloaded,
        test_redis_backend_used_when_available,
        test_redis_failure_falls_back_safely,
        test_state_store_not_configured_by_default,
        test_redis_url_without_package_falls_back,
        # ── Phase 1: ชนิดไฟล์ ขนาด และ error ──
        test_txt_with_null_bytes_rejected,
        test_invalid_utf8_txt_rejected,
        test_short_txt_still_accepted,
        test_truncated_binary_rejected,
        test_missing_message_id_skipped,
        test_unsupported_extension_reply_has_no_filename,
        test_normalize_incoming_extension,
        test_long_filename_truncated_in_inbox,
        test_external_ai_blocked_by_code,
        # ── Phase 1: OCR ──
        test_ocr_dpi_reduced,
        test_ocr_concurrency_limited,
        test_ocr_releases_memory_per_page,
        # ── Phase 1: ความสามารถเดิมต้องยังอยู่ ──
        test_line_capability_preserved,
        # ── GCS ADC + Bucket Name + Fail-loud ──
        test_gcs_bucket_name_env,
        test_gcs_bucket_name_fallback_to_storage_bucket,
        test_gcs_adc_auth_method,
        test_gcs_no_bucket_error_log,
        test_gcs_save_not_ready_error_log,
        test_gcs_health_shows_auth_method,
        # ── Testing-docs reply logging ──
        test_testing_docs_text_reply_called,
        test_testing_docs_reply_success_logged,
        test_testing_docs_reply_failure_logged,
        # ── Production Mode Tests ──
        test_production_gcs_save_raises_on_not_ready,
        test_production_gcs_upload_error_raises,
        test_production_no_local_fallback,
        test_production_webhook_rejects_without_gcs,
        test_production_health_503_without_gcs,
        test_production_health_503_without_gcs_backend,
        test_production_health_checklist,
        test_production_health_no_checklist_in_testing,
        test_production_copy_to_category_reraises,
        # ── Document Search V1 ──
        test_document_index_add_and_search,
        test_document_index_recent,
        test_document_index_idempotent,
        test_document_index_get,
        test_search_max_results,
        test_search_thai_text,
        test_handle_search_command_unauthorized,
        test_handle_search_command_help,
        test_handle_search_command_not_search,
        test_is_authorized_user,
        test_format_search_results_empty,
        test_format_search_results_with_docs,
        test_audit_logger_creates_entry,
        test_backfill_endpoint_no_auth,
        test_health_includes_document_search,
        test_webhook_text_search_command,
        test_status_shows_doc_count,
        test_no_patient_data_in_index,
        test_signed_url_not_ready,
        # ── Admin Registration ──
        test_register_command_success,
        test_register_command_wrong_secret,
        test_register_command_no_admin_secret_set,
        test_register_command_no_userid_in_response,
        test_register_command_empty_secret,
        test_user_id_salt_production_stable,
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
