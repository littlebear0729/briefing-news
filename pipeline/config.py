"""Load and interpret ``pipeline/config.yaml``.

Kept separate from :mod:`pipeline.fetch` so that rendering and publishing never
import feedparser: every stage needs configuration, only one stage needs feeds.

Also the single place where environment variables are resolved (DESIGN.md §5.4).
An **empty** environment value falls back to the default in ``config.yaml`` —
that is deliberate, because GitHub Actions renders unset variables as ``""``.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "ConfigError",
    "DEFAULT_CONFIG_PATH",
    "REPO_ROOT",
    "artifact_stem",
    "chat_ids",
    "cutoff_datetime",
    "edition_label",
    "edition_number",
    "env_or",
    "expected_pages",
    "footer_text",
    "id_prefix",
    "layout",
    "load_config",
    "lookback_hours",
    "max_pdf_bytes",
    "normalize_date",
    "normalize_edition",
    "paths",
    "resolve_model_settings",
    "section_ids",
    "section_title",
    "telegram_settings",
    "window",
]

#: Repository root (the directory containing ``pipeline/``).
REPO_ROOT: Path = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH: Path = Path(__file__).resolve().parent / "config.yaml"

#: Edition names, in the order they are cut each day.
EDITIONS: tuple[str, str] = ("AM", "PM")


class ConfigError(RuntimeError):
    """Configuration is missing, unreadable or nonsensical."""


def load_config(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Parse ``pipeline/config.yaml`` (or *path*) into a dict.

    PyYAML is a hard runtime dependency: without it the pipeline cannot know which
    feeds to read, so a missing import is a configuration error, never a silent
    fallback to hand-parsed YAML.
    """
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    try:
        import yaml  # imported here so the error message is actionable
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on environment
        raise ConfigError(
            "PyYAML is not installed, so pipeline/config.yaml cannot be read. "
            "Run: pip install -r requirements.txt"
        ) from exc

    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except Exception as exc:  # yaml.YAMLError and friends
        raise ConfigError(f"could not parse {config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{config_path} must contain a mapping at the top level")

    for required in ("editions", "budgets", "model", "feeds"):
        if required not in data:
            raise ConfigError(f"{config_path} is missing the '{required}' section")
    return data


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


def env_or(default: Any, *names: str) -> Any:
    """First non-empty environment variable among *names*, else *default*.

    Keeps the documented behaviour that an empty string means "use the default".
    """
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip() != "":
            return value.strip()
    return default


def resolve_model_settings(config: Mapping[str, Any]) -> dict[str, Any]:
    """Merge ``config['model']`` with the ``BRIEF_LLM_*`` environment overrides."""
    model_cfg = dict(config.get("model") or {})
    retries_raw = env_or(model_cfg.get("retries", 3), "BRIEF_LLM_RETRIES")
    temperature_raw = env_or(model_cfg.get("temperature", 0.1), "BRIEF_LLM_TEMPERATURE")
    try:
        retries = max(1, int(retries_raw))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"BRIEF_LLM_RETRIES must be an integer, got {retries_raw!r}") from exc
    try:
        temperature = float(temperature_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"BRIEF_LLM_TEMPERATURE must be a number, got {temperature_raw!r}"
        ) from exc

    return {
        "base_url": str(env_or(model_cfg.get("base_url", ""), "BRIEF_LLM_BASE_URL")),
        "model": str(env_or(model_cfg.get("name", ""), "BRIEF_LLM_MODEL")),
        "api_key": env_or("", "BRIEF_LLM_API_KEY"),
        "retries": retries,
        "temperature": temperature,
        "timeout_seconds": float(model_cfg.get("timeout_seconds", 120)),
        "json_mode": bool(model_cfg.get("json_mode", True)),
        "max_summary_chars_in_prompt": int(model_cfg.get("max_summary_chars_in_prompt", 600)),
    }


# ---------------------------------------------------------------------------
# Dates, editions, numbering
# ---------------------------------------------------------------------------


