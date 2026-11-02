"""PDF Text & Structure Extractor with multi-engine fallback.

Supports:
1. PyMuPDF (fitz) - High performance layout extraction
2. Poppler pdftotext - System C++ utility (/usr/bin/pdftotext)
3. pypdf / pypdf2 - Pure Python fallback
4. Safe text-stream decoder
"""

import logging
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ExtractedPaper:
    def __init__(self, file_path: str, title: str, full_text: str, metadata: Optional[Dict[str, Any]] = None):
        self.file_path = file_path
        self.title = title
        self.full_text = full_text
        self.metadata = metadata or {}


class PDFExtractor:
    """Extracts text from PDF documents strictly using Marker (by Surya) for advanced OCR/Layout parsing."""

    def __init__(self):
        self.marker_models = None
        self._load_marker()

    def _load_marker(self):
        try:
            from marker.models import load_all_models
            logger.info("Loading Marker models onto GPU...")
            self.marker_models = load_all_models()
            logger.info("Marker models loaded successfully.")
        except ImportError as e:
            logger.error("marker-pdf is not installed. Please install it using 'pip install marker-pdf'")
            raise e

    def extract(self, pdf_path: str) -> Optional[ExtractedPaper]:
        if not os.path.exists(pdf_path) or os.path.getsize(pdf_path) == 0:
            return None

        clean_basename = os.path.basename(pdf_path).replace(".pdf", "")

        try:
            from marker.convert import convert_single_pdf
            full_text, _, out_meta = convert_single_pdf(pdf_path, self.marker_models)
            if full_text and full_text.strip():
                return ExtractedPaper(
                    pdf_path, 
                    clean_basename, 
                    full_text.strip(), 
                    {"extractor": "marker", "total_pages": out_meta.get("pages", 0)}
                )
        except Exception as exc:
            logger.error("Marker extraction failed for %s: %s", pdf_path, exc)
            raise exc

        return None
