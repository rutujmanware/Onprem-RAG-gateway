#!/usr/bin/env python3
"""Query Metadata Extractor with Canonical Alias Resolution for Institutional RAG.

Extracts structured academic metadata (advisor/professor, author/owner, department,
date/year, degree) from natural language user queries to pre-filter RediSearch vector indices.
Enables hybrid fallback to full-corpus vector search if the metadata filter returns 0 matches.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class ExtractedMetadata:
    """Holds extracted metadata filters and cleaned semantic query."""
    filters: Dict[str, Any] = field(default_factory=dict)
    clean_query: str = ""
    has_filters: bool = False
    extracted_spans: Dict[str, str] = field(default_factory=dict)
    is_catalog_query: bool = False
    is_count_query: bool = False


class QueryMetadataExtractor:
    """Extracts and canonicalizes query metadata for RediSearch pre-filtering."""

    # Canonical IISc Departments with comprehensive aliases
    DEPARTMENT_ALIASES: Dict[str, str] = {
        # Materials & Mechanical
        "materials engineering": "Materials Engineering",
        "materials science": "Materials Engineering",
        "materials": "Materials Engineering",
        "mateng": "Materials Engineering",
        "aerospace engineering": "Aerospace Engineering",
        "aerospace": "Aerospace Engineering",
        "aero": "Aerospace Engineering",
        "mechanical engineering": "Mechanical Engineering",
        "mechanical": "Mechanical Engineering",
        "mech": "Mechanical Engineering",
        "chemical engineering": "Chemical Engineering",
        "chemeng": "Chemical Engineering",
        "civil engineering": "Civil Engineering",
        "civil": "Civil Engineering",

        # Electrical, Electronics & Computer Science
        "computer science and automation": "Computer Science and Automation",
        "computer science": "Computer Science and Automation",
        "csa": "Computer Science and Automation",
        "electrical communication engineering": "Electrical Communication Engineering",
        "electrical communication": "Electrical Communication Engineering",
        "ece": "Electrical Communication Engineering",
        "electrical engineering": "Electrical Engineering",
        "electronic systems engineering": "Electronic Systems Engineering",
        "dese": "Electronic Systems Engineering",
        "computational and data sciences": "Computational and Data Sciences",
        "cds": "Computational and Data Sciences",

        # Physical, Chemical & Biological Sciences
        "physics": "Physics",
        "inorganic and physical chemistry": "Inorganic and Physical Chemistry",
        "ipc": "Inorganic and Physical Chemistry",
        "organic chemistry": "Organic Chemistry",
        "solid state and structural chemistry": "Solid State and Structural Chemistry Unit",
        "sscu": "Solid State and Structural Chemistry Unit",
        "molecular biophysics": "Molecular Biophysics Unit",
        "mbu": "Molecular Biophysics Unit",
        "biochemistry": "Biochemistry",
        "molecular reproduction": "Molecular Reproduction, Development and Genetics",
        "mrdg": "Molecular Reproduction, Development and Genetics",
        "microbiology and cell biology": "Microbiology and Cell Biology",
        "mcbl": "Microbiology and Cell Biology",

        # Interdisciplinary Centres
        "atmospheric and oceanic sciences": "Centre for Atmospheric and Oceanic Sciences",
        "caos": "Centre for Atmospheric and Oceanic Sciences",
        "ecological sciences": "Centre for Ecological Sciences",
        "ces": "Centre for Ecological Sciences",
        "high energy physics": "Centre for High Energy Physics",
        "chep": "Centre for High Energy Physics",
        "nano science and engineering": "Centre for Nano Science and Engineering",
        "nanoscience": "Centre for Nano Science and Engineering",
        "cense": "Centre for Nano Science and Engineering",
        "neuroscience": "Centre for Neuroscience",
        "cns": "Centre for Neuroscience",
        "sustainable technologies": "Centre for Sustainable Technologies",
        "cst": "Centre for Sustainable Technologies",
        "astra": "Centre for Sustainable Technologies",
        "management studies": "Management Studies",
        "mathematics": "Mathematics",
    }

    # Degree patterns
    DEGREE_PATTERNS = [
        (re.compile(r"\b(ph\.?d\.?|doctorate|doctoral)\b", re.IGNORECASE), "PhD"),
        (re.compile(r"\b(m\.?tech\.?|master of technology)\b", re.IGNORECASE), "MTech"),
        (re.compile(r"\b(m\.?sc\.?|master of science)\b", re.IGNORECASE), "MSc"),
    ]

    # Count patterns: "how many", "count of", "number of", "total number"
    COUNT_PATTERNS = [
        re.compile(r"\bhow\s+many\b", re.IGNORECASE),
        re.compile(r"\b(?:count|number|total\s+number)\s+of\b", re.IGNORECASE),
        re.compile(r"\btotal\s+(?:theses|thesis|papers|dissertations)\b", re.IGNORECASE),
    ]

    # Catalog & existence patterns
    CATALOG_PATTERNS = [
        re.compile(r"\b(?:is|are|was|were)\s+there\s+(?:any\s+)?(?:paper|papers|thesis|theses|work|study|studies|dissertation|dissertations)\b", re.IGNORECASE),
        re.compile(r"\b(?:do|does)\s+(?:we|iisc|the\s+repo|the\s+repository|the\s+system)\s+have\b", re.IGNORECASE),
        re.compile(r"\b(?:list|show|give\s+me)\s+(?:all\s+)?(?:the\s+)?(?:papers|theses|dissertations)\b", re.IGNORECASE),
        re.compile(r"\bexist(?:s)?\b", re.IGNORECASE),
        re.compile(r"\bany\s+paper\s+based\s+on\b", re.IGNORECASE),
        re.compile(r"\bany\s+(?:paper|thesis|theses)\s+(?:on|about|regarding)\b", re.IGNORECASE),
    ]

    # Handle / accession patterns
    HANDLE_PATTERN = re.compile(r"\b(?:(?:handle(?:\s+id)?|accession|item)\s*[:=]?\s*(?:2005/)?(\d{3,6})|2005/(\d{3,6}))\b", re.IGNORECASE)

    # Title lookup patterns
    TITLE_PATTERNS = [
        re.compile(r'["\']([^"\']{5,100})["\']'),
        re.compile(r"\b(?:titled|named|with title)\s+[\"']?([A-Za-z0-9\s,:-]{6,100})[\"']?", re.IGNORECASE),
    ]

    # Boilerplate patterns for clean query formulation
    BOILERPLATE_PATTERN = re.compile(
        r"\b(?:is|are|was|were|there|any|paper|papers|thesis|theses|dissertation|dissertations|"
        r"research|work|study|studies|what|tell|me|about|publish|published|write|written|"
        r"guided|supervised|under|department|dept|by|in|from|at|show|did|does|do|find|"
        r"how|many|exist|exists|based|on|of|with|author|advisor|prof|professor|dr|have|give|list)\b",
        re.IGNORECASE
    )

    # Stopwords/filler words that should not be parsed as names
    NAME_STOPWORDS = {
        "the", "a", "an", "this", "that", "these", "those", "what", "which", "how",
        "who", "whom", "whose", "where", "when", "why", "thesis", "theses", "paper",
        "papers", "study", "studies", "research", "work", "in", "from", "on", "about",
        "engineering", "science", "department", "center", "centre", "iisc", "campus",
        "publish", "published", "write", "written", "guided", "supervised", "under", "show",
        "prof", "professor", "dr", "mr", "mrs", "ms"
    }

    def __init__(self):
        # Sort department aliases by descending length so multi-word matches take precedence
        self.sorted_dept_keys = sorted(self.DEPARTMENT_ALIASES.keys(), key=len, reverse=True)

    def _clean_person_name(self, raw_name: str) -> str:
        """Strip trailing verbs, prepositions, or non-name tokens from extracted person names."""
        tokens = raw_name.strip().split()
        valid = []
        for t in tokens:
            cleaned = t.strip(" ,.:;!?\"'")
            if cleaned.lower() in self.NAME_STOPWORDS:
                break
            valid.append(cleaned)
        return " ".join(valid)

    def extract(self, query: str) -> ExtractedMetadata:
        """Parse natural language query and extract structured metadata filters."""
        if not query or not query.strip():
            return ExtractedMetadata(clean_query="")

        from pipeline.text_processing.typo_normalizer import TypoNormalizer
        raw_query = TypoNormalizer.get_instance().normalize(query.strip())
        filters: Dict[str, Any] = {}
        spans_to_remove: List[Tuple[int, int]] = []
        extracted_spans: Dict[str, str] = {}

        # 0. Intent Classification: Count vs Catalog / Existence
        is_count = any(bool(pat.search(raw_query)) for pat in self.COUNT_PATTERNS)
        is_catalog = is_count or any(bool(pat.search(raw_query)) for pat in self.CATALOG_PATTERNS)

        # 1. Extract Handle ID / Accession Number
        m_handle = self.HANDLE_PATTERN.search(raw_query)
        if m_handle:
            handle_val = m_handle.group(1) or m_handle.group(2)
            if handle_val:
                filters["handle_id"] = handle_val
                extracted_spans["handle_id"] = m_handle.group(0)
                spans_to_remove.append(m_handle.span())

        # 2. Extract Quoted or Explicit Title
        for t_pat in self.TITLE_PATTERNS:
            m_title = t_pat.search(raw_query)
            if m_title:
                title_cand = m_title.group(1).strip()
                if len(title_cand) >= 5 and title_cand.lower() not in self.NAME_STOPWORDS:
                    filters["title"] = title_cand
                    extracted_spans["title"] = m_title.group(0)
                    spans_to_remove.append(m_title.span())
                    break

        # 3. Extract Advisor / Professor
        advisor_patterns = [
            re.compile(r"\b(?:[Pp]rof(?:essor)?\.?|[Dd]r\.?)\s+([A-Za-z]+(?:\s+[A-Za-z]+)?)\b"),
            re.compile(r"\b(?:guided|supervised)\s+by\s+(?:[Pp]rof(?:essor)?\.?|[Dd]r\.?)?\s*([A-Za-z]+(?:\s+[A-Za-z]+)?)\b", re.IGNORECASE),
            re.compile(r"\bunder\s+(?:[Pp]rof(?:essor)?\.?|[Dd]r\.?)\s*([A-Za-z]+(?:\s+[A-Za-z]+)?)\b", re.IGNORECASE),
            re.compile(r"\badvisor\s*(?::|=|\s)\s*([A-Za-z]+(?:\s+[A-Za-z]+)?)\b", re.IGNORECASE),
        ]
        for pat in advisor_patterns:
            m = pat.search(raw_query)
            if m:
                name_cand = self._clean_person_name(m.group(1))
                if name_cand.lower() not in self.NAME_STOPWORDS and len(name_cand) > 2:
                    filters["advisor"] = name_cand
                    extracted_spans["advisor"] = m.group(0)
                    spans_to_remove.append(m.span())
                    break

        # 4. Extract Author / Student / Owner
        author_patterns = [
            re.compile(r"\b(?:with\s+author|by\s+author)\s+([A-Za-z]+(?:\s+[A-Za-z]+)?)\b", re.IGNORECASE),
            re.compile(r"\b(?:authored|written)\s+by\s+([A-Za-z]+(?:\s+[A-Za-z]+)?)\b", re.IGNORECASE),
            re.compile(r"\bthesis\s+by\s+([A-Za-z]+(?:\s+[A-Za-z]+)?)\b", re.IGNORECASE),
            re.compile(r"(?<!guided\s)(?<!supervised\s)\bby\s+(?:student\s+)?([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b"),
            re.compile(r"\bauthor\s*(?::|=|\s)\s*([A-Za-z]+(?:\s+[A-Za-z]+)?)\b", re.IGNORECASE),
        ]
        for pat in author_patterns:
            m = pat.search(raw_query)
            if m:
                name_cand = self._clean_person_name(m.group(1))
                if name_cand and name_cand.lower() not in self.NAME_STOPWORDS and len(name_cand) > 2:
                    # Don't overwrite advisor if identical
                    if filters.get("advisor") != name_cand:
                        filters["author"] = name_cand
                        extracted_spans["author"] = m.group(0)
                        spans_to_remove.append(m.span())
                        break

        # 5. Extract Academic Department
        query_lower = raw_query.lower()
        for alias in self.sorted_dept_keys:
            pattern = re.compile(rf"\b(?:in|from|at|department of|dept\.? of)?\s*{re.escape(alias)}(?:\s+(?:department|dept|centre|center|unit))?\b", re.IGNORECASE)
            m = pattern.search(raw_query)
            if m:
                canonical_dept = self.DEPARTMENT_ALIASES[alias]
                filters["department"] = canonical_dept
                extracted_spans["department"] = m.group(0)
                spans_to_remove.append(m.span())
                break

        # 6. Extract Publication Year / Date
        year_pattern = re.compile(r"\b(?:in|from|year|during|since)?\s*(19\d{2}|20[0-2]\d)\b", re.IGNORECASE)
        m_year = year_pattern.search(raw_query)
        if m_year:
            year_val = m_year.group(1)
            span_text = m_year.group(0).lower()
            if any(w in span_text for w in ["in", "from", "year", "during", "since"]) or any(w in query_lower for w in ["thesis", "theses", "published", "phd", "mtech"]):
                filters["date"] = year_val
                extracted_spans["date"] = m_year.group(0)
                spans_to_remove.append(m_year.span())

        # 7. Extract Degree
        for deg_pat, deg_val in self.DEGREE_PATTERNS:
            m = deg_pat.search(raw_query)
            if m:
                filters["degree"] = deg_val
                extracted_spans["degree"] = m.group(0)
                spans_to_remove.append(m.span())
                break

        # 8. Formulate Clean Semantic Query
        if not filters and not is_catalog:
            return ExtractedMetadata(
                filters={},
                clean_query=raw_query,
                has_filters=False,
                extracted_spans={},
                is_catalog_query=False,
                is_count_query=False,
            )

        clean_q = raw_query
        if spans_to_remove:
            sorted_spans = sorted(spans_to_remove, key=lambda s: s[0], reverse=True)
            for start, end in sorted_spans:
                clean_q = clean_q[:start] + " " + clean_q[end:]

        # Strip boilerplate stopwords
        cleaned_remnant = self.BOILERPLATE_PATTERN.sub(" ", clean_q)
        cleaned_remnant = re.sub(r"^\s*(?:on|for|of|about)\s+", "", cleaned_remnant, flags=re.IGNORECASE)
        cleaned_remnant = re.sub(r"\s+", " ", cleaned_remnant).strip(" ?,.:;!\"'")

        # If after removing entities and boilerplate, query has no substantive keywords left:
        if len(cleaned_remnant) < 3:
            if filters:
                clean_q = ""
                is_catalog = True
            else:
                clean_q = raw_query
        else:
            clean_q = cleaned_remnant

        if is_count:
            is_catalog = True

        return ExtractedMetadata(
            filters=filters,
            clean_query=clean_q,
            has_filters=bool(filters),
            extracted_spans=extracted_spans,
            is_catalog_query=is_catalog,
            is_count_query=is_count,
        )
