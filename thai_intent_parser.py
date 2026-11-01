"""Thai Intent Parser — V5.0 Feature 9: Conversational Command Center

แปลงข้อความภาษาไทยธรรมชาติ → intent + entities
โดย **ไม่ใช้ External AI** — rule-based pattern matching ทั้งหมด (PDPA-safe)

ตัวอย่าง:
  "สรุปค่าใช้จ่าย NIKKISO เดือนนี้"      → expense_summary {vendor: NIKKISO, period: this_month}
  "มีเอกสารใดจัดเก็บไม่สำเร็จ"            → failed_documents {}
  "ผลแล็บของ HN 1234 ล่าสุด"              → lab_result {hn: 1234}
  "ระบบเป็นยังไงบ้าง"                      → system_health {}
  "ขอดูรายการเอกสาร 10 รายการล่าสุด"      → list_recent_documents {limit: 10}

Design:
  - IntentRule: pattern (regex) + intent + priority + entity extractors
  - Entities: hn, vendor, period, keyword, limit, approval_id, month, year
  - Confidence score: 0.0–1.0 (ใช้ตัดสินใจว่าจะถามกลับหรือไม่)
  - ไม่ match → intent "unknown" + suggestions

ความปลอดภัย:
  - ไม่ log ข้อความดิบ (อาจมีข้อมูลผู้ป่วย)
  - log เฉพาะ intent name + confidence
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ─── Known entities ─────────────────────────────────────

KNOWN_VENDORS = [
    "NIKKISO", "FRESENIUS", "BAXTER", "B.BRAUN", "BBRAUN", "GAMBRO",
    "NIPRO", "TERUMO", "MEDTRONIC", "ASAHI", "TOray", "TORAY",
    "นิกคิโซ", "เฟรซีเนียส", "แบ็กซ์เตอร์",
]

THAI_MONTHS = {
    "มกราคม": 1, "ม.ค.": 1, "มค": 1,
    "กุมภาพันธ์": 2, "ก.พ.": 2, "กพ": 2,
    "มีนาคม": 3, "มี.ค.": 3, "มีค": 3,
    "เมษายน": 4, "เม.ย.": 4, "เมย": 4,
    "พฤษภาคม": 5, "พ.ค.": 5, "พค": 5,
    "มิถุนายน": 6, "มิ.ย.": 6, "มิย": 6,
    "กรกฎาคม": 7, "ก.ค.": 7, "กค": 7,
    "สิงหาคม": 8, "ส.ค.": 8, "สค": 8,
    "กันยายน": 9, "ก.ย.": 9, "กย": 9,
    "ตุลาคม": 10, "ต.ค.": 10, "ตค": 10,
    "พฤศจิกายน": 11, "พ.ย.": 11, "พย": 11,
    "ธันวาคม": 12, "ธ.ค.": 12, "ธค": 12,
}

# ─── Result ─────────────────────────────────────────────

@dataclass
class ParsedIntent:
    intent: str
    entities: dict = field(default_factory=dict)
    confidence: float = 0.0
    matched_rule: str = ""
    suggestions: list[str] = field(default_factory=list)
    original_length: int = 0

    @property
    def is_confident(self) -> bool:
        return self.confidence >= 0.6

    @property
    def is_unknown(self) -> bool:
        return self.intent == "unknown"

    def to_dict(self) -> dict:
        return {
            "intent": self.intent,
            "entities": self.entities,
            "confidence": round(self.confidence, 2),
            "matched_rule": self.matched_rule,
            "suggestions": self.suggestions,
        }


# ─── Entity Extractors ──────────────────────────────────

def extract_hn(text: str) -> Optional[str]:
    """ดึงเลข HN จากข้อความ — รองรับ 'HN 1234', 'HN1234', 'เอชเอ็น 1234', 'ผู้ป่วย 1234'."""
    patterns = [
        r"(?:HN|hn|Hn|เอชเอ็น)\s*[:：]?\s*(\d{3,10})",
        r"(?:ผู้ป่วย|คนไข้|เคส)\s*(?:เลข|หมายเลข)?\s*[:：]?\s*(\d{3,10})",
        r"(?:ของ|สำหรับ)\s*(\d{4,10})(?:\s|$)",
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group(1)
    return None


def extract_vendor(text: str) -> Optional[str]:
    """ดึงชื่อ vendor ที่รู้จัก."""
    upper = text.upper()
    for v in KNOWN_VENDORS:
        if v.upper() in upper:
            # Normalize Thai names
            if v in ("นิกคิโซ",):
                return "NIKKISO"
            if v in ("เฟรซีเนียส",):
                return "FRESENIUS"
            if v in ("แบ็กซ์เตอร์",):
                return "BAXTER"
            return v.upper().replace("BBRAUN", "B.BRAUN")
    return None


def extract_period(text: str) -> dict:
    """ดึงช่วงเวลา → {period, month, year, label}."""
    now = datetime.now(timezone(timedelta(hours=7)))  # Bangkok
    result: dict = {}

    if re.search(r"เดือนนี้|month นี้|this month", text, re.I):
        result = {"period": "this_month", "month": now.month, "year": now.year, "label": "เดือนนี้"}
    elif re.search(r"เดือนที่แล้ว|เดือนก่อน|last month", text, re.I):
        prev = (now.replace(day=1) - timedelta(days=1))
        result = {"period": "last_month", "month": prev.month, "year": prev.year, "label": "เดือนที่แล้ว"}
    elif re.search(r"ปีนี้|this year", text, re.I):
        result = {"period": "this_year", "year": now.year, "label": "ปีนี้"}
    elif re.search(r"วันนี้|today", text, re.I):
        result = {"period": "today", "label": "วันนี้"}
    elif re.search(r"สัปดาห์นี้|อาทิตย์นี้|this week", text, re.I):
        result = {"period": "this_week", "label": "สัปดาห์นี้"}
    elif re.search(r"สัปดาห์ที่แล้ว|อาทิตย์ที่แล้ว|last week", text, re.I):
        result = {"period": "last_week", "label": "สัปดาห์ที่แล้ว"}
    elif re.search(r"ไตรมาส|quarter", text, re.I):
        q = (now.month - 1) // 3 + 1
        result = {"period": "this_quarter", "quarter": q, "year": now.year, "label": f"ไตรมาส {q}"}
    else:
        # Thai month name
        for name, num in THAI_MONTHS.items():
            if name in text:
                year = now.year
                ym = re.search(r"(25\d{2}|20\d{2})", text)
                if ym:
                    y = int(ym.group(1))
                    year = y - 543 if y > 2400 else y
                result = {"period": "month", "month": num, "year": year, "label": f"{name} {year + 543}"}
                break

    return result


def extract_limit(text: str) -> Optional[int]:
    """ดึงจำนวน เช่น '10 รายการ', '5 ฉบับ'."""
    m = re.search(r"(\d{1,3})\s*(?:รายการ|ฉบับ|อัน|ตัว|เอกสาร)", text)
    if m:
        n = int(m.group(1))
        return max(1, min(n, 50))
    return None


def extract_approval_id(text: str) -> Optional[str]:
    m = re.search(r"(APR-[A-Za-z0-9\-]+)", text, re.I)
    return m.group(1).upper() if m else None


def extract_keyword(text: str, strip_words: list[str]) -> str:
    """ดึง keyword ค้นหา — ตัดคำสั่งออก เหลือแต่คำค้น."""
    kw = text
    for w in strip_words:
        kw = re.sub(w, " ", kw, flags=re.I)
    # ตัด HN/vendor/period ที่ดึงไปแล้ว
    kw = re.sub(r"(?:HN|hn)\s*\d+", " ", kw)
    kw = re.sub(r"\s+", " ", kw).strip()
    return kw


# ─── Intent Rules ───────────────────────────────────────

@dataclass
class IntentRule:
    name: str
    intent: str
    patterns: list[str]           # regex (any match)
    priority: int = 50            # สูง = ตรวจก่อน
    base_confidence: float = 0.8
    required_entities: list[str] = field(default_factory=list)
    extractors: list[str] = field(default_factory=list)   # which extractors to run
    strip_words: list[str] = field(default_factory=list)  # for keyword extraction

    def match(self, text: str) -> Optional[re.Match]:
        for p in self.patterns:
            m = re.search(p, text, re.I)
            if m:
                return m
        return None


# ลำดับความสำคัญ: เฉพาะเจาะจงก่อน → ทั่วไปทีหลัง
RULES: list[IntentRule] = [
    # ── Security / Approval (priority สูง — มี ID ชัดเจน) ──
    IntentRule(
        name="approve", intent="approve_request",
        patterns=[r"^อนุมัติ\s+APR-", r"อนุมัติ.*APR-"],
        priority=95, base_confidence=0.95,
        required_entities=["approval_id"], extractors=["approval_id"],
    ),
    IntentRule(
        name="reject", intent="reject_request",
        patterns=[r"^ปฏิเสธ\s+APR-", r"ปฏิเสธ.*APR-", r"ไม่อนุมัติ.*APR-"],
        priority=95, base_confidence=0.95,
        required_entities=["approval_id"], extractors=["approval_id"],
    ),
    IntentRule(
        name="pending_approvals", intent="approval_pending",
        patterns=[r"รออนุมัติ", r"รายการ.*อนุมัติ", r"มีอะไร.*อนุมัติ", r"pending"],
        priority=90, base_confidence=0.9,
    ),

    # ── Documents: failed (เฉพาะเจาะจง) ──
    IntentRule(
        name="failed_docs", intent="failed_documents",
        patterns=[
            r"จัดเก็บไม่สำเร็จ", r"เก็บไม่สำเร็จ", r"ไม่สำเร็จ.*เอกสาร",
            r"เอกสาร.*ไม่สำเร็จ", r"เอกสาร.*ล้มเหลว", r"เอกสาร.*error",
            r"เอกสาร.*ค้าง", r"เอกสาร.*ติด", r"dead.?letter", r"failed",
        ],
        priority=88, base_confidence=0.9,
    ),

    # ── Finance ──
    IntentRule(
        name="expense_vendor", intent="expense_summary",
        patterns=[
            r"(?:สรุป|ดู|ขอ|รายงาน|เช็ค|ตรวจ)?\s*ค่าใช้จ่าย",
            r"(?:สรุป|ดู|ขอ)?\s*(?:ยอด|รายจ่าย|จ่ายไป)",
            r"บิล.*(?:เท่าไร|เท่าไหร่|ยอด|รวม)",
            r"(?:เท่าไร|เท่าไหร่).*บิล",
            r"expense", r"invoice.*(?:สรุป|รวม|total)",
        ],
        priority=80, base_confidence=0.85,
        extractors=["vendor", "period"],
    ),
    IntentRule(
        name="monthly_report", intent="monthly_report",
        patterns=[r"รายงาน(?:ประจำ)?เดือน", r"สรุปเดือน", r"monthly"],
        priority=78, base_confidence=0.8,
        extractors=["period"],
    ),

    # ── Kidney / Lab (ต้องมี HN) ──
    IntentRule(
        name="lab_trend", intent="lab_trend",
        patterns=[r"แนวโน้ม", r"trend", r"กราฟ.*แล็บ", r"แล็บ.*กราฟ", r"เปลี่ยนแปลง.*แล็บ"],
        priority=76, base_confidence=0.85,
        required_entities=["hn"], extractors=["hn"],
    ),
    IntentRule(
        name="lab_analysis", intent="lab_analysis",
        patterns=[r"วิเคราะห์.*แล็บ", r"แล็บ.*วิเคราะห์", r"analy[sz]e.*lab", r"ประเมิน.*แล็บ"],
        priority=75, base_confidence=0.85,
        required_entities=["hn"], extractors=["hn"],
    ),
    IntentRule(
        name="lab_report", intent="lab_report",
        patterns=[r"รายงาน.*แล็บ", r"แล็บ.*รายงาน", r"lab.*report"],
        priority=74, base_confidence=0.8,
        required_entities=["hn"], extractors=["hn"],
    ),
    IntentRule(
        name="confirm_lab", intent="confirm_lab",
        patterns=[r"ยืนยัน.*แล็บ", r"ยืนยันผล", r"confirm.*lab"],
        priority=74, base_confidence=0.85,
        extractors=["hn"],
    ),
    IntentRule(
        name="ckd_staging", intent="ckd_staging",
        patterns=[r"CKD", r"stage", r"ระยะ.*ไต", r"ไต.*ระยะ", r"eGFR", r"GFR"],
        priority=73, base_confidence=0.8,
        extractors=["hn"],
    ),
    IntentRule(
        name="medication_check", intent="medication_check",
        patterns=[r"ตรวจยา", r"เช็คยา", r"ยา.*(?:ตรวจ|เช็ค|ทบทวน)", r"drug.*(?:check|review)", r"medication"],
        priority=72, base_confidence=0.85,
        extractors=["hn"],
    ),
    IntentRule(
        name="hd_summary", intent="hd_summary",
        patterns=[r"ฟอกเลือด", r"ฟอกไต", r"\bHD\b", r"dialysis", r"hemodialysis"],
        priority=71, base_confidence=0.75,
        extractors=["hn", "period"],
    ),
    IntentRule(
        name="kpi", intent="kpi_report",
        patterns=[r"\bKPI\b", r"ตัวชี้วัด", r"คุณภาพ.*(?:ไต|HD|ฟอก)"],
        priority=70, base_confidence=0.85,
        extractors=["period"],
    ),
    IntentRule(
        name="patient_summary", intent="patient_summary",
        patterns=[r"สรุป\s*HN", r"สรุปHN", r"สรุป(?:ผู้ป่วย|คนไข้)", r"ประวัติ.*(?:HN|ผู้ป่วย)", r"ข้อมูล.*(?:HN|ผู้ป่วย)"],
        priority=69, base_confidence=0.85,
        required_entities=["hn"], extractors=["hn"],
    ),
    IntentRule(
        name="lab_result", intent="lab_result",
        patterns=[r"ผลแล็บ", r"ผลเลือด", r"ผล\s*lab", r"\blab\b", r"แล็บ", r"แลบ", r"ค่าเลือด", r"ผลตรวจ"],
        priority=68, base_confidence=0.8,
        required_entities=["hn"], extractors=["hn"],
    ),

    # ── Documents ──
    IntentRule(
        name="doc_status_hn", intent="document_status",
        patterns=[r"สถานะ.*เอกสาร", r"เอกสาร.*สถานะ", r"เอกสาร.*(?:HN|ผู้ป่วย)", r"มีเอกสาร.*(?:HN|ของ)"],
        priority=66, base_confidence=0.8,
        required_entities=["hn"], extractors=["hn"],
    ),
    IntentRule(
        name="recent_docs", intent="list_recent_documents",
        patterns=[
            r"รายการล่าสุด", r"ล่าสุด", r"เอกสาร.*(?:ล่าสุด|ใหม่|recent)",
            r"(?:ดู|ขอ|แสดง)\s*(?:รายการ)?\s*เอกสาร", r"recent", r"มีอะไร(?:ใหม่|เข้ามา)",
        ],
        priority=64, base_confidence=0.8,
        extractors=["limit"],
    ),
    IntentRule(
        name="doc_stats", intent="document_stats",
        patterns=[r"สถิติ", r"จำนวนเอกสาร", r"เอกสาร.*(?:กี่|เท่าไร|เท่าไหร่|ทั้งหมด)", r"มีเอกสาร.*กี่", r"stats?"],
        priority=63, base_confidence=0.8,
    ),
    IntentRule(
        name="search", intent="search_document",
        patterns=[r"^ค้นหา", r"^หา\s", r"ค้นหา", r"search", r"(?:หา|ค้น)\s*เอกสาร", r"อยากได้.*เอกสาร"],
        priority=60, base_confidence=0.8,
        required_entities=["keyword"], extractors=["keyword"],
        strip_words=[r"ค้นหา", r"^หา\s", r"search", r"เอกสาร", r"ให้หน่อย", r"หน่อย", r"ที่เกี่ยวกับ", r"เกี่ยวกับ", r"อยากได้"],
    ),
    IntentRule(
        name="send_doc_guide", intent="send_document_guide",
        patterns=[r"^ส่งเอกสาร$", r"วิธีส่ง", r"ส่งเอกสาร.*(?:ยังไง|อย่างไร|ทำไง)", r"อัปโหลด.*ยังไง"],
        priority=58, base_confidence=0.85,
    ),
    IntentRule(
        name="register_hn", intent="register_hn",
        patterns=[r"ลงทะเบียน"],
        priority=57, base_confidence=0.85,
        extractors=["hn"],
    ),

    # ── System ──
    IntentRule(
        name="backup", intent="backup_status",
        patterns=[r"backup", r"สำรอง", r"แบ็คอัพ", r"แบ็กอัป"],
        priority=55, base_confidence=0.85,
    ),
    IntentRule(
        name="google_status", intent="google_status",
        patterns=[r"สถานะ\s*google", r"google.*สถานะ", r"drive", r"sheets?\b", r"gmail"],
        priority=54, base_confidence=0.8,
    ),
    IntentRule(
        name="health", intent="system_health",
        patterns=[
            r"สถานะระบบ", r"ระบบ.*(?:เป็นยังไง|เป็นอย่างไร|โอเค|ok|ปกติ|พร้อม)",
            r"health", r"status", r"ระบบ.*สถานะ", r"เช็คระบบ", r"ตรวจระบบ",
        ],
        priority=52, base_confidence=0.85,
    ),
    IntentRule(
        name="help", intent="help",
        patterns=[r"^help$", r"^ช่วย", r"ช่วยเหลือ", r"คำสั่ง", r"ใช้ยังไง", r"ทำอะไรได้", r"^เมนู$", r"\?$"],
        priority=50, base_confidence=0.9,
    ),
]

# เรียงตาม priority
RULES.sort(key=lambda r: -r.priority)


# ─── Parser ─────────────────────────────────────────────

class ThaiIntentParser:
    """Rule-based Thai NLU — ไม่ใช้ External AI."""

    def __init__(self, rules: Optional[list[IntentRule]] = None):
        self.rules = rules or RULES
        self._stats: dict[str, int] = {}
        self._unknown_count = 0
        self._total = 0

    def parse(self, text: str) -> ParsedIntent:
        """แปลงข้อความ → ParsedIntent."""
        self._total += 1
        t = (text or "").strip()
        if not t:
            return ParsedIntent(intent="unknown", confidence=0.0)

        # ลบ emoji / สัญลักษณ์พิเศษ ที่ไม่มีผล
        t_clean = re.sub(r"[​﻿]", "", t)

        for rule in self.rules:
            m = rule.match(t_clean)
            if not m:
                continue

            entities = self._extract(t_clean, rule)

            # ตรวจ required entities
            missing = [e for e in rule.required_entities if not entities.get(e)]
            if missing:
                # match pattern แต่ขาด entity → confidence ต่ำ + suggestion
                self._bump(rule.intent + "_missing")
                return ParsedIntent(
                    intent=rule.intent,
                    entities=entities,
                    confidence=rule.base_confidence * 0.5,
                    matched_rule=rule.name,
                    suggestions=[self._missing_hint(rule.intent, missing)],
                    original_length=len(t),
                )

            # confidence boost: match ที่ต้นข้อความ / match ยาว
            conf = rule.base_confidence
            if m.start() == 0:
                conf = min(1.0, conf + 0.1)
            if len(t_clean) <= 30:
                conf = min(1.0, conf + 0.05)

            self._bump(rule.intent)
            logger.debug("intent=%s conf=%.2f rule=%s", rule.intent, conf, rule.name)
            return ParsedIntent(
                intent=rule.intent,
                entities=entities,
                confidence=conf,
                matched_rule=rule.name,
                original_length=len(t),
            )

        # ไม่ match เลย
        self._unknown_count += 1
        return ParsedIntent(
            intent="unknown",
            confidence=0.0,
            suggestions=self._suggest(t_clean),
            original_length=len(t),
        )

    def _extract(self, text: str, rule: IntentRule) -> dict:
        ent: dict = {}
        ex = set(rule.extractors)
        if "hn" in ex:
            hn = extract_hn(text)
            if hn:
                ent["hn"] = hn
        if "vendor" in ex:
            v = extract_vendor(text)
            if v:
                ent["vendor"] = v
        if "period" in ex:
            p = extract_period(text)
            if p:
                ent.update(p)
        if "limit" in ex:
            lim = extract_limit(text)
            if lim:
                ent["limit"] = lim
        if "approval_id" in ex:
            a = extract_approval_id(text)
            if a:
                ent["approval_id"] = a
        if "keyword" in ex:
            kw = extract_keyword(text, rule.strip_words)
            if kw:
                ent["keyword"] = kw
        return ent

    @staticmethod
    def _missing_hint(intent: str, missing: list[str]) -> str:
        hints = {
            "hn": "กรุณาระบุ HN เช่น 'HN 1234'",
            "keyword": "กรุณาระบุคำค้นหา เช่น 'ค้นหา ผลแล็บ'",
            "approval_id": "กรุณาระบุรหัส เช่น 'อนุมัติ APR-xxxx'",
            "vendor": "กรุณาระบุชื่อ vendor เช่น 'NIKKISO'",
        }
        return " / ".join(hints.get(m, f"ต้องการ {m}") for m in missing)

    @staticmethod
    def _suggest(text: str) -> list[str]:
        """แนะนำคำสั่งใกล้เคียง."""
        sug = []
        if re.search(r"เงิน|บาท|จ่าย|บิล", text):
            sug.append("สรุปค่าใช้จ่าย <vendor> เดือนนี้")
        if re.search(r"ไต|เลือด|แล็บ|ผล", text):
            sug.append("ผลแล็บ HN <เลข>")
        if re.search(r"เอกสาร|ไฟล์|file", text):
            sug.append("รายการล่าสุด / ค้นหา <คำ>")
        if re.search(r"ระบบ|server|เซิร์ฟ", text):
            sug.append("สถานะระบบ")
        if not sug:
            sug.append("พิมพ์ 'ช่วยเหลือ' เพื่อดูคำสั่งทั้งหมด")
        return sug

    def _bump(self, key: str) -> None:
        self._stats[key] = self._stats.get(key, 0) + 1

    def get_stats(self) -> dict:
        return {
            "total_parsed": self._total,
            "unknown": self._unknown_count,
            "recognition_rate": round(1 - self._unknown_count / self._total, 3) if self._total else 0,
            "by_intent": dict(sorted(self._stats.items(), key=lambda x: -x[1])[:15]),
            "rules_count": len(self.rules),
        }

    def list_intents(self) -> list[dict]:
        seen = set()
        out = []
        for r in self.rules:
            if r.intent in seen:
                continue
            seen.add(r.intent)
            out.append({
                "intent": r.intent,
                "example_patterns": r.patterns[:2],
                "required": r.required_entities,
            })
        return out


# ─── Tests ──────────────────────────────────────────────

def _run_tests():
    print("🧪 Testing ThaiIntentParser...")
    p = ThaiIntentParser()

    cases = [
        # (text, expected_intent, expected_entities_subset)
        ("สรุปค่าใช้จ่าย NIKKISO เดือนนี้", "expense_summary", {"vendor": "NIKKISO", "period": "this_month"}),
        ("ค่าใช้จ่ายเดือนที่แล้วเท่าไหร่", "expense_summary", {"period": "last_month"}),
        ("มีเอกสารใดจัดเก็บไม่สำเร็จ", "failed_documents", {}),
        ("เอกสารค้างไหม", "failed_documents", {}),
        ("ผลแล็บของ HN 1234 ล่าสุด", "lab_result", {"hn": "1234"}),
        ("ผลแล็บ HN1234", "lab_result", {"hn": "1234"}),
        ("วิเคราะห์แล็บ HN 5678", "lab_analysis", {"hn": "5678"}),
        ("แนวโน้มแล็บ ผู้ป่วย 9999", "lab_trend", {"hn": "9999"}),
        ("ตรวจยา HN 1111", "medication_check", {"hn": "1111"}),
        ("สรุป HN 2222", "patient_summary", {"hn": "2222"}),
        ("ระบบเป็นยังไงบ้าง", "system_health", {}),
        ("สถานะระบบ", "system_health", {}),
        ("ขอดูรายการเอกสาร 10 รายการล่าสุด", "list_recent_documents", {"limit": 10}),
        ("รายการล่าสุด", "list_recent_documents", {}),
        ("ค้นหา ใบเสร็จ นิกคิโซ", "search_document", {"keyword": "ใบเสร็จ นิกคิโซ"}),
        ("อนุมัติ APR-abc123", "approve_request", {"approval_id": "APR-ABC123"}),
        ("ปฏิเสธ APR-xyz", "reject_request", {"approval_id": "APR-XYZ"}),
        ("รออนุมัติ", "approval_pending", {}),
        ("สถานะ backup", "backup_status", {}),
        ("ช่วยเหลือ", "help", {}),
        ("มีเอกสารกี่ฉบับ", "document_stats", {}),
        ("KPI เดือนนี้", "kpi_report", {"period": "this_month"}),
        ("ค่าใช้จ่าย ตุลาคม", "expense_summary", {"month": 10}),
        ("ลงทะเบียนHN 1234 56789", "register_hn", {"hn": "1234"}),
    ]

    passed = 0
    for text, exp_intent, exp_ent in cases:
        r = p.parse(text)
        ok = r.intent == exp_intent and all(r.entities.get(k) == v for k, v in exp_ent.items())
        mark = "✅" if ok else "❌"
        if ok:
            passed += 1
        print(f"  {mark} {text!r:45} → {r.intent} {r.entities if r.entities else ''} ({r.confidence:.2f})")
        if not ok:
            print(f"       expected {exp_intent} {exp_ent}")

    # Missing entity case
    r = p.parse("ผลแล็บ")
    assert r.intent == "lab_result" and not r.is_confident and r.suggestions, r
    print(f"  ✅ 'ผลแล็บ' (no HN) → low confidence + hint: {r.suggestions[0]}")
    passed += 1

    # Unknown case
    r = p.parse("สวัสดีครับ อากาศดีนะ")
    assert r.is_unknown and r.suggestions, r
    print(f"  ✅ unknown → suggestions: {r.suggestions}")
    passed += 1

    total = len(cases) + 2
    stats = p.get_stats()
    print(f"\n  📊 recognition_rate={stats['recognition_rate']}")
    print(f"\n{'✅' if passed == total else '❌'} {passed}/{total} tests PASSED")
    if passed != total:
        raise SystemExit(1)


if __name__ == "__main__":
    _run_tests()
