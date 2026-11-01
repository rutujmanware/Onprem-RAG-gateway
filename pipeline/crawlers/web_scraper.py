import httpx
from bs4 import BeautifulSoup

class CampusWebScraper:
    def __init__(self, allowed_domains=None):
        self.allowed_domains = allowed_domains or ["iisc.ac.in"]
    async def scrape_url(self, url: str):
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
            soup = BeautifulSoup(resp.text, "html.parser")
            return {"url": url, "text": soup.get_text(separator=" ", strip=True)}
