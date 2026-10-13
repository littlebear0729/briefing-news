"""Rendering invariants for both artefacts (DESIGN.md §7)."""

from __future__ import annotations

import re

import pytest

from pipeline import content as ch
from pipeline import render, render_mobile, sources

STYLE_RE = re.compile(r"<style>(.*?)</style>", re.S)
ROOT_BLOCK_RE = re.compile(r":root\s*\{.*?\}", re.S)
HREF_RE = re.compile(r'href="([^"]+)"')


@pytest.fixture(scope="module")
def rendered(config, repo_root):
    """Render both artefacts once from the committed fixture."""
    fixture = repo_root / "build" / "edition-2026-10-13-am.json"
    desktop, _path = render.run(fixture, "2026-10-13", "AM", config=config)
    mobile_path = render_mobile.run(
        fixture, "2026-10-13", "AM", None, repo_root / ".pytest-mobile.html", config=config, report=False
    )
    mobile = mobile_path.read_text(encoding="utf-8")
    mobile_path.unlink(missing_ok=True)
    return {"desktop": desktop, "mobile": mobile, "fixture": fixture}


def styles(html: str) -> str:
    return "\n".join(STYLE_RE.findall(html))


@pytest.mark.parametrize("artefact", ["desktop", "mobile"])
def test_artefact_structure(artefact: str, rendered: dict, config: dict) -> None:
    html = rendered[artefact]
    assert html.startswith("<!doctype html>")
    assert html.count('class="page page--') == 2, "exactly two printed page boxes"
    assert html.count(config["layout"]["footer_text"]) == 2, "footer on both pages"
    assert html.count('class="source-link"') >= 20, "citations are rendered as links"
    assert "<script" not in html, "no scripts in the artefact"


@pytest.mark.parametrize("artefact", ["desktop", "mobile"])
def test_no_unrendered_jinja(artefact: str, rendered: dict) -> None:
    for marker in ("{{", "{%", "{#"):
        assert marker not in rendered[artefact]


def test_assert_rendered_is_a_real_check() -> None:
    render.assert_rendered("<p>fine</p>")
    with pytest.raises(render.RenderError):
        render.assert_rendered("<p>{{ story.headline }}</p>")


@pytest.mark.parametrize("artefact", ["desktop", "mobile"])
def test_colour_literals_live_only_in_root_blocks(artefact: str, rendered: dict) -> None:
    """Invariant 7: no bare hex/rgb/hsl outside the two :root token blocks."""
    css = ROOT_BLOCK_RE.sub("", styles(rendered[artefact]))
    for pattern in (r"#[0-9a-fA-F]{3,8}\b", r"\brgba?\(", r"\bhsla?\("):
        offenders = re.findall(pattern, css)
        assert not offenders, f"{artefact}: colour literal outside :root: {offenders[:5]}"
    assert styles(rendered[artefact]).count(":root") >= 2, "design tokens plus project tokens"


@pytest.mark.parametrize("artefact", ["desktop", "mobile"])
def test_focus_visibility_uses_the_token(artefact: str, rendered: dict) -> None:
    css = styles(rendered[artefact])
    assert ":focus-visible" in css
    assert "var(--focus-ring)" in css
    assert ":root" in css and "--focus-ring" in css


def test_mobile_uses_the_fixed_breakpoints_and_scopes_them_to_screen(rendered: dict) -> None:
    css = styles(rendered["mobile"])
    assert "@media screen and (min-width: 640px)" in css
    assert "@media screen and (min-width: 1024px)" in css
    # phone-first rules must not leak into the PDF: a 210mm page still matches min-width: 640px
    assert "@media (min-width: 640px)" not in css


@pytest.mark.parametrize("artefact", ["desktop", "mobile"])
def test_no_multicol_in_the_print_layout(artefact: str, rendered: dict) -> None:
    """Regression guard: multicol silently dropped a lead paragraph in one artefact."""
    css = styles(rendered[artefact])
    assert not re.search(r"(?<!-)columns:\s*\d", css), "page boxes must not use CSS multi-column"
    assert "overflow: hidden" in css, "the fixed page box still clips; the PDF assertion is the net"


@pytest.mark.parametrize("artefact", ["desktop", "mobile"])
def test_every_link_is_whitelisted(artefact: str, rendered: dict) -> None:
    links = {href for href in HREF_RE.findall(rendered[artefact]) if href.startswith("http")}
    assert links, "expected external source links"
    for link in links:
        assert sources.is_allowed(link), f"{artefact} links to a non-whitelisted domain: {link}"


def test_every_story_paragraph_is_present_in_both_artefacts(rendered: dict, fixture_payload: dict) -> None:
    content = ch.edition_content(fixture_payload)
    paragraphs = [paragraph for _path, paragraph in ch.iter_paragraphs(content)]
    assert len(paragraphs) >= 25
    for artefact in ("desktop", "mobile"):
        for paragraph in paragraphs:
            assert paragraph in rendered[artefact], f"{artefact} is missing a paragraph"


