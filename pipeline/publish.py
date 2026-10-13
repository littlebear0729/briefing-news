"""Deliver the edition through a Telegram bot.

    python -m pipeline.publish --pdf dist/mobile-2026-10-13-am.pdf \
        --date 2026-10-13 --edition am --edition-json build/edition-2026-10-13-am.json

The design rules this stage exists to satisfy (DESIGN.md §6A, §6C):

* **Assertions first.** The PDF must pass :func:`pipeline.export.assert_pdf` (exactly
  two pages, footer present on both, under the size limit) before a single byte moves.
* **Idempotent.** ``state/sent.json`` records ``<date>-<EDITION>`` per chat, so a retry
  of the same job — a re-run, a manual dispatch, a duplicated cron — does not re-send.
* **Serial and polite.** One document at a time with a pause between chats; HTTP 429 is
  answered with Telegram's own ``retry_after``.
* **Retries reopen the file.** The document handle is opened inside each attempt; a
  handle consumed by a failed upload must never be reused.
* **The token never reaches a log.** It lives in the request URL, so URLs are never
  printed, and every message passes through :func:`redact` first.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from .config import (
    ConfigError,
    load_config,
    normalize_date,
    normalize_edition,
    paths,
    telegram_settings,
)
from .export import PdfCheckError, assert_pdf

__all__ = [
    "PublishError",
    "already_sent",
    "build_caption",
    "load_state",
    "notify_admin",
    "preflight",
    "publish",
    "redact",
    "save_state",
    "send_document",
    "send_message",
    "state_key",
]

STATE_VERSION = 1
DEFAULT_TIMEOUT = 120.0


class PublishError(RuntimeError):
    """Delivery failed. The message never contains the bot token."""


def redact(text: Any, *secrets: str) -> str:
    """Replace any secret substring with a marker, so logs stay safe by construction."""
    output = str(text)
    for secret in secrets:
        if secret and len(secret) >= 6:
            output = output.replace(secret, "***redacted***")
    return output


def _requests() -> Any:
    try:
        import requests  # noqa: PLC0415
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise PublishError("requests is not installed; run: pip install -r requirements.txt") from exc
    return requests


# ---------------------------------------------------------------------------
# State (idempotency)
# ---------------------------------------------------------------------------


def state_key(run_date: dt.date | str, edition: str) -> str:
    """The idempotency key: ``<date>-<EDITION>`` (DESIGN.md §6A)."""
    return f"{normalize_date(run_date).isoformat()}-{normalize_edition(edition)}"


def state_path(config: Mapping[str, Any]) -> Path:
    return paths(config)["state_dir"] / "sent.json"


def load_state(path: str | Path) -> dict[str, Any]:
    """Read the send ledger; a missing or unreadable file is an empty ledger."""
    state_file = Path(path)
    if not state_file.is_file():
        return {"version": STATE_VERSION, "entries": {}}
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PublishError(
            f"{state_file} is not valid JSON ({exc}); refusing to guess whether the edition was sent"
        ) from exc
    if not isinstance(data, Mapping) or not isinstance(data.get("entries"), Mapping):
        raise PublishError(f"{state_file} has an unexpected shape; expected {{'entries': {{...}}}}")
    return {"version": int(data.get("version", STATE_VERSION)), "entries": dict(data["entries"])}


def save_state(path: str | Path, state: Mapping[str, Any]) -> Path:
    """Write the ledger atomically (temp file + replace) so a crash cannot corrupt it."""
    state_file = Path(path)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(state)
    payload["version"] = STATE_VERSION
    payload["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    scratch = state_file.with_suffix(state_file.suffix + ".tmp")
    scratch.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    scratch.replace(state_file)
    return state_file


def already_sent(state: Mapping[str, Any], key: str, chat_id: str) -> dict[str, Any] | None:
    """Return the recorded delivery for ``(key, chat_id)``, or ``None``."""
    entry = (state.get("entries") or {}).get(key) or {}
    record = (entry.get("chats") or {}).get(str(chat_id))
    return record if isinstance(record, Mapping) else None


def _record(
    state: dict[str, Any],
    key: str,
    chat_id: str,
    *,
    run_date: dt.date | str,
    edition: str,
    filename: str,
    pdf: Path,
    response: Mapping[str, Any],
) -> None:
    entries = state.setdefault("entries", {})
    entry = entries.setdefault(
        key,
        {
            "date": normalize_date(run_date).isoformat(),
            "edition": normalize_edition(edition),
            "chats": {},
        },
    )
    result = (response.get("result") or {}) if isinstance(response, Mapping) else {}
    document = result.get("document") or {}
    entry["chats"][str(chat_id)] = {
        "message_id": result.get("message_id"),
        "file_id": document.get("file_id"),
        "file_unique_id": document.get("file_unique_id"),
        "file_name": document.get("file_name") or filename,
        "bytes": pdf.stat().st_size,
        "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
        "sent_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# Bot API
# ---------------------------------------------------------------------------


def _call(
    token: str,
    api_base: str,
    method: str,
    *,
    data: Mapping[str, Any] | None = None,
    files: Mapping[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """One Bot API call. Never logs or embeds the token in an error message."""
    requests = _requests()
    url = f"{api_base.rstrip('/')}/bot{token}/{method}"
    try:
        response = requests.post(url, data=data or {}, files=files, timeout=timeout)
    except Exception as exc:  # requests.RequestException
        raise PublishError(redact(f"{method}: request failed: {type(exc).__name__}: {exc}", token)) from exc

    try:
        payload = response.json()
    except ValueError:
        body = redact(response.text[:300], token)
        raise PublishError(f"{method}: HTTP {response.status_code} returned non-JSON: {body}") from None

    if response.status_code == 429:
        parameters = payload.get("parameters") or {}
        retry_after = float(parameters.get("retry_after") or response.headers.get("Retry-After") or 5)
        raise _RateLimited(retry_after, f"{method}: rate limited, retry_after={retry_after:g}s")

    if response.status_code >= 400 or not payload.get("ok", False):
        description = redact(payload.get("description") or response.text[:200], token)
        hint = ""
        if response.status_code == 401:
            hint = " (check TELEGRAM_BOT_TOKEN)"
        elif response.status_code == 403:
            hint = " (the user must start a conversation with the bot first: 403 bot can't initiate conversation)"
        elif response.status_code == 400 and "chat not found" in description.lower():
            hint = " (check TELEGRAM_CHAT_ID)"
        raise PublishError(f"{method}: HTTP {response.status_code}: {description}{hint}")
    return payload


class _RateLimited(PublishError):
    """Internal: a 429 with the delay Telegram asked for."""

    def __init__(self, retry_after: float, message: str) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def send_message(
    token: str, api_base: str, chat_id: str, text: str, *, parse_mode: str | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return _call(token, api_base, "sendMessage", data=payload, timeout=30.0)


def notify_admin(
    token: str, admin_chat_id: str, text: str, *, api_base: str = "https://api.telegram.org"
) -> bool:
    """Best-effort failure alert; never raises, so it cannot mask the real error."""
    if not (token and admin_chat_id):
        return False
    try:
        send_message(token, api_base, admin_chat_id, text)
        print(f"alerted admin chat {admin_chat_id}")
        return True
    except Exception as exc:
        print(f"warning: could not alert the admin chat: {redact(exc, token)}", file=sys.stderr)
        return False


def preflight(token: str, chat_ids: Iterable[str], *, api_base: str = "https://api.telegram.org") -> dict[str, Any]:
    """Check the token (``getMe``) and that the bot can see each chat (``getChat``)."""
    if not token:
        raise PublishError("TELEGRAM_BOT_TOKEN is not set")
    me = _call(token, api_base, "getMe", timeout=30.0)
    bot = me.get("result") or {}
    print(f"preflight: bot @{bot.get('username')} (id {bot.get('id')})")
    chats: dict[str, Any] = {}
    for chat_id in chat_ids:
        info = (_call(token, api_base, "getChat", data={"chat_id": chat_id}, timeout=30.0).get("result") or {})
        chats[str(chat_id)] = info
        label = info.get("title") or info.get("username") or info.get("first_name") or "?"
        print(f"preflight: chat {chat_id} -> {info.get('type')} {label!r}")
    return {"bot": bot, "chats": chats}


def send_document(
    token: str,
    api_base: str,
    chat_id: str,
    pdf: str | Path,
    *,
    filename: str,
    caption: str | None = None,
    parse_mode: str | None = "HTML",
    retries: int = 3,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Upload the document, retrying transient failures with a fresh file handle."""
    document = Path(pdf)
    if not document.is_file():
        raise PublishError(f"pdf not found: {document}")

    attempts = max(1, int(retries))
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        payload: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            payload["caption"] = caption
            if parse_mode:
                payload["parse_mode"] = parse_mode
        try:
            # Opened inside the loop on purpose: a handle already consumed by a failed
            # upload would send a truncated (or empty) document on the next attempt.
            with document.open("rb") as handle:
                files = {"document": (filename, handle, "application/pdf")}
                response = _call(token, api_base, "sendDocument", data=payload, files=files, timeout=timeout)
            message_id = ((response.get("result") or {}).get("message_id"))
            print(f"sent {filename} to chat {chat_id} (message_id={message_id}, {document.stat().st_size:,} bytes)")
            return response
        except _RateLimited as exc:
            last_error = exc
            wait = max(0.5, float(exc.retry_after)) + 0.5
            print(f"warning: rate limited on attempt {attempt}/{attempts}; waiting {wait:g}s", file=sys.stderr)
            if attempt < attempts:
                time.sleep(wait)
        except PublishError as exc:
            last_error = exc
            message = str(exc)
            retryable = any(token_ in message for token_ in ("HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504", "request failed"))
            if not retryable or attempt >= attempts:
                raise
            delay = min(2 ** attempt, 20)
            print(f"warning: attempt {attempt}/{attempts} failed ({message[:160]}); retrying in {delay}s", file=sys.stderr)
            time.sleep(delay)
    raise PublishError(f"sendDocument failed after {attempts} attempt(s): {last_error}")


