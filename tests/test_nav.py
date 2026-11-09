import json
import re
from pathlib import Path

from infradiag import cli, nav
from infradiag.access import IndexEntry, render_html, render_index
from infradiag.viewer import viewer_html
from tests.test_cli import SUB, capture_d2, setup

SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><text>x</text></svg>'


def diagram(path: Path, title: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(viewer_html(SVG, title), encoding="utf-8")
    return path


def table(path: Path, title: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html([], title, "x.csv"), encoding="utf-8")
    return path


def index(path: Path, title: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_index(title, [IndexEntry("e", 1, 1, "a.svg", "a.html")], []), encoding="utf-8")
    return path


def tree(root: Path) -> None:
    diagram(root / "Sub-A/detailed.html", "Detailed (Sub-A)")
    table(root / "Sub-A/detailed-identity-access.html", "Detailed: access")
    diagram(root / "applications/application-identity-prod.html", "app: prod")
    diagram(root / "applications/application-identity-test.html", "app: test")
    table(root / "applications/application-identity-access.html", "app: access")
    index(root / "applications/application-identity-index.html", "app: by environment")
    (root / "applications/notes.html").write_text("<html><title>not ours</title></html>")
    (root / "Sub-A/plain.svg").write_text(SVG)


def groups(root):
    js = nav.nav_js(nav.scan(root))
    return {g["name"]: g["items"] for g in json.loads(js.split("window.INFRADIAG_NAV = ")[1].rsplit(";", 1)[0])["groups"]}


def test_scan_finds_every_page_kind_by_content_and_reads_titles(tmp_path):
    tree(tmp_path)
    pages = {p.path: (p.kind, p.title) for p in nav.scan(tmp_path)}
    assert pages["Sub-A/detailed.html"] == ("diagram", "Detailed (Sub-A)")
    assert pages["Sub-A/detailed-identity-access.html"] == ("table", "Detailed: access")
    assert pages["applications/application-identity-index.html"][0] == "index"
    assert "applications/notes.html" not in pages and len(pages) == 6


def test_tree_groups_by_folder_nests_environments_under_their_index_and_keeps_tables_by_their_diagram(tmp_path):
    tree(tmp_path)
    g = groups(tmp_path)
    assert [i["path"] for i in g["Sub-A"]] == ["Sub-A/detailed.html", "Sub-A/detailed-identity-access.html"]
    (top,) = g["applications"]
    assert top["kind"] == "index"
    assert sorted(c["path"] for c in top["children"]) == [
        "applications/application-identity-access.html", "applications/application-identity-prod.html",
        "applications/application-identity-test.html"]


def test_unknown_filter_suffixes_still_appear(tmp_path):
    diagram(tmp_path / "applications/application-identity-prod--only-ops.html", "app: prod (filtered: ops)")
    (entry,) = groups(tmp_path)["applications"]
    assert entry["title"] == "app: prod (filtered: ops)"


def test_refresh_writes_master_page_and_nav_data_and_gives_every_page_a_relative_link_back(tmp_path):
    tree(tmp_path)
    pages = nav.refresh_nav(tmp_path)
    assert len(pages) == 6 and (tmp_path / "nav.js").exists()
    for p in pages:
        text = (tmp_path / p.path).read_text()
        assert text.count("data-infradiag-home") == 1, p.path
        up = "../" if "/" in p.path else ""
        assert f'href="{up}index.html#{p.path}"' in text, p.path
        assert 'target="_top"' in text
    for kind in ("diagram", "table", "index"):
        assert kind in {p.kind for p in pages}
    assert (tmp_path / "applications/notes.html").read_text().endswith("<title>not ours</title></html>")
    master = (tmp_path / "index.html").read_text()
    assert '<script src="nav.js"></script>' in master and "<iframe" in master


def test_master_page_loads_items_on_demand_and_keeps_selection_in_the_hash(tmp_path):
    tree(tmp_path)
    nav.refresh_nav(tmp_path)
    master = (tmp_path / "index.html").read_text()
    assert re.search(r'<iframe id="frame"[^>]* hidden>', master) and "src=" not in re.search(r"<iframe[^>]*>", master).group(0)
    assert "hashchange" in master and "location.hash" in master and "location.replace" in master
    assert "<svg" not in master  # nothing from the pages is inlined; they load when selected
    assert 'type="search"' in master and "collapsed" in master and "prefers-color-scheme:dark" in master


def test_home_link_is_idempotent_and_plain_svg_alone(tmp_path):
    tree(tmp_path)
    nav.refresh_nav(tmp_path)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    nav.refresh_nav(tmp_path)
    assert before == {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert (tmp_path / "Sub-A/plain.svg").read_text() == SVG


def test_removed_files_leave_the_nav_and_the_master_page(tmp_path):
    tree(tmp_path)
    nav.refresh_nav(tmp_path)
    (tmp_path / "applications/application-identity-test.html").unlink()
    nav.refresh_nav(tmp_path)
    assert "application-identity-test" not in (tmp_path / "nav.js").read_text()
    assert "application-identity-test" not in (tmp_path / "index.html").read_text()
    assert "application-identity-prod" in (tmp_path / "nav.js").read_text()


def test_pages_written_by_a_later_run_are_picked_up(tmp_path):
    diagram(tmp_path / "A/one.html", "one")
    nav.refresh_nav(tmp_path)
    diagram(tmp_path / "B/two.html", "two")
    nav.refresh_nav(tmp_path)
    assert "B/two.html" in (tmp_path / "nav.js").read_text()
    assert 'href="../index.html#B/two.html"' in (tmp_path / "B/two.html").read_text()


def test_nav_is_self_contained_and_not_dependent_on_a_server(tmp_path):
    tree(tmp_path)
    nav.refresh_nav(tmp_path)
    for name in ("nav.js", "index.html"):
        text = (tmp_path / name).read_text()
        assert not re.search(r"https?://|fetch\(|XMLHttpRequest|@import|import\(", text), name
        assert not re.search(r"^\s*(import|export)\s", text, re.M), name
    for p in nav.scan(tmp_path):
        assert not re.search(r"https?://", re.search(r"<a data-infradiag-home.*?</a>", (tmp_path / p.path).read_text(), re.S).group(0))


def test_titles_with_script_close_tags_cannot_break_nav_js(tmp_path):
    diagram(tmp_path / "A/x.html", "</script><b>")
    assert "</script>" not in nav.nav_js(nav.scan(tmp_path))


def test_render_builds_nav_across_separate_runs(tmp_path, monkeypatch, capsys):
    args = setup(tmp_path)
    capture_d2(monkeypatch)
    assert cli.main(args + ["render", "--view", "application", "--subscription", SUB]) == 0
    assert cli.main(args + ["render", "--view", "application", "--split", "environment", "--subscription", SUB]) == 0
    out = tmp_path / "out"
    pages = nav.scan(out)
    assert any(p.kind == "index" for p in pages) and any(p.kind == "diagram" for p in pages)
    assert "applications/application-index.html" in (out / "nav.js").read_text()
    assert "navigation:" in capsys.readouterr().err
    for p in pages:
        assert "data-infradiag-home" in (out / p.path).read_text()
    # An earlier run's pages survive in the nav until their files go.
    assert cli.main(args + ["render", "--view", "summary", "--subscription", SUB]) == 0
    assert "applications/application-index.html" in (out / "nav.js").read_text()
