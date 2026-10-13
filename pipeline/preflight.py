"""Pre-delivery checks on the HTML document itself.

The deliverable is the HTML file, so the checks that used to be made on a printed PDF
(exactly two pages, footer on every page, nothing silently clipped) are now made on the
document that actually gets sent:

* the structure is complete — one document, two sheets, the fixed footer on each;
* no template ever leaked into the output (``{{``/``{%``);
* **every paragraph reached the page**, compared with the edition JSON it came from:
  this is the check that catches a template which drops a slot, a copy-paste accident,
  or a CSS rule that renders content invisible;
* every external link is on the domain whitelist;
* the edition is about as long as the brief asks for (``layout.target_sheets``).

A failure means "do not send": the caller treats :class:`PreflightError` as fatal.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from . import sources
from .content import (
    closing_snippets,
    estimate_length,
    iter_paragraphs,
    normalise_text,
)

__all__ = [
    "PreflightError",
    "assert_deliverable",
    "document_text",
    "expected_sheets",
    "target_sheets",
]

_SCRIPT_RE = re.compile(r"<script\b.*?</script>", re.S | re.I)
_STYLE_RE = re.compile(r"<style\b.*?</style>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_HREF_RE = re.compile(r'href="([^"#][^"]*)"')
_JINJA_RE = re.compile(r"\{\{|\{%|\{#")


class PreflightError(RuntimeError):
    """The document is not fit to send."""


def document_text(html: str) -> str:
    """Visible text of the document: markup, CSS and scripts removed, whitespace folded."""
    without_scripts = _SCRIPT_RE.sub(" ", html)
    without_styles = _STYLE_RE.sub(" ", without_scripts)
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", without_styles)).strip()


def target_sheets(config: Mapping[str, Any]) -> float:
    layout = config.get("layout") or {}
    return float(layout.get("target_sheets", 2.0))


def expected_sheets(config: Mapping[str, Any]) -> int:
    return int((config.get("layout") or {}).get("sheets", 2))


def assert_deliverable(
    html: str,
    *,
    payload: Mapping[str, Any],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """Run every pre-delivery check and return a report. Raises :class:`PreflightError`."""
    from .content import edition_content

    content = edition_content(payload)
    layout = config.get("layout") or {}
    footer = str(layout.get("footer_text") or "")
    want_sheets = expected_sheets(config)
    sheets = html.count('data-sheet="')

    if (html.count("<html") != 1) or (html.count("<body") != 1):
        raise PreflightError("the document must contain exactly one <html> and one <body> element")
    if sheets != want_sheets:
        raise PreflightError(f"expected {want_sheets} sheets, found {sheets}")
    if _JINJA_RE.search(html):
        raise PreflightError("template markers are still present in the output")
    if "<script" in html.lower():
        raise PreflightError("the edition must not ship scripts")

    if footer:
        footer_count = html.count(footer)
        if footer_count < want_sheets:
            raise PreflightError(
                f"the footer {footer!r} appears {footer_count} time(s); expected it on every sheet"
            )

    text = document_text(html)
    needle = normalise_text(text)

    missing_paragraphs: list[str] = []
    for path, paragraph in iter_paragraphs(content):
        key = normalise_text(paragraph)
        if key and key not in needle:
            missing_paragraphs.append(path)
    if missing_paragraphs:
        raise PreflightError(
            f"{len(missing_paragraphs)} paragraph(s) did not reach the page: "
            + ", ".join(missing_paragraphs[:6])
        )

    snippets = closing_snippets(content)
    missing_snippets = [s for s in snippets if normalise_text(s) not in needle]
    if missing_snippets:
        raise PreflightError(
            f"{len(missing_snippets)} section ending(s) are missing: "
            + "; ".join(repr(s[:40]) for s in missing_snippets[:4])
        )

    links = {href for href in _HREF_RE.findall(html) if href.startswith("http")}
    bad_links = sorted(link for link in links if not sources.is_allowed(link))
    if bad_links:
        raise PreflightError(
            f"{len(bad_links)} link(s) outside the domain whitelist: " + ", ".join(bad_links[:5])
        )

    length = estimate_length(content)
    target = target_sheets(config)
    tolerance = float(layout.get("length_tolerance", 0.5))
    if not (target - tolerance <= length["total"] <= target + tolerance):
        raise PreflightError(
            f"the edition is {length['total']} sheet-equivalents long, outside the target "
            f"{target} ± {tolerance} (front {length['front']}, inside {length['inside']})"
        )

    report = {
        "sheets": sheets,
        "bytes": len(html.encode("utf-8")),
        "paragraphs": len(list(iter_paragraphs(content))),
        "snippets": len(snippets),
        "links": len(links),
        "footer_on_every_sheet": bool(footer),
        "length_sheets": length,
        "target_sheets": target,
    }
    print(
        f"preflight ok: {sheets} sheets · {report['bytes']:,} bytes · {report['paragraphs']} paragraphs · "
        f"{report['snippets']} section endings · {report['links']} links · "
        f"length {length['total']} sheets (target {target})"
    )
    return report
