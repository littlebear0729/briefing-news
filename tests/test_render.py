"""The delivered document: structure, newspaper styling, and the pre-delivery checks."""

from __future__ import annotations

import copy
import json
import re

import pytest

from pipeline import content as ch
from pipeline import preflight, render, sources

STYLE_RE = re.compile(r"<style>(.*?)</style>", re.S)
ROOT_BLOCK_RE = re.compile(r":root\s*\{.*?\}", re.S)
HREF_RE = re.compile(r'href="([^"]+)"')


@pytest.fixture(scope="module")
def document(config, repo_root) -> dict:
    """Render the committed fixture once and hand the HTML to the assertions below."""
    fixture = repo_root / "build" / "edition-2026-10-13-am.json"
    html, path = render.run(fixture, "2026-10-13", "AM", config=config, out=repo_root / ".pytest-edition.html", check=True)
    path.unlink(missing_ok=True)
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    return {"html": html, "payload": payload}


def styles(html: str) -> str:
    return "\n".join(STYLE_RE.findall(html))


def test_single_self_contained_document(document: dict) -> None:
    html = document["html"]
    assert html.startswith("<!doctype html>")
    assert html.count("<html") == 1 and html.count("<body") == 1
    assert html.count("<style>") == 1, "one inline stylesheet"
    assert "<script" not in html.lower(), "no scripts in a deliverable"
    assert 'name="viewport"' in html, "responsive: the viewport meta is required"
    assert 'rel="stylesheet"' not in html and "http://" not in styles(html), "no external requests"


def test_two_sheets_with_the_footer_on_each(document: dict, config: dict) -> None:
    html = document["html"]
    assert html.count('data-sheet="') == config["layout"]["sheets"] == 2
    assert html.count(config["layout"]["footer_text"]) == 2


def test_no_unrendered_template_markers(document: dict) -> None:
    for marker in ("{{", "{%", "{#"):
        assert marker not in document["html"]


def test_assert_rendered_is_a_real_check() -> None:
    render.assert_rendered("<p>fine</p>")
    with pytest.raises(render.RenderError):
        render.assert_rendered("<p>{{ story.headline }}</p>")


def test_colour_literals_live_only_in_root_blocks(document: dict) -> None:
    """Invariant 7: no bare hex/rgb/hsl outside the two :root token blocks."""
    css = ROOT_BLOCK_RE.sub("", styles(document["html"]))
    for pattern in (r"#[0-9a-fA-F]{3,8}\b", r"\brgba?\(", r"\bhsla?\("):
        offenders = re.findall(pattern, css)
        assert not offenders, f"colour literal outside :root: {offenders[:5]}"
    assert styles(document["html"]).count(":root") >= 2, "design tokens plus newspaper tokens"


def test_focus_visibility_uses_the_token(document: dict) -> None:
    css = styles(document["html"])
    assert ":focus-visible" in css and "var(--focus-ring)" in css


def test_document_contains_no_images(document: dict) -> None:
    """The brief asks for no illustrations: text, rules and links only."""
    html = document["html"]
    assert "<svg" not in html and "<img" not in html and "<figure" not in html
    assert "data:image" not in html
    assert "url(" not in styles(html), "no background images or webfonts"
    assert "class=\"visual" not in html.replace('class="visually-hidden"', "")


def test_responsive_breakpoints_are_fixed_and_screen_scoped(document: dict) -> None:
    css = styles(document["html"])
    assert "@media screen and (min-width: 640px)" in css
    assert "@media screen and (min-width: 1024px)" in css
    assert "@media print" in css
    # phone-first rules must not leak into print: a phone rule would otherwise collapse
    # the printed paper into one long column
    assert "@media (min-width: 640px)" not in css


def test_newspaper_typography_and_paper(document: dict) -> None:
    """The look the brief asked for: newsprint, heavy rules, serif display type."""
    css = styles(document["html"])
    assert "--paper:" in css and "--ink:" in css and "--accent:" in css
    assert "var(--font-display)" in css, "display serif for headlines"
    assert "repeating-linear-gradient" in css, "newsprint grain"
    assert "border-top: var(--rule-heavy) solid var(--rule-strong)" in css, "heavy masthead/section rules"
    html = document["html"]
    assert 'class="masthead-bar"' in html and 'class="sheet-label"' in html
    assert "hyphens: auto" in css and "text-align: justify" in css


