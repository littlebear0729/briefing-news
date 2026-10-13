"""HTML -> PDF, and the assertions that must pass before anything is sent.

    python -m pipeline.export dist/mobile-2026-10-13-am.html
    python -m pipeline.export --check dist/mobile-2026-10-13-am.pdf

Engine: Playwright/Chromium in print emulation (``emulate_media("print")``), which is
what the templates are written for. If Playwright is unavailable, a local Chrome/Edge
is driven with ``--headless --print-to-pdf`` instead — the same Chromium print pipeline,
useful for local runs and for CI images without a browser download. Set
``BRIEF_PDF_ENGINE=playwright|chrome|auto`` to force one.

The assertions exist because ``.page { overflow: hidden }`` guarantees "exactly two
pages" by silently clipping whatever does not fit (DESIGN.md §6C). Text extraction is
therefore a *correctness* check, not a nicety: if the fixed footer is missing from a
page, that page lost content.

pypdf is used for extraction when installed (full, spec-compliant). Without it a small
built-in reader handles Chromium-written PDFs (FlateDecode content streams + ToUnicode
CMaps), which keeps the assertions testable in a bare environment.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path
from typing import Any, Mapping

from .config import (
    ConfigError,
    expected_pages,
    footer_text,
    load_config,
    max_pdf_bytes,
    paths,
)

__all__ = [
    "PdfCheckError",
    "ExportError",
    "assert_pdf",
    "content_snippets",
    "extract_text",
    "page_count",
    "to_pdf",
]

DEFAULT_TIMEOUT = 180.0


class ExportError(RuntimeError):
    """The PDF could not be produced."""


class PdfCheckError(RuntimeError):
    """A produced PDF failed an assertion and must not be sent."""


# ---------------------------------------------------------------------------
# HTML -> PDF
# ---------------------------------------------------------------------------


def _chrome_candidates() -> list[Path]:
    """Locally installed Chromium-family browsers, best first."""
    override = os.environ.get("BRIEF_CHROME_PATH")
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override))
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "msedge"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    candidates += [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        Path("/usr/bin/google-chrome"),
        Path("/usr/bin/chromium"),
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    ]
    return [candidate for candidate in candidates if candidate.is_file()]


def _to_pdf_playwright(html_path: Path, pdf_path: Path, timeout: float) -> None:
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
    except ModuleNotFoundError as exc:
        raise ExportError("playwright is not installed") from exc

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            page = browser.new_page()
            page.emulate_media(media="print")
            page.goto(html_path.resolve().as_uri(), wait_until="load", timeout=timeout * 1000)
            page.pdf(
                path=str(pdf_path),
                format="A4",
                print_background=True,
                prefer_css_page_size=True,
                margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
            )
        finally:
            browser.close()


def _to_pdf_chrome(html_path: Path, pdf_path: Path, timeout: float) -> None:
    """Drive a locally installed Chrome/Edge. Fallback engine; same Chromium print path."""
    browsers = _chrome_candidates()
    if not browsers:
        raise ExportError(
            "no Chromium-family browser found; install Playwright browsers "
            "(python -m playwright install chromium) or set BRIEF_CHROME_PATH"
        )
    browser = browsers[0]
    # Chrome's own stdio goes to a file rather than a pipe (a confined environment
    # forbids pipes first), and both its profile and its log live in one scratch
    # directory well away from dist/. Cleanup is best-effort on purpose: a browser
    # that has just exited can still hold its profile files on Windows, and a locked
    # scratch directory must never turn a successful print into a failure.
    scratch = Path(tempfile.mkdtemp(prefix="global-brief-chrome-"))
    log_path = scratch / "chrome.log"
    command = [
        str(browser),
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--no-pdf-header-footer",
        "--print-to-pdf-no-header",
        f"--user-data-dir={scratch / 'profile'}",
        "--virtual-time-budget=15000",
        f"--print-to-pdf={pdf_path.resolve()}",
        html_path.resolve().as_uri(),
    ]
    try:
        try:
            with log_path.open("wb") as log:
                completed = subprocess.run(
                    command, stdout=log, stderr=subprocess.STDOUT, timeout=timeout, check=False
                )
        except subprocess.TimeoutExpired as exc:
            raise ExportError(
                f"{browser.name} did not finish printing {html_path.name} within {timeout:g}s"
            ) from exc

        if completed.returncode == 0 and pdf_path.is_file() and pdf_path.stat().st_size > 0:
            return

        try:
            log_text = log_path.read_text(encoding="utf-8", errors="replace")[-1500:]
        except OSError:  # pragma: no cover - the log is best effort
            log_text = "(no browser log captured)"
        raise ExportError(
            f"{browser.name} exited with {completed.returncode} without writing {pdf_path}\n{log_text}"
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def to_pdf(html_path: str | Path, pdf_path: str | Path | None = None) -> Path:
    """Print *html_path* to PDF, returning the PDF path (DESIGN.md §4)."""
    source = Path(html_path)
    if not source.is_file():
        raise ExportError(f"html file not found: {source}")
    destination = Path(pdf_path) if pdf_path else source.with_suffix(".pdf")
    destination.parent.mkdir(parents=True, exist_ok=True)

    engine = (os.environ.get("BRIEF_PDF_ENGINE") or "auto").strip().lower()
    timeout = float(os.environ.get("BRIEF_PDF_TIMEOUT_SECONDS") or DEFAULT_TIMEOUT)
    errors: list[str] = []

    attempts: list[tuple[str, Any]] = []
    if engine in {"auto", "playwright"}:
        attempts.append(("playwright", _to_pdf_playwright))
    if engine in {"auto", "chrome"}:
        attempts.append(("chrome", _to_pdf_chrome))
    if not attempts:
        raise ExportError(f"BRIEF_PDF_ENGINE must be auto, playwright or chrome (got {engine!r})")

    for name, printer in attempts:
        try:
            printer(source, destination, timeout)
        except Exception as exc:  # ExportError, Playwright errors, timeouts
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        if destination.is_file() and destination.stat().st_size > 0:
            print(f"printed {source.name} -> {destination} ({destination.stat().st_size:,} bytes, {name})")
            return destination
        errors.append(f"{name}: produced no output")

    raise ExportError("PDF export failed:\n  " + "\n  ".join(errors))


# ---------------------------------------------------------------------------
# Reading back what was printed
# ---------------------------------------------------------------------------


def page_count(pdf_path: str | Path) -> int:
    """Number of pages, via pypdf when available and a built-in reader otherwise."""
    path = Path(pdf_path)
    data = path.read_bytes()
    try:
        from pypdf import PdfReader  # noqa: PLC0415

        return len(PdfReader(str(path)).pages)
    except ModuleNotFoundError:
        return _simple_page_count(data)


def extract_text(pdf_path: str | Path) -> list[str]:
    """Extracted text, one entry per page (order = page order)."""
    path = Path(pdf_path)
    data = path.read_bytes()
    try:
        from pypdf import PdfReader  # noqa: PLC0415

        return [(page.extract_text() or "") for page in PdfReader(str(path)).pages]
    except ModuleNotFoundError:
        return _simple_extract_text(data)


def _normalise(text: str) -> str:
    """Comparison key: letters and digits only, upper case.

    Text extraction loses typography: word spacing becomes unreliable, and subset
    fonts can drop punctuation such as the pipe in the footer. Comparing on
    alphanumerics keeps the assertion about *content presence* (which is what the
    clipping check needs) instead of about glyph fidelity.
    """
    return re.sub(r"[^0-9A-Za-z]", "", text or "").upper()


def assert_pdf(
    pdf_path: str | Path,
    *,
    config: Mapping[str, Any] | None = None,
    expected_page_total: int | None = None,
    required_text: str | None = None,
    required_snippets: Iterable[str] | None = None,
    size_limit_bytes: int | None = None,
) -> dict[str, Any]:
    """Assert page count, per-page footer presence, content completeness and size.

    *required_text* (the footer) must appear on **every** page; *required_snippets*
    (typically the closing words of each section, built by :func:`content_snippets`)
    must appear somewhere in the document. The snippet check is what actually proves
    nothing was clipped: a fixed page box hides lost content from every layout
    measurement, but not from the text of the printed file.

    Raises :class:`PdfCheckError` — the caller must treat that as "do not send".
    """
    path = Path(pdf_path)
    if not path.is_file():
        raise PdfCheckError(f"pdf not found: {path}")

    config = config or load_config()
    want_pages = int(expected_page_total if expected_page_total is not None else expected_pages(config))
    want_footer = required_text if required_text is not None else footer_text(config)
    limit = int(size_limit_bytes if size_limit_bytes is not None else max_pdf_bytes(config))

    size = path.stat().st_size
    if size == 0:
        raise PdfCheckError(f"{path} is empty")
    if size > limit:
        raise PdfCheckError(
            f"{path} is {size:,} bytes, over the {limit:,}-byte limit (Telegram sendDocument)"
        )

    pages = page_count(path)
    if pages != want_pages:
        raise PdfCheckError(
            f"{path} printed {pages} page(s), expected exactly {want_pages}. "
            "Content overflowed a fixed page box, or a page box collapsed."
        )

    texts = extract_text(path)
    if len(texts) != pages:
        raise PdfCheckError(f"text extraction returned {len(texts)} pages for a {pages}-page file")

    needle = _normalise(want_footer)
    missing = [index + 1 for index, text in enumerate(texts) if needle not in _normalise(text)]
    if missing:
        raise PdfCheckError(
            f"{path}: the footer {want_footer!r} is missing from page(s) {missing}. "
            "A fixed page box clipped its content (overflow: hidden)."
        )

    whole_document = _normalise(" ".join(texts))
    snippets = [snippet for snippet in (required_snippets or []) if str(snippet).strip()]
    absent = [
        snippet
        for snippet in snippets
        if _normalise(snippet) and _normalise(snippet) not in whole_document
    ]
    if absent:
        preview = "; ".join(repr(snippet[:60]) for snippet in absent[:5])
        more = "" if len(absent) <= 5 else f" (+{len(absent) - 5} more)"
        raise PdfCheckError(
            f"{path}: {len(absent)} of {len(snippets)} content snippets are missing from the "
            f"printed pages — content was silently clipped: {preview}{more}"
        )

    page_chars = [len(_normalise(text)) for text in texts]
    thin = [index + 1 for index, count in enumerate(page_chars) if count < 300]
    if thin:
        print(
            f"warning: page(s) {thin} carry very little text ({page_chars}); "
            "check for a collapsed page box or missing sections",
            file=sys.stderr,
        )

    report = {
        "path": str(path),
        "bytes": size,
        "pages": pages,
        "footer": want_footer,
        "footer_on_every_page": True,
        "content_snippets": len(snippets),
        "content_complete": True,
        "page_text_chars": page_chars,
        "extractor": "pypdf" if _has_pypdf() else "builtin",
    }
    print(
        f"pdf ok: {path.name} · {pages} pages · {size:,} bytes · footer on all pages · "
        f"{len(snippets)} content snippets present · text per page {page_chars} ({report['extractor']})"
    )
    return report


def content_snippets(edition: Mapping[str, Any], *, words: int = 4) -> list[str]:
    """Closing words of every section of the edition, for the clipping assertion.

    Only the *ends* of each section are collected: if a page box clips, it clips the
    bottom of the flow, so the last lines of each column stack are the canaries.
    Comparison later strips punctuation, spaces and hyphens, which also makes the
    check insensitive to the hyphenation the print stylesheet asks for.
    """
    content = edition.get("content") if isinstance(edition.get("content"), Mapping) else edition
    snippets: list[str] = []

    def tail(text: Any) -> str:
        parts = str(text or "").split()
        return " ".join(parts[-words:]) if parts else ""

    tech = (content.get("tech") or {}).get("stories") or []
    money = content.get("money") or {}
    sport = content.get("sport") or {}
    culture = content.get("culture") or []

    for label, story in (
        ("lead", content.get("lead") or {}),
        ("secondary", (content.get("secondary") or [{}])[-1]),
        ("tech", tech[-1] if tech else {}),
        ("money-lead", money.get("lead") or {}),
        ("money-side", (money.get("side") or [{}])[-1]),
        ("sport", sport.get("lead") or {}),
        ("culture", culture[-1] if culture else {}),
    ):
        paras = [para for para in (story.get("paras") or []) if str(para).strip()]
        if paras:
            snippets.append(tail(paras[-1]))
    if content.get("briefs"):
        snippets.append(tail((content["briefs"][-1] or {}).get("text")))
    markets = content.get("markets") or {}
    if markets.get("note"):
        snippets.append(tail(markets["note"]))
    if sport.get("bullets"):
        snippets.append(tail(sport["bullets"][-1]))
    numbers = money.get("key_numbers") or []
    if numbers:
        snippets.append(tail((numbers[-1] or {}).get("label")))

    # de-duplicate while keeping order, and drop fragments too short to be meaningful
    seen: set[str] = set()
    unique: list[str] = []
    for snippet in snippets:
        key = _normalise(snippet)
        if len(key) >= 8 and key not in seen:
            seen.add(key)
            unique.append(snippet)
    return unique


def _has_pypdf() -> bool:
    try:
        import pypdf  # noqa: F401, PLC0415
    except ModuleNotFoundError:
        return False
    return True


# ---------------------------------------------------------------------------
# Built-in reader for Chromium-written PDFs (no third-party dependency)
# ---------------------------------------------------------------------------

_OBJECT_RE = re.compile(rb"(\d+)\s+(\d+)\s+obj\b(.*?)\bendobj", re.S)
_STREAM_RE = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.S)
_HEXSTRING_RE = re.compile(rb"<([0-9A-Fa-f\s]+)>")


def _objects(data: bytes) -> dict[int, bytes]:
    return {
        int(number): body
        for number, _generation, body in _OBJECT_RE.findall(data)
    }


def _stream_of(body: bytes) -> bytes:
    """Decoded stream payload of an object body (FlateDecode), or b""."""
    match = _STREAM_RE.search(body)
    if not match:
        return b""
    payload = match.group(1)
    if b"/FlateDecode" in body:
        try:
            return zlib.decompress(payload)
        except zlib.error:
            try:
                return zlib.decompressobj().decompress(payload)
            except zlib.error:
                return b""
    return payload


def _simple_page_count(data: bytes) -> int:
    """Count page objects (``/Type /Page``), falling back to the page-tree ``/Count``."""
    pages = len(re.findall(rb"/Type\s*/Page(?![s])", data))
    if pages:
        return pages
    counts = [int(value) for value in re.findall(rb"/Count\s+(\d+)", data)]
    if counts:
        return max(counts)
    raise PdfCheckError(
        "could not determine the page count (compressed object streams?); install pypdf"
    )


def _parse_cmap(text: bytes) -> dict[int, str]:
    """Parse a ToUnicode CMap into ``{code: character}``."""
    mapping: dict[int, str] = {}
    for block in re.findall(rb"beginbfchar(.*?)endbfchar", text, re.S):
        for source, target in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            mapping[int(source, 16)] = _utf16_be(target)
    for block in re.findall(rb"beginbfrange(.*?)endbfrange", text, re.S):
        for low, high, target in re.findall(
            rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block
        ):
            start, end, base = int(low, 16), int(high, 16), int(target, 16)
            for offset, code in enumerate(range(start, end + 1)):
                mapping[code] = chr(base + offset)
    return mapping


def _utf16_be(hex_bytes: bytes) -> str:
    raw = bytes.fromhex(hex_bytes.decode("ascii"))
    if len(raw) % 2:
        raw += b"\x00"
    try:
        return raw.decode("utf-16-be")
    except UnicodeDecodeError:
        return raw.decode("latin-1", "replace")


def _register_fonts(
    scope: bytes, objects: dict[int, bytes], fonts: dict[str, tuple[dict[int, str], int]]
) -> None:
    """Collect ``/Fn -> (ToUnicode cmap, code width)`` for the fonts in *scope*.

    The code width comes from the font's own declaration, not from guessing at the
    cmap: a Type0/Identity-H font addresses glyphs with two bytes per code, and
    reading those as single bytes silently injects NULs into the extracted text.
    """
    for font_name, font_ref in re.findall(rb"/(F\d+)\s+(\d+)\s+0\s+R", scope):
        font = objects.get(int(font_ref), b"")
        unicode_ref = re.search(rb"/ToUnicode\s+(\d+)\s+0\s+R", font)
        if not unicode_ref:
            continue
        cmap = _parse_cmap(_stream_of(objects.get(int(unicode_ref.group(1)), b"")))
        wide = b"/Type0" in font or b"/Identity-H" in font
        fonts[font_name.decode("ascii")] = (cmap, 2 if wide else 1)


def _simple_extract_text(data: bytes) -> list[str]:
    """Text per page for Chromium-written PDFs: content streams + ToUnicode CMaps.

    Deliberately narrow: it understands 2-byte CID codes with a ToUnicode map (what
    Chromium emits for embedded subsets), plus plain literal strings. pypdf remains the
    supported extractor; this only exists so the assertions can run without it.
    """
    objects = _objects(data)
    if not objects:
        raise PdfCheckError(
            "could not read PDF objects (object streams?); install pypdf for extraction"
        )

    page_numbers = [
        number
        for number, body in sorted(objects.items())
        if re.search(rb"/Type\s*/Page(?![s])", body)
    ]
    if not page_numbers:
        raise PdfCheckError("no page objects found; install pypdf for extraction")

    pages: list[str] = []
    for number in page_numbers:
        body = objects[number]
        fonts: dict[str, tuple[dict[int, str], int]] = {}
        _register_fonts(body, objects, fonts)
        resource_ref = re.search(rb"/Resources\s+(\d+)\s+0\s+R", body)
        if resource_ref:
            _register_fonts(objects.get(int(resource_ref.group(1)), b""), objects, fonts)

        content_refs = re.findall(rb"/Contents\s+(\d+)\s+0\s+R", body)
        array_ref = re.search(rb"/Contents\s*\[(.*?)\]", body, re.S)
        if array_ref:
            content_refs += re.findall(rb"(\d+)\s+0\s+R", array_ref.group(1))

        text_parts: list[str] = []
        for content_ref in content_refs:
            stream = _stream_of(objects.get(int(content_ref), b""))
            text_parts.append(_simple_page_text(stream, fonts))
        pages.append(" ".join(text_parts))
    return pages


def _simple_page_text(stream: bytes, fonts: dict[str, tuple[dict[int, str], int]]) -> str:
    current: tuple[dict[int, str], int] = ({}, 1)
    out: list[str] = []
    for token in re.finditer(
        rb"/(F\d+)\s+[\d.]+\s+Tf|<([0-9A-Fa-f\s]*)>\s*Tj|\((.*?)(?<!\\)\)\s*Tj|\[(.*?)\]\s*TJ",
        stream,
        re.S,
    ):
        if token.group(1):
            current = fonts.get(token.group(1).decode("ascii"), ({}, 1))
            continue
        cmap, width = current
        if token.group(2) is not None:
            codes = bytes.fromhex(token.group(2).decode("ascii"))
            out.append(_decode_codes(codes, cmap, width))
        elif token.group(3) is not None:
            out.append(token.group(3).decode("latin-1", "replace"))
        elif token.group(4) is not None:
            for hex_string in _HEXSTRING_RE.findall(token.group(4)):
                codes = bytes.fromhex(hex_string.decode("ascii"))
                out.append(_decode_codes(codes, cmap, width))
    return " ".join(part for part in out if part)


def _decode_codes(codes: bytes, cmap: dict[int, str], width: int) -> str:
    """Decode a PDF string into characters using the font's ToUnicode map."""
    if not codes:
        return ""
    if width == 2:
        return "".join(
            cmap.get(int.from_bytes(codes[index : index + 2], "big"), "")
            for index in range(0, len(codes) - 1, 2)
        )
    return "".join(cmap.get(byte, chr(byte)) for byte in codes)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _check_command(pdf_path: str, config_path: str | None, edition_path: str | None = None) -> int:
    snippets = None
    if edition_path:
        with open(edition_path, encoding="utf-8") as handle:
            snippets = content_snippets(json.load(handle))
    try:
        assert_pdf(pdf_path, config=load_config(config_path) if config_path else None, required_snippets=snippets)
    except PdfCheckError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export HTML to PDF and/or assert a PDF")
    parser.add_argument("html", nargs="?", help="HTML file to print")
    parser.add_argument("--out", default=None, help="output PDF path (default: same name as the HTML)")
    parser.add_argument("--check", action="store_true", help="assert page count, footer, content and size")
    parser.add_argument("--pdf", default=None, help="PDF to check when no HTML is given")
    parser.add_argument(
        "--edition-json",
        default=None,
        help="edition JSON: adds the per-section content-completeness check to --check",
    )
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)

    try:
        if args.check and not args.html:
            target = args.pdf
            if not target:
                parser.error("--check needs an HTML file or --pdf")
            return _check_command(target, args.config, args.edition_json)

        if not args.html:
            parser.error("an HTML file is required")
        produced = to_pdf(args.html, args.out)
        if args.check:
            return _check_command(str(produced), args.config, args.edition_json)
    except (ConfigError, ExportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
