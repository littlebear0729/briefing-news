"""Layout fit: the worst-case edition still prints to exactly two A4 pages.

This is the test that keeps ``config.budgets`` honest. The budgets are calibrated so
that *every* paragraph at its word limit, every list at its maximum length, still fits
the two fixed page boxes with the footer on the page it belongs to. If someone raises
a budget without re-measuring, the worst case no longer fits and this test fails —
either on the arithmetic check that always runs, or on the real browser print that
runs whenever a Chromium is available.
"""

from __future__ import annotations

import copy
import json

import pytest

from pipeline import content as ch
from pipeline import editorial, export, render

FILLER = (
    "Officials said the measure would be reviewed after sixty days and that the technical "
    "annexes covering verification inspections would be published once both delegations had "
    "signed the protocol in Geneva on Thursday morning according to two people briefed on the "
    "talks who asked not to be identified because the discussions were private"
).split()

#: Measured capacity of the two inside columns and the front page, in body words, at the
#: calibrated print scale. The worst case must stay below these; the numbers come from
#: tests/test_print.py's measurements, not from a guess.
FRONT_CAPACITY_WORDS = 700
INSIDE_CAPACITY_WORDS = 900


def sentence(count: int, offset: int = 0) -> str:
    return " ".join(FILLER[(index + offset) % len(FILLER)] for index in range(count)).capitalize() + "."


def worst_case(fixture_payload: dict, config: dict) -> dict:
    """Inflate the fixture to the largest edition the budgets allow."""
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


def test_worst_case_stays_within_the_measured_page_capacity(worst: dict) -> None:
    counts = ch.word_counts(ch.edition_content(worst))
    assert counts["front"] <= FRONT_CAPACITY_WORDS, f"front page worst case: {counts}"
    assert counts["inside"] <= INSIDE_CAPACITY_WORDS, f"inside page worst case: {counts}"


def test_plan_covers_every_section_exactly_once(worst: dict, config: dict) -> None:
    plan = ch.plan_inside_columns(ch.edition_content(worst), config)
    assert sorted(plan["left"] + plan["right"]) == sorted(ch.INSIDE_SECTIONS)


def test_plan_stays_balanced_for_the_worst_case(worst: dict, config: dict) -> None:
    content = ch.edition_content(worst)
    weights = ch.estimate_section_weights(content)
    plan = ch.plan_inside_columns(content, config)
    left = sum(weights[section] for section in plan["left"])
    right = sum(weights[section] for section in plan["right"])
    assert abs(left - right) / max(left, right) < 0.2, f"{left:.0f} vs {right:.0f}"


def test_plan_is_deterministic(worst: dict, config: dict) -> None:
    content = ch.edition_content(worst)
    assert ch.plan_inside_columns(content, config) == ch.plan_inside_columns(content, config)


def test_worst_case_renders_both_artefacts(worst: dict, config: dict, tmp_path) -> None:
    path = tmp_path / "worst.json"
    path.write_text(json.dumps(worst), encoding="utf-8")
    _html, desktop = render.run(path, "2026-10-13", "AM", config=config, out=tmp_path / "worst.html")
    assert desktop.is_file()


@pytest.mark.print_check
def test_worst_case_prints_to_exactly_two_pages(worst: dict, config: dict, tmp_path) -> None:
    """Print the worst case in a real browser and assert two pages with all content.

    Skipped when no Chromium is reachable (``python -m playwright install chromium``,
    or a local Chrome/Edge): the cheap capacity assertions above still run.
    """
    path = tmp_path / "worst.json"
    path.write_text(json.dumps(worst), encoding="utf-8")
    _html, html_path = render.run(path, "2026-10-13", "AM", config=config, out=tmp_path / "worst.html")
    try:
        pdf = export.to_pdf(html_path)
    except export.ExportError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"no browser available for the print check: {exc}")

    report = export.assert_pdf(pdf, config=config, required_snippets=export.content_snippets(worst))
    assert report["pages"] == 2
    assert report["content_complete"] is True
    assert report["bytes"] < config["layout"]["max_pdf_bytes"]
