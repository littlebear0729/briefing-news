"""The single LLM call: schema, citation resolution, budgets, offline replay."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pipeline import content as ch
from pipeline import editorial, sources


def make_archive(payload: dict) -> dict:
    """Turn the fixture's index into an ``articles-*.json`` candidate pool."""
    sections: dict[str, list[dict]] = {}
    for source_id, entry in payload["sources"].items():
        section = {"GEO": "geopolitics", "TEC": "tech", "FIN": "finance", "SPO": "sport", "ENT": "entertainment"}[
            source_id.split("-")[0]
        ]
        sections.setdefault(section, []).append(
            {
                "id": source_id,
                "section": section,
                "title": f"Candidate {source_id}",
                "url": entry["url"],
                "source": sources.domain_of(entry["url"]),
                "source_name": entry["source"],
                "published": "2026-10-13T00:30:00+00:00",
                "summary": f"Summary text for {source_id}.",
            }
        )
    return {
        "edition": "AM",
        "run_date": "2026-10-13",
        "window": {"start": "2026-10-12T13:15:00+00:00", "end": "2026-10-13T01:15:00+00:00"},
        "sections": sections,
    }


def llm_response(payload: dict) -> dict:
    """The model's form of the edition: source_ids, and never any URLs."""
    response = copy.deepcopy(payload["content"])
    for _path, story in ch.iter_stories(response):
        story.pop("sources", None)
    return response


def test_fixture_satisfies_the_schema(fixture_payload: dict) -> None:
    editorial.validate_edition(ch.edition_content(fixture_payload))


def test_schema_rejects_a_story_without_citations(fixture_payload: dict) -> None:
    content = copy.deepcopy(ch.edition_content(fixture_payload))
    content["lead"].pop("source_ids")
    content["lead"].pop("sources")
    with pytest.raises(editorial.EditorialError) as excinfo:
        editorial.validate_edition(content)
    assert "source_ids" in str(excinfo.value)


def test_schema_rejects_extra_properties(fixture_payload: dict) -> None:
    content = copy.deepcopy(ch.edition_content(fixture_payload))
    content["lead"]["html"] = "<b>no</b>"
    with pytest.raises(editorial.EditorialError):
        editorial.validate_edition(content)


def test_iter_stories_covers_every_slot(fixture_payload: dict) -> None:
    paths = [path for path, _story in ch.iter_stories(ch.edition_content(fixture_payload))]
    assert paths == [
        "lead",
        "secondary[0]",
        "secondary[1]",
        "tech.stories[0]",
        "tech.stories[1]",
        "tech.stories[2]",
        "money.lead",
        "money.side[0]",
        "money.side[1]",
        "sport.lead",
        "culture[0]",
        "culture[1]",
        "culture[2]",
    ]


def test_resolve_sources_replaces_ids_with_names_and_urls(fixture_payload: dict) -> None:
    content = copy.deepcopy(ch.edition_content(fixture_payload))
    for _path, story in ch.iter_stories(content):
        story.pop("sources", None)
    index = {source_id: entry for source_id, entry in fixture_payload["sources"].items()}

    cited = editorial._resolve_sources(content, index)

    assert cited == len({source_id for _path, story in ch.iter_stories(content) for source_id in story["source_ids"]})
    lead_sources = content["lead"]["sources"]
    assert [item["id"] for item in lead_sources] == content["lead"]["source_ids"]
    assert all(item["url"].startswith("https://") for item in lead_sources)


def test_resolve_sources_rejects_a_hallucinated_id(fixture_payload: dict) -> None:
    content = copy.deepcopy(ch.edition_content(fixture_payload))
    content["lead"]["source_ids"] = ["GEO-01", "GEO-99"]
    with pytest.raises(editorial.EditorialError) as excinfo:
        editorial._resolve_sources(content, fixture_payload["sources"])
    assert "GEO-99" in str(excinfo.value)


def test_enforce_budgets_trims_paragraphs_words_and_briefs(config: dict, fixture_payload: dict) -> None:
    content = copy.deepcopy(ch.edition_content(fixture_payload))
    budgets = config["budgets"]
    content["lead"]["paras"] = [" ".join(["word"] * 90) for _ in range(6)]
    content["briefs"] = [{"label": "Long label here", "text": " ".join(["w"] * 60)} for _ in range(6)]
    content["sport"]["bullets"] = [" ".join(["b"] * 40) for _ in range(6)]
    content["lead"]["headline"] = " ".join(["h"] * 20)
    content["tech"]["stories"][0]["paras"] = [" ".join(["p"] * 80) for _ in range(5)]

    report = editorial._enforce_budgets(content, config)

    assert len(content["lead"]["paras"]) == budgets["lead_paras"]
    assert all(ch.count_words(para) <= budgets["lead_words"] for para in content["lead"]["paras"])
    assert len(content["tech"]["stories"][0]["paras"]) == budgets["story_paras"]
    assert all(ch.count_words(para) <= budgets["story_words"] for para in content["tech"]["stories"][0]["paras"])
    assert len(content["briefs"]) == budgets["brief_count_max"]
    assert all(ch.count_words(brief["text"]) <= budgets["brief_words"] for brief in content["briefs"])
    assert len(content["sport"]["bullets"]) == 4
    assert ch.count_words(content["lead"]["headline"]) <= budgets["headline_words"]
    assert report["paragraphs_trimmed"] >= 2 and report["paragraphs_shortened"] > 0


