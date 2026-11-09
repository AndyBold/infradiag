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


def test_viewer_inlines_the_vendored_library_and_keeps_its_licence():
    text = viewer.viewer_html(SVG, "t")
    assert "svg-pan-zoom v3.6.2" in text
    assert "Redistribution and use in source and binary forms" in text
    assert "svgPanZoom(svg" in text


def test_viewer_is_self_contained(tmp_path):
    text = viewer.viewer_html(SVG, "t")
    assert not re.search(r"<script[^>]+src=|<link[^>]+href=|<img[^>]+src=|@import|url\(\s*['\"]?https?:", text, re.IGNORECASE)
    # Every URL left in the page is a W3C XML namespace identifier or the library's own credit line.
    urls = {u for u in re.findall(r"https?://[^\s\"'<>)]+", text) if not u.startswith("http://www.w3.org/")}
    assert urls == {"https://github.com/bumbu/svg-pan-zoom"}, urls


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


def test_viewer_handles_touch_pinch_itself_rather_than_relying_on_the_library():
    text = viewer.viewer_html(SVG, "t")
    assert "customEventsHandler: touchHandler" in text
    assert "zoomAtPointBy(" in text and "touchmove" in text


def test_one_to_one_sets_an_absolute_scale_not_the_fitted_one():
    text = viewer.viewer_html(SVG, "t")
    assert "resetZoom" not in text.split("svgPanZoom(svg, {", 1)[1]  # the library itself defines it; the page must not call it
    assert "pz.zoom(pz.getZoom() / pz.getSizes().realZoom)" in text
