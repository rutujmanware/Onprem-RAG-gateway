import re
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class ScopeInfo:
    thesis_id: str
    chapter: str
    doc_version: str = "v1"


class EntityGuard:
    """
    Enforces FR-3 Entity Guard scoping so semantic cache hits only occur when
    both thesis_id and chapter scope match the cached entry exactly.
    """

    _ROMAN_MAP = {
        "i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5,
        "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10,
        "xi": 11, "xii": 12, "xiii": 13, "xiv": 14, "xv": 15,
    }

    _CHAPTER_REGEX = re.compile(
        r"\b(?:chapter|ch\.?|sec(?:tion)?\.?)\s*([0-9]+|[ivxlcdm]+)\b",
        re.IGNORECASE,
    )
    _THESIS_REGEX = re.compile(
        r"\b(thesis[_\-\s]?[a-zA-Z0-9_\-]+)\b",
        re.IGNORECASE,
    )
    _REDIS_TAG_SPECIAL_CHARS = re.compile(r"([,\.<>{}\[\]\"':;!@#$%^&*()\-+=~\\/ ])")

    @classmethod
    def normalize_thesis_id(cls, raw_thesis_id: Optional[str]) -> str:
        if not raw_thesis_id or not raw_thesis_id.strip():
            return "global"
        cleaned = re.sub(r"[\s\-]+", "_", raw_thesis_id.strip().lower())
        return re.sub(r"[^a-z0-9_]", "", cleaned) or "global"

    @classmethod
    def normalize_chapter(cls, raw_chapter: Optional[str]) -> str:
        if not raw_chapter or not raw_chapter.strip():
            return "all"
        token = raw_chapter.strip().lower()

        # Check if already formatted like "ch_3", "chapter 3", "ch. xi", or "section 5"
        m = re.match(r"^(?:ch|chapter|sec(?:tion)?)\.?[_\-\s]*([0-9]+|[ivxlcdm]+)$", token)
        if m:
            token = m.group(1)

        if token.isdigit():
            return f"ch_{int(token)}"
        if token in cls._ROMAN_MAP:
            return f"ch_{cls._ROMAN_MAP[token]}"

        cleaned = re.sub(r"[\s\-]+", "_", token)
        cleaned = re.sub(r"[^a-z0-9_]", "", cleaned)
        return cleaned if cleaned else "all"

    @classmethod
    def extract_scope(
        cls,
        prompt: str,
        context_scope: Optional[Dict[str, Any]] = None,
        default_doc_version: str = "v1",
    ) -> ScopeInfo:
        """
        Two-tier scope resolution:
        1. Explicit context_scope payload/headers (Primary).
        2. Deterministic regex extraction from query text (Fallback).
        """
        ctx = context_scope or {}
        raw_thesis = ctx.get("thesis_id")
        raw_chapter = ctx.get("chapter")
        doc_version = str(ctx.get("doc_version") or default_doc_version).strip()

        if not raw_thesis and prompt:
            thesis_match = cls._THESIS_REGEX.search(prompt)
            if thesis_match:
                raw_thesis = thesis_match.group(1)

        if not raw_chapter and prompt:
            chapter_match = cls._CHAPTER_REGEX.search(prompt)
            if chapter_match:
                raw_chapter = chapter_match.group(1)

        return ScopeInfo(
            thesis_id=cls.normalize_thesis_id(raw_thesis),
            chapter=cls.normalize_chapter(raw_chapter),
            doc_version=doc_version,
        )

    @staticmethod
    def verify_match(
        query_scope: ScopeInfo,
        candidate_thesis_id: str,
        candidate_chapter: str,
        candidate_doc_version: Optional[str] = None,
    ) -> bool:
        """
        Explicit guard verification gate confirming candidate attributes match
        the normalized incoming scope.
        """
        scope_matches = (
            query_scope.thesis_id == EntityGuard.normalize_thesis_id(candidate_thesis_id)
            and query_scope.chapter == EntityGuard.normalize_chapter(candidate_chapter)
        )
        return scope_matches and (
            candidate_doc_version is None
            or query_scope.doc_version == str(candidate_doc_version).strip()
        )

    @classmethod
    def escape_redis_tag(cls, value: str) -> str:
        """Escapes special characters inside RediSearch TAG filter expressions."""
        return cls._REDIS_TAG_SPECIAL_CHARS.sub(r"\\\1", value)
