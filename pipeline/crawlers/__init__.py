try:
    from pipeline.crawlers.pdf_extractor import PDFExtractor
except Exception:
    PDFExtractor = None

try:
    from pipeline.crawlers.web_scraper import CampusWebScraper
except Exception:
    CampusWebScraper = None

__all__ = ["CampusWebScraper", "PDFExtractor"]
