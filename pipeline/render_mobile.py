"""Render the same edition for phones and laptops — and for print, identically.

    python -m pipeline.render_mobile build/edition-2026-10-13-am.json --date 2026-10-13

Produces ``dist/mobile-<date>-<edition>.html``: one responsive document that is a
single readable column on a phone, a two-column page from 640px, the full broadsheet
grid from 1024px — and exactly the same two A4 pages as the desktop artefact when
printed (both share templates/_pages.j2 and templates/_print.j2).

This stage does NOT call the model: one editorial JSON feeds both artefacts.

Stage order is contractual (invariant 4):

    check_allowlist -> validate(schema) -> resolve_sources -> _enforce_budgets

Validation must happen while stories still carry ``source_ids``; the schema is
written against that form, so resolving first fails with a misleading
"source_ids is a required property".
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping

from . import content as content_helpers
from . import sources as allowlist
from .config import (
    ConfigError,
    expected_pages,
    load_config,
    normalize_date,
    normalize_edition,
    paths,
    section_title,
)
from .editorial import _enforce_budgets, _resolve_sources, validate_edition
from .render import (
    SECTION_SLOTS,
    RenderError,
    _environment,
    assert_rendered,
    build_meta,
    load_assets,
)

__all__ = [
    "DEFAULT_TEMPLATE",
    "MobileRenderError",
    "check_allowlist",
    "check_prose",
    "load_payload",
    "normalize_payload",
    "run",
    "validate",
]

DEFAULT_TEMPLATE = "mobile.html.j2"

_URL_IN_PROSE_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_TAG_IN_PROSE_RE = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*[^>]*>")


class MobileRenderError(RenderError):
    """The payload cannot be turned into a mobile artefact without violating a rule."""


# ---------------------------------------------------------------------------
# Loading and checking
# ---------------------------------------------------------------------------


def load_payload(path: str | Path) -> dict[str, Any]:
    """Read an edition file, accepting both payload forms (DESIGN.md §5.3)."""
    edition_path = Path(path)
    if not edition_path.is_file():
        raise MobileRenderError(f"edition file not found: {edition_path}")
    try:
        payload = json.loads(edition_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MobileRenderError(f"{edition_path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise MobileRenderError(f"{edition_path} must contain a JSON object")
    return normalize_payload(dict(payload))


def normalize_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``{"run_date", "edition", "note", "sources", "content"}`` for either form."""
    content = content_helpers.edition_content(payload)
    return {
        "run_date": payload.get("run_date"),
        "edition": payload.get("edition"),
        "note": payload.get("note"),
        "generated_at": payload.get("generated_at"),
        "sources": content_helpers.edition_sources(payload),
        "content": content,
    }


def check_allowlist(payload: Mapping[str, Any]) -> int:
    """Invariant 6: refuse to render anything that cites a non-whitelisted URL.

    Returns the number of URLs checked.
    """
    try:
        return allowlist.assert_allowed(payload, context="mobile payload")
    except allowlist.AllowlistError as exc:
        raise MobileRenderError(str(exc)) from exc


def check_prose(payload: Mapping[str, Any]) -> int:
    """Reject copy that contains a URL or markup.

    The editorial contract says the model never writes URLs or HTML, and the
    allowlist gate only inspects URL *fields* — so body text is checked here, where
    a hallucinated link or an injected tag would otherwise reach the page.
    """
    checked = 0
    content = content_helpers.edition_content(payload)
    for path, story in content_helpers.iter_stories(content):
        texts: list[tuple[str, Any]] = [
            (f"{path}.headline", story.get("headline")),
            (f"{path}.deck", story.get("deck")),
            (f"{path}.kicker", story.get("kicker")),
            (f"{path}.dateline", story.get("dateline")),
        ]
        texts += [(f"{path}.paras[{i}]", p) for i, p in enumerate(story.get("paras") or [])]
        for label, value in texts:
            if not isinstance(value, str):
                continue
            checked += 1
            if _URL_IN_PROSE_RE.search(value):
                raise MobileRenderError(
                    f"{label} contains a URL; the model may only cite source ids "
                    f"(offending text: {value[:90]!r})"
                )
            if _TAG_IN_PROSE_RE.search(value):
                raise MobileRenderError(
                    f"{label} contains HTML markup; layout is a versioned asset, "
                    f"never model output (offending text: {value[:90]!r})"
                )

    for index, brief in enumerate(content.get("briefs") or []):
        if isinstance(brief, Mapping):
            text = str(brief.get("text") or "")
            checked += 1
            if _URL_IN_PROSE_RE.search(text) or _TAG_IN_PROSE_RE.search(text):
                raise MobileRenderError(f"briefs[{index}] contains a URL or markup: {text[:90]!r}")

    sport = content.get("sport") or {}
    if isinstance(sport, Mapping):
        for index, bullet in enumerate(sport.get("bullets") or []):
            text = str(bullet)
            checked += 1
            if _URL_IN_PROSE_RE.search(text) or _TAG_IN_PROSE_RE.search(text):
                raise MobileRenderError(f"sport.bullets[{index}] contains a URL or markup: {text[:90]!r}")

    return checked


