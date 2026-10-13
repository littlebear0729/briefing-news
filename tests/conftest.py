"""Test configuration: importable package root + dependency shims.

The suite is written to run in two environments:

* a full one (``pip install -r requirements-dev.txt``), where all dependencies exist;
* a bare one, where ``feedparser`` may be missing. Rather than skipping the feed
  tests there, a documented test double is installed *only* when the real parser is
  absent, so the window/dedupe/whitelist logic is still exercised. The double is a
  test fixture, never a production fallback — ``pipeline.fetch`` raises a clear error
  when feedparser is missing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_feedparser_double() -> bool:
    try:
        import feedparser  # noqa: F401
    except ModuleNotFoundError:
        from tests import fake_feedparser

        sys.modules["feedparser"] = fake_feedparser
        return True
    return False


USING_FAKE_FEEDPARSER = _install_feedparser_double()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail loudly if a test reaches the network.

    The suite must be deterministic and runnable offline: feed tests hand their bodies
    to ``fetch.collect``, orchestration tests replay a recorded model response, and the
    publish tests stub the Bot API at its single seam. A test that quietly starts
    depending on a live feed is a test that will flake in CI.
    """
    import socket

    allowed = {"127.0.0.1", "::1", "localhost", ""}
    original = socket.socket.connect

    def guarded(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in allowed:
            raise RuntimeError(f"network access attempted during tests: {address!r}")
        return original(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded, raising=False)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def config() -> dict:
    from pipeline.config import load_config

    return load_config()


@pytest.fixture(scope="session")
def fixture_payload() -> dict:
    import json

    return json.loads((ROOT / "build" / "edition-2026-10-13-am.json").read_text(encoding="utf-8"))


@pytest.fixture
def sandbox_config(config: dict, tmp_path: Path) -> dict:
    """Config whose build/dist/state directories live in a temporary directory."""
    import copy

    sandboxed = copy.deepcopy(config)
    sandboxed["paths"] = dict(sandboxed.get("paths") or {})
    for key in ("build_dir", "dist_dir", "state_dir"):
        sandboxed["paths"][key] = str(tmp_path / key)
    sandboxed["paths"]["template_dir"] = str(ROOT / "templates")
    return sandboxed
