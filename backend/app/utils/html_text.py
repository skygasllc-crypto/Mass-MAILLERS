"""HTML helpers: plain-text alternative generation and plain-text-to-HTML conversion."""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser

_BLOCK_TAGS = {"p", "div", "br", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol", "blockquote", "hr"}
_SKIP_TAGS = {"script", "style", "head", "title"}
_TAG_RE = re.compile(r"<\s*/?\s*[a-zA-Z!][^>]*>")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self._href: list[str | None] = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "a":
            self._href.append(dict(attrs).get("href"))

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS and tag != "br":
            self.parts.append("\n")
        if tag == "a" and self._href:
            href = self._href.pop()
            if href and href.startswith(("http://", "https://", "mailto:")):
                self.parts.append(f" ({href.removeprefix('mailto:')})")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(re.sub(r"\s+", " ", data))


def html_to_text(markup: str) -> str:
    parser = _TextExtractor()
    parser.feed(markup)
    parser.close()
    text = "".join(parser.parts)
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def looks_like_html(text: str) -> bool:
    return bool(_TAG_RE.search(text))


def text_to_html(text: str) -> str:
    """Turn plain text into simple paragraphs (used when HTML mode contains no tags)."""
    paragraphs = re.split(r"\n\s*\n", text.strip())
    return "\n".join(
        "<p>" + html.escape(p).replace("\n", "<br>\n") + "</p>" for p in paragraphs if p.strip()
    )


def wrap_document(body: str) -> str:
    if re.search(r"<html[\s>]", body, re.IGNORECASE):
        return body
    return (
        "<!DOCTYPE html>\n<html>\n<head><meta charset=\"utf-8\"></head>\n"
        "<body style=\"font-family: Arial, Helvetica, sans-serif; font-size: 15px; line-height: 1.5; color: #222;\">\n"
        f"{body}\n</body>\n</html>\n"
    )
