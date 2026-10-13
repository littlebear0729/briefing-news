"""Shared helpers for walking the edition JSON.

The edition has six story slots (``lead``, ``secondary[]``, ``tech.stories[]``,
``money.lead``, ``money.side[]``, ``sport.lead``, ``culture[]``). Both the budget
enforcer and the renderers must visit *all* of them and none of them twice, so the
slot list lives here instead of being re-derived in every stage.
"""

from __future__ import annotations

from typing import Any, Iterator, Mapping

__all__ = [
    "INSIDE_SECTIONS",
    "count_words",
    "edition_content",
    "edition_sources",
    "estimate_section_weights",
    "iter_paragraphs",
    "iter_sources",
    "iter_stories",
    "plan_inside_columns",
    "source_names",
    "story_count",
    "word_counts",
]

#: Section slots printed on page two, in canonical reading order.
INSIDE_SECTIONS: tuple[str, ...] = ("technology", "sport", "finance", "culture")


def edition_content(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the content object from either accepted payload form (DESIGN.md §5.3).

    ``{"run_date": …, "sources": …, "content": {...}}`` (the editor's output) and a
    bare content object (a hand-written fixture) both work.
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


def word_counts(content: Mapping[str, Any]) -> dict[str, int]:
    """Body-copy word totals split by printed page, for the manifest and masthead."""
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
# Page-two layout planning
# ---------------------------------------------------------------------------
#
# Page two is two fixed-height columns. Packing whole sections into them is a
# bin-packing problem, and the *order* the template would naturally use (technology,
# sport | finance, culture) is not the order that fits: measured on the worst-case
# edition the natural order ends 1 017px into a 1 018px column while the other column
# ends at 794px, wasting 220px and leaving no margin at all.
#
# Picking the assignment in code — never in CSS multicol, which balances perfectly but
# hides overflow from every measurement — turns "does it fit?" into a single checkable
# quantity, and keeps a real margin: the same worst case packs to 894px / 894px.

#: Rough pixel cost of the furniture each element contributes, at the calibrated print
#: scale. Only used for *relative* comparison between sections, so approximate values
#: are fine; tests/test_layout.py checks the resulting plan against real measurements.
STORY_OVERHEAD = 20.0
DECK_OVERHEAD = 8.0
SECTION_HEAD_OVERHEAD = 15.0
KEY_NUMBER_ROW_OVERHEAD = 25.0
BULLET_OVERHEAD = 12.0


def _story_weight(story: Mapping[str, Any], *, with_extras: bool) -> float:
    weight = sum(count_words(paragraph) for paragraph in story.get("paras") or [])
    weight += STORY_OVERHEAD
    if with_extras:
        if story.get("deck"):
            weight += DECK_OVERHEAD
        if story.get("dateline"):
            weight += DECK_OVERHEAD
    return weight


def estimate_section_weights(content: Mapping[str, Any]) -> dict[str, float]:
    """Estimate how much of a printed column each inside-page section occupies.

    The flags mirror the layout macros exactly (see templates/_macros.j2): decks and
    datelines are printed for a section's lead story only, and the key-number panel is
    printed with at most four values.
    """
    tech = content.get("tech") or {}
    money = content.get("money") or {}
    sport = content.get("sport") or {}

    weights: dict[str, float] = {}

    stories = list(tech.get("stories") or [])
    weights["technology"] = SECTION_HEAD_OVERHEAD + sum(
        _story_weight(story, with_extras=index == 0) for index, story in enumerate(stories)
    )

    weight = SECTION_HEAD_OVERHEAD
    if isinstance(sport.get("lead"), Mapping):
        weight += _story_weight(sport["lead"], with_extras=True)
    weight += BULLET_OVERHEAD * len(sport.get("bullets") or [])
    weights["sport"] = weight

    weight = SECTION_HEAD_OVERHEAD
    if isinstance(money.get("lead"), Mapping):
        weight += _story_weight(money["lead"], with_extras=True)
    for story in money.get("side") or []:
        weight += _story_weight(story, with_extras=False)
    numbers = len(money.get("key_numbers") or [])
    weight += KEY_NUMBER_ROW_OVERHEAD * ((min(numbers, 4) + 1) // 2)
    weights["finance"] = weight

    culture = list(content.get("culture") or [])
    weights["culture"] = SECTION_HEAD_OVERHEAD + sum(
        _story_weight(story, with_extras=False) for story in culture
    )
    return weights


def plan_inside_columns(
    content: Mapping[str, Any], config: Mapping[str, Any] | None = None
) -> dict[str, list[str]]:
    """Assign the inside-page sections to the two printed columns, largest first.

    Returns ``{"left": [...], "right": [...]}`` with section ids in canonical order
    within each column. Greedy largest-first is near-optimal for four items and, more
    importantly, is stable and explainable — the result can be logged and diffed.
    """
    weights = estimate_section_weights(content)
    order = sorted(INSIDE_SECTIONS, key=lambda section: (-weights.get(section, 0.0), section))
    columns: dict[str, list[str]] = {"left": [], "right": []}
    totals = {"left": 0.0, "right": 0.0}
    for section in order:
        target = "left" if totals["left"] <= totals["right"] else "right"
        columns[target].append(section)
        totals[target] += weights.get(section, 0.0)
    for key in ("left", "right"):
        columns[key].sort(key=lambda section: INSIDE_SECTIONS.index(section))
    return columns