def normalize_date(value: dt.date | dt.datetime | str | None) -> dt.date:
    """Coerce *value* (``None`` = today in UTC, ISO string, date or datetime) to a date."""
    if value is None:
        return dt.datetime.now(dt.timezone.utc).date()
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.timezone.utc).date() if value.tzinfo else value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ConfigError(f"invalid date {value!r}; expected YYYY-MM-DD") from exc


def normalize_edition(value: str | None) -> str:
    """Return ``"AM"`` or ``"PM"`` (case-insensitive input)."""
    edition = str(value or "").strip().upper()
    if edition not in EDITIONS:
        raise ConfigError(f"invalid edition {value!r}; expected one of {', '.join(EDITIONS)}")
    return edition


def section_ids(config: Mapping[str, Any]) -> list[str]:
    """Sections in feed order (geopolitics first: it owns the front page)."""
    feeds = config["feeds"]
    if not isinstance(feeds, Mapping) or not feeds:
        raise ConfigError("'feeds' must be a non-empty mapping of section -> feed list")
    return list(feeds)


def section_title(config: Mapping[str, Any], section: str) -> str:
    """Human-readable section name for page furniture."""
    titles = config.get("section_titles") or {}
    return str(titles.get(section) or section.replace("_", " ").title())


def id_prefix(section: str) -> str:
    """Candidate id prefix: first three letters, upper case (``geopolitics`` -> ``GEO``)."""
    letters = "".join(ch for ch in section if ch.isalpha())
    return letters[:3].upper()


def edition_label(config: Mapping[str, Any], edition: str) -> str:
    """``config['editions'][edition]['label']`` with a sane fallback."""
    editions = config.get("editions") or {}
    entry = editions.get(edition) or editions.get(edition.upper()) or {}
    if isinstance(entry, Mapping) and entry.get("label"):
        return str(entry["label"])
    return f"{edition.upper()} edition"


def _cutoff_time(config: Mapping[str, Any], edition: str) -> dt.time:
    editions = config.get("editions") or {}
    entry = editions.get(edition) or editions.get(edition.upper())
    if not isinstance(entry, Mapping) or "cutoff_utc" not in entry:
        raise ConfigError(f"config.editions.{edition}.cutoff_utc is missing")
    raw = str(entry["cutoff_utc"]).strip()
    try:
        hour, _, minute = raw.partition(":")
        return dt.time(hour=int(hour), minute=int(minute or 0), tzinfo=dt.timezone.utc)
    except ValueError as exc:
        raise ConfigError(f"invalid cutoff_utc {raw!r} for edition {edition}") from exc


def cutoff_datetime(
    config: Mapping[str, Any], run_date: dt.date | str, edition: str
) -> dt.datetime:
    """Aware UTC datetime at which *edition* closes on *run_date*."""
    return dt.datetime.combine(normalize_date(run_date), _cutoff_time(config, edition))


def lookback_hours(config: Mapping[str, Any], edition: str) -> float:
    """Window length in hours; a per-edition ``lookback_hours`` wins over the global one."""
    editions = config.get("editions") or {}
    entry = editions.get(edition) or editions.get(edition.upper()) or {}
    if isinstance(entry, Mapping) and entry.get("lookback_hours") is not None:
        return float(entry["lookback_hours"])
    return float(config.get("lookback_hours", 12))


def window(
    config: Mapping[str, Any], run_date: dt.date | str, edition: str
) -> tuple[dt.datetime, dt.datetime]:
    """Inclusive-end candidate window ``(start, end)`` in UTC."""
    end = cutoff_datetime(config, run_date, edition)
    start = end - dt.timedelta(hours=lookback_hours(config, edition))
    return start, end


def edition_number(
    config: Mapping[str, Any], run_date: dt.date | str, edition: str
) -> int:
    """Sequential issue number: AM of the epoch date is #1, its PM is #2, and so on."""
    layout = config.get("layout") or {}
    epoch = normalize_date(layout.get("edition_epoch") or "2026-01-01")
    day_offset = (normalize_date(run_date) - epoch).days
    return day_offset * len(EDITIONS) + EDITIONS.index(normalize_edition(edition)) + 1


