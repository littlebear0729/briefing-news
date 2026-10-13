"""Render the edition into the print-first A4 artefact.

    python -m pipeline.render build/edition-2026-10-13-am.json --date 2026-10-13

Produces ``dist/global-brief-<date>-<edition>.html``: a fixed 210mm newspaper whose
two ``.page`` boxes are exactly two A4 pages in print, with the same document also
readable in a browser.

Templates are versioned assets (invariant 1). The model never contributes markup;
this stage only substitutes code-generated metadata and code-resolved citations.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from . import content as content_helpers
from .config import (
    ConfigError,
    edition_label,
    expected_pages,
    footer_text,
    load_config,
    lookback_hours,
    normalize_date,
    normalize_edition,
    paths,
    section_title,
    window,
)
from .config import edition_number as _edition_number

__all__ = [
    "DEFAULT_TEMPLATE",
    "RenderError",
    "SECTION_SLOTS",
    "assert_rendered",
    "build_meta",
    "edition_number",
    "load_assets",
    "load_edition",
    "render_html",
    "run",
]

DEFAULT_TEMPLATE = "brief.html.j2"

#: The five sections the templates lay out, independent of feed configuration.
SECTION_SLOTS: tuple[str, ...] = ("geopolitics", "tech", "finance", "sport", "entertainment")


class RenderError(RuntimeError):
    """Rendering failed: missing template/asset, or Jinja syntax left in the output."""


def _jinja() -> Any:
    try:
        import jinja2  # noqa: PLC0415
    except ModuleNotFoundError as exc:  # pragma: no cover - environment dependent
        raise RenderError("Jinja2 is not installed; run: pip install -r requirements.txt") from exc
    return jinja2


# ---------------------------------------------------------------------------
# Metadata — code owns dates, issue numbers, file names
# ---------------------------------------------------------------------------


def edition_number(
    run_date: dt.date | str | None = None,
    edition: str = "AM",
    config: Mapping[str, Any] | None = None,
) -> int:
    """Sequential issue number of *edition* on *run_date* (DESIGN.md §4)."""
    config = config or load_config()
    return _edition_number(config, normalize_date(run_date), normalize_edition(edition))


def _collect_source_names(edition: Mapping[str, Any]) -> list[str]:
    """Names of every outlet the edition cites, sorted and de-duplicated.

    Accepts either payload form (wrapped editor output or a bare content object).
    """
    return content_helpers.source_names(edition)


def build_meta(
    run_date: dt.date | str,
    edition: str,
    config: Mapping[str, Any],
    sources: Mapping[str, Any] | None = None,
    *,
    content: Mapping[str, Any] | None = None,
    note: str | None = None,
    candidates: int | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Assemble every string the templates print that is not editorial content."""
    date = normalize_date(run_date)
    edition = normalize_edition(edition)
    label = edition_label(config, edition)
    number = _edition_number(config, date, edition)
    start, end = window(config, date, edition)

    content = content if isinstance(content, Mapping) else {}
    source_index = sources if isinstance(sources, Mapping) else {}
    cited_names = _collect_source_names({"content": content}) if content else []
    stats = content_helpers.word_counts(content)
    stories = content_helpers.story_count(content)
    inside_titles = " · ".join(
        section_title(config, section) for section in ("tech", "finance", "sport", "entertainment")
    )

    return {
        "title": (
            f"The Global Brief — {label}, {date.day} {date:%B %Y} "
            f"(No. {number})"
        ),
        "date_iso": date.isoformat(),
        "date_short": f"{date.day} {date:%b %Y}",
        "date_long": f"{date:%A}, {date.day} {date:%B %Y}",
        "edition": edition,
        "edition_label": label,
        "edition_number": number,
        "issue_label": f"No. {number}",
        "footer_text": footer_text(config),
        "page_count": expected_pages(config),
        "generated_at": generated_at or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "window_line": (
            f"Reporting window {start:%d %b %H:%M} to {end:%d %b %H:%M} UTC "
            f"({lookback_hours(config, edition):g}h)"
        ),
        "sources_cited": len(cited_names),
        "source_names": cited_names,
        "candidates": int(candidates if candidates is not None else len(source_index)),
        "inside_title": inside_titles,
        "inside_note": f"{label} · No. {number} · {len(cited_names)} sources cited",
        "visual_caption": (
            f"Global wire traffic over the {lookback_hours(config, edition):g}-hour window · illustration"
        ),
        "note": note or f"{edition} edition · {stories} stories",
        "stats": {
            "stories": stories,
            "words": stats["total"],
            "page1_words": stats["front"],
            "page2_words": stats["inside"],
        },
    }


