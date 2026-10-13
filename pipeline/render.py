"""Render the edition into one responsive, newspaper-styled HTML document.

    python -m pipeline.render build/edition-2026-10-13-am.json --date 2026-10-13 --check

Produces ``dist/global-brief-<date>-<edition>.html``: a single self-contained document
(CSS inline, illustrations inline SVG, no scripts, no external requests) that reads as a
newspaper on a laptop and as a clean single column on a phone.

There is no fixed page box and no pagination: the brief asks for *about two A4 pages of
newspaper*, so length is a target (``layout.target_sheets``) that the preflight checks,
not a constraint the layout enforces by clipping.

Stage order is contractual (invariant 4):

    check_allowlist -> validate(schema) -> resolve_sources -> _enforce_budgets -> render

Validation must happen while stories still carry ``source_ids``; the schema is written
against that form, so resolving first makes validation fail with a misleading
"source_ids is a required property".
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping

from . import content as content_helpers
from . import sources as allowlist
from .config import (
    ConfigError,
    cutoff_datetime,
    edition_label,
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
from .editorial import EditorialError, _enforce_budgets, _resolve_sources, validate_edition
from .preflight import PreflightError, assert_deliverable

__all__ = [
    "DEFAULT_TEMPLATE",
    "RenderError",
    "SECTION_SLOTS",
    "assert_rendered",
    "build_meta",
    "check_allowlist",
    "check_prose",
    "edition_number",
    "load_edition",
    "normalize_payload",
    "render_html",
    "run",
    "validate",
]

DEFAULT_TEMPLATE = "newspaper.html.j2"

#: The five sections the template lays out, independent of feed configuration.
SECTION_SLOTS: tuple[str, ...] = ("geopolitics", "tech", "finance", "sport", "entertainment")

_URL_IN_PROSE_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_TAG_IN_PROSE_RE = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*[^>]*>")


class RenderError(RuntimeError):
    """Rendering failed: missing template/asset, invalid payload, or leaked markup."""


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
    """Names of every outlet the edition cites, sorted and de-duplicated."""
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
    """Assemble every string the template prints that is not editorial content."""
    date = normalize_date(run_date)
    edition = normalize_edition(edition)
    label = edition_label(config, edition)
    number = _edition_number(config, date, edition)
    start, end = window(config, date, edition)

    content = content if isinstance(content, Mapping) else {}
    source_index = sources if isinstance(sources, Mapping) else {}
    cited_names = _collect_source_names({"content": content}) if content else []
    stats = content_helpers.word_counts(content)
    length = content_helpers.estimate_length(content) if content else {"front": 0.0, "inside": 0.0, "total": 0.0}
    stories = content_helpers.story_count(content)
    inside_titles = " · ".join(
        section_title(config, section) for section in ("tech", "finance", "sport", "entertainment")
    )

    return {
        "title": f"The Global Brief — {label}, {date.day} {date:%B %Y} (No. {number})",
        "date_iso": date.isoformat(),
        "date_short": f"{date.day} {date:%b %Y}",
        "date_long": f"{date:%A}, {date.day} {date:%B %Y}",
        "masthead_date": f"{date:%A}, {date.day} {date:%B %Y}".upper(),
        "edition": edition,
        "edition_label": label,
        "edition_number": number,
        "issue_label": f"No. {number}",
        "footer_text": footer_text(config),
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
        "note": note or f"{edition} edition · {stories} stories",
        "target_sheets": float((config.get("layout") or {}).get("target_sheets", 2.0)),
        "stats": {
            "stories": stories,
            "words": stats["total"],
            "page1_words": stats["front"],
            "page2_words": stats["inside"],
            "sheets": length,
        },
    }


# ---------------------------------------------------------------------------
# Loading and checking
# ---------------------------------------------------------------------------


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
    return normalize_payload(dict(payload))


def normalize_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``{"run_date", "edition", "note", "sources", "content"}`` for either form."""
    return {
        "run_date": payload.get("run_date"),
        "edition": payload.get("edition"),
        "note": payload.get("note"),
        "generated_at": payload.get("generated_at"),
        "sources": content_helpers.edition_sources(payload),
        "content": content_helpers.edition_content(payload),
    }


def check_allowlist(payload: Mapping[str, Any]) -> int:
    """Invariant 6: refuse to render anything that cites a non-whitelisted URL."""
    try:
        return allowlist.assert_allowed(payload, context="edition payload")
    except allowlist.AllowlistError as exc:
        raise RenderError(str(exc)) from exc


