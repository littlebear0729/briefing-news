"""Domain whitelist — the first gate of the pipeline (DESIGN.md invariants 4 and 6).

Nothing enters the candidate pool, and nothing reaches a rendered page, unless its
URL lives on a domain listed here. Article URLs are taken from the feed entry's
``link``/``url`` field only; free text (RSS summaries) is *never* scanned for URLs,
because summaries legitimately mention third-party links and are untrusted input.

The candidate ids the LLM is allowed to cite are derived from the same whitelist
pass, so "only cite the ids you were given" doubles as an anti-hallucination and
anti-prompt-injection contract.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Iterator
from urllib.parse import urlsplit

__all__ = [
    "ALLOWED_DOMAINS",
    "AllowlistError",
    "URL_FIELDS",
    "assert_allowed",
    "domain_of",
    "find_disallowed",
    "is_allowed",
    "iter_urls",
]

#: Authoritative outlets only (DESIGN.md §6E). Suffix matching means subdomains such as
#: ``feeds.bbci.co.uk`` or ``rss.nytimes.com`` are covered. Every domain here was checked
#: live on 2026-10-13 together with the feed list in config.yaml; see the note there about
#: the publishers that have discontinued or blocked public feeds (Reuters, AP, IMF, ATP).
ALLOWED_DOMAINS: frozenset[str] = frozenset(
    {
        # wire services and broadcasters
        "reuters.com",
        "apnews.com",
        "bbc.co.uk",
        "bbc.com",
        "bbci.co.uk",  # the BBC's actual feed host (feeds.bbci.co.uk)
        "aljazeera.com",
        "cnn.com",
        "abcnews.go.com",
        "npr.org",
        "cbc.ca",
        "abc.net.au",
        "dw.com",
        "france24.com",
        "skynews.com",
        "skysports.com",
        # newspapers and magazines
        "nytimes.com",
        "washingtonpost.com",
        "ft.com",
        "bloomberg.com",
        "cnbc.com",
        "economist.com",
        "theguardian.com",
        "politico.com",
        "foreignpolicy.com",
        "scmp.com",
        "nikkei.com",
        "marketwatch.com",
        "dj.com",  # Dow Jones feeds host for WSJ and MarketWatch
        "newyorker.com",
        # institutions
        "imf.org",
        "nobelprize.org",
        # sport
        "formula1.com",
        "atptour.com",
        "espn.com",
        "autosport.com",
        # culture
        "deadline.com",
        "variety.com",
        "hollywoodreporter.com",
        "rollingstone.com",
        "pitchfork.com",
        "artnews.com",
        # science and technology
        "nature.com",
        "science.org",
        "newscientist.com",
        "technologyreview.com",
        "ieee.org",
        "sciencedaily.com",
        "arstechnica.com",
        "theverge.com",
        "wired.com",
    }
)

#: Only values of these keys are treated as URLs when walking a payload.
URL_FIELDS: frozenset[str] = frozenset({"url", "link", "href", "uri", "canonical_url"})


class AllowlistError(ValueError):
    """Raised when a payload contains a URL outside :data:`ALLOWED_DOMAINS`."""


def domain_of(url: str) -> str:
    """Return the bare lowercase host of *url* (no userinfo, port or ``www.``).

    ``domain_of("https://WWW.Reuters.com:443/a/b?c#d") == "reuters.com"``.
    Schemeless input such as ``"apnews.com/x"`` is still parsed as a host.
    """
    if not isinstance(url, str):
        return ""
    candidate = url.strip()
    if not candidate:
        return ""
    if "//" not in candidate.split("?", 1)[0]:
        candidate = "//" + candidate.lstrip("/")
    try:
        netloc = urlsplit(candidate).netloc
    except ValueError:
        return ""
    if "@" in netloc:  # strip userinfo
        netloc = netloc.rsplit("@", 1)[1]
    host = netloc.split(":", 1)[0].strip().strip(".").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def is_allowed(url: str) -> bool:
    """True when *url*'s host is, or is a subdomain of, a whitelisted domain."""
    host = domain_of(url)
    if not host:
        return False
    if host in ALLOWED_DOMAINS:
        return True
    return any(host.endswith("." + allowed) for allowed in ALLOWED_DOMAINS)


def iter_urls(obj: Any, path: str = "$") -> Iterator[tuple[str, str]]:
    """Yield ``(json_path, url)`` for every URL-valued field inside *obj*.

    Lists and dicts are walked recursively; a string is reported only when its key
    is one of :data:`URL_FIELDS`, so prose is never mistaken for a link.
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            child = f"{path}.{key}"
            if isinstance(value, str):
                if key in URL_FIELDS:
                    yield child, value
            elif isinstance(value, (dict, list, tuple)):
                yield from iter_urls(value, child)
    elif isinstance(obj, (list, tuple)):
        for index, value in enumerate(obj):
            child = f"{path}[{index}]"
            if isinstance(value, dict):
                yield from iter_urls(value, child)
            elif isinstance(value, (list, tuple)):
                yield from iter_urls(value, child)


def find_disallowed(obj: Any) -> list[tuple[str, str, str]]:
    """Return ``(json_path, url, host)`` for every URL outside the whitelist."""
    return [
        (path, url, domain_of(url) or "<none>")
        for path, url in iter_urls(obj)
        if not is_allowed(url)
    ]


def assert_allowed(obj: Any, context: str = "payload", limit: int = 8) -> int:
    """Raise :class:`AllowlistError` if *obj* holds a non-whitelisted URL.

    Returns the number of URLs that were checked, so callers can log a positive
    signal (``checked=27``) instead of only ever reporting failures.
    """
    checked = 0
    violations: list[tuple[str, str, str]] = []
    for path, url in iter_urls(obj):
        checked += 1
        if not is_allowed(url):
            violations.append((path, url, domain_of(url) or "<none>"))
    if violations:
        shown: Iterable[str] = (
            f"  - {path}: {url} (host: {host})" for path, url, host in violations[:limit]
        )
        more = "" if len(violations) <= limit else f"\n  ... and {len(violations) - limit} more"
        raise AllowlistError(
            f"{context}: {len(violations)} URL(s) outside the domain whitelist "
            f"(checked {checked}).\n" + "\n".join(shown) + more
        )
    return checked


def _main(argv: list[str] | None = None) -> int:
    """``python -m pipeline.sources [payload.json]`` — check a file, or print the list."""
    import argparse

    parser = argparse.ArgumentParser(description="Domain whitelist gate")
    parser.add_argument("payload", nargs="?", help="JSON file whose URL fields are checked")
    args = parser.parse_args(argv)

    if not args.payload:
        print(f"{len(ALLOWED_DOMAINS)} allowed domains:")
        for domain in sorted(ALLOWED_DOMAINS):
            print(f"  {domain}")
        return 0

    with open(args.payload, encoding="utf-8") as handle:
        payload = json.load(handle)
    try:
        checked = assert_allowed(payload, context=args.payload)
    except AllowlistError as exc:
        print(f"FAIL {exc}")
        return 1
    print(f"OK {args.payload}: {checked} URL(s), all whitelisted")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(_main())
