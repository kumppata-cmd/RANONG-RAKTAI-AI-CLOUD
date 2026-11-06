#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Treatment Change Tracker — บันทึกและติดตามการเปลี่ยนแปลงการรักษา

ครบวงจร: ปรับ → บันทึก → ติดตาม → ประเมินผล

Features:
1. Structured treatment change recording (ยา, dose, เหตุผล)
2. Outcome tracking — ติดตาม lab หลังปรับยาอัตโนมัติ
3. Treatment timeline — ประวัติการเปลี่ยนแปลงทั้งหมดของผู้ป่วย
4. Monthly evaluation — สรุปว่าการปรับยาได้ผลกี่คน

Safety:
- Human-in-the-loop: AI แนะนำ แพทย์ตัดสินใจเท่านั้น
- ทุก change มี audit trail
- ไม่ส่งข้อมูลออกนอกระบบ
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


# ── Data Models ──

@dataclass
class TreatmentChange:
    """บันทึกการเปลี่ยนแปลงการรักษา 1 รายการ"""
    change_id: str                  # unique ID: TC-{HN}-{timestamp}
    patient_hn: str
    change_date: str                # ISO date
    category: str                   # "esa", "medication", "dialysis", "other"
    action: str                     # "start", "stop", "increase", "decrease", "switch"

    # รายละเอียดการเปลี่ยนแปลง
    drug_name: str = ""             # ชื่อยา
    old_value: str = ""             # ค่าเดิม (เช่น "Epiao 4000u")
    new_value: str = ""             # ค่าใหม่ (เช่น "Epiao 6000u")
    reason: str = ""                # เหตุผล (เช่น "Hb 8.5 ต่ำกว่าเป้า")

    # การติดตามผล
    monitor_lab: str = ""           # lab ที่ต้องติดตาม (เช่น "Hb")
    monitor_target: str = ""        # เป้าหมาย (เช่น "10-12 g/dL")
    followup_days: int = 14         # ติดตามผลภายในกี่วัน
    followup_date: str = ""         # วันที่ต้องติดตาม (คำนวณอัตโนมัติ)

    # ผลลัพธ์ (อัปเดตภายหลัง)
    outcome: str = ""               # "improved", "stable", "worsened", "pending"
    outcome_detail: str = ""        # รายละเอียด (เช่น "Hb 8.5→9.8 ใน 2 สัปดาห์")
    outcome_date: str = ""

    # metadata
    recorded_by: str = "physician"  # physician / system
    created_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> TreatmentChange:
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class OutcomeEvaluation:
    """ผลการประเมินหลังปรับยา"""
    change_id: str
    patient_hn: str
    lab_name: str                   # "Hb", "K+", "eGFR"
    baseline_value: float           # ค่าก่อนปรับ
    current_value: float            # ค่าปัจจุบัน
    target_range: str               # "10-12"
    change_pct: float               # % เปลี่ยนแปลง
    verdict: str                    # "improved", "stable", "worsened", "target_reached"
    days_elapsed: int               # จำนวนวันหลังปรับ
    summary_th: str = ""            # สรุปภาษาไทย


# ── Treatment Category Config ──

TREATMENT_CATEGORIES = {
    "esa": {
        "name_th": "ยากระตุ้นเม็ดเลือดแดง (ESA)",
        "monitor_lab": "Hb",
        "target": "10-12 g/dL",
        "followup_days": 14,
        "drugs": ["epiao", "hemax", "hema-plus", "hypercrit", "eposis",
                  "eprex", "recormon", "mircera", "aranesp"],
    },
    "iron": {
        "name_th": "ธาตุเหล็ก (Iron)",
        "monitor_lab": "Ferritin,TSAT",
        "target": "Ferritin 200-500, TSAT 20-50%",
        "followup_days": 30,
        "drugs": ["venofer", "ferinject", "ferrous", "iron sucrose",
                  "iv iron", "ferric"],
    },
    "phosphate_binder": {
        "name_th": "ยาลดฟอสเฟต",
        "monitor_lab": "PO4",
        "target": "3.5-5.5 mg/dL",
        "followup_days": 30,
        "drugs": ["caco3", "calcium carbonate", "sevelamer", "lanthanum",
                  "aluminium hydroxide"],
    },
    "antihypertensive": {
        "name_th": "ยาลดความดัน",
        "monitor_lab": "BP",
        "target": "<140/90 mmHg",
        "followup_days": 14,
        "drugs": ["amlodipine", "losartan", "enalapril", "atenolol",
                  "manidipine", "nifedipine", "carvedilol"],
    },
    "dialysis": {
        "name_th": "การฟอกเลือด",
        "monitor_lab": "Kt/V",
        "target": "Kt/V ≥1.2, URR ≥65%",
        "followup_days": 7,
        "drugs": [],
    },
    "other": {
        "name_th": "อื่นๆ",
        "monitor_lab": "",
        "target": "",
        "followup_days": 30,
        "drugs": [],
    },
}


