"""The domain whitelist — the pipeline's first gate."""

from __future__ import annotations

import json

import pytest

from pipeline import sources
from pipeline.sources import ALLOWED_DOMAINS, AllowlistError, assert_allowed, domain_of, find_disallowed, is_allowed, iter_urls


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.reuters.com/world/x", "reuters.com"),
        ("http://feeds.bbci.co.uk/news/rss.xml", "feeds.bbci.co.uk"),
        ("https://WWW.APNews.com:443/a/b?c=1#top", "apnews.com"),
        ("//edition.cnn.com/x", "edition.cnn.com"),
        ("apnews.com/article", "apnews.com"),
        ("", ""),
        ("not a url", "not a url"),
    ],
)
def test_domain_of(url: str, expected: str) -> None:
    assert domain_of(url) == expected


@pytest.mark.parametrize(
    "url,allowed",
    [
        ("https://www.reuters.com/x", True),
        ("https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml", True),
        ("https://search.cnbc.com/rs/search/view.xml", True),
        ("https://cdn.reuters.com.evil.example/x", False),
        ("https://reuters.com.attacker.net/x", False),
        ("https://example.com/x", False),
        ("ftp://reuters.com/x", True),  # scheme is irrelevant; the host is what matters
        ("", False),
    ],
)
def test_is_allowed_uses_host_suffix_matching(url: str, allowed: bool) -> None:
    assert is_allowed(url) is allowed


def test_whitelist_covers_the_domains_named_in_the_brief() -> None:
    for domain in ("apnews.com", "bbc.co.uk", "cnn.com", "aljazeera.com", "reuters.com", "imf.org", "formula1.com", "deadline.com"):
        assert domain in ALLOWED_DOMAINS


def test_iter_urls_only_treats_url_fields_as_urls() -> None:
    payload = {
        "url": "https://www.reuters.com/a",
        "summary": "see https://evil.example/x for details",  # prose is not scanned
        "items": [{"link": "https://www.bbc.co.uk/b"}],
        "nested": {"deep": {"href": "https://example.com/c"}},
        "count": 3,
    }
    found = {url for _path, url in iter_urls(payload)}
    assert found == {
        "https://www.reuters.com/a",
        "https://www.bbc.co.uk/b",
        "https://example.com/c",
    }


def test_assert_allowed_reports_every_violation_and_counts_checks() -> None:
    with pytest.raises(AllowlistError) as excinfo:
        assert_allowed({"url": "https://good.example/x", "sources": [{"url": "https://evil.example/y"}]})
    message = str(excinfo.value)
    assert "evil.example" in message and "whitelist" in message

    assert assert_allowed({"a": {"url": "https://www.reuters.com/x"}, "b": {"url": "https://apnews.com/y"}}) == 2


def test_find_disallowed_returns_paths() -> None:
    violations = find_disallowed({"sources": [{"url": "https://nope.example/z"}]})
    assert violations and violations[0][0].endswith("sources[0].url")


def test_edition_fixture_and_its_urls_are_clean(repo_root) -> None:
    payload = json.loads((repo_root / "build" / "edition-2026-10-13-am.json").read_text(encoding="utf-8"))
    checked = assert_allowed(payload, context="fixture")
    assert checked >= 30
    assert sources.is_allowed(next(iter(payload["sources"].values()))["url"])
