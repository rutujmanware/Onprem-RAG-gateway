"""Authenticated / Intranet PDF Crawler & Downloader.

Crawls institutional web pages (e.g. course websites on IISc WLAN)
to discover and download all linked PDF documents into data/raw_pdfs/.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
from urllib.parse import unquote, urljoin, urlparse
from typing import List, Optional, Set
import httpx
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("pdf_downloader")


class AuthenticatedPDFDownloader:
    """Discovers and downloads PDFs from institutional portals and course websites."""

    def __init__(
        self,
        base_url: str,
        cookies: Optional[str] = None,
        auth_header: Optional[str] = None,
        output_dir: str = "data/raw_pdfs",
        max_depth: int = 1,
    ):
        self.base_url = base_url
        self.domain = urlparse(base_url).netloc
        self.output_dir = output_dir
        self.max_depth = max_depth
        self.visited_urls: Set[str] = set()
        self.downloaded_pdfs: Set[str] = set()

        os.makedirs(self.output_dir, exist_ok=True)

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,*/*;q=0.8",
        }
        if auth_header:
            headers["Authorization"] = auth_header

        # Parse cookie string if supplied
        cookie_dict = {}
        if cookies:
            for item in cookies.split(";"):
                if "=" in item:
                    k, v = item.strip().split("=", 1)
                    cookie_dict[k.strip()] = v.strip()

        self.client = httpx.AsyncClient(
            headers=headers,
            cookies=cookie_dict,
            timeout=30.0,
            follow_redirects=True,
            verify=False,  # nosec B501 - Intranet crawler for campus repositories with internal/self-signed certs
        )

    def _sanitize_filename(self, url: str) -> str:
        """Derives a clean, readable local filename from a PDF URL."""
        parsed = urlparse(url)
        path_name = os.path.basename(unquote(parsed.path))
        if path_name and path_name.lower().endswith(".pdf"):
            base_name = path_name
        else:
            query_part = re.sub(r"[^\w\.-]", "_", unquote(parsed.query))[:40]
            base_name = f"doc_{abs(hash(url))}_{query_part}.pdf"

        # Remove invalid filesystem characters
        clean_name = re.sub(r"[^\w\.\-]", "_", base_name)
        if not clean_name.lower().endswith(".pdf"):
            clean_name += ".pdf"
        return clean_name

    async def download_file(self, pdf_url: str) -> Optional[str]:
        """Downloads a single PDF to the output directory."""
        if pdf_url in self.downloaded_pdfs:
            return None

        filename = self._sanitize_filename(pdf_url)
        dest_path = os.path.join(self.output_dir, filename)

        try:
            logger.info("Downloading: %s -> %s", pdf_url, filename)
            resp = await self.client.get(pdf_url)
            resp.raise_for_status()

            content = resp.content
            # Validate PDF signature (%PDF-)
            if not content.startswith(b"%PDF") and "application/pdf" not in resp.headers.get("content-type", ""):
                logger.warning("URL %s did not return valid PDF bytes (content-type: %s). Skipping.",
                               pdf_url, resp.headers.get("content-type"))
                return None

            with open(dest_path, "wb") as f:
                f.write(content)

            self.downloaded_pdfs.add(pdf_url)
            logger.info("Saved: %s (%.1f KB)", dest_path, len(content) / 1024.0)
            return dest_path
        except Exception as exc:
            logger.error("Failed to download %s: %s", pdf_url, exc)
            return None

    async def crawl_and_extract_pdf_links(self, url: str, current_depth: int = 0) -> List[str]:
        """Recursively parses HTML pages to locate all PDF links."""
        if url in self.visited_urls or current_depth > self.max_depth:
            return []

        self.visited_urls.add(url)
        logger.info("Scanning page: %s (depth=%d)", url, current_depth)

        pdf_links: List[str] = []
        subpage_links: List[str] = []

        try:
            resp = await self.client.get(url)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            for tag in soup.find_all("a", href=True):
                href = tag["href"].strip()
                if not href or href.startswith("#") or href.startswith("javascript:"):
                    continue

                abs_url = urljoin(url, href)
                parsed = urlparse(abs_url)
                anchor_text = tag.get_text().strip().lower()

                # Check if link points to a PDF
                is_pdf_link = (
                    parsed.path.lower().endswith(".pdf")
                    or "format=pdf" in parsed.query.lower()
                    or "download=pdf" in parsed.query.lower()
                    or "type=pdf" in parsed.query.lower()
                    or ".pdf" in parsed.query.lower()
                    or anchor_text.endswith(".pdf")
                    or "[pdf]" in anchor_text
                    or "(pdf)" in anchor_text
                )

                if is_pdf_link:
                    if abs_url not in pdf_links:
                        pdf_links.append(abs_url)
                elif parsed.netloc == self.domain and current_depth < self.max_depth:
                    # Same-domain subpage to crawl
                    if abs_url not in self.visited_urls and abs_url not in subpage_links:
                        subpage_links.append(abs_url)

            # Follow subpages if within max_depth
            for sub_url in subpage_links:
                sub_pdfs = await self.crawl_and_extract_pdf_links(sub_url, current_depth + 1)
                for sp in sub_pdfs:
                    if sp not in pdf_links:
                        pdf_links.append(sp)

        except Exception as exc:
            logger.error("Failed to parse page %s: %s", url, exc)

        return pdf_links

    async def run(self) -> List[str]:
        """Executes full discovery and download pipeline."""
        logger.info("Starting crawler on: %s", self.base_url)
        pdf_urls = await self.crawl_and_extract_pdf_links(self.base_url)
        logger.info("Discovered %d potential PDF link(s). Commencing download...", len(pdf_urls))

        downloaded_paths = []
        for pdf_url in pdf_urls:
            path = await self.download_file(pdf_url)
            if path:
                downloaded_paths.append(path)

        await self.client.aclose()
        logger.info("=" * 60)
        logger.info("Done! Downloaded %d PDFs to '%s'", len(downloaded_paths), self.output_dir)
        logger.info("=" * 60)
        return downloaded_paths


def main():
    parser = argparse.ArgumentParser(description="Download all PDFs from IISc WLAN intranet or course pages")
    parser.add_argument("--url", required=True, help="Target portal/course URL (e.g. http://10.x.x.x/... or https://...)")
    parser.add_argument("--cookies", default=os.getenv("INTRA_COOKIES"), help="Optional cookie string if required")
    parser.add_argument("--auth-header", default=os.getenv("INTRA_AUTH"), help="Optional Authorization header")
    parser.add_argument("--output-dir", default="data/raw_pdfs", help="Directory where PDFs will be stored")
    parser.add_argument("--depth", type=int, default=1, help="Crawl depth for following intranet links (default 1)")

    args = parser.parse_args()
    downloader = AuthenticatedPDFDownloader(
        base_url=args.url,
        cookies=args.cookies,
        auth_header=args.auth_header,
        output_dir=args.output_dir,
        max_depth=args.depth,
    )
    asyncio.run(downloader.run())


if __name__ == "__main__":
    main()
