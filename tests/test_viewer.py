import re
from pathlib import Path

from infradiag import cli, viewer
from tests.factories import fake_svg
from tests.test_cli import setup

SVG = ('<?xml version="1.0" encoding="utf-8"?><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
       '<text>a &amp; b</text></svg>')


def test_write_viewer_puts_html_beside_svg_with_the_svg_inlined(tmp_path):
    svg = tmp_path / "application-c4-identity.svg"
    svg.write_text(SVG)
    html = viewer.write_viewer(svg)
    assert html == tmp_path / "application-c4-identity.html"
    text = html.read_text()
    assert '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><text>a &amp; b</text></svg>' in text
    assert "<?xml" not in text  # the XML prolog is not valid inside HTML
    assert "<title>application-c4-identity</title>" in text


def test_viewer_inlines_the_camera_logic_and_has_every_control():
    text = viewer.viewer_html(SVG, "t")
    assert "var ViewerLogic" in text
    for control in ('id="zin"', 'id="zout"', 'id="fit"', 'id="reset"', 'id="zoom"'):
        assert control in text
    assert "svgPanZoom" not in text


def test_viewer_is_self_contained(tmp_path):
    text = viewer.viewer_html(SVG, "t")
    assert not re.search(r"<script[^>]+src=|<link[^>]+href=|<img[^>]+src=|@import|url\(\s*['\"]?https?:", text, re.IGNORECASE)
    # The only URLs left in the page are W3C XML namespace identifiers.
    urls = {u for u in re.findall(r"https?://[^\s\"'<>)]+", text) if not u.startswith("http://www.w3.org/")}
    assert urls == set(), urls


def test_viewer_title_is_escaped():
    assert "<title>a &amp; &lt;b></title>" in viewer.viewer_html(SVG, "a & <b>")


def test_svg_text_that_looks_like_a_placeholder_is_left_alone():
    svg = SVG.replace("a &amp; b", "__LIBRARY__ __TITLE__")
    text = viewer.viewer_html(svg, "t")
    assert "<text>__LIBRARY__ __TITLE__</text>" in text


def test_render_writes_a_viewer_per_svg_and_prints_both_paths(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "render_svg", lambda text, d2, svg, **kw: fake_svg(svg))
    assert cli.main(setup(tmp_path) + ["render", "--view", "all"]) == 0
    lines = capsys.readouterr().out.splitlines()
    svgs = [line for line in lines if line.endswith(".svg")]
    assert [line.removesuffix(".svg") + ".html" for line in svgs] == [line for line in lines if line.endswith(".html")]
    assert len(svgs) == 2
    for line in lines:
        assert Path(line).exists()


def test_gestures_move_a_composited_layer_and_settle_to_a_crisp_viewbox():
    text = viewer.viewer_html(SVG, "t")
    assert "#stage.moving > svg { will-change: transform; }" in text
    assert "svg.style.transform = 'translate(" in text  # a gesture step is only a transform ...
    assert "svg.setAttribute('viewBox'" in text  # ... and settling re-renders by rewriting the viewBox
    assert "SETTLE_MS = 150" in text


def test_viewer_handles_wheel_drag_pinch_keys_and_iframes():
    text = viewer.viewer_html(SVG, "t")
    assert "addEventListener('wheel'" in text and "passive: false" in text
    assert "'pointerdown'" in text and "pointers.size === 2" in text  # one pointer pans, two pinch
    assert "touch-action: none" in text
    assert "window.focus()" in text  # keys reach the page inside an iframe
    assert "e.ctrlKey || e.metaKey" in text  # Ctrl+F and browser shortcuts are not hijacked
    assert "ResizeObserver" in text  # the iframe can be resized by the master page
    assert "selectionchange" in text  # find-in-page matches are brought on screen


def test_hover_titles_are_left_to_the_svg():
    svg = SVG.replace("<text>", "<g><title>hover me</title><path d='M0 0' style='stroke:transparent;stroke-width:12'/></g><text>")
    text = viewer.viewer_html(svg, "t")
    assert "<title>hover me</title>" in text and "stroke-width:12" in text