# ---------------------------------------------------------------------------
# Caption — written by code, never by the model
# ---------------------------------------------------------------------------


def build_caption(
    *,
    edition_label: str,
    date_long: str,
    issue_number: int | None = None,
    lead_headline: str | None = None,
    stories: int | None = None,
    sources_cited: int | None = None,
    pages: int = 2,
    extra: str | None = None,
    max_chars: int = 1024,
    parse_mode: str | None = "HTML",
) -> str:
    """Compose the attachment caption (body copy is deliberately *not* repeated).

    Falls back to plain text when ``parse_mode`` is None; otherwise every dynamic
    value is HTML-escaped so a headline can never break the message.
    """
    def esc(value: Any) -> str:
        return html.escape(str(value), quote=False) if parse_mode else str(value)

    lines = [f"<b>The Global Brief</b> — {esc(edition_label)}" if parse_mode else f"The Global Brief — {edition_label}"]
    meta = [esc(date_long)]
    if issue_number:
        meta.append(f"No. {issue_number}")
    lines.append(" · ".join(meta))
    if lead_headline:
        lines.append((f"<i>{esc(lead_headline)}</i>" if parse_mode else lead_headline))
    stats = []
    if stories:
        stats.append(f"{stories} stories")
    if sources_cited:
        stats.append(f"{sources_cited} sources cited")
    stats.append(f"{pages} pages, A4")
    lines.append(" · ".join(stats))
    if extra:
        lines.append(esc(extra))
    caption = "\n".join(line for line in lines if line)
    if len(caption) > max_chars:
        caption = caption[: max(0, max_chars - 1)].rstrip() + "…"
    return caption


