"""Configuration: dates, editions, windows, numbering, environment overrides."""

from __future__ import annotations

import datetime as dt

import pytest

from pipeline import config as config_module
from pipeline.config import (
    ConfigError,
    artifact_stem,
    chat_ids,
    cutoff_datetime,
    edition_number,
    env_or,
    load_config,
    normalize_date,
    normalize_edition,
    paths,
    resolve_model_settings,
    section_ids,
    telegram_settings,
    window,
)


def test_config_has_the_documented_sections(config: dict) -> None:
    assert section_ids(config) == ["geopolitics", "tech", "finance", "sport", "entertainment"]
    assert set(config["editions"]) == {"AM", "PM"}
    for section, feeds in config["feeds"].items():
        assert feeds, f"{section} has no feeds"
        for feed in feeds:
            assert feed["url"].startswith("http"), feed


def test_missing_config_file_is_an_error(tmp_path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml")


def test_normalize_edition_and_date() -> None:
    assert normalize_edition("am") == "AM"
    assert normalize_edition("PM") == "PM"
    with pytest.raises(ConfigError):
        normalize_edition("noon")
    assert normalize_date("2026-10-13") == dt.date(2026, 10, 13)
    with pytest.raises(ConfigError):
        normalize_date("13/10/2026")


def test_window_is_the_lookback_before_the_cutoff(config: dict) -> None:
    start, end = window(config, "2026-10-13", "AM")
    assert end == dt.datetime(2026, 10, 13, 1, 15, tzinfo=dt.timezone.utc)
    assert start == end - dt.timedelta(hours=config["lookback_hours"])
    assert cutoff_datetime(config, "2026-10-13", "PM").hour == 13


def test_edition_number_counts_am_then_pm(config: dict) -> None:
    first = edition_number(config, "2026-01-01", "AM")
    assert first == 1
    assert edition_number(config, "2026-01-01", "PM") == 2
    assert edition_number(config, "2026-01-02", "AM") == 3


def test_artifact_stem_matches_the_documented_filenames(config: dict) -> None:
    assert artifact_stem("2026-10-13", "AM") == "2026-10-13-am"
    assert artifact_stem(dt.date(2026, 10, 13), "pm") == "2026-10-13-pm"


def test_env_override_and_empty_string_fallback(monkeypatch) -> None:
    """An empty variable must fall back to the config default (Actions sets "")."""
    config = {"model": {"base_url": "https://default/v1", "name": "default-model", "retries": 3, "temperature": 0.1}}
    monkeypatch.setenv("BRIEF_LLM_BASE_URL", "")
    monkeypatch.setenv("BRIEF_LLM_MODEL", "override-model")
    settings = resolve_model_settings(config)
    assert settings["base_url"] == "https://default/v1"
    assert settings["model"] == "override-model"
    monkeypatch.setenv("BRIEF_LLM_RETRIES", "not-a-number")
    with pytest.raises(ConfigError):
        resolve_model_settings(config)


def test_env_or_ignores_blank_values(monkeypatch) -> None:
    monkeypatch.setenv("SOME_BLANK", "   ")
    assert env_or("fallback", "SOME_BLANK") == "fallback"
    monkeypatch.setenv("SOME_SET", " value ")
    assert env_or("fallback", "SOME_SET") == "value"


def test_chat_ids_parses_env_and_deduplicates(monkeypatch, config: dict) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123, -100456 123")
    assert chat_ids(config) == ["123", "-100456"]
    monkeypatch.delenv("TELEGRAM_CHAT_ID")
    assert chat_ids({"telegram": {"chat_ids": ["7", "7", "8"]}}) == ["7", "8"]


def test_telegram_settings_filename_template(config: dict) -> None:
    settings = telegram_settings(config)
    assert settings["filename_template"].format(date="2026-10-13", edition="AM") == (
        "The-Global-Brief-2026-10-13-AM.pdf"
    )


def test_paths_resolve_absolute_overrides(config: dict, tmp_path) -> None:
    absolute = dict(config)
    absolute["paths"] = {"dist_dir": str(tmp_path / "elsewhere")}
    assert paths(absolute)["dist_dir"] == tmp_path / "elsewhere"


def test_repo_root_is_the_package_parent() -> None:
    assert (config_module.REPO_ROOT / "pipeline" / "config.yaml").is_file()
