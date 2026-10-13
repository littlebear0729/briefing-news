"""End-to-end orchestration: offline replay through every stage, plus the manifest."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pipeline import content as ch
from pipeline import run as run_module


def build_inputs(fixture_payload: dict, config: dict, build_dir: Path) -> tuple[Path, Path]:
    """Write an articles archive and a recorded model response into a temp build dir."""
    build_dir.mkdir(parents=True, exist_ok=True)
    sections: dict[str, list[dict]] = {}
    prefix_sections = {
        "GEO": "geopolitics",
        "TEC": "tech",
        "FIN": "finance",
        "SPO": "sport",
        "ENT": "entertainment",
    }
    for source_id, entry in fixture_payload["sources"].items():
        section = prefix_sections[source_id.split("-")[0]]
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


def test_run_edition_without_network_or_browser(sandbox_config, fixture_payload, capsys) -> None:
    build_dir = Path(sandbox_config["paths"]["build_dir"])
    _archive, response_path = build_inputs(fixture_payload, sandbox_config, build_dir)

    manifest = run_module.run_edition(
        "am",
        "2026-10-13",
        config=sandbox_config,
        make_pdf=False,
        reuse_archive=True,
        llm_response=response_path,
    )

    assert manifest["ok"] is True, manifest.get("error")
    assert manifest["run_date"] == "2026-10-13" and manifest["edition"] == "AM"
    assert manifest["artifacts"]["edition"]["bytes"] > 0
    assert manifest["artifacts"]["desktop_html"]["bytes"] > 0
    assert manifest["artifacts"]["mobile_html"]["bytes"] > 0
    assert "desktop_pdf" not in manifest["artifacts"]
    assert manifest["sources_cited"] >= 15
    assert manifest["stats"]["stories"] == 13
    assert [stage["name"] for stage in manifest["stages"]] == [
        "fetch",
        "editorial",
        "render-desktop",
        "render-mobile",
    ]
    assert any("science" in warning for warning in manifest["warnings"])

    manifest_path = Path(sandbox_config["paths"]["dist_dir"]) / "manifest-2026-10-13-am.json"
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert stored["ok"] is True and stored["edition_number"] == 571
    assert stored["github"]["run_id"] is None  # not running inside Actions


def test_manifest_is_written_even_when_a_stage_fails(sandbox_config, monkeypatch, capsys) -> None:
    """The audit trail matters most on the night something breaks."""
    from pipeline import fetch

    def failing_fetch(*args, **kwargs):
        raise fetch.FetchError("every feed for this section failed")

    monkeypatch.setattr(fetch, "run", failing_fetch)

    manifest = run_module.run_edition(
        "am",
        "2026-10-13",
        config=sandbox_config,
        make_pdf=False,
        llm_response="{}",
    )

    assert manifest["ok"] is False
    assert "every feed for this section failed" in manifest["error"]
    manifest_path = Path(sandbox_config["paths"]["dist_dir"]) / "manifest-2026-10-13-am.json"
    assert manifest_path.is_file()
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["ok"] is False
    assert manifest["stages"] and manifest["stages"][0]["status"] == "failed"
    assert manifest["stages"][0]["name"] == "fetch"


def test_publish_flag_requires_a_pdf(sandbox_config, fixture_payload) -> None:
    build_dir = Path(sandbox_config["paths"]["build_dir"])
    _archive, response_path = build_inputs(fixture_payload, sandbox_config, build_dir)

    manifest = run_module.run_edition(
        "am",
        "2026-10-13",
        config=sandbox_config,
        make_pdf=False,
        publish_after=True,
        reuse_archive=True,
        llm_response=response_path,
    )

    assert manifest["ok"] is False
    assert "needs a PDF" in manifest["error"]


def test_run_edition_reports_a_failing_pdf_assertion(sandbox_config, fixture_payload, monkeypatch) -> None:
    """A PDF that fails its assertions must never be marked deliverable."""
    build_dir = Path(sandbox_config["paths"]["build_dir"])
    _archive, response_path = build_inputs(fixture_payload, sandbox_config, build_dir)

    from pipeline import export

    def fake_to_pdf(html_path, pdf_path=None):
        target = Path(pdf_path) if pdf_path else Path(html_path).with_suffix(".pdf")
        target.write_bytes(b"%PDF-1.4\n")
        return target

    def fake_assert_pdf(*args, **kwargs):
        raise export.PdfCheckError("printed 3 page(s), expected exactly 2")

    monkeypatch.setattr(run_module, "to_pdf", fake_to_pdf)
    monkeypatch.setattr(run_module, "assert_pdf", fake_assert_pdf)

    manifest = run_module.run_edition(
        "am", "2026-10-13", config=sandbox_config, reuse_archive=True, llm_response=response_path
    )

    assert manifest["ok"] is False
    assert "printed 3 page(s)" in manifest["error"]
    assert [stage["name"] for stage in manifest["stages"]][-1] == "assert-desktop"
    assert manifest["stages"][-1]["status"] == "failed"


def test_main_returns_non_zero_on_failure(sandbox_config, fixture_payload, monkeypatch) -> None:
    """The CLI must fail closed and stay offline: reuse an archive, replay a bad response."""
    build_dir = Path(sandbox_config["paths"]["build_dir"])
    build_inputs(fixture_payload, sandbox_config, build_dir)
    monkeypatch.setattr(run_module, "load_config", lambda path=None: sandbox_config)
    exit_code = run_module.main(
        ["--edition", "am", "--date", "2026-10-13", "--no-pdf", "--reuse-archive", "--llm-response", "{}"]
    )
    assert exit_code == 2