def caption_from_edition(
    edition_path: str | Path,
    *,
    config: Mapping[str, Any],
    run_date: dt.date | str | None = None,
    edition: str | None = None,
    max_chars: int = 1024,
    parse_mode: str | None = "HTML",
) -> str:
    """Build the caption from the edition JSON, using the same metadata the pages use."""
    from . import content as content_helpers  # noqa: PLC0415 - avoids a cycle at import time
    from .render import build_meta  # noqa: PLC0415

    payload = json.loads(Path(edition_path).read_text(encoding="utf-8"))
    content = content_helpers.edition_content(payload)
    resolved_edition = normalize_edition(edition or payload.get("edition") or "AM")
    resolved_date = normalize_date(run_date if run_date is not None else payload.get("run_date"))
    meta = build_meta(
        resolved_date,
        resolved_edition,
        config,
        content_helpers.edition_sources(payload),
        content=content,
        note=payload.get("note"),
        generated_at=payload.get("generated_at"),
    )
    lead = content.get("lead") or {}
    return build_caption(
        edition_label=meta["edition_label"],
        date_long=meta["date_long"],
        issue_number=meta["edition_number"],
        lead_headline=str(lead.get("headline") or "") or None,
        stories=meta["stats"]["stories"],
        sources_cited=meta["sources_cited"],
        pages=meta["page_count"],
        extra=meta.get("note"),
        max_chars=max_chars,
        parse_mode=parse_mode,
    )