def strip_print_blocks(css: str) -> str:
    """Remove every ``@media print { … }`` block, brace-matched."""
    parts: list[str] = []
    index = 0
    while True:
        start = css.find("@media print", index)
        if start == -1:
            parts.append(css[index:])
            break
        brace = css.find("{", start)
        depth, cursor = 1, brace + 1
        while depth and cursor < len(css):
            if css[cursor] == "{":
                depth += 1
            elif css[cursor] == "}":
                depth -= 1
            cursor += 1
        parts.append(css[index:start])
        index = cursor
    return "".join(parts)


def test_no_fixed_page_boxes(document: dict) -> None:
    """Length is a target now: nothing may clip, so there is no fixed-height page box."""
    css = styles(document["html"])
    # .visually-hidden legitimately clips (that is what it is for); a page box must not.
    layout_css = re.sub(r"\.visually-hidden\s*\{[^}]*\}", "", css, flags=re.S)
    assert "overflow: hidden" not in layout_css, "a document that grows must never hide content"
    assert "297mm" not in css and "210mm" not in css, "no A4 page box"


def test_multi_column_is_print_only(document: dict) -> None:
    """On screen the columns are explicit grids; multicol is used only for the printed
    flow, where a document that grows cannot lose content to an overflowing column."""
    screen_css = strip_print_blocks(styles(document["html"]))
    assert not re.search(r"(?<!-)columns:\s*\d", screen_css)
    assert re.search(r"(?<!-)columns:\s*\d", styles(document["html"])), "print uses a column flow"


def test_every_link_is_whitelisted(document: dict) -> None:
    links = {href for href in HREF_RE.findall(document["html"]) if href.startswith("http")}
    assert links
    for link in links:
        assert sources.is_allowed(link), f"non-whitelisted link in the document: {link}"


def test_every_paragraph_and_headline_reached_the_page(document: dict) -> None:
    """What the PDF snippet check used to do, now done on the document we actually send."""
    content = ch.edition_content(document["payload"])
    text = preflight.document_text(document["html"])
    for _path, story in ch.iter_stories(content):
        assert story["headline"] in text
        for paragraph in story["paras"]:
            assert paragraph in text


def test_preflight_catches_a_dropped_slot(config: dict, document: dict) -> None:
    """If the template ever stops rendering a section, delivery must fail, not shrink."""
    payload = copy.deepcopy(document["payload"])
    payload["content"]["culture"][2]["paras"][-1] = "A paragraph that the template never sees."
    with pytest.raises(preflight.PreflightError) as excinfo:
        preflight.assert_deliverable(document["html"], payload=payload, config=config)
    assert "did not reach the page" in str(excinfo.value)


def test_preflight_ignores_a_key_number_the_page_cannot_show(config: dict, document: dict) -> None:
    """The Key numbers panel renders at most four pairs (newspaper.html.j2 [:4]).

    A payload may legally carry a fifth (old schema allowed 3-5); preflight samples the
    closing words of what the page actually shows, so an unrendered fifth label must not
    fail delivery. Regression for the 2026-10-14 AM run: 'workers leaving Royal Mail'.
    """
    payload = copy.deepcopy(document["payload"])
    numbers = payload["content"]["money"]["key_numbers"]
    assert len(numbers) <= 4
    numbers.append({"value": "99.99", "label": "workers leaving Royal Mail today"})
    html = render.render_html(payload, config, run_date="2026-10-13", edition_name="AM")
    preflight.assert_deliverable(html, payload=payload, config=config)
    snippets = ch.closing_snippets(payload["content"])
    assert "workers leaving Royal Mail today" not in snippets[-1]


def test_preflight_catches_a_missing_footer(config: dict, document: dict) -> None:
    broken = document["html"].replace(
        config["layout"]["footer_text"], "SOMETHING ELSE ENTIRELY", 1
    )
    with pytest.raises(preflight.PreflightError) as excinfo:
        preflight.assert_deliverable(broken, payload=document["payload"], config=config)
    assert "footer" in str(excinfo.value)


