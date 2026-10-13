"""Telegram delivery: idempotency, retries, rate limits, caption, token safety."""

from __future__ import annotations

import json

import pytest

from pipeline import publish


class FakeBot:
    """Stands in for the Bot API at its single seam: ``pipeline.publish._call``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, dict | None]] = []
        self.uploads: list[int] = []

    def __call__(self, token, api_base, method, *, data=None, files=None, timeout=None):
        data = dict(data or {})
        self.calls.append((method, data, files))
        if method == "getMe":
            return {"ok": True, "result": {"id": 7, "username": "brief_bot"}}
        if method == "getChat":
            return {"ok": True, "result": {"id": data["chat_id"], "type": "private", "first_name": "Reader"}}
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": 1}}
        if method == "sendDocument":
            filename, handle = files["document"][0], files["document"][1]
            payload = handle.read()
            assert payload, "the uploaded document was empty (a reused file handle?)"
            self.uploads.append(len(payload))
            return {
                "ok": True,
                "result": {
                    "message_id": 4242,
                    "document": {"file_id": "FILE", "file_unique_id": "UNIQ", "file_name": filename},
                },
            }
        raise AssertionError(f"unexpected method {method}")

    @property
    def methods(self) -> list[str]:
        return [method for method, _data, _files in self.calls]

    def documents(self) -> list[dict]:
        return [data for method, data, _files in self.calls if method == "sendDocument"]


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "The-Global-Brief-2026-10-13-AM.pdf"
    path.write_bytes(b"%PDF-1.4\n" + b"x" * 2048)
    return path


@pytest.fixture
def ledger(tmp_path):
    return tmp_path / "state" / "sent.json"


def test_publish_sends_records_state_and_preflights(monkeypatch, pdf, ledger, config) -> None:
    bot = FakeBot()
    monkeypatch.setattr(publish, "_call", bot)

    report = publish.publish(
        pdf,
        ["123", "-100456"],
        token="tok",
        caption="caption",
        run_date="2026-10-13",
        edition="AM",
        state_file=ledger,
        delay_seconds=0,
        config=config,
    )

    assert report["key"] == "2026-10-13-AM"
    assert report["filename"] == "The-Global-Brief-2026-10-13-AM.pdf"
    assert [entry["chat_id"] for entry in report["sent"]] == ["123", "-100456"]
    assert bot.methods.count("getMe") == 1
    assert bot.methods.count("getChat") == 2
    assert bot.methods.count("sendDocument") == 2
    assert all(data["caption"] == "caption" for data in bot.documents())

    state = json.loads(ledger.read_text(encoding="utf-8"))
    record = state["entries"]["2026-10-13-AM"]["chats"]["123"]
    assert record["message_id"] == 4242
    assert record["file_id"] == "FILE"
    assert record["bytes"] == pdf.stat().st_size
    assert len(record["sha256"]) == 64


def test_publish_is_idempotent_across_runs(monkeypatch, pdf, ledger, config, capsys) -> None:
    bot = FakeBot()
    monkeypatch.setattr(publish, "_call", bot)
    kwargs = dict(token="tok", run_date="2026-10-13", edition="AM", state_file=ledger, delay_seconds=0, config=config)

    first = publish.publish(pdf, ["123"], **kwargs)
    second = publish.publish(pdf, ["123"], **kwargs)

    assert len(first["sent"]) == 1 and not first["skipped"]
    assert not second["sent"] and len(second["skipped"]) == 1
    assert bot.methods.count("sendDocument") == 1, "the second run must not re-send"
    assert "already delivered" in capsys.readouterr().out


def test_publish_force_resends(monkeypatch, pdf, ledger, config) -> None:
    bot = FakeBot()
    monkeypatch.setattr(publish, "_call", bot)
    kwargs = dict(token="tok", run_date="2026-10-13", edition="AM", state_file=ledger, delay_seconds=0, config=config)

    publish.publish(pdf, ["123"], **kwargs)
    forced = publish.publish(pdf, ["123"], force=True, **kwargs)

    assert len(forced["sent"]) == 1
    assert bot.methods.count("sendDocument") == 2


def test_partial_failure_keeps_the_delivered_chat_recorded(monkeypatch, pdf, ledger, config) -> None:
    bot = FakeBot()
    original = bot.__call__

    def flaky(token, api_base, method, *, data=None, files=None, timeout=None):
        if method == "sendDocument" and str((data or {}).get("chat_id")) == "second":
            raise publish.PublishError("sendDocument: HTTP 400: chat not found")
        return original(token, api_base, method, data=data, files=files, timeout=timeout)

    monkeypatch.setattr(publish, "_call", flaky)
    with pytest.raises(publish.PublishError):
        publish.publish(
            pdf,
            ["first", "second"],
            token="tok",
            run_date="2026-10-13",
            edition="AM",
            state_file=ledger,
            delay_seconds=0,
            config=config,
        )

    state = json.loads(ledger.read_text(encoding="utf-8"))
    chats = state["entries"]["2026-10-13-AM"]["chats"]
    assert "first" in chats and "second" not in chats

    # a re-run only retries the chat that is missing
    bot2 = FakeBot()
    monkeypatch.setattr(publish, "_call", bot2)
    report = publish.publish(
        pdf,
        ["first", "second"],
        token="tok",
        run_date="2026-10-13",
        edition="AM",
        state_file=ledger,
        delay_seconds=0,
        config=config,
    )
    assert [entry["chat_id"] for entry in report["sent"]] == ["second"]


def test_rate_limit_backs_off_for_the_requested_delay(monkeypatch, pdf, config, tmp_path) -> None:
    slept: list[float] = []
    monkeypatch.setattr(publish.time, "sleep", lambda seconds: slept.append(seconds))
    attempts = {"n": 0}
    captured: list[bytes] = []

    def flaky(token, api_base, method, *, data=None, files=None, timeout=None):
        if method != "sendDocument":
            return {"ok": True, "result": {}}
        filename, handle = files["document"][0], files["document"][1]
        captured.append(handle.read())
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise publish._RateLimited(2.5, "rate limited")
        return {"ok": True, "result": {"message_id": 1, "document": {"file_name": filename}}}

    monkeypatch.setattr(publish, "_call", flaky)
    response = publish.send_document("tok", "https://api.telegram.org", "123", pdf, filename="x.pdf", retries=3)

    assert response["ok"] and attempts["n"] == 2
    assert slept and slept[0] >= 2.5, f"expected to honour retry_after, slept {slept}"
    assert all(len(blob) > 0 for blob in captured), "each retry must upload a fresh, complete file"


def test_retryable_transport_errors_are_retried_and_400_is_not(monkeypatch, pdf) -> None:
    slept: list[float] = []
    monkeypatch.setattr(publish.time, "sleep", lambda seconds: slept.append(seconds))
    attempts = {"n": 0}

    def flaky(token, api_base, method, *, data=None, files=None, timeout=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise publish.PublishError("sendDocument: HTTP 502: bad gateway")
        return {"ok": True, "result": {"message_id": 3, "document": {}}}

    monkeypatch.setattr(publish, "_call", flaky)
    publish.send_document("tok", "https://api.telegram.org", "1", pdf, filename="x.pdf", retries=3)
    assert attempts["n"] == 2 and slept

    def chat_not_found(token, api_base, method, *, data=None, files=None, timeout=None):
        raise publish.PublishError("sendDocument: HTTP 400: chat not found (check TELEGRAM_CHAT_ID)")

    monkeypatch.setattr(publish, "_call", chat_not_found)
    with pytest.raises(publish.PublishError):
        publish.send_document("tok", "https://api.telegram.org", "1", pdf, filename="x.pdf", retries=3)


def test_call_redacts_the_token_from_errors(monkeypatch) -> None:
    token = "123456789:AAF-secret-token-value"

    class Response:
        status_code = 401
        headers: dict[str, str] = {}
        text = f'{{"ok":false,"description":"Unauthorized for bot{token}"}}'

        def json(self):
            return {"ok": False, "description": f"Unauthorized for bot{token}"}

    class Requests:
        @staticmethod
        def post(url, data=None, files=None, timeout=None):
            assert token in url, "the token belongs in the URL"
            return Response()

    monkeypatch.setattr(publish, "_requests", lambda: Requests)
    with pytest.raises(publish.PublishError) as excinfo:
        publish._call(token, "https://api.telegram.org", "getMe")

    message = str(excinfo.value)
    assert token not in message, "the token leaked into an error message"
    assert "***redacted***" in message
    assert "HTTP 401" in message and "TELEGRAM_BOT_TOKEN" in message


def test_publish_requires_recipients(pdf, ledger, config) -> None:
    with pytest.raises(publish.PublishError) as excinfo:
        publish.publish(pdf, [], token="tok", run_date="2026-10-13", edition="AM", state_file=ledger, config=config)
    assert "TELEGRAM_CHAT_ID" in str(excinfo.value)


def test_publish_requires_an_existing_pdf(ledger, config, tmp_path) -> None:
    with pytest.raises(publish.PublishError):
        publish.publish(tmp_path / "nope.pdf", ["1"], token="tok", state_file=ledger, config=config)


def test_dry_run_sends_nothing(monkeypatch, pdf, ledger, config) -> None:
    def explode(*args, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("dry run must not touch the network")

    monkeypatch.setattr(publish, "_call", explode)
    report = publish.publish(
        pdf, ["123"], token="tok", run_date="2026-10-13", edition="AM", state_file=ledger, dry_run=True, config=config
    )
    assert report["dry_run"] is True and report["sent"] and not ledger.exists()


def test_caption_is_bounded_and_escaped() -> None:
    caption = publish.build_caption(
        edition_label="Morning Edition",
        date_long="Tuesday, 13 October 2026",
        issue_number=571,
        lead_headline="Truce & talks <hold>",
        stories=13,
        sources_cited=22,
        pages=2,
    )
    assert "&amp;" in caption and "&lt;hold&gt;" in caption
    assert len(caption) <= 1024
    assert "13 stories" in caption

    long_caption = publish.build_caption(
        edition_label="Morning Edition",
        date_long="Tuesday, 13 October 2026",
        lead_headline="word " * 400,
        max_chars=120,
    )
    assert len(long_caption) == 120 and long_caption.endswith("…")


def test_caption_from_edition_uses_the_same_metadata(config: dict, repo_root) -> None:
    caption = publish.caption_from_edition(
        repo_root / "build" / "edition-2026-10-13-am.json", config=config, run_date="2026-10-13", edition="AM"
    )
    assert "The Global Brief" in caption
    assert "Truce holds in Kestrel corridor" in caption
    assert "No. 571" in caption  # issue number for 2026-10-13 AM
    assert len(caption) <= config["telegram"]["caption_max_chars"]


def test_state_ledger_rejects_corrupt_files(tmp_path) -> None:
    corrupt = tmp_path / "sent.json"
    corrupt.write_text("{not json", encoding="utf-8")
    with pytest.raises(publish.PublishError) as excinfo:
        publish.load_state(corrupt)
    assert "refusing to guess" in str(excinfo.value)


def test_state_key_format() -> None:
    assert publish.state_key("2026-10-13", "pm") == "2026-10-13-PM"
    assert publish.already_sent({"entries": {}}, "2026-10-13-PM", "1") is None