# ---------------------------------------------------------------------------
# publish()
# ---------------------------------------------------------------------------


def publish(
    pdf: str | Path,
    chat_ids: list[str],
    *,
    token: str,
    caption: str | None = None,
    api_base: str = "https://api.telegram.org",
    run_date: dt.date | str | None = None,
    edition: str = "AM",
    state_file: str | Path | None = None,
    filename: str | None = None,
    parse_mode: str | None = "HTML",
    force: bool = False,
    dry_run: bool = False,
    delay_seconds: float = 0.6,
    retries: int = 3,
    max_caption_chars: int = 1024,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Send *pdf* to every chat that has not received this edition yet.

    Returns a report: ``{"key", "sent": [...], "skipped": [...], "failed": [...],
    "chat_ids": [...], "filename": ...}``.
    """
    config = config or load_config()
    document = Path(pdf)
    if not document.is_file():
        raise PublishError(f"pdf not found: {document}")

    if not chat_ids:
        raise PublishError(
            "no recipients: set TELEGRAM_CHAT_ID (comma separated for several) "
            "or telegram.chat_ids in config.yaml"
        )

    date = normalize_date(run_date)
    resolved_edition = normalize_edition(edition)
    key = state_key(date, resolved_edition)
    template = str((config.get("telegram") or {}).get("filename_template") or "The-Global-Brief-{date}-{edition}.pdf")
    document_name = filename or template.format(date=date.isoformat(), edition=resolved_edition)
    if caption and len(caption) > max_caption_chars:
        caption = caption[: max(0, max_caption_chars - 1)].rstrip() + "…"

    ledger_path = Path(state_file) if state_file else state_path(config)
    state = load_state(ledger_path)
    report: dict[str, Any] = {
        "key": key,
        "filename": document_name,
        "chat_ids": [str(chat_id) for chat_id in chat_ids],
        "sent": [],
        "skipped": [],
        "failed": [],
        "dry_run": bool(dry_run),
    }

    if dry_run:
        for chat_id in report["chat_ids"]:
            record = already_sent(state, key, chat_id)
            report["skipped" if record and not force else "sent"].append(
                {"chat_id": chat_id, "reason": "already sent (dry run)" if record and not force else "dry run"}
            )
        print(
            f"dry run: {key} -> {document} as {document_name}; "
            f"{len(report['sent'])} would send, {len(report['skipped'])} already recorded"
        )
        return report

    preflight(token, report["chat_ids"], api_base=api_base)

    for index, chat_id in enumerate(report["chat_ids"]):
        record = already_sent(state, key, chat_id)
        if record and not force:
            digest = hashlib.sha256(document.read_bytes()).hexdigest()
            note = "" if record.get("sha256") == digest else " (content differs; pass --force to resend)"
            print(f"skip chat {chat_id}: {key} already delivered at {record.get('sent_at')}{note}")
            report["skipped"].append({"chat_id": chat_id, "sent_at": record.get("sent_at"), "reason": "already sent"})
            continue
        try:
            response = send_document(
                token,
                api_base,
                chat_id,
                document,
                filename=document_name,
                caption=caption,
                parse_mode=parse_mode,
                retries=retries,
            )
        except PublishError as exc:
            report["failed"].append({"chat_id": chat_id, "error": str(exc)})
            raise
        _record(
            state,
            key,
            chat_id,
            run_date=date,
            edition=resolved_edition,
            filename=document_name,
            pdf=document,
            response=response,
        )
        # Saved after every chat: a crash halfway through must not resend the rest.
        save_state(ledger_path, state)
        report["sent"].append({"chat_id": chat_id, "message_id": (response.get("result") or {}).get("message_id")})

        if index < len(report["chat_ids"]) - 1 and delay_seconds > 0:
            time.sleep(delay_seconds)

    print(
        f"published {key}: {len(report['sent'])} sent, {len(report['skipped'])} skipped, "
        f"{len(report['failed'])} failed · ledger {ledger_path}"
    )
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send the edition PDF through the Telegram bot")
    parser.add_argument("--pdf", required=True, help="PDF to send")
    parser.add_argument("--date", default=None, help="run date, YYYY-MM-DD (default: today UTC)")
    parser.add_argument("--edition", default="am", choices=["am", "pm", "AM", "PM"])
    parser.add_argument("--chat-id", action="append", default=None, help="recipient (repeatable; default from env/config)")
    parser.add_argument("--caption", default=None, help="caption text (ignored when --edition-json is given)")
    parser.add_argument("--caption-file", default=None, help="file containing the caption")
    parser.add_argument("--edition-json", default=None, help="edition JSON to build the caption from")
    parser.add_argument("--filename", default=None, help="document name shown in Telegram")
    parser.add_argument("--state", default=None, help="ledger path (default: state/sent.json)")
    parser.add_argument("--force", action="store_true", help="send even if the ledger says it was delivered")
    parser.add_argument("--skip-pdf-check", action="store_true", help="do not assert page count/footer/size")
    parser.add_argument("--dry-run", action="store_true", help="report what would be sent, send nothing")
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
        settings = telegram_settings(config)
        recipients = args.chat_id or settings["chat_ids"]
        run_date = normalize_date(args.date)

        if not args.dry_run and not args.skip_pdf_check:
            snippets = None
            if args.edition_json:
                from .export import content_snippets  # noqa: PLC0415

                snippets = content_snippets(json.loads(Path(args.edition_json).read_text(encoding="utf-8")))
            assert_pdf(args.pdf, config=config, required_snippets=snippets)

        caption = None
        if args.edition_json:
            caption = caption_from_edition(
                args.edition_json,
                config=config,
                run_date=run_date,
                edition=args.edition,
                max_chars=settings["caption_max_chars"],
                parse_mode=settings["caption_parse_mode"] or None,
            )
        elif args.caption_file:
            caption = Path(args.caption_file).read_text(encoding="utf-8").strip()
        elif args.caption:
            caption = args.caption

        publish(
            args.pdf,
            recipients,
            token=settings["token"],
            caption=caption,
            api_base=settings["api_base"],
            run_date=run_date,
            edition=args.edition,
            state_file=args.state,
            filename=args.filename,
            parse_mode=settings["caption_parse_mode"] or None,
            force=args.force,
            dry_run=args.dry_run,
            delay_seconds=settings["send_delay_seconds"],
            retries=settings["max_retries"],
            max_caption_chars=settings["caption_max_chars"],
            config=config,
        )
    except (ConfigError, PublishError, PdfCheckError) as exc:
        message = redact(exc, os.environ.get("TELEGRAM_BOT_TOKEN", ""))
        print(f"error: {message}", file=sys.stderr)
        if not args.dry_run:
            admin = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "")
            notify_admin(os.environ.get("TELEGRAM_BOT_TOKEN", ""), admin, f"The Global Brief delivery failed:\n{message}")
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
