"""PDF export and the pre-send assertions.

The real print is exercised (when a Chromium is available) by
``test_layout.test_worst_case_prints_to_exactly_two_pages``; here the assertion gates
themselves are tested with a hand-built PDF and with the extraction helpers stubbed.
"""

from __future__ import annotations

import zlib

import pytest

from pipeline import export


def minimal_pdf(pages: int = 2) -> bytes:
    """A valid, minimal PDF with *pages* blank pages (written by pypdf itself)."""
    import io

    from pypdf import PdfWriter

    buffer = io.BytesIO()
    writer = PdfWriter()
    for _index in range(pages):
        writer.add_blank_page(width=595, height=842)
    writer.write(buffer)
    return buffer.getvalue()


def test_page_count_reads_the_page_objects(tmp_path) -> None:
    path = tmp_path / "two.pdf"
    path.write_bytes(minimal_pdf(2))
    assert export.page_count(path) == 2
    path.write_bytes(minimal_pdf(3))
    assert export.page_count(path) == 3


def test_normalise_ignores_spacing_punctuation_and_hyphenation() -> None:
    assert export._normalise("THE GLOBAL BRIEF | WIRE EDITION") == export._normalise("TheGlobalBriefWireEdition")
    # hyphenation is applied by the print stylesheet, so extraction may split a word
    assert export._normalise("gov-ernment") == export._normalise("government") == "GOVERNMENT"


def test_assert_pdf_reports_missing_footer(tmp_path, config) -> None:
    path = tmp_path / "empty.pdf"
    path.write_bytes(minimal_pdf(2))
    with pytest.raises(export.PdfCheckError) as excinfo:
        export.assert_pdf(path, config=config)
    assert "footer" in str(excinfo.value).lower()


def test_assert_pdf_reports_the_wrong_page_count(tmp_path, config, monkeypatch) -> None:
    path = tmp_path / "two.pdf"
    path.write_bytes(minimal_pdf(2))
    monkeypatch.setattr(export, "extract_text", lambda _path: ["THE GLOBAL BRIEF | WIRE EDITION"] * 3)
    monkeypatch.setattr(export, "page_count", lambda _path: 3)
    with pytest.raises(export.PdfCheckError) as excinfo:
        export.assert_pdf(path, config=config)
    assert "3 page" in str(excinfo.value)


def test_assert_pdf_reports_clipped_content(tmp_path, config, monkeypatch) -> None:
    path = tmp_path / "two.pdf"
    path.write_bytes(minimal_pdf(2))
    footer = config["layout"]["footer_text"]
    monkeypatch.setattr(export, "page_count", lambda _path: 2)
    monkeypatch.setattr(
        export,
        "extract_text",
        lambda _path: [f"{footer} the closing words of the technology section", footer],
    )
    # the snippet only exists on page one in this stub, so the document as a whole has it
    assert export.assert_pdf(path, config=config, required_snippets=["closing words"])["content_complete"]

    monkeypatch.setattr(export, "extract_text", lambda _path: [footer, footer])
    with pytest.raises(export.PdfCheckError) as excinfo:
        export.assert_pdf(path, config=config, required_snippets=["closing words"])
    assert "clipped" in str(excinfo.value)


def test_assert_pdf_enforces_the_size_limit(tmp_path, config, monkeypatch) -> None:
    path = tmp_path / "big.pdf"
    path.write_bytes(minimal_pdf(2) + b"0" * 4096)
    footer = config["layout"]["footer_text"]
    monkeypatch.setattr(export, "page_count", lambda _path: 2)
    monkeypatch.setattr(export, "extract_text", lambda _path: [footer, footer])
    with pytest.raises(export.PdfCheckError) as excinfo:
        export.assert_pdf(path, config=config, size_limit_bytes=1024)
    assert "over the" in str(excinfo.value)


def test_assert_pdf_requires_a_file(tmp_path, config) -> None:
    with pytest.raises(export.PdfCheckError):
        export.assert_pdf(tmp_path / "nope.pdf", config=config)


def test_content_snippets_take_the_tail_of_every_section(fixture_payload) -> None:
    snippets = export.content_snippets(fixture_payload)
    assert len(snippets) >= 8
    assert len(set(snippets)) == len(snippets)
    joined = " ".join(snippets)
    assert "extend beyond the weekend" in joined  # end of the lead
    assert "reworking release schedules" in joined  # end of the last culture story
    assert "dollars a barrel" in joined  # last key number


def test_content_snippets_ignore_tiny_fragments() -> None:
    payload = {"content": {"lead": {"paras": ["ok"]}, "tech": {"stories": []}}}
    assert export.content_snippets(payload) == []


def test_simple_extractor_reads_a_chromium_style_stream() -> None:
    """The built-in reader handles 2-byte CID codes with a ToUnicode CMap."""
    cmap = (
        b"/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
        b"1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
        b"2 beginbfchar\n<0003> <0048>\n<0004> <0069>\nendbfchar\nendcmap\nend\nend\n"
    )
    content = b"BT /F1 10 Tf <00030004> Tj ET"
    page = (
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 9 0 R >> >> /Contents 8 0 R >>"
    )
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [7 0 R] /Count 1 >>",
        7: page,
        8: b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(zlib.compress(content))
        + zlib.compress(content)
        + b"\nendstream",
        9: b"<< /Type /Font /Subtype /Type0 /Encoding /Identity-H /ToUnicode 10 0 R >>",
        10: b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(zlib.compress(cmap))
        + zlib.compress(cmap)
        + b"\nendstream",
    }
    data = bytearray(b"%PDF-1.4\n")
    for number, body in sorted(objects.items()):
        data += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    data += b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"

    pages = export._simple_extract_text(bytes(data))
    assert len(pages) == 1
    assert export._normalise(pages[0]) == "HI"


def test_to_pdf_rejects_a_missing_file(tmp_path) -> None:
    with pytest.raises(export.ExportError):
        export.to_pdf(tmp_path / "nope.html")


def test_to_pdf_rejects_an_unknown_engine(tmp_path, monkeypatch) -> None:
    html = tmp_path / "x.html"
    html.write_text("<html></html>", encoding="utf-8")
    monkeypatch.setenv("BRIEF_PDF_ENGINE", "weasyprint")
    with pytest.raises(export.ExportError):
        export.to_pdf(html)


def test_check_command_returns_a_status(tmp_path, config, monkeypatch, repo_root) -> None:
    """`python -m pipeline.export --check` must exit non-zero when content is missing."""
    path = tmp_path / "two.pdf"
    path.write_bytes(minimal_pdf(2))
    footer = config["layout"]["footer_text"]
    monkeypatch.setattr(export, "page_count", lambda _path: 2)
    monkeypatch.setattr(export, "extract_text", lambda _path: [footer, footer])
    monkeypatch.setattr(export, "load_config", lambda *_args, **_kwargs: config)

    assert export._check_command(str(path), None) == 0
    assert export._check_command(str(path), None, str(repo_root / "build" / "edition-2026-10-13-am.json")) == 1


def test_chrome_candidates_honours_the_override(monkeypatch, tmp_path) -> None:
    fake = tmp_path / "chrome.exe"
    fake.write_bytes(b"")
    monkeypatch.setenv("BRIEF_CHROME_PATH", str(fake))
    assert export._chrome_candidates()[0] == fake
