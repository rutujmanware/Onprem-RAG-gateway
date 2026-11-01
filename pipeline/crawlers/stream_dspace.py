"""DSpace Institutional Repository Streaming Crawler & Ingester.

Streams research PDFs directly into Redis Stack with zero disk footprint
(auto-deleting raw PDFs after vector indexing to preserve local SSD storage).
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import http.cookiejar
import json
import logging
import os
import re
import sys
from urllib.parse import unquote, urljoin, urlparse
from typing import Any, Dict, List, Optional, Set
import httpx
from bs4 import BeautifulSoup

from pipeline.crawlers.pdf_extractor import PDFExtractor
from pipeline.indexer import RedisDocumentIndexer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("stream_dspace")


class StreamDSpaceCrawler:
    """Streams DSpace PDFs directly into Redis Stack vector DB without filling local disk."""

    def __init__(
        self,
        start_url: str,
        output_dir: str = "data/raw_pdfs",
        max_papers: int = 50,
        cookies: Optional[str] = None,
        cookies_file: Optional[str] = None,
        auth_header: Optional[str] = None,
        stream_ingest: bool = True,
        delete_after_ingest: bool = True,
        concurrency: int = 1,
        request_delay: float = 1.0,
    ):
        self.start_url = start_url.rstrip("/")
        parsed = urlparse(self.start_url)
        self.base_origin = f"{parsed.scheme}://{parsed.netloc}"
        self.domain = parsed.netloc
        self.output_dir = output_dir
        self.max_papers = float("inf") if (max_papers is None or max_papers <= 0) else max_papers
        self.request_delay = request_delay
        self.stream_ingest = stream_ingest
        self.delete_after_ingest = delete_after_ingest
        self.sem = asyncio.Semaphore(concurrency)

        os.makedirs(self.output_dir, exist_ok=True)

        self.visited_urls: Set[str] = set()
        self.downloaded_count = 0

        self.extractor = PDFExtractor()
        self.indexer = RedisDocumentIndexer()

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,*/*;q=0.8",
        }
        if auth_header:
            headers["Authorization"] = auth_header

        cookie_dict = self._load_cookies(cookies, cookies_file)

        self.client = httpx.AsyncClient(
            headers=headers,
            cookies=cookie_dict,
            timeout=35.0,
            follow_redirects=True,
            verify=False,
        )

    def _load_cookies(self, cookie_str: Optional[str], cookie_file: Optional[str]) -> Dict[str, str]:
        cookie_dict: Dict[str, str] = {}
        if cookie_file and os.path.exists(cookie_file):
            print(f"[AUTH] Loading cookies from file: {cookie_file}", flush=True)
            try:
                with open(cookie_file, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                if content.startswith("[") or content.startswith("{"):
                    data = json.loads(content)
                    if isinstance(data, list):
                        for item in data:
                            if "name" in item and "value" in item:
                                cookie_dict[item["name"]] = item["value"]
                    elif isinstance(data, dict):
                        cookie_dict.update(data)
                else:
                    cj = http.cookiejar.MozillaCookieJar(cookie_file)
                    cj.load(ignore_discard=True, ignore_expires=True)
                    for c in cj:
                        cookie_dict[c.name] = c.value
            except Exception as exc:
                with open(cookie_file, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            cookie_dict[k.strip()] = v.strip()

        if cookie_str:
            for item in cookie_str.split(";"):
                if "=" in item:
                    k, v = item.strip().split("=", 1)
                    cookie_dict[k.strip()] = v.strip()

        if "JSESSIONID" in cookie_dict:
            print(f"[AUTH] Detected DSpace JSESSIONID cookie: {cookie_dict['JSESSIONID'][:12]}...", flush=True)
        elif cookie_dict:
            print(f"[AUTH] Loaded {len(cookie_dict)} session cookies: {list(cookie_dict.keys())}", flush=True)
        else:
            print("[AUTH] Warning: No session cookies detected. Restricted bitstreams may require authentication.", flush=True)

        return cookie_dict

    def _clean_url(self, url: str) -> str:
        parsed = urlparse(url)
        params = []
        if parsed.query:
            for q in parsed.query.split("&"):
                if any(q.startswith(p) for p in ["page=", "offset="]):
                    params.append(q)
        query = "&".join(params)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}" + (f"?{query}" if query else "")

    async def _get_with_retry(self, url: str, max_retries: int = 4) -> Optional[httpx.Response]:
        """Performs HTTP GET with exponential backoff for transient 502/503/504 server overload."""
        for attempt in range(1, max_retries + 1):
            try:
                resp = await self.client.get(url)
                if resp.status_code in [502, 503, 504]:
                    backoff = attempt * 5
                    print(
                        f"[BACKOFF] Upstream DSpace server returned HTTP {resp.status_code}. "
                        f"Waiting {backoff}s before retry ({attempt}/{max_retries})...",
                        flush=True,
                    )
                    await asyncio.sleep(backoff)
                    continue
                return resp
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as err:
                backoff = attempt * 5
                print(
                    f"[RETRY] Connection glitch ({type(err).__name__}). "
                    f"Waiting {backoff}s before retry ({attempt}/{max_retries})...",
                    flush=True,
                )
                await asyncio.sleep(backoff)
            except Exception as err:
                print(f"[ERR] Request error for {url}: {err}", flush=True)
                return None
        return None

    async def _fetch(self, url: str) -> Optional[BeautifulSoup]:
        clean_url = self._clean_url(url)
        if clean_url in self.visited_urls:
            return None
        self.visited_urls.add(clean_url)

        await asyncio.sleep(self.request_delay)
        async with self.sem:
            try:
                print(f"[SCAN] Visiting: {clean_url}", flush=True)
                resp = await self._get_with_retry(clean_url)
                if not resp or resp.status_code != 200:
                    code = resp.status_code if resp else "NO_RESPONSE"
                    print(f"[WARN] HTTP {code} for {clean_url}", flush=True)
                    return None
                return BeautifulSoup(resp.text, "html.parser")
            except Exception as exc:
                print(f"[ERR] Error parsing {clean_url}: {exc}", flush=True)
                return None

    def _extract_item_details(self, soup: BeautifulSoup, item_url: str) -> Optional[Dict[str, Any]]:
        pdf_url = None
        # 1. Dublin Core and citation meta tags
        citation_meta = soup.find("meta", attrs={"name": "citation_pdf_url"})
        if citation_meta and citation_meta.get("content"):
            pdf_url = urljoin(item_url, citation_meta["content"])

        title_meta = soup.find("meta", attrs={"name": re.compile(r"(citation_title|DC\.title)", re.I)})
        title = title_meta["content"].strip() if title_meta and title_meta.get("content") else ""

        author_meta = soup.find_all("meta", attrs={"name": re.compile(r"(citation_author|DC\.creator|DC\.contributor\.author)", re.I)})
        authors = [m["content"].strip() for m in author_meta if m.get("content")]

        advisor_meta = soup.find_all("meta", attrs={"name": re.compile(r"(citation_advisor|DC\.contributor\.advisor)", re.I)})
        advisors = [m["content"].strip() for m in advisor_meta if m.get("content")]

        abstract_meta = soup.find("meta", attrs={"name": re.compile(r"(citation_abstract|DCTERMS\.abstract|DC\.description\.abstract)", re.I)})
        abstract = abstract_meta["content"].strip() if abstract_meta and abstract_meta.get("content") else ""

        date_meta = soup.find("meta", attrs={"name": re.compile(r"(citation_publication_date|citation_date|DC\.date\.issued)", re.I)})
        date = date_meta["content"].strip() if date_meta and date_meta.get("content") else ""

        dept_meta = soup.find("meta", attrs={"name": re.compile(r"(DC\.contributor\.department|citation_dissertation_institution)", re.I)})
        department = dept_meta["content"].strip() if dept_meta and dept_meta.get("content") else ""

        degree_meta = soup.find("meta", attrs={"name": re.compile(r"(DC\.type|citation_dissertation_name)", re.I)})
        degree = degree_meta["content"].strip() if degree_meta and degree_meta.get("content") else ""

        keywords_meta = soup.find_all("meta", attrs={"name": re.compile(r"(citation_keywords|DC\.subject)", re.I)})
        keywords = [m["content"].strip() for m in keywords_meta if m.get("content")]

        # 2. Extract from DSpace Table / Field Rows
        for tr in soup.find_all("tr"):
            th_or_td = tr.find(["th", "td", "span", "div"], class_=re.compile(r"(label|metadataFieldLabel)", re.I))
            val_td = tr.find(["td", "div"], class_=re.compile(r"(value|metadataFieldValue)", re.I))
            if th_or_td and val_td:
                lbl = th_or_td.get_text().strip().lower()
                val = val_td.get_text().strip()
                if not title and "title" in lbl:
                    title = val
                elif not authors and ("author" in lbl or "creator" in lbl):
                    authors = [a.strip() for a in re.split(r"[;,\n]", val) if a.strip()]
                elif not advisors and ("advisor" in lbl or "guide" in lbl or "supervisor" in lbl):
                    advisors = [a.strip() for a in re.split(r"[;,\n]", val) if a.strip()]
                elif not department and any(d in lbl for d in ["department", "centre", "center", "division", "school"]):
                    department = val
                elif not date and ("date" in lbl or "year" in lbl):
                    date = val
                elif not degree and "degree" in lbl:
                    degree = val
                elif not abstract and ("abstract" in lbl or "description" in lbl):
                    abstract = val

        # 3. Extract Department / Faculty Hierarchy from Breadcrumbs
        breadcrumbs = []
        for bc in soup.find_all(["ol", "ul", "div"], class_=re.compile(r"(breadcrumb|ds-trail|trail)", re.I)):
            for li in bc.find_all(["li", "a"]):
                txt = li.get_text().strip()
                if txt and txt.lower() not in ["home", "iisc", "dspace", "theses", "handle"]:
                    breadcrumbs.append(txt)

        if not department and breadcrumbs:
            filtered_bc = [b for b in breadcrumbs if title and b.lower() != title.lower() and len(b) > 2]
            if filtered_bc:
                department = " / ".join(filtered_bc[-2:]) if len(filtered_bc) >= 2 else filtered_bc[0]

        # 4. Search for View/Open or Bitstream link in DOM
        if not pdf_url:
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if "/bitstream/" in href:
                    if (
                        href.lower().endswith(".pdf")
                        or "isallowed=y" in href.lower()
                        or "isallowed=n" in href.lower()
                        or "sequence=" in href.lower()
                        or "view" in a.get_text().lower()
                    ):
                        pdf_url = urljoin(item_url, href)
                        break
                    elif not pdf_url and not any(ext in href.lower() for ext in [".txt", ".jpg", ".png", ".license"]):
                        pdf_url = urljoin(item_url, href)

        if not pdf_url:
            return None

        if not title:
            h1 = soup.find(["h1", "h2", "h3"])
            title = h1.get_text().strip() if h1 else f"thesis_{hash(item_url)}"

        return {
            "title": title,
            "authors": authors,
            "advisors": advisors,
            "department": department,
            "date": date,
            "degree": degree,
            "keywords": keywords,
            "abstract": abstract,
            "breadcrumbs": breadcrumbs,
            "pdf_url": pdf_url,
            "item_url": item_url,
        }

    async def _download_and_ingest(self, item_info: Dict[str, Any]) -> None:
        pdf_url = item_info["pdf_url"]
        handle_match = re.search(r"handle/2005/(\d+)", item_info["item_url"])
        handle_id = handle_match.group(1) if handle_match else str(abs(hash(pdf_url)))

        clean_title = re.sub(r"[^\w\s-]", "", item_info["title"]).strip()
        clean_title = re.sub(r"[-\s]+", "_", clean_title)[:50]

        pdf_filename = f"dspace_2005_{handle_id}_{clean_title}.pdf"
        meta_filename = f"dspace_2005_{handle_id}_{clean_title}.json"

        pdf_path = os.path.join(self.output_dir, pdf_filename)
        meta_path = os.path.join(self.output_dir, meta_filename)

        # Resume support: skip if this handle has already been ingested
        existing = glob.glob(os.path.join(self.output_dir, f"dspace_2005_{handle_id}_*.json"))
        if existing or os.path.exists(meta_path):
            print(f"[RESUME SKIP] Already ingested (handle {handle_id}): {item_info['title'][:60]}", flush=True)
            return

        async with self.sem:
            try:
                print(f"\n[DOWNLOAD] [{self.downloaded_count + 1}] {item_info['title'][:65]}...", flush=True)
                if item_info.get("department"):
                    print(f"  Department       : {item_info['department']}", flush=True)
                if item_info.get("authors"):
                    print(f"  Author(s)        : {', '.join(item_info['authors'])}", flush=True)
                if item_info.get("advisors"):
                    print(f"  Advisor(s)       : {', '.join(item_info['advisors'])}", flush=True)

                resp = await self._get_with_retry(pdf_url)
                if not resp:
                    print(f"[ERR] Failed to download {pdf_url} after retries.", flush=True)
                    return

                if resp.status_code in [401, 403] or "login" in str(resp.url).lower():
                    print(f"[AUTH REQUIRED] PDF redirected to login ({resp.url}). Check your cookies!", flush=True)
                    return

                resp.raise_for_status()
                content = resp.content

                if not content.startswith(b"%PDF") and "application/pdf" not in resp.headers.get("content-type", ""):
                    if b"login" in content.lower() or b"sign in" in content.lower():
                        print(f"[AUTH REQUIRED] Download returned an HTML login page. Cookie expired or missing.", flush=True)
                    else:
                        print(f"[SKIP] URL did not return PDF bytes ({len(content)} bytes).", flush=True)
                    return

                with open(pdf_path, "wb") as f:
                    f.write(content)

                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump(item_info, f, indent=2)

                self.downloaded_count += 1
                size_mb = len(content) / (1024.0 * 1024.0)
                print(f"[SAVED] {pdf_filename} ({size_mb:.2f} MB)", flush=True)

                # 1. Stream Ingest into Redis Stack
                if self.stream_ingest:
                    print(f"[INGEST] Vectorizing and indexing into Redis Stack: {pdf_filename}...", flush=True)
                    try:
                        extracted = self.extractor.extract(pdf_path)
                        body_text = extracted.full_text if extracted else ""

                        # Prepend rich metadata block for semantic alignment
                        header_parts = []
                        if item_info.get("title"):
                            header_parts.append(f"Title: {item_info['title']}")
                        if item_info.get("authors"):
                            header_parts.append(f"Author(s): {', '.join(item_info['authors'])}")
                        if item_info.get("advisors"):
                            header_parts.append(f"Advisor(s): {', '.join(item_info['advisors'])}")
                        if item_info.get("department"):
                            header_parts.append(f"Department: {item_info['department']}")
                        if item_info.get("date"):
                            header_parts.append(f"Date: {item_info['date']}")
                        if item_info.get("degree"):
                            header_parts.append(f"Degree: {item_info['degree']}")
                        if item_info.get("keywords"):
                            header_parts.append(f"Keywords: {', '.join(item_info['keywords'])}")
                        if item_info.get("abstract"):
                            header_parts.append(f"Abstract: {item_info['abstract']}")

                        metadata_header = "\n".join(header_parts)
                        text = f"{metadata_header}\n\n=== Paper Body ===\n{body_text}" if metadata_header else body_text

                        chunks_cnt = await self.indexer.index_document(
                            doc_id=f"dspace_2005_{handle_id}",
                            raw_text=text,
                            metadata={**item_info, "source": pdf_filename, "handle_id": handle_id},
                            is_paper=True,
                        )
                        if chunks_cnt > 0:
                            print(f"[SUCCESS] Indexed {chunks_cnt} vector chunks into Redis Stack for '{pdf_filename}'!", flush=True)
                        else:
                            print(f"[WARN] 0 chunks indexed for '{pdf_filename}'. Ensure 'redis' package is installed ('pip install redis').", flush=True)
                    except Exception as iexc:
                        print(f"[ERR] Ingestion into Redis failed: {iexc}", flush=True)

                # 2. Free Disk Space Immediately
                if self.delete_after_ingest:
                    try:
                        if os.path.exists(pdf_path):
                            os.remove(pdf_path)
                            print(f"[CLEANUP] Deleted raw PDF to preserve local SSD space: {pdf_filename}", flush=True)
                    except Exception as dexc:
                        pass

            except Exception as exc:
                print(f"[ERR] Download error for {pdf_url}: {exc}", flush=True)

    async def crawl(self, url: str) -> None:
        if self.downloaded_count >= self.max_papers:
            return

        soup = await self._fetch(url)
        if not soup:
            return

        # Check if page is an item page (has bitstream / thesis link)
        item_info = self._extract_item_details(soup, url)
        if item_info:
            await self._download_and_ingest(item_info)
            return

        # Discover sub-communities, collections, items, pagination
        discovered_links = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href or href.startswith("#") or href.startswith("javascript:"):
                continue

            abs_url = urljoin(url, href)
            parsed = urlparse(abs_url)

            if parsed.netloc != self.domain:
                continue

            if any(trap in abs_url for trap in [
                "/feedback", "/contact", "/login", "/register",
                "sort_by=", "order=", "/discover", "filtertype", "filter_"
            ]):
                continue

            if "/handle/2005/" in parsed.path:
                discovered_links.append(abs_url)
            elif "handle/2005" in url and any(p in parsed.query for p in ["page=", "offset="]):
                discovered_links.append(abs_url)

        unique_links = []
        for link in discovered_links:
            clean = self._clean_url(link)
            if clean not in self.visited_urls and clean not in unique_links:
                unique_links.append(clean)

        print(f"[DISCOVER] Found {len(unique_links)} DSpace links on {url}", flush=True)

        for link in unique_links:
            if self.downloaded_count >= self.max_papers:
                break
            await self.crawl(link)

    async def run(self) -> None:
        print("\n" + "=" * 65, flush=True)
        print("Starting DSpace Streaming Vector Ingestion", flush=True)
        print(f"  Target URL       : {self.start_url}", flush=True)
        max_str = "Unlimited (Indexing ALL documents)" if self.max_papers == float("inf") else str(int(self.max_papers))
        print(f"  Max Papers       : {max_str}", flush=True)
        print(f"  Stream to Redis  : {self.stream_ingest}", flush=True)
        print(f"  Delete Local PDF : {self.delete_after_ingest} (Keeps SSD at 0 MB extra usage)", flush=True)
        print("=" * 65 + "\n", flush=True)

        await self.crawl(self.start_url)
        await self.client.aclose()

        print("\n" + "=" * 65, flush=True)
        print(f"Crawl Complete! Ingested {self.downloaded_count} papers into Redis Stack.", flush=True)
        print("=" * 65 + "\n", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Stream DSpace PDFs directly into Redis Stack vector DB")
    parser.add_argument("url", help="Starting URL or handle (e.g., https://etd.iisc.ac.in/handle/2005/1)")
    parser.add_argument("--max", type=int, default=0, help="Maximum papers to index (default 0 = ALL / unlimited)")
    parser.add_argument("--unlimited", action="store_true", help="Crawl and index all documents without limit")
    parser.add_argument("--output-dir", default="data/raw_pdfs", help="Temporary destination folder for PDFs")
    parser.add_argument("--cookies", default=os.getenv("INTRA_COOKIES"), help="Cookie string (e.g. 'JSESSIONID=...')")
    parser.add_argument("--cookies-file", help="Path to cookies.txt (Netscape or JSON format)")
    parser.add_argument("--auth", default=os.getenv("INTRA_AUTH"), help="Optional Authorization header")
    parser.add_argument("--concurrency", type=int, default=1, help="Concurrent download workers (default 1)")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay between requests in seconds (default 1.0)")
    parser.add_argument("--keep-pdfs", action="store_true", help="Keep local PDF files instead of deleting them")

    args = parser.parse_args()
    max_papers = float("inf") if (args.unlimited or args.max <= 0) else args.max

    crawler = StreamDSpaceCrawler(
        start_url=args.url,
        output_dir=args.output_dir,
        max_papers=max_papers,
        cookies=args.cookies,
        cookies_file=args.cookies_file,
        auth_header=args.auth,
        stream_ingest=True,
        delete_after_ingest=not args.keep_pdfs,
        concurrency=args.concurrency,
        request_delay=args.delay,
    )
    asyncio.run(crawler.run())


if __name__ == "__main__":
    main()