def artifact_stem(run_date: dt.date | str, edition: str) -> str:
    """``("<date>", "<edition>")`` as the shared artefact stem, e.g. ``2026-10-13-am``."""
    return f"{normalize_date(run_date).isoformat()}-{normalize_edition(edition).lower()}"


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def paths(config: Mapping[str, Any], root: str | os.PathLike[str] | None = None) -> dict[str, Path]:
    """Resolve ``config['paths']`` against the repository root (created on demand)."""
    base = Path(root).resolve() if root else REPO_ROOT
    configured = config.get("paths") or {}
    resolved = {
        "root": base,
        "build_dir": base / str(configured.get("build_dir", "build")),
        "dist_dir": base / str(configured.get("dist_dir", "dist")),
        "template_dir": base / str(configured.get("template_dir", "templates")),
        "asset_dir": base / str(configured.get("asset_dir", "assets")),
        "state_dir": base / str(configured.get("state_dir", "state")),
    }
    return resolved


# ---------------------------------------------------------------------------
# Layout and delivery accessors
# ---------------------------------------------------------------------------


def layout(config: Mapping[str, Any]) -> Mapping[str, Any]:
    """The ``layout`` mapping (templates, footer text, page count, size limit)."""
    value = config.get("layout")
    return value if isinstance(value, Mapping) else {}


def footer_text(config: Mapping[str, Any]) -> str:
    """The fixed footer string printed on every page.

    ``export.assert_pdf`` asserts this text is present on *each* page, which is how
    silent clipping by ``overflow: hidden`` is caught. It is owned by config so the
    template and the assertion can never drift apart.
    """
    return str(layout(config).get("footer_text") or "THE GLOBAL BRIEF | WIRE EDITION")


def expected_pages(config: Mapping[str, Any]) -> int:
    """Number of pages every artefact must print to (exactly two, by design)."""
    return int(layout(config).get("page_count", 2))


def max_pdf_bytes(config: Mapping[str, Any]) -> int:
    """Hard size ceiling for the outgoing document (Telegram's sendDocument limit)."""
    return int(layout(config).get("max_pdf_bytes", 52428800))


def telegram_settings(config: Mapping[str, Any]) -> dict[str, Any]:
    """Merge the ``telegram`` section with the ``TELEGRAM_*`` environment variables."""
    telegram = dict(config.get("telegram") or {})
    return {
        "api_base": str(env_or(telegram.get("api_base", "https://api.telegram.org"), "TELEGRAM_API_BASE")).rstrip("/"),
        "token": str(env_or("", "TELEGRAM_BOT_TOKEN")),
        "chat_ids": chat_ids(config),
        "admin_chat_id": str(env_or("", "TELEGRAM_ADMIN_CHAT_ID")),
        "caption_max_chars": int(telegram.get("caption_max_chars", 1024)),
        "caption_parse_mode": str(telegram.get("caption_parse_mode", "HTML")),
        "filename_template": str(
            telegram.get("filename_template", "The-Global-Brief-{date}-{edition}.pdf")
        ),
        "send_delay_seconds": float(telegram.get("send_delay_seconds", 0.6)),
        "max_retries": int(telegram.get("max_retries", 3)),
    }


def chat_ids(config: Mapping[str, Any]) -> list[str]:
    """Recipients: ``TELEGRAM_CHAT_ID`` (comma/space separated) wins over config."""
    raw = env_or("", "TELEGRAM_CHAT_ID")
    candidates: list[str] = []
    if raw:
        candidates = [item.strip() for item in str(raw).replace(",", " ").split()]
    else:
        configured = (config.get("telegram") or {}).get("chat_ids") or []
        candidates = [str(item).strip() for item in configured]
    seen: list[str] = []
    for chat_id in candidates:
        if chat_id and chat_id not in seen:
            seen.append(chat_id)
    return seen