# ---------------------------------------------------------------------------
# Template plumbing
# ---------------------------------------------------------------------------


def load_assets(asset_dir: str | Path) -> dict[str, Any]:
    """Inline the versioned SVG assets.

    These are hand-drawn, hand-versioned files in ``assets/`` — layout assets in the
    same sense as the templates, and like them never generated by a model. They are
    marked safe here because they are repository content, not feed or model output.
    """
    from markupsafe import Markup  # noqa: PLC0415 - dependency of Jinja2

    directory = Path(asset_dir)
    if not directory.is_dir():
        raise RenderError(f"asset directory not found: {directory}")
    assets: dict[str, Any] = {}
    for asset in sorted(directory.glob("*.svg")):
        assets[asset.stem] = Markup(asset.read_text(encoding="utf-8"))
    if not assets:
        raise RenderError(f"no .svg assets found in {directory}")
    return assets


def _environment(template_dir: str | Path) -> Any:
    jinja2 = _jinja()
    directory = Path(template_dir)
    if not directory.is_dir():
        raise RenderError(f"template directory not found: {directory}")
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(directory)),
        autoescape=jinja2.select_autoescape(["html", "xml"], default_for_string=False),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        # Undefined (not StrictUndefined) so optional story fields such as
        # `deck`/`kicker`/`dateline` can simply be absent; anything iterated or
        # printed to a required slot is asserted by the tests instead.
        undefined=jinja2.Undefined,
    )


def assert_rendered(html: str) -> None:
    """Fail loudly on unrendered Jinja — a half-substituted page must never ship."""
    for marker in ("{{", "{%", "{#"):
        if marker in html:
            index = html.find(marker)
            raise RenderError(
                f"template output still contains {marker!r} at offset {index}: "
                f"{html[max(0, index - 60):index + 60]!r}"
            )


