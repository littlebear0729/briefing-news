"""Run the whole edition end to end.

    python -m pipeline.run --edition am [--date YYYY-MM-DD] [--publish] [--dry-run]

Stages, in order: fetch -> editorial (the single LLM call) -> render the responsive
newspaper HTML -> preflight checks -> manifest. ``--publish`` adds the Telegram
delivery at the end; the daily workflow deliberately keeps publishing in a separate job
that only holds the bot token (DESIGN.md §6D).

The deliverable is one HTML document. There is no PDF step and no pagination: length is
a target (``layout.target_sheets``) that the preflight verifies, not a constraint the
layout enforces by clipping.

The manifest (``dist/manifest-<date>-<edition>.json``) is written even when a stage
fails, so a failed night still leaves an auditable record of what happened.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from . import content as content_helpers
from . import sources
from .config import (
    ConfigError,
    edition_number,
    load_config,
    normalize_date,
    normalize_edition,
    paths,
    telegram_settings,
)
from .editorial import EditorialError
from .fetch import FetchError
from .preflight import PreflightError, assert_deliverable
from .render import RenderError, build_meta, run as render_run

__all__ = ["EditionRunError", "main", "run_edition"]


class EditionRunError(RuntimeError):
    """A stage failed; the manifest records which one."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _describe(path: Path | None) -> dict[str, Any] | None:
    if path is None or not Path(path).is_file():
        return None
    file = Path(path)
    relative = str(file)
    try:
        relative = str(file.relative_to(Path.cwd()))
    except ValueError:  # pragma: no cover - paths outside the working tree
        pass
    return {"path": relative, "bytes": file.stat().st_size, "sha256": _sha256(file)}


