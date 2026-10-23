"""HTML parsing helpers for League of Comic Geeks responses."""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from bs4 import BeautifulSoup, Tag


def parse_list_response(text: str) -> tuple[int, BeautifulSoup]:
    """Parse a JSON response from /comic/get_comics.

    Returns (count, soup of the HTML list).
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        snippet = " ".join((text or "").split())[:120]
        raise ValueError(
            "League returned a non-JSON reply (Cloudflare page, rate limit or "
            f"site change), not an empty result: {snippet!r}"
        ) from e
    count = int(data.get("count", 0))
    html = data.get("list", "")
    soup = BeautifulSoup(html, "html.parser")
    return count, soup


def parse_page(html: str) -> BeautifulSoup:
    """Parse a full HTML page."""
    return BeautifulSoup(html, "html.parser")


def extract_id_from_href(href: str) -> Optional[int]:
    """Extract numeric ID from a URL like /comic/9559460/batman-8."""
    m = re.search(r"/(\d+)/", href)
    return int(m.group(1)) if m else None


def extract_price(text: str) -> Optional[float]:
    """Extract price from text like ' · $4.99'."""
    m = re.search(r"\$(\d+\.?\d*)", text)
    return float(m.group(1)) if m else None


def extract_date_timestamp(tag: Tag) -> Optional[str]:
    """Extract date from a span with data-date attribute (unix timestamp).

    Issues with a TBD release date carry a negative or otherwise invalid
    stamp; ``fromtimestamp`` raises on those (``[Errno 22]`` on Windows), so
    fall back to the visible text instead of crashing the whole listing.
    """
    import datetime
    date_el = tag.find(class_="date")
    if date_el and date_el.get("data-date"):
        try:
            ts = int(date_el["data-date"])
            if ts < 0:
                raise ValueError("negative timestamp")
            return datetime.date.fromtimestamp(ts).isoformat()
        except (OSError, OverflowError, ValueError):
            return date_el.get_text(strip=True) or None
    # Fallback: parse text
    if date_el:
        return date_el.get_text(strip=True)
    return None


def get_text_clean(tag: Optional[Tag]) -> str:
    """Get cleaned text from a tag, or empty string if None."""
    if tag is None:
        return ""
    return tag.get_text(strip=True)