def test_enforce_budgets_is_idempotent(config: dict, fixture_payload: dict) -> None:
    content = copy.deepcopy(ch.edition_content(fixture_payload))
    first = editorial._enforce_budgets(content, config)
    snapshot = json.dumps(content, sort_keys=True)
    second = editorial._enforce_budgets(content, config)
    assert json.dumps(content, sort_keys=True) == snapshot
    assert not any(value for key, value in second.items() if key != "ok" and isinstance(value, int))


def test_build_user_prompt_never_leaks_urls(config: dict, fixture_payload: dict) -> None:
    prompt = editorial.build_user_prompt(make_archive(fixture_payload), config)
    assert "http://" not in prompt and "https://" not in prompt
    assert "GEO-01" in prompt and "ENT-06" in prompt
    assert "geopolitics" in prompt


def test_build_user_prompt_flags_empty_sections(config: dict, fixture_payload: dict) -> None:
    archive = make_archive(fixture_payload)
    archive["sections"]["sport"] = []
    prompt = editorial.build_user_prompt(archive, config)
    assert "no candidates in this window: sport" in prompt


def test_extract_json_tolerates_code_fences() -> None:
    assert editorial._extract_json('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert editorial._extract_json('prefix {"a": 1} suffix') == '{"a": 1}'
    with pytest.raises(editorial.EditorialError):
        editorial._extract_json("no json here")


def test_redact_hides_the_key() -> None:
    assert "sk-secret-value" not in editorial._redact("Bearer sk-secret-value", "sk-secret-value")
    assert editorial._redact("nothing to hide", "short") == "nothing to hide"


def test_run_replays_a_recorded_response_offline(
    config: dict, sandbox_config: dict, fixture_payload: dict
) -> None:
    """Editorial end to end without a network: allowlist -> schema -> resolve -> budgets."""
    archive = make_archive(fixture_payload)
    archive_path = Path(sandbox_config["paths"]["build_dir"]) / "articles-2026-10-13-am.json"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_text(json.dumps(archive), encoding="utf-8")

    edition_path = editorial.run(
        archive_path,
        "AM",
        config=sandbox_config,
        llm_response=json.dumps(llm_response(fixture_payload)),
    )

    written = json.loads(edition_path.read_text(encoding="utf-8"))
    assert edition_path.name == "edition-2026-10-13-am.json"
    assert written["edition"] == "AM" and written["run_date"] == "2026-10-13"
    assert written["model"]["attempts"] == 1
    assert "sources cited" in written["note"]
    assert len(written["sources"]) == len(fixture_payload["sources"])
    assert written["allowlist_checked"]["candidates"] > 0
    story = written["content"]["lead"]
    assert story["sources"][0]["url"].startswith("https://")
    # budgets are enforced on the way out
    assert all(ch.count_words(para) <= config["budgets"]["lead_words"] for para in story["paras"])


def test_run_rejects_an_archive_with_a_non_whitelisted_url(
    sandbox_config: dict, fixture_payload: dict
) -> None:
    archive = make_archive(fixture_payload)
    archive["sections"]["geopolitics"][0]["url"] = "https://evil.example/story"
    archive_path = Path(sandbox_config["paths"]["build_dir"]) / "articles-2026-10-13-am.json"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_text(json.dumps(archive), encoding="utf-8")

    with pytest.raises(sources.AllowlistError):
        editorial.run(archive_path, "AM", config=sandbox_config, llm_response=json.dumps(llm_response(fixture_payload)))


def test_run_needs_an_api_key_without_an_offline_response(sandbox_config: dict, fixture_payload: dict, monkeypatch) -> None:
    monkeypatch.delenv("BRIEF_LLM_API_KEY", raising=False)
    monkeypatch.delenv("BRIEF_LLM_RESPONSE", raising=False)
    archive_path = Path(sandbox_config["paths"]["build_dir"]) / "articles-2026-10-13-am.json"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_text(json.dumps(make_archive(fixture_payload)), encoding="utf-8")

    with pytest.raises(editorial.EditorialError) as excinfo:
        editorial.run(archive_path, "AM", config=sandbox_config)
    assert "BRIEF_LLM_API_KEY" in str(excinfo.value)


def test_run_fails_on_an_empty_candidate_pool(sandbox_config: dict, fixture_payload: dict) -> None:
    archive = make_archive(fixture_payload)
    archive["sections"] = {section: [] for section in archive["sections"]}
    archive_path = Path(sandbox_config["paths"]["build_dir"]) / "articles-2026-10-13-am.json"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_text(json.dumps(archive), encoding="utf-8")

    with pytest.raises(editorial.EditorialError):
        editorial.run(archive_path, "AM", config=sandbox_config, llm_response="{}")
