"""DSpace Institutional Repository Crawler & Authenticated Bitstream Downloader.

Specialized crawler for DSpace digital repositories (handle/2005/...) such as
ePrints and eTheses at IISc.

Supports:
- Authenticated bitstream downloads (full-text research PDFs restricted to logged-in users)
- Loading session cookies via --cookies or --cookies-file (Netscape / JSON / key=val)
- Programmatic DSpace / LDAP login (--user and --password)
- Dublin Core metadata extraction (Title, Authors, Abstract, Date, Handle)
"""

from __future__ import annotations

import argparse
import asyncio
import http.cookiejar
import json
import logging
import os
import re
from urllib.parse import unquote, urljoin, urlparse
from typing import Any, Dict, List, Optional, Set
import httpx
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("dspace_crawler")


class DSpaceCrawler:
    """Recursively traverses DSpace repository hierarchies and downloads authenticated research PDFs."""

    def __init__(
        self,
        start_url: str,
        output_dir: str = "data/raw_pdfs",
        max_papers: int = 50,
        cookies: Optional[str] = None,
        cookies_file: Optional[str] = None,
        auth_header: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        concurrency: int = 3,
        request_delay: float = 0.3,
    ):
        self.start_url = start_url.rstrip("/")
        parsed = urlparse(self.start_url)
        self.base_origin = f"{parsed.scheme}://{parsed.netloc}"
        self.domain = parsed.netloc
        self.output_dir = output_dir
        self.max_papers = max_papers
        self.request_delay = request_delay
        self.username = username
        self.password = password
        self.sem = asyncio.Semaphore(concurrency)

        os.makedirs(self.output_dir, exist_ok=True)

        self.visited_urls: Set[str] = set()
        self.downloaded_count = 0

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,*/*;q=0.8",
        }
        if auth_header:
            headers["Authorization"] = auth_header

        # Parse cookies from string and/or file
        cookie_dict = self._load_cookies(cookies, cookies_file)

        self.client = httpx.AsyncClient(
            headers=headers,
            cookies=cookie_dict,
            timeout=35.0,
            follow_redirects=True,
            verify=False,
        )

    def _load_cookies(self, cookie_str: Optional[str], cookie_file: Optional[str]) -> Dict[str, str]:
        """Loads cookies from a string, a Netscape cookies.txt file, or a JSON export."""
        cookie_dict: Dict[str, str] = {}

        if cookie_file and os.path.exists(cookie_file):
            logger.info("Loading cookies from file: %s", cookie_file)
            try:
                # 1. Try JSON format
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
                    # 2. Try Netscape cookies.txt format
                    cj = http.cookiejar.MozillaCookieJar(cookie_file)
                    cj.load(ignore_discard=True, ignore_expires=True)
                    for c in cj:
                        cookie_dict[c.name] = c.value
            except Exception as exc:
                # 3. Fallback: simple line-by-line key=value
                logger.debug("Parsing cookie file as key=val lines (%s)", exc)
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
            logger.info("Detected DSpace session cookie: JSESSIONID=%s...", cookie_dict["JSESSIONID"][:8])
        elif cookie_dict:
            logger.info("Loaded %d session cookies: %s", len(cookie_dict), list(cookie_dict.keys()))

        return cookie_dict

    async def login_if_configured(self) -> bool:
        """Attempts direct programmatic DSpace / LDAP form login if credentials provided."""
        if not self.username:
            return False

        login_endpoints = [
            f"{self.base_origin}/password-login",
            f"{self.base_origin}/ldap-login",
            f"{self.base_origin}/login",
        ]
        for endpoint in login_endpoints:
            try:
                logger.info("Attempting automatic login at %s...", endpoint)
                # First GET to initialize session cookie and parse hidden form fields
                get_resp = await self.client.get(endpoint)
                form_action = endpoint
                payload = {
                    "login_email": self.username,
                    "login_password": self.password,
                    "user": self.username,
                    "password": self.password,
                    "submit": "Log In",
                }
                if get_resp.status_code == 200:
                    soup = BeautifulSoup(get_resp.text, "html.parser")
                    form = soup.find("form")
                    if form:
                        action = form.get("action")
                        if action:
                            form_action = urljoin(endpoint, action)
                        # Extract any CSRF or hidden redirect tokens
                        for hidden in form.find_all("input", type="hidden"):
                            name = hidden.get("name")
                            val = hidden.get("value", "")
                            if name:
                                payload[name] = val

                post_resp = await self.client.post(form_action, data=payload)
                if post_resp.status_code in [200, 302]:
                    text_lower = post_resp.text.lower()
                    if "invalid" in text_lower or "authentication failed" in text_lower or "incorrect" in text_lower:
                        logger.warning("DSpace login rejected! Invalid username or password for: %s", self.username)
                        return False
                    logger.info("DSpace login successful! Active session cookies: %s", [c.name for c in self.client.cookies.jar])
                    return True
            except Exception as exc:
                logger.debug("Endpoint %s failed: %s", endpoint, exc)
        return False

    def _clean_url(self, url: str) -> str:
        """Strips query parameters like ?sort_by, ?order, etc. that cause crawl loops."""
        parsed = urlparse(url)
        query = ""
        if "page=" in parsed.query or "offset=" in parsed.query or "rpp=" in parsed.query:
            query = parsed.query
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}" + (f"?{query}" if query else "")

    async def _fetch(self, url: str) -> Optional[BeautifulSoup]:
        """Fetches page and returns parsed BeautifulSoup DOM."""
        clean_url = self._clean_url(url)
        if clean_url in self.visited_urls:
            return None
        self.visited_urls.add(clean_url)

        await asyncio.sleep(self.request_delay)
        async with self.sem:
            try:
                resp = await self.client.get(clean_url)
                if resp.status_code != 200:
                    logger.warning("HTTP %d for %s", resp.status_code, clean_url)
                    return None
                return BeautifulSoup(resp.text, "html.parser")
            except Exception as exc:
                logger.error("Error fetching %s: %s", clean_url, exc)
                return None

    def _extract_item_details(self, soup: BeautifulSoup, item_url: str) -> Optional[Dict[str, Any]]:
        """Extracts Dublin Core metadata and PDF download link from a thesis/item page."""
        pdf_url = None
        # 1. Dublin Core meta tags
        citation_meta = soup.find("meta", attrs={"name": "citation_pdf_url"})
        if citation_meta and citation_meta.get("content"):
            pdf_url = urljoin(item_url, citation_meta["content"])

        title_meta = soup.find("meta", attrs={"name": re.compile(r"(citation_title|DC\.title)", re.I)})
        title = title_meta["content"].strip() if title_meta and title_meta.get("content") else ""

        author_meta = soup.find_all("meta", attrs={"name": re.compile(r"(citation_author|DC\.creator)", re.I)})
        authors = [m["content"].strip() for m in author_meta if m.get("content")]

        abstract_meta = soup.find("meta", attrs={"name": re.compile(r"(citation_abstract|DCTERMS\.abstract|DC\.description\.abstract)", re.I)})
        abstract = abstract_meta["content"].strip() if abstract_meta and abstract_meta.get("content") else ""

        # 2. Look for View/Open or Bitstream links in the DOM
        if not pdf_url:
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if "/bitstream/" in href:
                    if (
                        href.lower().endswith(".pdf")
                        or "isallowed=y" in href.lower()
                        or "sequence=" in href.lower()
                        or "view" in a.get_text().lower()
                    ):
                        pdf_url = urljoin(item_url, href)
                        break
                    elif not pdf_url and not any(ext in href.lower() for ext in [".txt", ".jpg", ".png", ".license"]):
                        pdf_url = urljoin(item_url, href)

        if not pdf_url:
            # Check if page has a "restricted" login button
            for a in soup.find_all("a", href=True):
                if any(k in a["href"].lower() for k in ["/password-login", "/shibboleth-login", "/ldap-login", "/login"]):
                    if "bitstream" in a.get("href", "") or "restricted" in soup.get_text().lower():
                        logger.warning(
                            "Paper '%s' appears to have restricted access requiring login! Link: %s",
                            title or item_url,
                            item_url,
                        )
            return None

        if not title:
            h1 = soup.find(["h1", "h2", "h3"])
            title = h1.get_text().strip() if h1 else f"thesis_{hash(item_url)}"

        return {
            "title": title,
            "authors": authors,
            "abstract": abstract,
            "pdf_url": pdf_url,
            "item_url": item_url,
        }

    async def _download_pdf(self, item_info: Dict[str, Any]) -> Optional[str]:
        """Downloads the PDF and writes metadata sidecar."""
        pdf_url = item_info["pdf_url"]
        handle_match = re.search(r"handle/2005/(\d+)", item_info["item_url"])
        handle_id = handle_match.group(1) if handle_match else str(abs(hash(pdf_url)))

        clean_title = re.sub(r"[^\w\s-]", "", item_info["title"]).strip()
        clean_title = re.sub(r"[-\s]+", "_", clean_title)[:50]

        pdf_filename = f"dspace_2005_{handle_id}_{clean_title}.pdf"
        meta_filename = f"dspace_2005_{handle_id}_{clean_title}.json"

        pdf_path = os.path.join(self.output_dir, pdf_filename)
        meta_path = os.path.join(self.output_dir, meta_filename)

        if os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 0:
            logger.info("Already downloaded: %s", pdf_filename)
            return pdf_path

        async with self.sem:
            try:
                logger.info("Downloading PDF [%d/%d]: %s", self.downloaded_count + 1, self.max_papers, item_info["title"][:60])
                resp = await self.client.get(pdf_url)

                if resp.status_code in [401, 403] or "login" in str(resp.url).lower():
                    logger.warning(
                        "ACCESS RESTRICTED: PDF at %s redirected to login (%s). Please provide --cookies with your logged-in session!",
                        pdf_url,
                        resp.url,
                    )
                    return None

                resp.raise_for_status()
                content = resp.content

                # Check if it returned an HTML login page instead of PDF
                if not content.startswith(b"%PDF") and "application/pdf" not in resp.headers.get("content-type", ""):
                    if b"login" in content.lower() or b"sign in" in content.lower():
                        logger.warning(
                            "ACCESS RESTRICTED: Download returned an HTML login form. Session cookie is missing or expired for: %s",
                            pdf_url,
                        )
                    else:
                        logger.warning("URL %s is not a valid PDF file. Skipping.", pdf_url)
                    return None

                with open(pdf_path, "wb") as f:
                    f.write(content)

                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump(item_info, f, indent=2)

                self.downloaded_count += 1
                logger.info("Successfully Saved: %s (%.1f KB)", pdf_filename, len(content) / 1024.0)
                return pdf_path
            except Exception as exc:
                logger.error("Failed to download %s: %s", pdf_url, exc)
                return None

    async def crawl(self, url: str) -> None:
        """Main recursive crawler discovering communities, collections, and papers."""
        if self.downloaded_count >= self.max_papers:
            return

        soup = await self._fetch(url)
        if not soup:
            return

        # Check if current page is already an item page
        item_info = self._extract_item_details(soup, url)
        if item_info:
            await self._download_pdf(item_info)
            if self.downloaded_count >= self.max_papers:
                return
            return

        # Otherwise discover next links
        discovered_links = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href or href.startswith("#") or href.startswith("javascript:"):
                continue

            abs_url = urljoin(url, href)
            parsed = urlparse(abs_url)

            if parsed.netloc != self.domain:
                continue

            if any(trap in abs_url for trap in ["/feedback", "/contact", "/login", "/register", "sort_by=", "order="]):
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

        for link in unique_links:
            if self.downloaded_count >= self.max_papers:
                break
            await self.crawl(link)

    async def run(self) -> None:
        """Executes the DSpace repository crawl."""
        logger.info("=" * 65)
        logger.info("Starting Authenticated DSpace Repository Crawler")
        logger.info("  Start URL   : %s", self.start_url)
        logger.info("  Output Dir  : %s", self.output_dir)
        logger.info("  Max Papers  : %d", self.max_papers)
        logger.info("=" * 65)

        await self.login_if_configured()
        await self.crawl(self.start_url)
        await self.client.aclose()

        logger.info("=" * 65)
        logger.info("Crawl Complete! Downloaded %d PDFs to %s", self.downloaded_count, self.output_dir)
        logger.info("=" * 65)


def main():
    parser = argparse.ArgumentParser(description="Crawl and download PDFs from DSpace institutional repositories")
    parser.add_argument("url", help="Starting URL or handle (e.g., https://.../handle/2005/1)")
    parser.add_argument("--max", type=int, default=50, help="Maximum number of papers/theses to download (default 50)")
    parser.add_argument("--output-dir", default="data/raw_pdfs", help="Destination folder for PDFs")
    parser.add_argument("--cookies", default=os.getenv("INTRA_COOKIES"), help="Cookie string (e.g. 'JSESSIONID=...')")
    parser.add_argument("--cookies-file", help="Path to cookies.txt (Netscape or JSON format)")
    parser.add_argument("--auth", default=os.getenv("INTRA_AUTH"), help="Optional Authorization header")
    parser.add_argument("--user", help="Username/email if DSpace form login is supported")
    parser.add_argument("--password", help="Password if DSpace form login is supported")
    parser.add_argument("--concurrency", type=int, default=1, help="Concurrent download workers (default 1)")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay between requests in seconds (default 1.0)")
    parser.add_argument("--stream-ingest", action="store_true", help="Stream PDFs directly into Redis Stack vector index")
    parser.add_argument("--delete-after-ingest", action="store_true", help="Delete raw PDF after indexing to save SSD space")

    args = parser.parse_args()

    if args.stream_ingest:
        from pipeline.crawlers.stream_dspace import StreamDSpaceCrawler
        crawler = StreamDSpaceCrawler(
            start_url=args.url,
            output_dir=args.output_dir,
            max_papers=args.max,
            cookies=args.cookies,
            cookies_file=args.cookies_file,
            auth_header=args.auth,
            stream_ingest=True,
            delete_after_ingest=args.delete_after_ingest,
            concurrency=args.concurrency,
            request_delay=args.delay,
        )
        asyncio.run(crawler.run())
        return

    user = args.user
    password = args.password
    if user and not password and not args.cookies and not args.cookies_file:
        import getpass
        password = getpass.getpass(f"Enter DSpace password for {user}: ")

    crawler = DSpaceCrawler(
        start_url=args.url,
        output_dir=args.output_dir,
        max_papers=args.max,
        cookies=args.cookies,
        cookies_file=args.cookies_file,
        auth_header=args.auth,
        username=user,
        password=password,
        concurrency=args.concurrency,
    )
    asyncio.run(crawler.run())


if __name__ == "__main__":
    main()
