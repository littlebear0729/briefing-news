"""Shared helpers for walking the edition JSON and reasoning about its length.

The edition has six story slots (``lead``, ``secondary[]``, ``tech.stories[]``,
``money.lead``, ``money.side[]``, ``sport.lead``, ``culture[]``). Everything that must
visit *all* of them and none of them twice — budget enforcement, rendering, length
estimation, the pre-send checks — shares the slot list here.

There is no pagination any more: the deliverable is one responsive HTML document, so
"two pages" is a *length* target rather than a layout constraint (see
:func:`estimate_length`).
"""

from __future__ import annotations

import re
from typing import Any, Iterator, Mapping

__all__ = [
    "INSIDE_SECTIONS",
    "count_words",
    "closing_snippets",
    "edition_content",
    "edition_sources",
    "estimate_length",
    "iter_paragraphs",
    "iter_sources",
    "iter_stories",
    "normalise_text",
    "source_names",
    "story_count",
    "word_counts",
]

#: Sections printed on the second sheet, in canonical reading order.
INSIDE_SECTIONS: tuple[str, ...] = ("technology", "sport", "finance", "culture")


def edition_content(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the content object from either accepted payload form (DESIGN.md §5.3).

    ``{"run_date": …, "sources": …, "content": {...}}`` (the editor's output) and a bare
    content object (a hand-written fixture) both work.
    """
    if not isinstance(payload, Mapping):
        raise TypeError(f"expected a mapping, got {type(payload).__name__}")
    content = payload.get("content")
    if isinstance(content, Mapping):
        return content
    return payload


def edition_sources(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the citation index (``id -> {source, url}``), or an empty mapping."""
    sources = payload.get("sources")
    return sources if isinstance(sources, Mapping) else {}


def iter_stories(content: Mapping[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ``(slot_path, story)`` for every story in the edition, in reading order."""
    lead = content.get("lead")
    if isinstance(lead, dict):
        yield "lead", lead

    for index, story in enumerate(content.get("secondary") or []):
        if isinstance(story, dict):
            yield f"secondary[{index}]", story

    tech = content.get("tech") or {}
    if isinstance(tech, Mapping):
        for index, story in enumerate(tech.get("stories") or []):
            if isinstance(story, dict):
                yield f"tech.stories[{index}]", story

    money = content.get("money") or {}
    if isinstance(money, Mapping):
        if isinstance(money.get("lead"), dict):
            yield "money.lead", money["lead"]
        for index, story in enumerate(money.get("side") or []):
            if isinstance(story, dict):
                yield f"money.side[{index}]", story

    sport = content.get("sport") or {}
    if isinstance(sport, Mapping) and isinstance(sport.get("lead"), dict):
        yield "sport.lead", sport["lead"]

    for index, story in enumerate(content.get("culture") or []):
        if isinstance(story, dict):
            yield f"culture[{index}]", story


def iter_paragraphs(content: Mapping[str, Any]) -> Iterator[tuple[str, str]]:
    """Yield ``(slot_path, paragraph)`` for every body paragraph."""
    for path, story in iter_stories(content):
        for index, paragraph in enumerate(story.get("paras") or []):
            if isinstance(paragraph, str):
                yield f"{path}.paras[{index}]", paragraph


def iter_sources(payload: Mapping[str, Any]) -> Iterator[dict[str, str]]:
    """Yield every cited source (deduplicated by URL, in first-seen order)."""
    seen: set[str] = set()
    for _path, story in iter_stories(edition_content(payload)):
        for source in story.get("sources") or []:
            if not isinstance(source, Mapping):
                continue
            url = str(source.get("url") or "")
            if not url or url in seen:
                continue
            seen.add(url)
            yield {
                "id": str(source.get("id") or ""),
                "name": str(source.get("name") or ""),
                "url": url,
            }


def source_names(payload: Mapping[str, Any]) -> list[str]:
    """Sorted, de-duplicated names of every outlet cited in the edition."""
    return sorted({source["name"] for source in iter_sources(payload) if source["name"]})


def count_words(text: Any) -> int:
    """Whitespace-delimited word count; the unit every budget in config.yaml uses."""
    return len(str(text or "").split())


def story_count(content: Mapping[str, Any]) -> int:
    return sum(1 for _ in iter_stories(content))


def normalise_text(text: str) -> str:
    """Comparison key for "did this reach the page?": letters and digits only.

    Punctuation, whitespace and the soft hyphens a browser may insert when it breaks a
    justified line all disappear, so a match means the words really are there.
    """
    return re.sub(r"[^0-9a-z]+", "", (text or "").lower())


def closing_snippets(content: Mapping[str, Any], *, words: int = 4) -> list[str]:
    """Closing words of every section, for the "did anything get lost?" check.

    The rendered document is the deliverable, so these are checked against the HTML
    itself; a copy-paste accident or a template that drops a slot shows up immediately.
    """
    snippets: list[str] = []

    def tail(text: Any) -> str:
        parts = str(text or "").split()
        return " ".join(parts[-words:]) if parts else ""

    tech = (content.get("tech") or {}).get("stories") or []
    money = content.get("money") or {}
    sport = content.get("sport") or {}
    culture = content.get("culture") or []
    secondary = content.get("secondary") or []

    for story in (
        content.get("lead") or {},
        secondary[-1] if secondary else {},
        tech[-1] if tech else {},
        money.get("lead") or {},
        (money.get("side") or [{}])[-1],
        sport.get("lead") or {},
        culture[-1] if culture else {},
    ):
        paragraphs = [p for p in (story.get("paras") or []) if str(p).strip()]
        if paragraphs:
            snippets.append(tail(paragraphs[-1]))

    if content.get("briefs"):
        snippets.append(tail((content["briefs"][-1] or {}).get("text")))
    markets = content.get("markets") or {}
    if markets.get("note"):
        snippets.append(tail(markets["note"]))
    if sport.get("bullets"):
        snippets.append(tail(sport["bullets"][-1]))
    numbers = money.get("key_numbers") or []
    if numbers:
        snippets.append(tail((numbers[-1] or {}).get("label")))

    seen: set[str] = set()
    unique: list[str] = []
    for snippet in snippets:
        key = normalise_text(snippet)
        if len(key) >= 8 and key not in seen:
            seen.add(key)
            unique.append(snippet)
    return unique


def word_counts(content: Mapping[str, Any]) -> dict[str, int]:
    """Body-copy word totals split by sheet, for the masthead, the manifest and tests."""
    front = 0
    inside = 0

    def story_words(story: Mapping[str, Any]) -> int:
        total = count_words(story.get("headline")) + count_words(story.get("deck"))
        total += sum(count_words(paragraph) for paragraph in story.get("paras") or [])
        return total

    lead = content.get("lead")
    if isinstance(lead, Mapping):
        front += story_words(lead)
    for story in content.get("secondary") or []:
        if isinstance(story, Mapping):
            front += story_words(story)

    for brief in content.get("briefs") or []:
        if isinstance(brief, Mapping):
            front += count_words(brief.get("text")) + count_words(brief.get("label"))

    markets = content.get("markets") or {}
    if isinstance(markets, Mapping):
        front += count_words(markets.get("note"))
        for item in markets.get("items") or []:
            if isinstance(item, Mapping):
                front += count_words(item.get("note")) + count_words(item.get("label"))

    tech = content.get("tech") or {}
    if isinstance(tech, Mapping):
        for story in tech.get("stories") or []:
            if isinstance(story, Mapping):
                inside += story_words(story)

    money = content.get("money") or {}
    if isinstance(money, Mapping):
        if isinstance(money.get("lead"), Mapping):
            inside += story_words(money["lead"])
        for story in money.get("side") or []:
            if isinstance(story, Mapping):
                inside += story_words(story)
        for number in money.get("key_numbers") or []:
            if isinstance(number, Mapping):
                inside += count_words(number.get("label")) + count_words(number.get("value"))

    sport = content.get("sport") or {}
    if isinstance(sport, Mapping):
        if isinstance(sport.get("lead"), Mapping):
            inside += story_words(sport["lead"])
        inside += sum(count_words(bullet) for bullet in sport.get("bullets") or [])

    for story in content.get("culture") or []:
        if isinstance(story, Mapping):
            inside += story_words(story)

    return {"front": front, "inside": inside, "total": front + inside}


# ---------------------------------------------------------------------------
# Length estimation
# ---------------------------------------------------------------------------
#
# The brief asks for "about two A4 pages worth of newspaper" without a fixed page size,
# so length is a budget, not a constraint: the estimator below turns an edition into
# sheet-equivalents, the target lives in config.yaml, and tests/test_layout.py holds the
# worst case inside that band.
#
# The constants are deliberately simple and were checked against real rendering: a
# column at the desktop scale is roughly 300mm tall and holds roughly 9 words per line
# at 3.6mm per line.

#: Millimetres of column height one word of body copy occupies.
MM_PER_WORD = 0.40
#: Fixed furniture, in millimetres of column height.
MM_PER_STORY = 16.0  # kicker + headline + attribution
MM_PER_DECK = 4.0
MM_PER_SECTION_HEAD = 9.0
MM_PER_BRIEF = 9.0
MM_PER_MARKET_ROW = 7.0
MM_PER_KEY_NUMBER_ROW = 13.0
MM_PER_BULLET = 5.5
MM_MASTHEAD = 38.0
#: How much of the above one printed A4 sheet turns out to hold, measured rather than
#: derived: the committed fixture (1 011 words, 13 stories, 4 briefs) prints to exactly
#: two A4 pages at newspaper density. Re-calibration recipe: render an edition, print it
#: through a browser, count pages, then set this to (this edition's millimetres / pages).
MM_PER_SHEET = 376.0


def _story_length(story: Mapping[str, Any], *, extras: bool) -> float:
    length = MM_PER_STORY
    length += sum(count_words(paragraph) for paragraph in story.get("paras") or []) * MM_PER_WORD
    if extras:
        if story.get("deck"):
            length += MM_PER_DECK
        if story.get("dateline"):
            length += MM_PER_DECK / 2
    return length


def estimate_length(content: Mapping[str, Any]) -> dict[str, float]:
    """Estimate the rendered length of each sheet, in A4-sheet equivalents.

    Returns ``{"front", "inside", "total"}``. A total near ``layout.target_sheets``
    means the edition is about as long as the brief asks for; the individual numbers are
    only meaningful relative to each other.
    """
    front = MM_MASTHEAD
    lead = content.get("lead")
    if isinstance(lead, Mapping):
        front += _story_length(lead, extras=True)
    for story in content.get("secondary") or []:
        if isinstance(story, Mapping):
            front += _story_length(story, extras=True)
    for brief in content.get("briefs") or []:
        if isinstance(brief, Mapping):
            front += MM_PER_BRIEF + count_words(brief.get("text")) * MM_PER_WORD
    markets = content.get("markets") or {}
    if isinstance(markets, Mapping):
        front += MM_PER_SECTION_HEAD
        front += len(markets.get("items") or []) * MM_PER_MARKET_ROW
        front += count_words(markets.get("note")) * MM_PER_WORD

    inside = MM_PER_SECTION_HEAD
    tech = content.get("tech") or {}
    for index, story in enumerate(tech.get("stories") or []):
        if isinstance(story, Mapping):
            inside += _story_length(story, extras=index == 0)
    inside += MM_PER_SECTION_HEAD
    money = content.get("money") or {}
    if isinstance(money.get("lead"), Mapping):
        inside += _story_length(money["lead"], extras=True)
    for story in money.get("side") or []:
        if isinstance(story, Mapping):
            inside += _story_length(story, extras=False)
    inside += MM_PER_KEY_NUMBER_ROW * ((min(len(money.get("key_numbers") or []), 4) + 1) // 2)
    inside += MM_PER_SECTION_HEAD
    sport = content.get("sport") or {}
    if isinstance(sport.get("lead"), Mapping):
        inside += _story_length(sport["lead"], extras=True)
    inside += len(sport.get("bullets") or []) * MM_PER_BULLET
    inside += MM_PER_SECTION_HEAD
    for story in content.get("culture") or []:
        if isinstance(story, Mapping):
            inside += _story_length(story, extras=False)

    return {
        "front": round(front / MM_PER_SHEET, 2),
        "inside": round(inside / MM_PER_SHEET, 2),
        "total": round((front + inside) / MM_PER_SHEET, 2),
    }
