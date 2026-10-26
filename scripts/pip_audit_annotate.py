#!/usr/bin/env python3
"""แปลงผล pip-audit (JSON) เป็น GitHub annotations + step summary

เหตุผลที่ต้องมีสคริปต์นี้
─────────────────────────
pip-audit ใน CI รายงานผลลง log ของ step เท่านั้น และ log ของ step
ดาวน์โหลดผ่าน API ไม่ได้จากสภาพแวดล้อมที่เราใช้ตรวจ (ตัว log ถูกเก็บ
ใน blob storage ที่ egress proxy ปฏิเสธ — ยืนยันแล้วว่าได้ 403
Forbidden) ผลที่ได้คือ CVE ถูกตรวจพบจริงแต่ไม่มีใครอ่านได้
เป็นเวลาหลายรอบ deploy (ดู TD-14)

GitHub annotations กับ step summary ไม่ได้อยู่ใน blob storage —
อ่านได้ทาง REST API ปกติ:
    GET /repos/{owner}/{repo}/check-runs/{check_run_id}/annotations

สคริปต์นี้จึงทำให้ผล pip-audit "ออกมาข้างนอก" ได้เสมอ ไม่ว่าจะอ่าน
log ได้หรือไม่

ข้อควรระวังด้านความปลอดภัย: สคริปต์นี้พิมพ์เฉพาะชื่อแพ็กเกจ เวอร์ชัน
และรหัส advisory ซึ่งเป็นข้อมูลสาธารณะทั้งหมด ไม่มีการอ่านหรือพิมพ์
ข้อมูลผู้ป่วย credentials หรือเนื้อหาไฟล์ใด ๆ
"""

from __future__ import annotations

import json
import os
import pathlib
import sys


def _ann_escape(text: str) -> str:
    """เข้ารหัสข้อความสำหรับ annotation — ขึ้นบรรทัดใหม่ต้องเป็น %0A"""
    return (
        text.replace("%", "%25")
        .replace("\r", "")
        .replace("\n", "%0A")
        .replace(":", "%3A")
        .replace(",", "%2C")
    )


def _ver_key(version: str) -> tuple:
    """เรียงเวอร์ชันแบบตัวเลข — "6.9.1" ต้องน้อยกว่า "6.10.0"

    การเทียบเป็นสตริงจะได้ผลกลับกัน และทำให้เลือกเวอร์ชันเป้าหมายผิด
    ส่วนที่ไม่ใช่ตัวเลข (rc, post) ถือเป็น 0 — พอสำหรับการเลือกตัวสูงสุด
    """
    parts = []
    for chunk in str(version).split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _fix_hint(name: str, fix_versions: list) -> str:
    if fix_versions:
        return f"แก้ได้: upgrade {name} -> {', '.join(fix_versions)}"
    return "ยังไม่มีเวอร์ชันที่แก้แล้ว — ต้องประเมินผลกระทบรายกรณี"