def test_preflight_catches_a_non_whitelisted_link(config: dict, document: dict) -> None:
    broken = document["html"].replace(
        "https://www.reuters.com/world/middle-east/truce-monitors-deploy-2026-10-13/",
        "https://evil.example/story",
        1,
    )
    with pytest.raises(preflight.PreflightError) as excinfo:
        preflight.assert_deliverable(broken, payload=document["payload"], config=config)
    assert "whitelist" in str(excinfo.value)


def test_preflight_catches_scripts_and_leftover_jinja(config: dict, document: dict) -> None:
    with pytest.raises(preflight.PreflightError):
        preflight.assert_deliverable(
            document["html"].replace("</body>", "<script>alert(1)</script></body>"),
            payload=document["payload"],
            config=config,
        )
    with pytest.raises(preflight.PreflightError):
        preflight.assert_deliverable(
            document["html"].replace("<h1", "{{ oops }}<h1", 1),
            payload=document["payload"],
            config=config,
        )


def test_document_text_strips_markup_and_style() -> None:
    text = preflight.document_text("<style>p{color:red}</style><p>Hello <b>world</b></p>")
    assert text == "Hello world"


def test_render_accepts_a_bare_content_object(config: dict, fixture_payload: dict, tmp_path) -> None:
    """DESIGN.md §5.3: the resolved content object alone is a valid payload."""
    bare = tmp_path / "bare.json"
    bare.write_text(json.dumps(ch.edition_content(fixture_payload)), encoding="utf-8")
    _html, path = render.run(bare, "2026-10-13", "AM", config=config, out=tmp_path / "out.html")
    assert path.is_file() and path.stat().st_size > 10_000


def test_render_resolves_source_ids_when_only_ids_are_present(
    config: dict, fixture_payload: dict, tmp_path
) -> None:
    payload = copy.deepcopy(fixture_payload)
    for _path, story in ch.iter_stories(payload["content"]):
        story.pop("sources", None)
    edition = tmp_path / "ids-only.json"
    edition.write_text(json.dumps(payload), encoding="utf-8")
    html, _path = render.run(edition, "2026-10-13", "AM", config=config, out=tmp_path / "out.html")
    assert 'class="source-link"' in html


def test_render_rejects_prose_containing_a_url(config: dict, fixture_payload: dict, tmp_path) -> None:
    payload = copy.deepcopy(fixture_payload)
    payload["content"]["lead"]["paras"][0] = "See https://evil.example/x for the full text."
    path = tmp_path / "edition.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(render.RenderError):
        render.run(path, "2026-10-13", "AM", config=config, out=tmp_path / "out.html")


def test_render_rejects_markup_in_prose(config: dict, fixture_payload: dict, tmp_path) -> None:
    payload = copy.deepcopy(fixture_payload)
    payload["content"]["lead"]["headline"] = "<b>Bold claim</b>"
    path = tmp_path / "edition.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(render.RenderError):
        render.run(path, "2026-10-13", "AM", config=config, out=tmp_path / "out.html")


def test_render_rejects_a_non_whitelisted_citation(config: dict, fixture_payload: dict, tmp_path) -> None:
    payload = copy.deepcopy(fixture_payload)
    payload["content"]["lead"]["sources"][0]["url"] = "https://evil.example/x"
    del payload["content"]["lead"]["source_ids"]
    path = tmp_path / "edition.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(render.RenderError):
        render.run(path, "2026-10-13", "AM", config=config, out=tmp_path / "out.html")


def test_render_writes_the_documented_path(config: dict, repo_root, tmp_path) -> None:
    sandboxed = dict(config)
    sandboxed["paths"] = {**config["paths"], "dist_dir": str(tmp_path)}
    _html, path = render.run(repo_root / "build" / "edition-2026-10-13-am.json", "2026-10-13", "AM", config=sandboxed)
    assert path == tmp_path / "global-brief-2026-10-13-am.html"


def test_meta_is_code_owned(document: dict, config: dict) -> None:
    meta = render.build_meta(
        "2026-10-13",
        "AM",
        config,
        document["payload"]["sources"],
        content=ch.edition_content(document["payload"]),
    )
    assert meta["edition_number"] == 571
    assert meta["masthead_date"] == "TUESDAY, 13 OCTOBER 2026"
    assert meta["target_sheets"] == config["layout"]["target_sheets"]
    assert meta["stats"]["sheets"]["total"] > 0
    assert meta["footer_text"] == config["layout"]["footer_text"]
