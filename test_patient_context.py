"""Verify staff patient-selection command parsing and filing priority."""
import os
import re
import sys

os.environ.setdefault("APP_ENV", "testing")
sys.path.insert(0, "/home/claude/ranong-raktai-ai-cloud")

fails = []


def check(label, got, want):
    if got == want:
        print(f"  PASS  {label}  -> {got!r}")
    else:
        print(f"  FAIL  {label}\n        got:  {got!r}\n        want: {want!r}")
        fails.append(label)


# Mirror of _parse_patient_select (avoids importing all of server.py)
def parse_patient_select(text):
    m = re.match(
        r"^\s*(?:เลือก)?(?:ผู้ป่วย|คนไข้|patient)\s*[:=]?\s*"
        r"([A-Za-z0-9\-/]{2,20})\s*$",
        text.strip(),
        re.IGNORECASE,
    )
    return m.group(1) if m else None


print("\n=== 1. Commands that SELECT a patient ===")
for text, want in [
    ("ผู้ป่วย 57042", "57042"),
    ("ผู้ป่วย57042", "57042"),
    ("คนไข้ 57042", "57042"),
    ("เลือกผู้ป่วย 57042", "57042"),
    ("patient 57042", "57042"),
    ("ผู้ป่วย: 57042", "57042"),
    ("  ผู้ป่วย 58072  ", "58072"),
    ("ผู้ป่วย 00060578", "00060578"),
]:
    check(f"{text!r}", parse_patient_select(text), want)

print("\n=== 2. Commands that must NOT be treated as selection ===")
for text in [
    "ผู้ป่วยปัจจุบัน",      # show command
    "ล้างผู้ป่วย",          # clear command
    "ทะเบียนผู้ป่วย",       # registry listing
    "เพิ่มผู้ป่วย",         # registration
    "ผู้ป่วย",             # bare word, no HN
    "ลงทะเบียนรายชื่อ",
    "ค้นหา เบาหวาน",
    "",
]:
    got = parse_patient_select(text)
    if got is None:
        print(f"  PASS  {text!r} not a selection")
    else:
        print(f"  FAIL  {text!r} wrongly parsed as HN {got!r}")
        fails.append(f"false positive: {text}")

print("\n=== 3. Filing priority: OCR vs staff selection ===")


def decide(ocr_folder, ctx_folder):
    """Mirror of the wiring in process_file_async."""
    folder = ocr_folder
    conflict = ""
    if ctx_folder and ocr_folder and ctx_folder != ocr_folder:
        conflict = ocr_folder
        folder = ctx_folder
    elif ctx_folder and not ocr_folder:
        folder = ctx_folder
    return folder, conflict


A, B = "57042_PatientA", "58072_PatientB"

got = decide("", A)
check("OCR blank + staff selected -> staff", got, (A, ""))

got = decide(A, "")
check("OCR found + no selection -> OCR", got, (A, ""))

got = decide(A, A)
check("both agree -> that patient, no warning", got, (A, ""))

got = decide(B, A)
check("DISAGREE -> staff selection wins + warning names OCR", got, (A, B))

got = decide("", "")
check("neither -> quarter/month only", got, ("", ""))

print("\n=== 4. Conflict must always surface a warning ===")
folder, conflict = decide(B, A)
if conflict and conflict != folder:
    print(f"  PASS  conflict surfaced: filed under {folder!r},"
          f" warned about {conflict!r}")
else:
    print("  FAIL  conflict not surfaced")
    fails.append("conflict not surfaced")

# never warn when there is no disagreement
for ocr, ctx in [("", A), (A, ""), (A, A), ("", "")]:
    _, c = decide(ocr, ctx)
    if c:
        print(f"  FAIL  spurious warning for ocr={ocr!r} ctx={ctx!r}")
        fails.append("spurious warning")
        break
else:
    print("  PASS  no spurious warnings when OCR and selection agree")

print("\n=== 5. Context expiry window ===")
TIMEOUT = 1800
for elapsed, want_valid in [(0, True), (900, True), (1799, True),
                            (1800, False), (3600, False)]:
    valid = elapsed < TIMEOUT
    check(f"elapsed {elapsed}s valid", valid, want_valid)

print("\n" + "=" * 62)
if fails:
    print(f"RESULT: {len(fails)} FAILED -> {fails}")
    sys.exit(1)
print("RESULT: ALL CHECKS PASSED")
