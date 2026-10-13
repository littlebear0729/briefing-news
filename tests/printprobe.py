"""Test-only helper: how many A4 pages does this document actually print to?

"About two pages of newspaper" is a claim about the deliverable, so at least one test
measures it rather than trusting the estimator. Printing goes through Playwright's
Chromium when it is installed, otherwise through a local Chrome/Edge — the same two
engines the pipeline used before it went HTML-only. When neither is available the test
that uses this helper is skipped.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
)


class NoBrowser(RuntimeError):
    """No Chromium-family browser is reachable."""


def printed_pages(html_path: str | Path, *, timeout: float = 180.0) -> int:
    """Print *html_path* and return the page count. Raises :class:`NoBrowser`."""
    path = Path(html_path)

    # Playwright first when it happens to be installed (it is not a project dependency
    # any more — nothing in the pipeline prints), then any local Chrome/Edge.
    try:
        return _printed_pages_playwright(path, timeout)
    except ModuleNotFoundError:
        pass
    except Exception:
        pass  # installed without its browser: fall through to a local Chromium

    browser = _chrome()
    if browser is None:
        raise NoBrowser("no Chromium-family browser available for the print check")

    with tempfile.TemporaryDirectory() as scratch:
        pdf = Path(scratch) / "print.pdf"
        profile = Path(scratch) / "profile"
        command = [
            str(browser),
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--no-pdf-header-footer",
            f"--user-data-dir={profile}",
            "--virtual-time-budget=8000",
            f"--print-to-pdf={pdf}",
            path.resolve().as_uri(),
        ]
        subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout, check=False)
        if not pdf.is_file():
            raise NoBrowser(f"{browser.name} produced no PDF for {path.name}")
        data = pdf.read_bytes()
    pages = len(re.findall(rb"/Type\s*/Page(?![s])", data))
    return pages or max((int(value) for value in re.findall(rb"/Count\s+(\d+)", data)), default=0)


def _printed_pages_playwright(path: Path, timeout: float) -> int:
    with tempfile.TemporaryDirectory() as scratch:
        pdf = Path(scratch) / "print.pdf"
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(args=["--no-sandbox"])
            try:
                page = browser.new_page()
                page.emulate_media(media="print")
                page.goto(path.resolve().as_uri(), wait_until="load", timeout=timeout * 1000)
                page.pdf(path=str(pdf), print_background=True)
            finally:
                browser.close()
        data = pdf.read_bytes()
    return len(re.findall(rb"/Type\s*/Page(?![s])", data))


def _chrome() -> Path | None:
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).is_file():
            return Path(candidate)
    for name in ("google-chrome", "chromium", "chromium-browser", "msedge"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None
