"""Candidate collection: window, allowlist, de-duplication, ordering, caps, ids."""

from __future__ import annotations

import datetime as dt

import pytest

from pipeline import fetch
from pipeline.config import window


def rss(items: list[tuple[str, str, str]]) -> str:
    """Build an RSS document from ``(title, link, pubDate)`` triples."""
    entries = "".join(
        f"<item><title>{title}</title><link>{link}</link>"
        f"<description>Summary of {title}.</description><pubDate>{date}</pubDate></item>"
        for title, link, date in items
    )
    return f"<?xml version='1.0'?><rss version='2.0'><channel><title>F</title>{entries}</channel></rss>"


def feed_url(config: dict, section: str, index: int = 0) -> str:
    """The URL of a configured feed, so tests never hardcode a rot-able endpoint."""
    return config["feeds"][section][index]["url"]


def documents_for(config: dict, bodies: dict[str, str]) -> dict[str, str]:
    """Map every configured feed to a body, defaulting to one in-window entry."""
    bodies = dict(bodies)
    for feeds in config["feeds"].values():
        for feed in feeds:
            bodies.setdefault(
                feed["url"],
                rss([("Filler story", "https://www.reuters.com/filler", "Tue, 13 Oct 2026 00:30:00 GMT")]),
            )
    return bodies


def test_parse_feed_keeps_only_in_window_whitelisted_dated_entries(config: dict) -> None:
    start, end = window(config, "2026-10-13", "AM")
    document = rss(
        [
            ("Inside window", "https://www.reuters.com/inside", "Tue, 13 Oct 2026 00:30:00 GMT"),
            ("Too old", "https://www.reuters.com/old", "Mon, 12 Oct 2026 09:00:00 GMT"),
            ("Too new", "https://www.reuters.com/new", "Tue, 13 Oct 2026 03:00:00 GMT"),
            ("Not whitelisted", "https://evil.example/x", "Tue, 13 Oct 2026 00:45:00 GMT"),
        ]
    )
    feed = {"name": "Reuters World", "source": "reuters.com", "url": "https://www.reuters.com/feed"}
    candidates, seen = fetch.parse_feed(document, feed, "geopolitics", start, end)

    assert seen == 4  # every entry is inspected; only in-window whitelisted ones survive
    assert [item["title"] for item in candidates] == ["Inside window"]
    candidate = candidates[0]
    assert candidate["source"] == "reuters.com"
    assert candidate["source_name"] == "Reuters World"
    assert candidate["published"].startswith("2026-10-13T00:30")
    assert candidate["summary"] == "Summary of Inside window."


def test_parse_feed_handles_atom_and_missing_summaries(config: dict) -> None:
    start, end = window(config, "2026-10-13", "AM")
    atom = """<?xml version='1.0'?>
    <feed xmlns='http://www.w3.org/2005/Atom'>
      <entry><title>Atom story</title><link rel='alternate' href='https://www.bbc.co.uk/news/x'/>
      <updated>2026-10-13T00:10:00Z</updated><summary>Atom summary</summary></entry>
    </feed>"""
    feed = {"name": "BBC", "source": "bbc.co.uk", "url": "https://feeds.bbci.co.uk/news/world/rss.xml"}
    candidates, _seen = fetch.parse_feed(atom, feed, "geopolitics", start, end)
    assert [item["title"] for item in candidates] == ["Atom story"]
    assert candidates[0]["summary"] == "Atom summary"


def test_clean_text_strips_markup_and_caps_length() -> None:
    assert fetch.clean_text("<p>Hello&nbsp;<b>world</b></p>") == "Hello world"
    assert fetch.clean_text(None) == ""
    capped = fetch.clean_text("word " * 100, 20)
    assert len(capped) == 20 and capped.endswith("…")


def test_url_key_normalises_tracking_and_www() -> None:
    assert fetch.url_key("https://WWW.Reuters.com/world/x/?utm_source=rss#top") == fetch.url_key(
        "https://reuters.com/world/x"
    )