def validate(payload: Mapping[str, Any]) -> None:
    """Validate the edition content against :data:`pipeline.editorial.EDITION_SCHEMA`."""
    try:
        validate_edition(content_helpers.edition_content(payload))
    except Exception as exc:  # EditorialError, plus anything jsonschema raises
        raise MobileRenderError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def run(
    edition_path: str | Path,
    run_date: Any = None,
    edition: str | None = None,
    template: str | Path | None = None,
    out: str | Path | None = None,
    *,
    config: Mapping[str, Any] | None = None,
    config_path: str | Path | None = None,
    root_copy: bool = False,
    report: bool = True,
) -> Path:
    """Render the responsive artefact. Returns the written path (DESIGN.md §4)."""
    config = config or load_config(config_path)
    payload = load_payload(edition_path)

    resolved_date = normalize_date(run_date if run_date is not None else payload.get("run_date"))
    resolved_edition = normalize_edition(edition or payload.get("edition") or "AM")

    # 1. whitelist gate (before anything is trusted)
    checked_urls = check_allowlist(payload)
    # 2. schema, against the source_ids form
    validate(payload)
    # 3. ids -> names + urls
    content = payload["content"]
    index = payload["sources"]
    unresolved = [path for path, story in content_helpers.iter_stories(content) if story.get("source_ids") and not story.get("sources")]
    if unresolved:
        if not index:
            raise MobileRenderError(
                "stories cite source_ids but the payload has no 'sources' index to resolve "
                f"them against (slots: {', '.join(unresolved[:5])})"
            )
        try:
            cited = _resolve_sources(content, index)
        except Exception as exc:
            raise MobileRenderError(str(exc)) from exc
        if report:
            print(f"resolved citations for {len(unresolved)} story slot(s) from {len(index)} index entries ({cited} cited)")
    # 4. budgets, in code
    budget_report = _enforce_budgets(content, config)
    # 5. whitelist gate again, now on the resolved document
    check_allowlist(content)
    prose_checked = check_prose(payload)

    meta = build_meta(
        resolved_date,
        resolved_edition,
        config,
        index,
        content=content,
        note=payload.get("note"),
        candidates=len(index),
        generated_at=payload.get("generated_at"),
    )

    config_paths = paths(config)
    environment = _environment(config_paths["template_dir"])
    template_name = str(template or (config.get("layout") or {}).get("mobile_template") or DEFAULT_TEMPLATE)
    try:
        compiled = environment.get_template(template_name)
    except Exception as exc:
        raise MobileRenderError(f"could not load template {template_name}: {exc}") from exc

    html = compiled.render(
        e=content,
        meta=meta,
        assets=load_assets(config_paths["asset_dir"]),
        titles={section: section_title(config, section) for section in SECTION_SLOTS},
        inside_plan=content_helpers.plan_inside_columns(content, config),
    )
    assert_rendered(html)

    destination = (
        Path(out)
        if out
        else config_paths["dist_dir"] / f"mobile-{resolved_date.isoformat()}-{resolved_edition.lower()}.html"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(html, encoding="utf-8")

    if root_copy:
        # Optional legacy fixed name. Known trade-off (DESIGN.md §6B): the PM run
        # overwrites the AM file, so the dated dist/ artefact remains canonical.
        legacy = config_paths["root"] / "mobile.html"
        legacy.write_text(html, encoding="utf-8")
        if report:
            print(f"also wrote {legacy} (fixed name — PM overwrites AM)")

    if report:
        stats = meta["stats"]
        print(
            f"wrote {destination} ({destination.stat().st_size:,} bytes) · "
            f"{stats['stories']} stories · {meta['sources_cited']} sources cited · "
            f"{checked_urls} urls allowlisted · {prose_checked} prose fields clean · "
            f"page 1 ≈{stats['page1_words']} words / page 2 ≈{stats['page2_words']} words"
        )
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the responsive / print-identical artefact")
    parser.add_argument("edition", help="build/edition-<date>-<edition>.json")
    parser.add_argument("--date", default=None, help="run date, YYYY-MM-DD")
    parser.add_argument("--edition-name", dest="edition_name", default=None, choices=["am", "pm", "AM", "PM"])
    parser.add_argument("--config", default=None)
    parser.add_argument("--template", default=None)
    parser.add_argument("--out", default=None, help="output file (default: dist/mobile-<date>-<ed>.html)")
    parser.add_argument("--root-copy", action="store_true", help="also write ./mobile.html (fixed name)")
    args = parser.parse_args(argv)

    try:
        run(
            args.edition,
            args.date,
            args.edition_name,
            args.template,
            args.out,
            config_path=args.config,
            root_copy=args.root_copy,
        )
    except (ConfigError, RenderError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
