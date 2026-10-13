"""Collect candidates from the whitelisted feeds — pure code, fully deterministic.

    python -m pipeline.fetch --edition am [--date YYYY-MM-DD] [--offline-dir DIR]

Produces ``build/articles-<date>-<edition>.json`` (DESIGN.md §5.1), the only input
the single LLM call ever sees. Everything the model could get wrong is decided
here: which feeds are read, the time window, de-duplication, ordering, the per
section cap, and the candidate ids it may cite.

``collect()`` accepts pre-downloaded feed bodies so the whole stage is testable
without a network.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit

from . import sources
from .config import (
    ConfigError,
    cutoff_datetime,
    id_prefix,
    load_config,  # re-exported: DESIGN.md §4 lists fetch.load_config
    lookback_hours,
    normalize_date,
    normalize_edition,
    paths,
    section_ids,
    window,
)

__all__ = [
    "FetchError",
    "clean_text",
    "collect",
    "fetch_feed",
    "load_config",
    "main",
    "run",
    "url_key",
]

USER_AGENT = "TheGlobalBrief/1.0 (+https://github.com/littlebear0729/briefing-news)"
DEFAULT_TIMEOUT = 20.0
MAX_TITLE_CHARS = 300

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_TRACKING_PARAM_RE = re.compile(r"^(utm_|fbclid$|gclid$|mc_|ref$|cmpid$)")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]+")


class FetchError(RuntimeError):
    """A feed could not be read and the candidate pool would be unusable."""


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def clean_text(raw: Any, limit: int | None = None) -> str:
    """Strip HTML, unescape entities and collapse whitespace.

    RSS summaries are untrusted input (they sometimes carry markup, trackers and
    prompt-injection attempts); the LLM contract never treats them as instructions,
    but they are still normalised and length-capped here.
    """
    if raw is None:
        return ""
    text = html.unescape(str(raw))
    text = _TAG_RE.sub(" ", text)
    text = text.replace("\u00a0", " ")
    text = _WS_RE.sub(" ", text).strip()
    if limit is not None and len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def url_key(url: str) -> str:
    """Normalised URL key for de-duplication (drops fragments and tracking params).

    ``https://WWW.Reuters.com/world/x/?utm_source=rss#top`` and
    ``https://reuters.com/world/x`` collapse to the same key.
    """
    if not isinstance(url, str) or not url.strip():
        return ""
    candidate = url.strip()
    if "//" not in candidate.split("?", 1)[0]:
        candidate = "//" + candidate.lstrip("/")
    parts = urlsplit(candidate)
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _TRACKING_PARAM_RE.match(key.lower())
    ]
    path = parts.path.rstrip("/")
    if path.endswith("/index.html"):
        path = path[: -len("/index.html")]
    query = urlencode(kept)
    return f"{host}{path}?{query}" if query else f"{host}{path}"


def title_key(title: str) -> str:
    """Normalised title key: case-folded letters/digits only, for near-duplicate detection."""
    return _NON_ALNUM_RE.sub(" ", str(title or "").lower()).strip()


# ---------------------------------------------------------------------------
# Feed reading
# ---------------------------------------------------------------------------


def _feedparser() -> Any:
    try:
        import feedparser  # noqa: PLC0415 - optional heavy import, lazily needed
    except ModuleNotFoundError as exc:  # pragma: no cover - environment dependent
        raise FetchError(
            "feedparser is not installed, so feeds cannot be parsed. "
            "Run: pip install -r requirements.txt"
        ) from exc
    return feedparser


def fetch_feed(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> bytes:
    """Download one feed body. Raises :class:`FetchError` on any HTTP-level failure."""
    if not sources.is_allowed(url):
        raise FetchError(f"refusing to fetch non-whitelisted feed url: {url}")
    try:
        import requests  # noqa: PLC0415
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise FetchError("requests is not installed; run: pip install -r requirements.txt") from exc

    try:
        response = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"},
        )
        response.raise_for_status()
    except Exception as exc:  # requests.RequestException and friends
        raise FetchError(f"GET {url} failed: {type(exc).__name__}: {exc}") from exc
    return response.content


def _download_with_retry(url: str, timeout: float, attempts: int = 3) -> bytes:
    """Download one feed, retrying transient failures.

    Dozens of feeds are fetched at once, and publishers do throttle concurrent TLS
    connections (a bare ``SSLEOFError`` from a host that answers fine seconds later).
    A lost feed is a thinner section, so one retry is worth the wall-clock.
    """
    last_error: FetchError | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return fetch_feed(url, timeout=timeout)
        except FetchError as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(0.4 * attempt)
    raise last_error or FetchError(f"GET {url} failed")


def _entry_published(entry: Mapping[str, Any]) -> dt.datetime | None:
    """Published (or updated) time as an aware UTC datetime, else ``None``."""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return dt.datetime(*parsed[:6], tzinfo=dt.timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def _entry_link(entry: Mapping[str, Any]) -> str:
    """The article URL: ``entry.link``, else the first ``rel=alternate`` link."""
    link = entry.get("link")
    if isinstance(link, str) and link.strip():
        return link.strip()
    for candidate in entry.get("links") or []:
        if isinstance(candidate, Mapping) and candidate.get("href"):
            if candidate.get("rel") in (None, "alternate"):
                return str(candidate["href"]).strip()
    return ""


def _entry_summary(entry: Mapping[str, Any]) -> str:
    """Best available summary text, HTML-stripped."""
    for key in ("summary", "description", "subtitle"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value
    contents = entry.get("content") or []
    if contents and isinstance(contents[0], Mapping):
        return str(contents[0].get("value") or "")
    return ""


def parse_feed(
    document: bytes | str,
    feed: Mapping[str, Any],
    section: str,
    window_start: dt.datetime,
    window_end: dt.datetime,
    *,
    summary_chars: int = 800,
) -> tuple[list[dict[str, Any]], int]:
    """Parse one feed body into candidate dicts inside the window.

    Returns ``(candidates, entries_seen)``. Entries without a usable date are
    dropped (the time window is code-owned) and counted as seen.
    """
    feedparser = _feedparser()
    if isinstance(document, str):
        document = document.encode("utf-8", "replace")
    parsed = feedparser.parse(document)
    feed_name = str(feed.get("name") or feed.get("source") or "")

    candidates: list[dict[str, Any]] = []
    entries = list(parsed.get("entries") or [])
    for entry in entries:
        link = _entry_link(entry)
        if not link or not sources.is_allowed(link):
            continue
        published = _entry_published(entry)
        if published is None or not (window_start <= published <= window_end):
            continue
        title = clean_text(entry.get("title"), MAX_TITLE_CHARS)
        if not title:
            continue
        candidates.append(
            {
                "section": section,
                "title": title,
                "url": link,
                "source": sources.domain_of(link),
                "source_name": feed_name or sources.domain_of(link),
                "published": published.isoformat(),
                "summary": clean_text(_entry_summary(entry), summary_chars),
            }
        )
    return candidates, len(entries)


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def _warn(message: str) -> None:
    """Emit a GitHub annotation when running in Actions, stderr otherwise."""
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::warning::{message}")
    else:
        print(f"warning: {message}", file=sys.stderr)


def collect(
    config: Mapping[str, Any],
    edition: str,
    run_date: dt.date | str,
    *,
    documents: Mapping[str, bytes | str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_workers: int = 8,
) -> dict[str, Any]:
    """Build the articles archive payload for one edition.

    *documents* maps feed URL -> raw feed body, which makes the whole stage runnable
    offline (tests) with identical code paths to production. Without it, feeds are
    downloaded concurrently: there are dozens of them, one lagging publisher must not
    hold up the rest, and the per-feed results are still reported in config order.
    """
    edition = normalize_edition(edition)
    date = normalize_date(run_date)
    start, end = window(config, date, edition)
    budgets = config.get("budgets") or {}
    section_cap = int(budgets.get("section_cap", 12))
    summary_chars = int(budgets.get("summary_chars", 800))

    # every configured feed, in config order
    planned: list[tuple[str, Mapping[str, Any]]] = [
        (section, feed)
        for section in section_ids(config)
        for feed in (config["feeds"].get(section) or [])
        if str(feed.get("url") or "").strip()
    ]

    bodies: dict[str, bytes | str] = {}
    failures: dict[str, str] = {}
    to_download: list[str] = []
    for _section, feed in planned:
        url = str(feed["url"]).strip()
        if not sources.is_allowed(url):
            failures[url] = f"feed {url} is not on the domain whitelist — skipped"
            continue
        if documents is not None and url in documents:
            bodies[url] = documents[url]
            continue
        to_download.append(url)

    if to_download:
        with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(to_download)))) as pool:
            futures = {
                pool.submit(_download_with_retry, url, timeout): url for url in to_download
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    bodies[url] = future.result()
                except FetchError as exc:
                    failures[url] = str(exc)

    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    sections: dict[str, list[dict[str, Any]]] = {}
    feed_report: list[dict[str, Any]] = []

    for section, feed in planned:
        url = str(feed.get("url") or "").strip()
        record: dict[str, Any] = {"section": section, "url": url, "feed": feed.get("name")}
        if url in failures:
            record.update(status="rejected" if "whitelist" in failures[url] else "error", reason=failures[url], kept=0)
            feed_report.append(record)
            _warn(f"{section}: {failures[url]}")
            continue
        body = bodies.get(url)
        if body is None:  # pragma: no cover - defensive
            record.update(status="error", reason="no body", kept=0)
            feed_report.append(record)
            continue

        candidates, entries_seen = parse_feed(
            body, feed, section, start, end, summary_chars=summary_chars
        )
        record.update(status="ok", entries=entries_seen, in_window=len(candidates), kept=0)
        kept_here = 0
        for candidate in candidates:
            key = url_key(candidate["url"])
            title_key_value = title_key(candidate["title"])
            if key in seen_urls or (title_key_value and title_key_value in seen_titles):
                continue
            seen_urls.add(key)
            if title_key_value:
                seen_titles.add(title_key_value)
            sections.setdefault(section, []).append(candidate)
            kept_here += 1
        record["kept"] = kept_here
        feed_report.append(record)

    for section in section_ids(config):
        collected = sections.get(section, [])
        collected.sort(key=lambda item: (item["published"], item["title"]), reverse=True)
        if len(collected) > section_cap:
            collected = collected[:section_cap]
        for index, candidate in enumerate(collected, start=1):
            candidate["id"] = f"{id_prefix(section)}-{index:02d}"
        sections[section] = collected
        if not collected:
            _warn(
                f"{section}: 0 candidates in the window {start.isoformat()}..{end.isoformat()} "
                "— the feed list for this section may have rotted"
            )

    payload = {
        "edition": edition,
        "run_date": date.isoformat(),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "lookback_hours": lookback_hours(config, edition),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "counts": {section: len(items) for section, items in sections.items()},
        "feeds": feed_report,
        "sections": sections,
    }
    total = sum(len(items) for items in sections.values())
    print(
        f"{date.isoformat()} {edition}: {total} candidates "
        f"({', '.join(f'{s}={len(i)}' for s, i in sections.items())})"
    )
    return payload


def run(
    edition: str,
    run_date: dt.date | str | None = None,
    *,
    config: Mapping[str, Any] | None = None,
    config_path: str | os.PathLike[str] | None = None,
    documents: Mapping[str, bytes | str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Path:
    """Collect candidates and write ``build/articles-<date>-<edition>.json``.

    Returns the archive path (DESIGN.md §4).
    """
    config = config or load_config(config_path)
    edition = normalize_edition(edition)
    date = normalize_date(run_date)
    payload = collect(config, edition, date, documents=documents, timeout=timeout)

    destination = paths(config)["build_dir"]
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / f"articles-{date.isoformat()}-{edition.lower()}.json"
    archive.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {archive} ({archive.stat().st_size:,} bytes)")
    return archive


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect candidates from whitelisted feeds")
    parser.add_argument("--edition", required=True, choices=["am", "pm", "AM", "PM"])
    parser.add_argument("--date", default=None, help="run date, YYYY-MM-DD (default: today UTC)")
    parser.add_argument("--config", default=None, help="path to config.yaml")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    args = parser.parse_args(argv)

    try:
        run(args.edition, args.date, config_path=args.config, timeout=args.timeout)
    except (ConfigError, FetchError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