def run_edition(
    edition: str,
    run_date: dt.date | str | None = None,
    *,
    config: Mapping[str, Any] | None = None,
    config_path: str | Path | None = None,
    publish_after: bool = False,
    dry_run: bool = False,
    reuse_archive: bool = False,
    llm_response: str | Path | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute every stage for one edition and return the manifest."""
    # Imported here so a fetch-only or render-only run never needs the others' deps.
    from . import editorial, fetch, publish as publish_stage

    config = config or load_config(config_path)
    resolved_edition = normalize_edition(edition)
    date = normalize_date(run_date)
    config_paths = paths(config)
    config_paths["dist_dir"].mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    stages: list[dict[str, Any]] = []
    manifest: dict[str, Any] = {
        "run_date": date.isoformat(),
        "edition": resolved_edition,
        "edition_number": edition_number(config, date, resolved_edition),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "github": {
            "run_id": os.environ.get("GITHUB_RUN_ID"),
            "workflow": os.environ.get("GITHUB_WORKFLOW"),
            "event": os.environ.get("GITHUB_EVENT_NAME"),
            "sha": os.environ.get("GITHUB_SHA"),
        },
        "artifacts": {},
        "checks": {},
        "warnings": [],
        "stages": stages,
        "publish": None,
        "ok": False,
    }

    def stage(name: str, action: Callable[[], Any]) -> Any:
        began = time.monotonic()
        record: dict[str, Any] = {"name": name, "status": "ok"}
        try:
            result = action()
        except Exception as exc:
            record.update(
                status="failed",
                seconds=round(time.monotonic() - began, 2),
                error=f"{type(exc).__name__}: {exc}",
            )
            stages.append(record)
            raise
        record["seconds"] = round(time.monotonic() - began, 2)
        stages.append(record)
        return result

    archive_path = config_paths["build_dir"] / f"articles-{date.isoformat()}-{resolved_edition.lower()}.json"
    try:
        if reuse_archive and archive_path.is_file():
            stages.append({"name": "fetch", "status": "reused", "detail": str(archive_path)})
            print(f"reusing {archive_path}")
        else:
            archive_path = stage("fetch", lambda: fetch.run(resolved_edition, date, config=config))
        manifest["artifacts"]["articles"] = _describe(Path(archive_path))

        archive = json.loads(Path(archive_path).read_text(encoding="utf-8"))
        empty_sections = [section for section, items in (archive.get("sections") or {}).items() if not items]
        for section in empty_sections:
            message = f"{section}: no candidates in this window — the feed list may have rotted"
            manifest["warnings"].append(message)
            print(
                f"::warning::{message}"
                if os.environ.get("GITHUB_ACTIONS")
                else f"warning: {message}",
                file=sys.stderr,
            )

        manifest["candidates"] = sum(len(items) for items in (archive.get("sections") or {}).values())

        edition_path = stage(
            "editorial",
            lambda: editorial.run(archive_path, resolved_edition, config=config, llm_response=llm_response),
        )
        manifest["artifacts"]["edition"] = _describe(Path(edition_path))

        # Render, then check the document before anything is allowed to send it.
        html, html_path = stage(
            "render", lambda: render_run(edition_path, date, resolved_edition, config=config)
        )
        manifest["artifacts"]["html"] = _describe(html_path)

        payload = json.loads(Path(edition_path).read_text(encoding="utf-8"))
        content = content_helpers.edition_content(payload)
        meta = build_meta(
            date,
            resolved_edition,
            config,
            content_helpers.edition_sources(payload),
            content=content,
            note=payload.get("note"),
            candidates=manifest.get("candidates"),
            generated_at=payload.get("generated_at"),
        )
        manifest["stats"] = meta["stats"]
        manifest["sources_cited"] = meta["sources_cited"]
        manifest["source_names"] = meta["source_names"]
        manifest["budget_report"] = payload.get("budget_report")
        manifest["model"] = payload.get("model")
        manifest["usage"] = payload.get("usage")
        manifest["checks"]["deliverable"] = stage(
            "check-document",
            lambda: assert_deliverable(html, payload=payload, config=config),
        )

        if publish_after:
            settings = telegram_settings(config)
            from .publish import caption_from_edition

            caption = caption_from_edition(
                edition_path,
                config=config,
                run_date=date,
                edition=resolved_edition,
                max_chars=settings["caption_max_chars"],
                parse_mode=settings["caption_parse_mode"] or None,
            )
            manifest["publish"] = stage(
                "publish",
                lambda: publish_stage.publish(
                    html_path,
                    settings["chat_ids"],
                    token=settings["token"],
                    caption=caption,
                    api_base=settings["api_base"],
                    run_date=date,
                    edition=resolved_edition,
                    parse_mode=settings["caption_parse_mode"] or None,
                    dry_run=dry_run,
                    delay_seconds=settings["send_delay_seconds"],
                    retries=settings["max_retries"],
                    max_caption_chars=settings["caption_max_chars"],
                    config=config,
                ),
            )

        manifest["ok"] = True
    except (
        ConfigError,
        sources.AllowlistError,
        EditionRunError,
        EditorialError,
        RenderError,
        PreflightError,
        FetchError,
    ) as exc:
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        print(f"error: {exc}", file=sys.stderr)
    finally:
        manifest["duration_seconds"] = round(time.monotonic() - started, 2)
        manifest_path = config_paths["dist_dir"] / f"manifest-{date.isoformat()}-{resolved_edition.lower()}.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {manifest_path}")

    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build one edition of The Global Brief")
    parser.add_argument("--edition", required=True, choices=["am", "pm", "AM", "PM"])
    parser.add_argument("--date", default=None, help="run date, YYYY-MM-DD (default: today UTC)")
    parser.add_argument("--config", default=None)
    parser.add_argument("--publish", action="store_true", help="send the HTML after the checks pass")
    parser.add_argument("--dry-run", action="store_true", help="with --publish: report, send nothing")
    parser.add_argument("--reuse-archive", action="store_true", help="reuse an existing build/articles-*.json")
    parser.add_argument(
        "--llm-response",
        default=None,
        help="offline development: replay a recorded model response instead of calling the endpoint",
    )
    args = parser.parse_args(argv)

    manifest = run_edition(
        args.edition,
        args.date,
        config_path=args.config,
        publish_after=args.publish,
        dry_run=args.dry_run,
        reuse_archive=args.reuse_archive,
        llm_response=args.llm_response,
    )
    return 0 if manifest.get("ok") else 2


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