def load_edition(path: str | Path) -> dict[str, Any]:
    """Read an edition JSON file (either accepted payload form)."""
    edition_path = Path(path)
    if not edition_path.is_file():
        raise RenderError(f"edition file not found: {edition_path}")
    try:
        payload = json.loads(edition_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RenderError(f"{edition_path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise RenderError(f"{edition_path} must contain a JSON object")
    return dict(payload)


def render_html(
    edition: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    template_name: str = DEFAULT_TEMPLATE,
    meta: Mapping[str, Any] | None = None,
    assets: Mapping[str, Any] | None = None,
    run_date: dt.date | str | None = None,
    edition_name: str | None = None,
) -> str:
    """Render *edition* (wrapped payload or bare content) to a complete HTML document.

    Budgets are applied here as well as in the editor: the enforcer is idempotent, and
    running it on both render paths is what guarantees the print and the responsive
    artefact can never be built from different text.
    """
    from .editorial import _enforce_budgets  # noqa: PLC0415 - shared with the editor

    content = content_helpers.edition_content(edition)
    sources_index = content_helpers.edition_sources(edition)
    resolved_date = normalize_date(
        run_date if run_date is not None else edition.get("run_date")
    )
    resolved_edition = normalize_edition(
        edition_name or edition.get("edition") or "AM"
    )
    config_paths = paths(config)
    _enforce_budgets(content, config)
    meta = dict(
        meta
        or build_meta(
            resolved_date,
            resolved_edition,
            config,
            sources_index,
            content=content,
            note=edition.get("note"),
            candidates=len(sources_index),
            generated_at=edition.get("generated_at"),
        )
    )
    assets = dict(assets) if assets is not None else load_assets(config_paths["asset_dir"])
    titles = {section: section_title(config, section) for section in SECTION_SLOTS}

    environment = _environment(config_paths["template_dir"])
    try:
        template = environment.get_template(template_name)
    except Exception as exc:  # jinja2.TemplateNotFound / TemplateSyntaxError
        raise RenderError(f"could not load template {template_name}: {exc}") from exc

    # Page-two packing is computed here, in code, and handed to the template: the
    # natural section order does not fit two fixed-height columns (see _pages.j2).
    inside_plan = content_helpers.plan_inside_columns(content, config)
    weights = content_helpers.estimate_section_weights(content)
    print(
        "inside columns: "
        + " | ".join(
            f"{side}=[{', '.join(plan)}] weight {sum(weights.get(section, 0.0) for section in plan):.0f}"
            for side, plan in inside_plan.items()
        )
    )

    html = template.render(
        e=content, meta=meta, assets=assets, titles=titles, inside_plan=inside_plan
    )
    assert_rendered(html)
    return html


def run(
    edition_path: str | Path,
    run_date: dt.date | str | None = None,
    edition: str | None = None,
    *,
    config: Mapping[str, Any] | None = None,
    config_path: str | Path | None = None,
    out: str | Path | None = None,
    template: str | None = None,
) -> tuple[str, Path]:
    """Render an edition file and write the desktop artefact.

    Returns ``(html, path)`` as documented in DESIGN.md §4.
    """
    from .editorial import _enforce_budgets  # noqa: PLC0415 - shared with the editor

    config = config or load_config(config_path)
    payload = load_edition(edition_path)
    content = content_helpers.edition_content(payload)
    # Enforced before the metadata is computed, so the word counts printed on the
    # masthead and recorded in the manifest describe the text that is actually laid out.
    _enforce_budgets(content, config)
    sources_index = content_helpers.edition_sources(payload)
    resolved_edition = normalize_edition(edition or payload.get("edition") or "AM")
    resolved_date = normalize_date(run_date if run_date is not None else payload.get("run_date"))

    meta = build_meta(
        resolved_date,
        resolved_edition,
        config,
        sources_index,
        content=content,
        note=payload.get("note"),
        candidates=len(sources_index),
        generated_at=payload.get("generated_at"),
    )
    html = render_html(
        payload,
        config,
        template_name=template or str((config.get("layout") or {}).get("desktop_template") or DEFAULT_TEMPLATE),
        meta=meta,
        run_date=resolved_date,
        edition_name=resolved_edition,
    )

    destination = (
        Path(out)
        if out
        else paths(config)["dist_dir"] / f"global-brief-{resolved_date.isoformat()}-{resolved_edition.lower()}.html"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(html, encoding="utf-8")

    stats = meta["stats"]
    print(
        f"wrote {destination} ({destination.stat().st_size:,} bytes) · "
        f"{stats['stories']} stories · {meta['sources_cited']} sources cited · "
        f"page 1 ≈{stats['page1_words']} words / page 2 ≈{stats['page2_words']} words"
    )
    return html, destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the print-first A4 artefact")
    parser.add_argument("edition", help="build/edition-<date>-<edition>.json")
    parser.add_argument("--date", default=None, help="run date, YYYY-MM-DD")
    parser.add_argument("--edition-name", dest="edition_name", default=None, choices=["am", "pm", "AM", "PM"])
    parser.add_argument("--config", default=None)
    parser.add_argument("--template", default=None)
    parser.add_argument("--out", default=None, help="output file (default: dist/global-brief-<date>-<ed>.html)")
    args = parser.parse_args(argv)

    try:
        run(
            args.edition,
            args.date,
            args.edition_name,
            config_path=args.config,
            out=args.out,
            template=args.template,
        )
    except (ConfigError, RenderError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