class TreatmentTracker:
    """จัดการ Treatment Change Tracking ทั้งระบบ"""

    GCS_PREFIX = "_treatment_changes"

    def __init__(self, storage=None):
        self._storage = storage

    def set_storage(self, storage):
        self._storage = storage

    # ── Record a treatment change ──

    def record_change(
        self,
        patient_hn: str,
        category: str,
        action: str,
        drug_name: str = "",
        old_value: str = "",
        new_value: str = "",
        reason: str = "",
        followup_days: int = 0,
    ) -> TreatmentChange:
        """บันทึกการเปลี่ยนแปลงการรักษา

        Args:
            patient_hn: HN ผู้ป่วย
            category: "esa", "iron", "phosphate_binder", "antihypertensive",
                      "dialysis", "other"
            action: "start", "stop", "increase", "decrease", "switch"
            drug_name: ชื่อยา
            old_value: ค่าเดิม
            new_value: ค่าใหม่
            reason: เหตุผลที่ปรับ
            followup_days: ติดตามผลภายในกี่วัน (0 = ใช้ค่า default ตาม category)

        Returns: TreatmentChange object
        """
        now = datetime.now(timezone.utc)
        cat_config = TREATMENT_CATEGORIES.get(category, TREATMENT_CATEGORIES["other"])

        if not followup_days:
            followup_days = cat_config.get("followup_days", 14)

        followup_date = (now + timedelta(days=followup_days)).strftime("%Y-%m-%d")

        change = TreatmentChange(
            change_id=f"TC-{patient_hn}-{now.strftime('%Y%m%d%H%M%S')}",
            patient_hn=patient_hn,
            change_date=now.strftime("%Y-%m-%d"),
            category=category,
            action=action,
            drug_name=drug_name,
            old_value=old_value,
            new_value=new_value,
            reason=reason,
            monitor_lab=cat_config.get("monitor_lab", ""),
            monitor_target=cat_config.get("target", ""),
            followup_days=followup_days,
            followup_date=followup_date,
            outcome="pending",
            created_at=now.isoformat(),
        )

        self._save_change(change)
        logger.info("Treatment change recorded: %s %s %s → %s for HN %s",
                     action, drug_name, old_value, new_value, patient_hn)
        return change

    # ── Get treatment history ──

    def get_history(self, patient_hn: str, limit: int = 20) -> list[TreatmentChange]:
        """ดึงประวัติการเปลี่ยนแปลงการรักษาของผู้ป่วย"""
        if not self._storage:
            return []

        try:
            path = f"{self.GCS_PREFIX}/{patient_hn}/changes.json"
            data = self._storage.get(path)
            if not data:
                return []
            changes = json.loads(data)
            result = [TreatmentChange.from_dict(c) for c in changes]
            # เรียงจากใหม่สุด
            result.sort(key=lambda x: x.created_at, reverse=True)
            return result[:limit]
        except Exception as e:
            logger.warning("get_history(%s): %s", patient_hn, e)
            return []

    # ── Get pending follow-ups ──

    def get_pending_followups(self, as_of_date: str = "") -> list[TreatmentChange]:
        """ดึงรายการที่ต้องติดตามผล (ครบกำหนด followup_date)

        Returns: list of TreatmentChange ที่ outcome == "pending"
                 และ followup_date <= as_of_date
        """
        if not self._storage:
            return []

        if not as_of_date:
            as_of_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        pending = []
        try:
            files = self._storage.list_files(prefix=f"{self.GCS_PREFIX}/")
            if not files:
                return []

            # Extract unique HNs
            hns = set()
            for f in files:
                name = f if isinstance(f, str) else f.get("name", "")
                parts = name.split("/")
                if len(parts) >= 2:
                    hns.add(parts[1])

            for hn in hns:
                changes = self.get_history(hn, limit=50)
                for c in changes:
                    if c.outcome == "pending" and c.followup_date <= as_of_date:
                        pending.append(c)

        except Exception as e:
            logger.warning("get_pending_followups: %s", e)

        pending.sort(key=lambda x: x.followup_date)
        return pending

    # ── Evaluate outcome ──

    def evaluate_outcome(
        self,
        change: TreatmentChange,
        current_lab_value: float,
        baseline_lab_value: float = 0,
    ) -> OutcomeEvaluation:
        """ประเมินผลลัพธ์หลังปรับการรักษา

        เปรียบเทียบค่า lab ปัจจุบัน กับค่าก่อนปรับ + เป้าหมาย
        """
        if not baseline_lab_value:
            baseline_lab_value = current_lab_value  # no baseline

        days_elapsed = 0
        try:
            change_dt = datetime.strptime(change.change_date, "%Y-%m-%d")
            now_dt = datetime.now(timezone.utc).replace(tzinfo=None)
            days_elapsed = (now_dt - change_dt).days
        except Exception:
            pass

        change_pct = 0
        if baseline_lab_value > 0:
            change_pct = ((current_lab_value - baseline_lab_value)
                          / baseline_lab_value * 100)

        # Parse target range
        target_low, target_high = self._parse_target(change.monitor_target)

        # Determine verdict
        in_target = (target_low <= current_lab_value <= target_high
                     if target_low and target_high else False)

        if in_target:
            verdict = "target_reached"
        elif change.action in ("increase", "start") and current_lab_value > baseline_lab_value:
            verdict = "improved"
        elif change.action in ("decrease", "stop") and current_lab_value < baseline_lab_value:
            verdict = "improved"
        elif abs(change_pct) < 5:
            verdict = "stable"
        else:
            verdict = "worsened"

        # Thai summary
        verdict_th = {
            "target_reached": "✅ ถึงเป้าหมาย",
            "improved": "📈 ดีขึ้น",
            "stable": "➡️ คงที่",
            "worsened": "⚠️ ไม่ดีขึ้น",
        }.get(verdict, verdict)

        arrow = "↑" if change_pct > 0 else ("↓" if change_pct < 0 else "→")
        summary = (
            f"{verdict_th}\n"
            f"ปรับ {change.drug_name}: {change.old_value} → {change.new_value}\n"
            f"{change.monitor_lab}: {baseline_lab_value:.1f} → {current_lab_value:.1f} {arrow} "
            f"({change_pct:+.1f}%)\n"
            f"เป้าหมาย: {change.monitor_target}\n"
            f"ระยะเวลา: {days_elapsed} วัน"
        )

        eval_result = OutcomeEvaluation(
            change_id=change.change_id,
            patient_hn=change.patient_hn,
            lab_name=change.monitor_lab,
            baseline_value=baseline_lab_value,
            current_value=current_lab_value,
            target_range=change.monitor_target,
            change_pct=round(change_pct, 1),
            verdict=verdict,
            days_elapsed=days_elapsed,
            summary_th=summary,
        )

        # Update the change record
        change.outcome = verdict
        change.outcome_detail = (
            f"{change.monitor_lab} {baseline_lab_value:.1f}→{current_lab_value:.1f}"
        )
        change.outcome_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._save_change(change)

        return eval_result

    # ── Format for LINE ──

    def format_history_for_line(self, patient_hn: str,
                                patient_name: str = "",
                                limit: int = 5) -> str:
        """สร้างข้อความประวัติการรักษาสำหรับ LINE"""
        changes = self.get_history(patient_hn, limit=limit)
        if not changes:
            return f"📋 HN: {patient_hn}\nยังไม่มีประวัติการปรับการรักษา"

        lines = [
            "📋 ประวัติการปรับการรักษา",
            "━━━━━━━━━━━━━━━━",
            f"🔢 HN: {patient_hn}",
        ]
        if patient_name:
            lines.append(f"👤 {patient_name}")
        lines.append("")

        for i, c in enumerate(changes[:limit], 1):
            action_th = {
                "start": "เริ่ม", "stop": "หยุด",
                "increase": "เพิ่ม", "decrease": "ลด",
                "switch": "เปลี่ยน",
            }.get(c.action, c.action)

            outcome_icon = {
                "target_reached": "✅",
                "improved": "📈",
                "stable": "➡️",
                "worsened": "⚠️",
                "pending": "⏳",
            }.get(c.outcome, "❓")

            lines.append(f"{i}. [{c.change_date}] {action_th} {c.drug_name}")
            if c.old_value or c.new_value:
                lines.append(f"   {c.old_value} → {c.new_value}")
            if c.reason:
                lines.append(f"   เหตุผล: {c.reason}")
            lines.append(f"   ผล: {outcome_icon} {c.outcome_detail or c.outcome}")
            lines.append("")

        lines.append("─" * 20)
        lines.append("⚕️ ข้อมูลประกอบการตัดสินใจ ต้องแพทย์พิจารณา")
        return "\n".join(lines)

    def format_pending_for_line(self, pending: list[TreatmentChange]) -> str:
        """สร้างข้อความแจ้งเตือนรายการที่ต้องติดตาม"""
        if not pending:
            return "✅ ไม่มีรายการที่ต้องติดตามผลวันนี้"

        lines = [
            "🔔 รายการติดตามผลการรักษา",
            "━━━━━━━━━━━━━━━━",
            f"📅 วันนี้มี {len(pending)} รายการ",
            "",
        ]

        for i, c in enumerate(pending[:10], 1):
            days = 0
            try:
                cd = datetime.strptime(c.change_date, "%Y-%m-%d")
                fd = datetime.strptime(c.followup_date, "%Y-%m-%d")
                days = (fd - cd).days
            except Exception:
                pass

            lines.append(
                f"{i}. HN {c.patient_hn} — {c.drug_name} "
                f"({c.old_value}→{c.new_value})"
            )
            lines.append(f"   ปรับเมื่อ: {c.change_date} ({days} วันก่อน)")
            lines.append(f"   ตรวจ: {c.monitor_lab} (เป้า {c.monitor_target})")
            lines.append("")

        lines.append("─" * 20)
        lines.append("💡 กรุณาเจาะ lab ติดตามผล")
        return "\n".join(lines)

    # ── Monthly evaluation summary ──

    def monthly_summary(self, year_month: str = "") -> dict:
        """สรุปผลการปรับการรักษารายเดือน

        Returns: {
            "total_changes": 15,
            "target_reached": 8,
            "improved": 3,
            "stable": 2,
            "worsened": 1,
            "pending": 1,
            "success_rate": 73.3,
        }
        """
        if not self._storage:
            return {"total_changes": 0, "message": "no storage"}

        if not year_month:
            year_month = datetime.now(timezone.utc).strftime("%Y-%m")

        summary = {
            "year_month": year_month,
            "total_changes": 0,
            "by_outcome": {
                "target_reached": 0,
                "improved": 0,
                "stable": 0,
                "worsened": 0,
                "pending": 0,
            },
            "by_category": {},
            "success_rate": 0,
        }

        try:
            files = self._storage.list_files(prefix=f"{self.GCS_PREFIX}/")
            if not files:
                return summary

            hns = set()
            for f in files:
                name = f if isinstance(f, str) else f.get("name", "")
                parts = name.split("/")
                if len(parts) >= 2:
                    hns.add(parts[1])

            for hn in hns:
                changes = self.get_history(hn, limit=100)
                for c in changes:
                    if c.change_date.startswith(year_month):
                        summary["total_changes"] += 1
                        outcome = c.outcome or "pending"
                        summary["by_outcome"][outcome] = (
                            summary["by_outcome"].get(outcome, 0) + 1
                        )
                        cat = c.category or "other"
                        summary["by_category"][cat] = (
                            summary["by_category"].get(cat, 0) + 1
                        )

            # Success rate = (target_reached + improved) / (total - pending)
            evaluated = summary["total_changes"] - summary["by_outcome"].get("pending", 0)
            if evaluated > 0:
                success = (summary["by_outcome"].get("target_reached", 0)
                           + summary["by_outcome"].get("improved", 0))
                summary["success_rate"] = round(success / evaluated * 100, 1)

        except Exception as e:
            logger.warning("monthly_summary: %s", e)

        return summary

    # ── Auto-detect category from drug name ──

    @staticmethod
    def detect_category(drug_name: str) -> str:
        """จำแนกหมวดยาอัตโนมัติจากชื่อยา"""
        name_lower = drug_name.lower().strip()
        if not name_lower:
            return "other"
        for cat, config in TREATMENT_CATEGORIES.items():
            if cat == "other":
                continue
            for drug in config.get("drugs", []):
                if drug in name_lower or (len(name_lower) >= 3 and name_lower in drug):
                    return cat
        return "other"

    # ── Parse treatment command (Thai) ──

    @staticmethod
    def parse_treatment_text(text: str) -> dict:
        """แปลงข้อความภาษาไทยเป็น structured treatment change

        รองรับ:
          "เพิ่ม Epiao 4000→6000 เพราะ Hb ต่ำ"
          "หยุด amlodipine เพราะ BP ต่ำ"
          "เริ่ม IV iron เพราะ Ferritin ต่ำ"
          "เปลี่ยน Epiao → Hemax 4000u"
        """
        result = {
            "action": "",
            "drug_name": "",
            "old_value": "",
            "new_value": "",
            "reason": "",
        }

        # Detect action
        action_map = {
            "เพิ่ม": "increase", "เพิ่มขนาด": "increase",
            "ลด": "decrease", "ลดขนาด": "decrease",
            "เริ่ม": "start", "เริ่มให้": "start",
            "หยุด": "stop", "หยุดให้": "stop", "off": "stop",
            "เปลี่ยน": "switch", "เปลี่ยนเป็น": "switch",
        }

        for thai, eng in action_map.items():
            if text.strip().startswith(thai):
                result["action"] = eng
                text = text.strip()[len(thai):].strip()
                break

        # Extract reason (after "เพราะ" or "เนื่องจาก")
        for sep in ["เพราะ", "เนื่องจาก", "reason:", "เหตุผล"]:
            if sep in text:
                parts = text.split(sep, 1)
                text = parts[0].strip()
                result["reason"] = parts[1].strip()
                break

        # Extract dose change (e.g., "4000→6000", "4000 เป็น 6000")
        dose_match = re.search(
            r'(\d[\d,]*)\s*(?:→|->|เป็น|to)\s*(\d[\d,]*)', text
        )
        if dose_match:
            result["old_value"] = dose_match.group(1).replace(",", "")
            result["new_value"] = dose_match.group(2).replace(",", "")
            text = text[:dose_match.start()].strip()

        # Remaining text = drug name
        drug = text.strip().rstrip(".")
        if drug:
            result["drug_name"] = drug

        return result

    # ── Private helpers ──

    def _save_change(self, change: TreatmentChange):
        """บันทึก/อัปเดต change ลง GCS"""
        if not self._storage:
            return

        path = f"{self.GCS_PREFIX}/{change.patient_hn}/changes.json"
        try:
            existing = []
            data = self._storage.get(path)
            if data:
                existing = json.loads(data)

            # Update if exists, else append
            found = False
            for i, c in enumerate(existing):
                if c.get("change_id") == change.change_id:
                    existing[i] = change.to_dict()
                    found = True
                    break
            if not found:
                existing.append(change.to_dict())

            payload = json.dumps(existing, ensure_ascii=False,
                                 default=str).encode("utf-8")
            self._storage.put(path, payload, content_type="application/json")
        except Exception as e:
            logger.warning("_save_change(%s): %s", change.change_id, e)

    @staticmethod
    def _parse_target(target_str: str) -> tuple:
        """Parse target range string → (low, high)"""
        if not target_str:
            return (0, 0)
        m = re.search(r'([\d.]+)\s*[-–]\s*([\d.]+)', target_str)
        if m:
            try:
                return (float(m.group(1)), float(m.group(2)))
            except ValueError:
                pass
        return (0, 0)