def check_prose(payload: Mapping[str, Any]) -> int:
    """Reject copy that contains a URL or markup.

    The editorial contract says the model never writes URLs or HTML, and the allowlist
    gate only inspects URL *fields* — so body text is checked here, where a hallucinated
    link or an injected tag would otherwise reach the page.
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
        texts += [(f"{path}.paras[{index}]", p) for index, p in enumerate(story.get("paras") or [])]
        for label, value in texts:
            if not isinstance(value, str):
                continue
            checked += 1
            if _URL_IN_PROSE_RE.search(value):
                raise RenderError(
                    f"{label} contains a URL; the model may only cite source ids "
                    f"(offending text: {value[:90]!r})"
                )
            if _TAG_IN_PROSE_RE.search(value):
                raise RenderError(
                    f"{label} contains HTML markup; layout is a versioned asset, never "
                    f"model output (offending text: {value[:90]!r})"
                )

    for index, brief in enumerate(content.get("briefs") or []):
        if isinstance(brief, Mapping):
            text = str(brief.get("text") or "")
            checked += 1
            if _URL_IN_PROSE_RE.search(text) or _TAG_IN_PROSE_RE.search(text):
                raise RenderError(f"briefs[{index}] contains a URL or markup: {text[:90]!r}")

    sport = content.get("sport") or {}
    if isinstance(sport, Mapping):
        for index, bullet in enumerate(sport.get("bullets") or []):
            text = str(bullet)
            checked += 1
            if _URL_IN_PROSE_RE.search(text) or _TAG_IN_PROSE_RE.search(text):
                raise RenderError(f"sport.bullets[{index}] contains a URL or markup: {text[:90]!r}")
    return checked


def validate(payload: Mapping[str, Any]) -> None:
    """Validate the edition content against :data:`pipeline.editorial.EDITION_SCHEMA`."""
    try:
        validate_edition(content_helpers.edition_content(payload))
    except EditorialError as exc:
        raise RenderError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Template plumbing
# ---------------------------------------------------------------------------


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
        # `deck`/`kicker`/`dateline` may simply be absent; anything required is
        # asserted by the preflight and the tests instead.
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


def render_html(
    edition: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    template_name: str = DEFAULT_TEMPLATE,
    meta: Mapping[str, Any] | None = None,
    run_date: dt.date | str | None = None,
    edition_name: str | None = None,
) -> str:
    """Render *edition* (wrapped payload or bare content) to a complete HTML document.

    Budgets are applied here as well as in the editor: the enforcer is idempotent, and
    running it on this path is what keeps the delivered text identical to the text the
    manifest and the length estimate describe.
    """
    content = content_helpers.edition_content(edition)
    _enforce_budgets(content, config)
    sources_index = content_helpers.edition_sources(edition)
    resolved_date = normalize_date(run_date if run_date is not None else edition.get("run_date"))
    resolved_edition = normalize_edition(edition_name or edition.get("edition") or "AM")
    config_paths = paths(config)
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
    titles = {section: section_title(config, section) for section in SECTION_SLOTS}

    environment = _environment(config_paths["template_dir"])
    template_path = str((config.get("layout") or {}).get("template") or template_name)
    try:
        template = environment.get_template(template_path)
    except Exception as exc:  # jinja2.TemplateNotFound / TemplateSyntaxError
        raise RenderError(f"could not load template {template_path}: {exc}") from exc

    # No image assets: the document is text, CSS and source links only.
    html = template.render(e=content, meta=meta, titles=titles)
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
    check: bool = False,
) -> tuple[str, Path]:
    """Render an edition file and write the HTML deliverable.

    Returns ``(html, path)``. With ``check=True`` the document is passed through
    :func:`pipeline.preflight.assert_deliverable` before being written.
    """
    config = config or load_config(config_path)
    payload = load_edition(edition_path)

    # 1. whitelist gate, 2. schema against the source_ids form, 3. resolve ids, 4. budgets
    checked_urls = check_allowlist(payload)
    validate(payload)
    content = payload["content"]
    index = payload["sources"]
    unresolved = [
        path
        for path, story in content_helpers.iter_stories(content)
        if story.get("source_ids") and not story.get("sources")
    ]
    if unresolved:
        if not index:
            raise RenderError(
                "stories cite source_ids but the payload has no 'sources' index to resolve them "
                f"against (slots: {', '.join(unresolved[:5])})"
            )
        try:
            cited = _resolve_sources(content, index)
        except EditorialError as exc:
            raise RenderError(str(exc)) from exc
        print(f"resolved citations for {len(unresolved)} story slot(s) from {len(index)} index entries ({cited} cited)")
    _enforce_budgets(content, config)
    check_allowlist(content)
    prose_checked = check_prose(payload)

    resolved_edition = normalize_edition(edition or payload.get("edition") or "AM")
    resolved_date = normalize_date(run_date if run_date is not None else payload.get("run_date"))
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

    html = render_html(
        payload,
        config,
        template_name=template or str((config.get("layout") or {}).get("template") or DEFAULT_TEMPLATE),
        meta=meta,
        run_date=resolved_date,
        edition_name=resolved_edition,
    )

    if check:
        assert_deliverable(html, payload=payload, config=config)

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
        f"{checked_urls} urls allowlisted · {prose_checked} prose fields clean · "
        f"{stats['sheets']['total']} sheet-equivalents (target {meta.get('target_sheets', 2)})"
    )
    return html, destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the responsive newspaper HTML")
    parser.add_argument("edition", help="build/edition-<date>-<edition>.json")
    parser.add_argument("--date", default=None, help="run date, YYYY-MM-DD")
    parser.add_argument("--edition-name", dest="edition_name", default=None, choices=["am", "pm", "AM", "PM"])
    parser.add_argument("--config", default=None)
    parser.add_argument("--template", default=None)
    parser.add_argument("--out", default=None, help="output file (default: dist/global-brief-<date>-<ed>.html)")
    parser.add_argument("--check", action="store_true", help="run the pre-delivery checks")
    args = parser.parse_args(argv)

    try:
        run(
            args.edition,
            args.date,
            args.edition_name,
            config_path=args.config,
            out=args.out,
            template=args.template,
            check=args.check,
        )
    except (ConfigError, RenderError, PreflightError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
