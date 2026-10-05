import re

class TextCleaner:
    def __init__(self):
        self.page_num = re.compile(r"^\s*(?:page\s+\d+|\d+\s*/\s*\d+|\d+)\s*$", re.I | re.M)
        self.ref_header = re.compile(r"\n\s*(?:references|bibliography)\s*\n", re.I)

    def clean(self, text: str, is_academic_paper=True) -> str:
        if not text: return ""
        if is_academic_paper:
            m = self.ref_header.search(text)
            if m: text = text[:m.start()]
        text = self.page_num.sub("", text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
