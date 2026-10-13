"""A small test double for feedparser.

This is NOT a feedparser reimplementation and is not used by the pipeline: it exists
so ``pipeline.fetch``'s own logic (time window, allowlist, de-duplication, id
assignment, section cap) can be tested in an environment where the real library is
not installed. CI installs feedparser, and the same tests then run against it, which
is the real coverage; this double keeps them from being skipped locally.

It returns plain dicts, which is all ``parse_feed`` consumes: ``.get("entries")``, and
per entry ``link``, ``title``, ``summary``/``description`` and ``published_parsed``.
"""

from __future__ import annotations

import email.utils
import time
import xml.etree.ElementTree as ET
from typing import Any

__version__ = "test-double"

_ATOM_DATE_KEYS = ("published", "updated", "issued", "created")


def _text(element: ET.Element) -> str:
    return "".join(element.itertext()).strip()


def _published(raw: str) -> time.struct_time | None:
    if not raw:
        return None
    try:
        return email.utils.parsedate_to_datetime(raw).timetuple()
    except (TypeError, ValueError):
        try:
            return time.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None


def _entry(element: ET.Element) -> dict[str, Any]:
    entry: dict[str, Any] = {}
    for child in element:
        tag = child.tag.split("}")[-1].lower()
        if tag == "title":
            entry["title"] = _text(child)
        elif tag == "link":
            href = child.get("href")
            rel = child.get("rel", "alternate")
            if href and rel in ("alternate", None) and "link" not in entry:
                entry["link"] = href
            elif not href and _text(child):
                entry.setdefault("link", _text(child))
        elif tag in ("summary", "description", "content", "subtitle"):
            value = child.get("src") or _text(child)
            entry.setdefault("summary", value)
        elif tag in _ATOM_DATE_KEYS or tag == "pubdate":
            parsed = _published(_text(child))
            if parsed is not None:
                entry.setdefault("published_parsed", parsed)
    return entry


def parse(document: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Parse an RSS or Atom document into the subset of feedparser's shape we use."""
    if isinstance(document, str):
        document = document.encode("utf-8", "replace")
    try:
        root = ET.fromstring(document)
    except ET.ParseError:
        return {"entries": [], "bozo": True, "version": "test-double"}
    entries = [
        _entry(element)
        for element in root.iter()
        if element.tag.split("}")[-1].lower() in ("item", "entry")
    ]
    return {"entries": entries, "bozo": False, "version": "test-double"}