def main() -> int:
    if len(sys.argv) < 2:
        print("ใช้: pip_audit_annotate.py <pip-audit.json> [exit_code]")
        return 2

    report_path = pathlib.Path(sys.argv[1])
    audit_rc = sys.argv[2] if len(sys.argv) > 2 else "?"

    if not report_path.exists() or not report_path.stat().st_size:
        # pip-audit ล้มก่อนเขียนไฟล์ (เช่น เครือข่ายล่ม) — ต้องไม่กลืนเงียบ
        print("::warning::pip-audit ไม่ได้ผลิตรายงาน JSON "
              f"(exit={audit_rc}) — ยังไม่ทราบสถานะ vulnerabilities")
        return 0

    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"::warning::อ่านรายงาน pip-audit ไม่สำเร็จ: {type(e).__name__}")
        return 0

    deps = data.get("dependencies", data if isinstance(data, list) else [])

    findings = []
    for dep in deps:
        name = dep.get("name", "?")
        version = dep.get("version", "?")
        for vuln in dep.get("vulns", []) or []:
            findings.append({
                "name": name,
                "version": version,
                "id": vuln.get("id", "?"),
                "aliases": [a for a in (vuln.get("aliases") or [])
                            if a.startswith("CVE-")],
                "fix_versions": vuln.get("fix_versions") or [],
                "description": (vuln.get("description") or "").strip(),
            })

    summary_lines = ["## pip-audit — dependency vulnerabilities", ""]

    if not findings:
        summary_lines += [
            f"ไม่พบ vulnerability ในแพ็กเกจที่ตรวจ ({len(deps)} รายการ)",
            "",
            f"pip-audit exit code: `{audit_rc}`",
        ]
        print(f"::notice::pip-audit: ไม่พบ vulnerability "
              f"({len(deps)} แพ็กเกจ, exit={audit_rc})")
    else:
        summary_lines += [
            f"พบ **{len(findings)}** รายการ "
            f"จาก {len(deps)} แพ็กเกจที่ตรวจ (exit code `{audit_rc}`)",
            "",
            "| แพ็กเกจ | เวอร์ชันที่ใช้ | Advisory | CVE | เวอร์ชันที่แก้แล้ว |",
            "|---|---|---|---|---|",
        ]
        # ── เวอร์ชันเป้าหมายต่อแพ็กเกจ — ส่งก่อนรายการย่อยเสมอ ──
        #
        # ทำไมต้องมี: advisory แต่ละรายการบอก "เวอร์ชันที่แก้รายการนั้น"
        # ซึ่งมักไม่ใช่ตัวเดียวกัน การ upgrade ไปตามตัวแรกที่เห็นจึงยัง
        # เหลือ CVE ค้างและต้อง upgrade ซ้ำหลายรอบ — เกิดขึ้นจริงกับ
        # pypdf ห้ารอบติดเมื่อ 26 ต.ค. 2026
        #   5.1.0 -> 6.7.1 -> 6.10.2 -> 6.13.1 -> 6.15.0 -> 6.19.0
        #
        # ทำไมต้องพิมพ์ "ก่อน" รายการย่อย: GitHub เก็บ annotation ได้
        # 10 รายการต่อ step ส่วนที่เกินถูกตัดทิ้งเงียบ ๆ รอบแรกที่ใส่
        # ฟีเจอร์นี้ annotation เป้าหมายจึงหายไปทั้งหมด เพราะ CVE
        # สิบรายการกินโควตาไปก่อน (ยืนยันจาก /check-runs/{id}/annotations)
        targets = {}
        for f in findings:
            for v in f["fix_versions"]:
                cur = targets.get(f["name"])
                if cur is None or _ver_key(v) > _ver_key(cur):
                    targets[f["name"]] = v

        target_lines = []
        if targets:
            target_lines = ["", "### เวอร์ชันเป้าหมาย", ""]
            for name, v in sorted(targets.items()):
                target_lines.append(f"- `{name}` → **{v}**")
                hint = (
                    f"{name}: upgrade ไปที่ {v} "
                    f"(เวอร์ชันสูงสุดที่ advisory ทั้งหมดของแพ็กเกจนี้ระบุ)"
                    f" — อย่า upgrade ตามตัวแรกที่เห็น"
                )
                print(
                    f"::warning file=requirements.txt,line=1,"
                    f"title=pip-audit target::{_ann_escape(hint)}"
                )

        # รายการย่อย — เหลือโควตาเท่าไรก็ส่งเท่านั้น ที่เกินยังอยู่ใน
        # step summary ครบทุกรายการ
        for f in findings:
            cves = ", ".join(f["aliases"]) or "—"
            fixes = ", ".join(f["fix_versions"]) or "ยังไม่มี"
            summary_lines.append(
                f"| `{f['name']}` | `{f['version']}` | {f['id']} "
                f"| {cves} | {fixes} |"
            )

            head = f["description"].split("\n")[0][:300]
            msg = (
                f"{f['name']} {f['version']} — {f['id']}"
                + (f" ({', '.join(f['aliases'])})" if f["aliases"] else "")
                + f". {_fix_hint(f['name'], f['fix_versions'])}"
                + (f" | {head}" if head else "")
            )
            print(f"::warning file=requirements.txt,line=1,"
                  f"title=pip-audit {f['id']}::{_ann_escape(msg)}")

        summary_lines += target_lines
        summary_lines += [
            "",
            "รายละเอียดแต่ละรายการอยู่ใน annotation ของ job นี้ "
            "(อ่านทาง API ได้ที่ `/check-runs/{id}/annotations`)",
            "GitHub เก็บ annotation ได้ 10 รายการต่อ step — "
            "ตารางข้างบนคือรายการครบทุกตัว",
        ]

    step_summary = os.getenv("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write("\n".join(summary_lines) + "\n")
    else:
        print("\n".join(summary_lines))

    # สคริปต์นี้มีหน้าที่รายงานเท่านั้น การตัดสินว่า CI ควรแดงหรือไม่
    # เป็นหน้าที่ของ exit code ของ pip-audit เองใน workflow
    return 0


if __name__ == "__main__":
    sys.exit(main())