def test_collect_dedupes_orders_caps_and_assigns_ids(config: dict) -> None:
    """Same story from two feeds is kept once; ids follow the documented prefix scheme."""
    duplicate = ("Shared headline", "https://www.reuters.com/shared", "Tue, 13 Oct 2026 00:20:00 GMT")
    bodies = documents_for(
        config,
        {
            feed_url(config, "geopolitics", 0): rss(
                [
                    duplicate,
                    ("Later story", "https://www.reuters.com/later", "Tue, 13 Oct 2026 01:00:00 GMT"),
                ]
            ),
            feed_url(config, "geopolitics", 1): rss(
                [
                    duplicate,
                    ("Earlier story", "https://apnews.com/earlier", "Mon, 12 Oct 2026 18:00:00 GMT"),
                ]
            ),
        },
    )
    payload = fetch.collect(config, "am", "2026-10-13", documents=bodies)

    geopolitics = payload["sections"]["geopolitics"]
    titles = [item["title"] for item in geopolitics]
    assert titles.count("Shared headline") == 1
    # newest first, and the filler entry each other feed contributes is de-duplicated
    assert titles == ["Later story", "Filler story", "Shared headline", "Earlier story"]
    assert [item["id"] for item in geopolitics] == [f"GEO-{index:02d}" for index in range(1, len(geopolitics) + 1)]
    assert payload["counts"] == {section: len(items) for section, items in payload["sections"].items()}
    assert payload["window"]["end"].startswith("2026-10-13T01:15")


def test_collect_caps_each_section(config: dict) -> None:
    cap = int(config["budgets"]["section_cap"])
    items = [
        (f"Story {index:02d}", f"https://www.reuters.com/s{index}", "Tue, 13 Oct 2026 00:30:00 GMT")
        for index in range(cap + 5)
    ]
    bodies = documents_for(config, {feed_url(config, "geopolitics", 0): rss(items)})
    payload = fetch.collect(config, "am", "2026-10-13", documents=bodies)
    assert payload["counts"]["geopolitics"] == cap


def test_collect_warns_when_a_section_is_empty(config: dict, capsys) -> None:
    bodies = documents_for(config, {})
    for feed in config["feeds"]["sport"]:
        bodies[feed["url"]] = rss([])
    fetch.collect(config, "am", "2026-10-13", documents=bodies)
    captured = capsys.readouterr()
    assert "sport" in captured.err and "0 candidates" in captured.err


def test_collect_reports_unreadable_feeds_without_failing(config: dict) -> None:
    bodies = documents_for(config, {})
    bodies["https://www.aljazeera.com/xml/rss/all.xml"] = "<not-xml"
    payload = fetch.collect(config, "am", "2026-10-13", documents=bodies)
    statuses = {record["url"]: record["status"] for record in payload["feeds"]}
    assert statuses["https://www.aljazeera.com/xml/rss/all.xml"] == "ok"
    assert any(record.get("entries") == 0 for record in payload["feeds"])


def test_run_writes_the_documented_archive_path(config: dict, sandbox_config: dict, tmp_path) -> None:
    bodies = documents_for(config, {})
    archive = fetch.run("AM", "2026-10-13", config=sandbox_config, documents=bodies)
    assert archive.name == "articles-2026-10-13-am.json"
    assert archive.parent == tmp_path / "build_dir"
    assert '"edition": "AM"' in archive.read_text(encoding="utf-8")


def test_fetch_rejects_a_non_whitelisted_feed_url() -> None:
    with pytest.raises(fetch.FetchError):
        fetch.fetch_feed("https://evil.example/feed.xml")


def test_entry_dates_without_timezone_are_treated_as_utc() -> None:
    parsed = fetch._entry_published({"published_parsed": dt.datetime(2026, 10, 13, 1, 0).timetuple()})
    assert parsed == dt.datetime(2026, 10, 13, 1, 0, tzinfo=dt.timezone.utc)
    assert fetch._entry_published({}) is None
