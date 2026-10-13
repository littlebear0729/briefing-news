"""End-to-end orchestration: offline replay through every stage, plus the manifest."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pipeline import content as ch
from pipeline import preflight
from pipeline import run as run_module


def build_inputs(fixture_payload: dict, build_dir: Path) -> tuple[Path, Path]:
    """Write an articles archive and a recorded model response into a temp build dir."""
    build_dir.mkdir(parents=True, exist_ok=True)
    section_of = {
        "GEO": "geopolitics",
        "TEC": "tech",
        "FIN": "finance",
        "SPO": "sport",
        "ENT": "entertainment",
    }
    sections: dict[str, list[dict]] = {}
    for source_id, entry in fixture_payload["sources"].items():
        section = section_of[source_id.split("-")[0]]
        sections.setdefault(section, []).append(
            {
                "id": source_id,
                "section": section,
                "title": f"Candidate {source_id}",
                "url": entry["url"],
                "source": entry["url"].split("/")[2],
                "source_name": entry["source"],
                "published": "2026-10-13T00:30:00+00:00",
                "summary": f"Summary for {source_id}.",
            }
        )
    archive = {
        "edition": "AM",
        "run_date": "2026-10-13",
        "window": {"start": "2026-10-12T13:15:00+00:00", "end": "2026-10-13T01:15:00+00:00"},
        # one extra section with no candidates: it must raise a warning, not break the
        # run, and it is never referenced by the recorded response below.
        "sections": {**sections, "science": []},
    }
    archive_path = build_dir / "articles-2026-10-13-am.json"
    archive_path.write_text(json.dumps(archive), encoding="utf-8")

    response = copy.deepcopy(ch.edition_content(fixture_payload))
    for _path, story in ch.iter_stories(response):
        story.pop("sources", None)
    response_path = build_dir / "llm-response.json"
    response_path.write_text(json.dumps(response), encoding="utf-8")
    return archive_path, response_path


def test_run_edition_without_network_or_browser(sandbox_config, fixture_payload) -> None:
    build_dir = Path(sandbox_config["paths"]["build_dir"])
    _archive, response_path = build_inputs(fixture_payload, build_dir)

    manifest = run_module.run_edition(
        "am",
        "2026-10-13",
        config=sandbox_config,
        reuse_archive=True,
        llm_response=response_path,
    )

    assert manifest["ok"] is True, manifest.get("error")
    assert manifest["run_date"] == "2026-10-13" and manifest["edition"] == "AM"
    assert manifest["artifacts"]["edition"]["bytes"] > 0
    assert manifest["artifacts"]["html"]["path"].endswith("global-brief-2026-10-13-am.html")
    assert manifest["artifacts"]["html"]["bytes"] > 10_000
    assert manifest["sources_cited"] >= 15
    assert manifest["stats"]["stories"] == 13
    assert [stage["name"] for stage in manifest["stages"]] == [
        "fetch",
        "editorial",
        "render",
        "check-document",
    ]
    assert manifest["checks"]["deliverable"]["sheets"] == 2
    assert manifest["checks"]["deliverable"]["footer_on_every_sheet"] is True
    assert any("science" in warning for warning in manifest["warnings"])

    manifest_path = Path(sandbox_config["paths"]["dist_dir"]) / "manifest-2026-10-13-am.json"
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert stored["ok"] is True and stored["edition_number"] == 571
    assert stored["github"]["run_id"] is None  # not running inside Actions


def test_manifest_is_written_even_when_a_stage_fails(sandbox_config, monkeypatch) -> None:
    """The audit trail matters most on the night something breaks."""
    from pipeline import fetch

    def failing_fetch(*args, **kwargs):
        raise fetch.FetchError("every feed for this section failed")

    monkeypatch.setattr(fetch, "run", failing_fetch)

    manifest = run_module.run_edition("am", "2026-10-13", config=sandbox_config, llm_response="{}")

    assert manifest["ok"] is False
    assert "every feed for this section failed" in manifest["error"]
    manifest_path = Path(sandbox_config["paths"]["dist_dir"]) / "manifest-2026-10-13-am.json"
    assert manifest_path.is_file()
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["ok"] is False
    assert manifest["stages"] and manifest["stages"][0]["status"] == "failed"
    assert manifest["stages"][0]["name"] == "fetch"


def test_a_failing_document_check_stops_the_run(sandbox_config, fixture_payload, monkeypatch) -> None:
    """A document that fails its checks must never be reported as deliverable."""
    build_dir = Path(sandbox_config["paths"]["build_dir"])
    _archive, response_path = build_inputs(fixture_payload, build_dir)

    def failing_check(*args, **kwargs):
        raise preflight.PreflightError("expected 2 sheets, found 1")

    monkeypatch.setattr(run_module, "assert_deliverable", failing_check)

    manifest = run_module.run_edition(
        "am", "2026-10-13", config=sandbox_config, reuse_archive=True, llm_response=response_path
    )

    assert manifest["ok"] is False
    assert "expected 2 sheets" in manifest["error"]
    assert [stage["name"] for stage in manifest["stages"]][-1] == "check-document"
    assert manifest["stages"][-1]["status"] == "failed"


def test_publish_stage_sends_the_html(sandbox_config, fixture_payload, monkeypatch) -> None:
    """--publish delivers the rendered document, and only after the checks pass."""
    build_dir = Path(sandbox_config["paths"]["build_dir"])
    _archive, response_path = build_inputs(fixture_payload, build_dir)

    sent: dict = {}

    def fake_publish(document, chat_ids, **kwargs):
        sent["document"] = Path(document)
        sent["chat_ids"] = chat_ids
        sent["caption"] = kwargs.get("caption")
        return {"sent": [{"chat_id": "1"}], "skipped": [], "failed": []}

    from pipeline import publish as publish_module

    monkeypatch.setattr(publish_module, "publish", fake_publish)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    sandboxed = copy.deepcopy(sandbox_config)

    manifest = run_module.run_edition(
        "am",
        "2026-10-13",
        config=sandboxed,
        reuse_archive=True,
        llm_response=response_path,
        publish_after=True,
        dry_run=True,
    )

    assert manifest["ok"] is True, manifest.get("error")
    assert sent["document"].name == "global-brief-2026-10-13-am.html"
    assert sent["chat_ids"] == ["12345"]
    assert "Truce holds in Kestrel corridor" in sent["caption"]
    assert manifest["publish"]["sent"][0]["chat_id"] == "1"


def test_main_returns_non_zero_on_failure(sandbox_config, fixture_payload, monkeypatch) -> None:
    """The CLI must fail closed and stay offline: reuse an archive, replay a bad response."""
    build_inputs(fixture_payload, Path(sandbox_config["paths"]["build_dir"]))
    monkeypatch.setattr(run_module, "load_config", lambda path=None: sandbox_config)
    exit_code = run_module.main(
        ["--edition", "am", "--date", "2026-10-13", "--reuse-archive", "--llm-response", "{}"]
    )
    assert exit_code == 2