def test_lead_is_split_across_two_explicit_columns(rendered: dict, config: dict) -> None:
    html = rendered["desktop"]
    assert html.count('class="lead-col"') == 2
    assert "grid-template-columns: 1fr 1fr" in styles(html)


def test_inside_columns_follow_the_code_computed_plan(rendered: dict, fixture_payload: dict, config: dict) -> None:
    content = ch.edition_content(fixture_payload)
    plan = ch.plan_inside_columns(content, config)
    html = rendered["desktop"]

    left = html.split('inside-column inside-column--left', 1)[1].split('inside-column inside-column--right', 1)[0]
    right = html.split('inside-column inside-column--right', 1)[1]

    assert sorted(plan["left"] + plan["right"]) == ["culture", "finance", "sport", "technology"]
    for section in plan["left"]:
        assert f'id="{section}"' in left
    for section in plan["right"]:
        assert f'id="{section}"' in right


def test_plan_balances_the_two_columns(fixture_payload: dict, config: dict) -> None:
    content = ch.edition_content(fixture_payload)
    weights = ch.estimate_section_weights(content)
    plan = ch.plan_inside_columns(content, config)
    left = sum(weights[section] for section in plan["left"])
    right = sum(weights[section] for section in plan["right"])
    assert abs(left - right) / max(left, right) < 0.25, f"unbalanced columns: {left:.0f} vs {right:.0f}"


def test_both_artefacts_carry_the_same_edition(rendered: dict) -> None:
    """Same JSON, same text: only the CSS may differ between the artefacts."""
    def paragraphs(html: str) -> list[str]:
        return re.findall(r'<p class="para">(.*?)</p>', html, re.S)

    desktop, mobile = paragraphs(rendered["desktop"]), paragraphs(rendered["mobile"])
    assert len(desktop) == len(mobile)
    assert [re.sub(r"\s+", " ", item) for item in desktop] == [re.sub(r"\s+", " ", item) for item in mobile]


def test_mobile_is_responsive_and_has_a_reading_nav(rendered: dict) -> None:
    html = rendered["mobile"]
    assert 'name="viewport"' in html
    assert 'class="screen-nav"' in html
    assert 'class="skip-link"' in html
    assert "@media print" in styles(html)


def test_render_mobile_accepts_a_bare_content_object(config: dict, fixture_payload: dict, tmp_path) -> None:
    """DESIGN.md §5.3: the resolved content object alone is a valid payload."""
    import json

    bare = tmp_path / "bare.json"
    bare.write_text(json.dumps(ch.edition_content(fixture_payload)), encoding="utf-8")
    path = render_mobile.run(bare, "2026-10-13", "AM", None, tmp_path / "out.html", config=config, report=False)
    assert path.is_file() and path.stat().st_size > 10_000


def test_render_mobile_rejects_prose_containing_a_url(config: dict, fixture_payload: dict, tmp_path) -> None:
    import copy
    import json

    payload = copy.deepcopy(fixture_payload)
    payload["content"]["lead"]["paras"][0] = "See https://evil.example/x for the full text."
    path = tmp_path / "edition.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(render_mobile.MobileRenderError):
        render_mobile.run(path, "2026-10-13", "AM", None, tmp_path / "out.html", config=config, report=False)


def test_render_mobile_rejects_markup_in_prose(config: dict, fixture_payload: dict, tmp_path) -> None:
    import copy
    import json

    payload = copy.deepcopy(fixture_payload)
    payload["content"]["lead"]["headline"] = "<b>Bold claim</b>"
    path = tmp_path / "edition.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(render_mobile.MobileRenderError):
        render_mobile.run(path, "2026-10-13", "AM", None, tmp_path / "out.html", config=config, report=False)


def test_render_mobile_rejects_a_non_whitelisted_citation(config: dict, fixture_payload: dict, tmp_path) -> None:
    import copy
    import json

    payload = copy.deepcopy(fixture_payload)
    payload["content"]["lead"]["sources"][0]["url"] = "https://evil.example/x"
    payload["content"]["lead"]["source_ids"] = None
    del payload["content"]["lead"]["source_ids"]
    path = tmp_path / "edition.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(render_mobile.MobileRenderError):
        render_mobile.run(path, "2026-10-13", "AM", None, tmp_path / "out.html", config=config, report=False)


def test_render_writes_the_documented_path(config: dict, fixture_payload, repo_root, tmp_path) -> None:
    from pipeline.config import paths

    sandboxed = dict(config)
    sandboxed["paths"] = {**config["paths"], "dist_dir": str(tmp_path)}
    _html, path = render.run(repo_root / "build" / "edition-2026-10-13-am.json", "2026-10-13", "AM", config=sandboxed)
    assert path == tmp_path / "global-brief-2026-10-13-am.html"
    assert paths(sandboxed)["dist_dir"] == tmp_path
