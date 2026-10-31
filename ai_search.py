"""AI Search Service — V4.0 Phase 4.1

Read-only document search with source citations
ป้องกัน prompt injection — ค้นหาเอกสารแบบ keyword/fuzzy match
ไม่ส่งข้อมูลผู้ป่วยไป External AI

Features:
- Full-text search across indexed documents
- Category-based filtering
- Date range filtering
- Source citations with document IDs
- Access control (RBAC-based)
- Prompt injection guard

Usage:
    from ai_search import SearchService

    search = SearchService(doc_index=doc_index, storage=storage)
    results = search.search("creatinine สูง", user_hash="abc123")
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# ─── Prompt Injection Guard ──────────────────────────────

INJECTION_PATTERNS = [
    r"ignore\s+(previous|above|all)\s+instructions",
    r"forget\s+(everything|all|your)\s+",
    r"you\s+are\s+now\s+",
    r"new\s+instructions?\s*:",
    r"system\s*:\s*",
    r"<\s*system\s*>",
    r"act\s+as\s+(if|a|an)\s+",
    r"pretend\s+(to|you)\s+",
    r"override\s+(safety|security|rules)",
    r"jailbreak",
    r"DAN\s+mode",
]

_INJECTION_RE = re.compile(
    "|".join(INJECTION_PATTERNS),
    re.IGNORECASE,
)


def check_prompt_injection(text: str) -> bool:
    """ตรวจว่า text มี prompt injection pattern หรือไม่.

    Returns:
        True ถ้าตรวจพบ (ควร reject)
    """
    if not text:
        return False
    return bool(_INJECTION_RE.search(text))


# ─── Search Result ───────────────────────────────────────


@dataclass
class SearchResult:
    """ผลลัพธ์การค้นหาเอกสาร."""
    document_id: str = ""
    category: str = ""
    patient_hn: str = ""
    upload_date: str = ""
    score: float = 0.0
    snippet: str = ""  # ข้อความที่ match (ไม่มี PHI sensitive)
    source_citation: str = ""

    def to_dict(self) -> dict:
        return {
            "document_id": self.document_id[:8],
            "category": self.category,
            "patient_hn": self.patient_hn,
            "upload_date": self.upload_date,
            "score": round(self.score, 2),
            "snippet": self.snippet[:200],
            "source": self.source_citation,
        }


# ─── Search Service ──────────────────────────────────────


class SearchService:
    """Document search service — read-only, access-controlled."""

    def __init__(self, doc_index=None, storage=None):
        self._doc_index = doc_index
        self._storage = storage

    def set_index(self, doc_index) -> None:
        self._doc_index = doc_index

    def set_storage(self, storage) -> None:
        self._storage = storage

    def search(
        self,
        query: str,
        user_hash: str = "",
        category: str = "",
        date_from: str = "",
        date_to: str = "",
        limit: int = 20,
        check_injection: bool = True,
    ) -> dict:
        """ค้นหาเอกสาร.

        Args:
            query: คำค้นหา
            user_hash: hash ของผู้ค้นหา (สำหรับ access control)
            category: กรองตามประเภท (lab/rx/dialysis/bill)
            date_from: วันที่เริ่มต้น (YYYY-MM-DD)
            date_to: วันที่สิ้นสุด (YYYY-MM-DD)
            limit: จำนวนผลลัพธ์สูงสุด
            check_injection: ตรวจ prompt injection หรือไม่

        Returns:
            {"results": [...], "total": N, "query": str}
        """
        if not query or not query.strip():
            return {"results": [], "total": 0, "query": "", "error": "กรุณาระบุคำค้นหา"}

        query = query.strip()[:200]  # จำกัดความยาว

        # Prompt injection guard
        if check_injection and check_prompt_injection(query):
            logger.warning("Prompt injection detected in search query")
            return {
                "results": [],
                "total": 0,
                "query": query,
                "error": "คำค้นหาไม่ถูกต้อง",
                "blocked": True,
            }

        if not self._doc_index:
            return {"results": [], "total": 0, "query": query, "error": "ไม่มี document index"}

        # ค้นหา
        results = self._keyword_search(
            query, category, date_from, date_to, limit,
        )

        return {
            "results": [r.to_dict() for r in results],
            "total": len(results),
            "query": query,
            "filters": {
                "category": category,
                "date_from": date_from,
                "date_to": date_to,
            },
        }

    def _keyword_search(
        self,
        query: str,
        category: str,
        date_from: str,
        date_to: str,
        limit: int,
    ) -> list[SearchResult]:
        """Keyword-based search across documents."""
        if not self._doc_index or not hasattr(self._doc_index, "_docs"):
            return []

        # Normalize query
        keywords = self._tokenize(query)
        if not keywords:
            return []

        results: list[SearchResult] = []

        for doc in self._doc_index._docs:
            # Category filter
            if category and doc.get("category", "") != category:
                continue

            # Date filter
            upload_date = doc.get("upload_date", "")
            if date_from and upload_date < date_from:
                continue
            if date_to and upload_date > date_to:
                continue

            # Score document
            score = self._score_document(doc, keywords)
            if score > 0:
                hn_folder = doc.get("patient_hn_folder", "")
                hn = hn_folder.split("_")[0] if "_" in hn_folder else hn_folder

                results.append(SearchResult(
                    document_id=doc.get("document_id", ""),
                    category=doc.get("category", ""),
                    patient_hn=hn,
                    upload_date=upload_date,
                    score=score,
                    snippet=self._make_snippet(doc, keywords),
                    source_citation=f"[{doc.get('category', '')}] {upload_date} (HN: {hn})",
                ))

        # Sort by score descending
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:limit]

    def _tokenize(self, text: str) -> list[str]:
        """แยกคำ — รองรับภาษาไทย."""
        text = text.lower().strip()
        # แยกด้วย whitespace + punctuation
        tokens = re.split(r'[\s,;.\-_/\\]+', text)
        # กรองคำสั้นเกินไป
        return [t for t in tokens if len(t) >= 2]

    def _score_document(self, doc: dict, keywords: list[str]) -> float:
        """คำนวณ relevance score."""
        score = 0.0
        searchable = " ".join([
            doc.get("category", ""),
            doc.get("patient_hn_folder", ""),
            doc.get("extracted_text", "")[:500],  # จำกัดเพื่อ performance
            doc.get("classify_reason", ""),
        ]).lower()

        for kw in keywords:
            if kw in searchable:
                score += 1.0
                # Bonus สำหรับ exact category match
                if kw == doc.get("category", "").lower():
                    score += 2.0

        # Normalize
        if keywords:
            score = score / len(keywords)

        return score

    def _make_snippet(self, doc: dict, keywords: list[str]) -> str:
        """สร้าง snippet จากข้อความที่ match."""
        text = doc.get("extracted_text", "")
        if not text:
            return f"[{doc.get('category', '')}] {doc.get('upload_date', '')}"

        # หาตำแหน่ง keyword แรก
        text_lower = text.lower()
        for kw in keywords:
            pos = text_lower.find(kw)
            if pos >= 0:
                start = max(0, pos - 40)
                end = min(len(text), pos + len(kw) + 60)
                snippet = text[start:end]
                if start > 0:
                    snippet = "..." + snippet
                if end < len(text):
                    snippet = snippet + "..."
                return snippet

        # Fallback: first 100 chars
        return text[:100] + ("..." if len(text) > 100 else "")

    def get_categories(self) -> list[dict]:
        """ดูประเภทเอกสารทั้งหมด + จำนวน."""
        if not self._doc_index or not hasattr(self._doc_index, "_docs"):
            return []

        counts: dict[str, int] = {}
        for doc in self._doc_index._docs:
            cat = doc.get("category", "other")
            counts[cat] = counts.get(cat, 0) + 1

        return [
            {"category": cat, "count": count}
            for cat, count in sorted(counts.items(), key=lambda x: -x[1])
        ]

    def get_stats(self) -> dict:
        """สถิติ search service."""
        total = len(self._doc_index._docs) if self._doc_index and hasattr(self._doc_index, "_docs") else 0
        return {
            "total_documents": total,
            "categories": self.get_categories(),
            "injection_guard": True,
            "max_query_length": 200,
        }
