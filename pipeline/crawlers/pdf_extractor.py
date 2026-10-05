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
    """Extracts text from PDF documents using the best available local engine."""

    def __init__(self):
        self._pdftotext_bin = shutil.which("pdftotext")

    def extract(self, pdf_path: str) -> Optional[ExtractedPaper]:
        if not os.path.exists(pdf_path) or os.path.getsize(pdf_path) == 0:
            return None

        clean_basename = os.path.basename(pdf_path).replace(".pdf", "")

        # Engine 1: PyMuPDF (fitz)
        try:
            import fitz
            doc = fitz.open(pdf_path)
            meta = doc.metadata or {}
            title = meta.get("title") or clean_basename
            total_pages = len(doc)
            pages_text = [page.get_text() for page in doc]
            doc.close()
            full_text = "\n\n".join(pages_text).strip()
            if full_text:
                return ExtractedPaper(pdf_path, title, full_text, {"extractor": "pymupdf", "total_pages": total_pages})
        except ImportError:
            pass
        except Exception as exc:
            logger.debug("PyMuPDF extraction failed for %s: %s", pdf_path, exc)

        # Engine 2: System pdftotext (/usr/bin/pdftotext, fast Poppler C++ binary)
        if self._pdftotext_bin:
            try:
                proc = subprocess.run(
                    [self._pdftotext_bin, "-layout", pdf_path, "-"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=60,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    return ExtractedPaper(
                        pdf_path,
                        clean_basename,
                        proc.stdout.strip(),
                        {"extractor": "poppler_pdftotext"},
                    )
            except Exception as exc:
                logger.debug("pdftotext extraction failed for %s: %s", pdf_path, exc)

        # Engine 3: pypdf / pypdf2
        try:
            from pypdf import PdfReader
            reader = PdfReader(pdf_path)
            pages = [page.extract_text() or "" for page in reader.pages]
            full_text = "\n\n".join(pages).strip()
            if full_text:
                return ExtractedPaper(
                    pdf_path,
                    clean_basename,
                    full_text,
                    {"extractor": "pypdf", "total_pages": len(reader.pages)},
                )
        except Exception:
            pass

        # Engine 4: Fallback ASCII/UTF-8 stream filter
        try:
            with open(pdf_path, "rb") as f:
                raw_bytes = f.read()
            import re
            strings = re.findall(b"[\x20-\x7e\n]{5,}", raw_bytes)
            decoded = "\n".join(s.decode("latin1", errors="ignore") for s in strings)
            return ExtractedPaper(pdf_path, clean_basename, decoded, {"extractor": "stream_fallback"})
        except Exception as exc:
            logger.error("All PDF extraction methods failed for %s: %s", pdf_path, exc)
            return None
