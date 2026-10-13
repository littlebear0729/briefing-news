"""Length: the edition is about two sheets of newspaper, and it really is.

The brief asks for roughly two A4 pages of content without a fixed page size, so length
is a *target*: `pipeline.content.estimate_length` turns an edition into sheet-equivalents,
`config.layout.target_sheets` names the target, and the preflight refuses to deliver an
edition that is half or double it.

The estimator is calibrated against reality: `MM_PER_SHEET` was set from the measured
print length of the committed fixture (two A4 pages), and the last test here re-measures
the worst case in a real browser whenever one is available.
"""

from __future__ import annotations

import copy
import json

import pytest

from pipeline import content as ch
from pipeline import editorial, preflight, render
from tests.printprobe import NoBrowser, printed_pages

FILLER = (
    "Officials said the measure would be reviewed after sixty days and that the technical "
    "annexes covering verification inspections would be published once both delegations had "
    "signed the protocol in Geneva on Thursday morning according to two people briefed on the "
    "talks who asked not to be identified because the discussions were private"
).split()


def sentence(count: int, offset: int = 0) -> str:
    return " ".join(FILLER[(index + offset) % len(FILLER)] for index in range(count)).capitalize() + "."


def worst_case(fixture_payload: dict, config: dict) -> dict:
    """The largest edition the budgets allow: every paragraph and list at its maximum."""
    payload = copy.deepcopy(fixture_payload)
    content = payload["content"]
    budgets = config["budgets"]
    counter = {"n": 0}

    def story(node: dict, paras: int, words: int) -> None:
        counter["n"] += 1
        node["paras"] = [sentence(words, counter["n"] * 3) for _ in range(paras)]
        node["headline"] = " ".join(FILLER[: budgets["headline_words"]]) + "."
        node["deck"] = " ".join(FILLER[: budgets["deck_words"]]) + "."

    story(content["lead"], budgets["lead_paras"], budgets["lead_words"])
    for node in content["secondary"]:
        story(node, budgets["story_paras"], budgets["story_words"])
    content["briefs"] = [
        {"label": "Brief", "text": sentence(budgets["brief_words"], index * 5)}
        for index in range(budgets["brief_count_max"])
    ]
    content["markets"]["items"] = [
        {"label": " ".join(FILLER[:3]), "dir": "flat", "note": sentence(12, index)} for index in range(6)
    ]
    for node in content["tech"]["stories"]:
        story(node, budgets["story_paras"], budgets["story_words"])
    money = content["money"]
    story(money["lead"], budgets["story_paras"], budgets["story_words"])
    for node in money["side"]:
        story(node, budgets["story_paras"], budgets["story_words"])
    money["key_numbers"] = [{"value": "123.45", "label": " ".join(FILLER[:8])} for _ in range(5)]
    sport = content["sport"]
    story(sport["lead"], budgets["story_paras"], budgets["story_words"])
    sport["bullets"] = [sentence(budgets["bullet_words"], index * 7) for index in range(4)]
    for node in content["culture"]:
        story(node, budgets["story_paras"], budgets["story_words"])

    editorial._enforce_budgets(content, config)  # exactly what the pipeline does
    return payload


@pytest.fixture(scope="module")
def worst(fixture_payload: dict, config: dict) -> dict:
    return worst_case(fixture_payload, config)


def test_worst_case_respects_every_budget(worst: dict, config: dict) -> None:
    budgets = config["budgets"]
    content = ch.edition_content(worst)
    for path, story in ch.iter_stories(content):
        is_lead = path == "lead"
        assert len(story["paras"]) <= (budgets["lead_paras"] if is_lead else budgets["story_paras"])
        limit = budgets["lead_words"] if is_lead else budgets["story_words"]
        for paragraph in story["paras"]:
            assert ch.count_words(paragraph) <= limit, path
        assert ch.count_words(story["headline"]) <= budgets["headline_words"]
    assert len(content["briefs"]) <= budgets["brief_count_max"]


def test_fixture_is_about_two_sheets(fixture_payload: dict, config: dict) -> None:
    length = ch.estimate_length(ch.edition_content(fixture_payload))
    target = config["layout"]["target_sheets"]
    tolerance = config["layout"]["length_tolerance"]
    assert target - tolerance <= length["total"] <= target + tolerance, length


def test_worst_case_is_still_about_two_sheets(worst: dict, config: dict) -> None:
    length = ch.estimate_length(ch.edition_content(worst))
    target = config["layout"]["target_sheets"]
    tolerance = config["layout"]["length_tolerance"]
    assert length["total"] <= target + tolerance, f"the worst case would be a {length['total']}-sheet paper"
    assert length["front"] >= 0.6 and length["inside"] >= 0.6, "both sheets must carry real content"


def test_estimate_moves_with_the_content(fixture_payload: dict) -> None:
    content = ch.edition_content(fixture_payload)
    short = ch.estimate_length(content)["total"]

    longer = copy.deepcopy(content)
    for _path, story in ch.iter_stories(longer):
        story["paras"] = story["paras"] + [" ".join(FILLER[:30])]
    assert ch.estimate_length(longer)["total"] > short

    empty = copy.deepcopy(content)
    for _path, story in ch.iter_stories(empty):
        story["paras"] = ["Two words."]
    assert ch.estimate_length(empty)["total"] < short


def test_preflight_rejects_an_edition_that_is_too_long(config: dict, fixture_payload: dict) -> None:
    """A model that writes twice as much must be caught, not delivered."""
    generous = copy.deepcopy(config)
    generous["budgets"] = {
        **config["budgets"],
        "lead_paras": 12,
        "lead_words": 70,
        "story_paras": 6,
        "story_words": 60,
        "brief_count_max": 8,
        "bullet_words": 40,
    }
    payload = copy.deepcopy(fixture_payload)
    for _path, story in ch.iter_stories(payload["content"]):
        story["paras"] = story["paras"] * 5
    rendered = render.render_html(payload, generous)
    with pytest.raises(preflight.PreflightError) as excinfo:
        preflight.assert_deliverable(rendered, payload=payload, config=config)
    assert "sheet-equivalents" in str(excinfo.value)


def test_worst_case_prints_to_about_two_a4_pages(worst: dict, config: dict, tmp_path) -> None:
    """Measured, not estimated: print the worst case and count the sheets."""
    path = tmp_path / "worst.json"
    path.write_text(json.dumps(worst), encoding="utf-8")
    _html, html_path = render.run(path, "2026-10-13", "AM", config=config, out=tmp_path / "worst.html")
    try:
        pages = printed_pages(html_path)
    except NoBrowser as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"no browser available for the print measurement: {exc}")

    target = config["layout"]["target_sheets"]
    assert target - 1 <= pages <= target + 1.5, (
        f"the worst-case edition printed to {pages} A4 pages; the brief asks for about {target}"
    )
