from pathlib import Path

from infradiag.config import DEFAULT_VIEWS
from infradiag.icons import BUNDLED_ICONS, IconResolver, cache_path, update_icons
from infradiag.render_d2 import icon_url, render_d2
from infradiag.rules import Rules, load_rules
from infradiag.views import View, ViewNode

KV = "azure/Security Service Color/Key Vaults.svg"
SQL = "azure/Databases Service Color/SQL Servers.svg"
SVG = b"<svg xmlns='http://www.w3.org/2000/svg'/>"


def test_update_downloads_each_unique_icon_and_reports_failures(tmp_path):
    rules = Rules(icons={"microsoft.keyvault/vaults": KV, "_x": KV, "microsoft.sql/servers": SQL})
    fetched = []

    def fetch(url):
        fetched.append(url)
        if "SQL" in url:
            raise OSError("404")
        return SVG

    failures = update_icons(rules, tmp_path / "icons", fetch=fetch)
    assert fetched == [icon_url(KV), icon_url(SQL)]
    assert cache_path(tmp_path / "icons", KV).read_bytes() == SVG
    assert failures == {SQL: "404"}


def test_update_skips_custom_local_icons(tmp_path):
    (tmp_path / "custom").mkdir()
    (tmp_path / "custom" / "aca.svg").write_bytes(SVG)
    rules = Rules(icons={"microsoft.app/containerapps": "custom/aca.svg"})
    assert update_icons(rules, tmp_path / "icons", fetch=lambda url: 1 / 0, base_dir=tmp_path) == {}


def test_resolver_prefers_custom_path_then_cache_then_remote(tmp_path):
    (tmp_path / "custom").mkdir()
    (tmp_path / "custom" / "aca.svg").write_bytes(SVG)
    cache_path(tmp_path / "icons", KV).parent.mkdir(parents=True)
    cache_path(tmp_path / "icons", KV).write_bytes(SVG)
    resolve = IconResolver(tmp_path, tmp_path / "icons", bundled_dir=None)
    assert resolve("custom/aca.svg") == str(tmp_path / "custom" / "aca.svg")
    assert resolve(KV) == str(cache_path(tmp_path / "icons", KV))
    assert resolve(SQL) == icon_url(SQL)
    assert resolve.missing == {SQL}


def test_resolver_falls_back_to_bundled_icons(tmp_path):
    resolve = IconResolver(tmp_path, tmp_path / "icons")
    assert resolve(KV) == str(cache_path(BUNDLED_ICONS.resolve(), KV))
    assert resolve.missing == set()


def test_bundled_icons_cover_the_default_views():
    rules = load_rules(DEFAULT_VIEWS)
    assert [p for p in rules.icons.values() if not cache_path(BUNDLED_ICONS, p).is_file()] == []


def test_render_uses_resolver_for_icon_href(tmp_path):
    view = View("T", [ViewNode("n0", "kv", icon=KV)], [])
    text = render_d2(view, icon_href=lambda p: "/local/kv.svg")
    assert 'icon: "/local/kv.svg"' in text


def test_cache_path_has_no_spaces_because_d2_cannot_bundle_them(tmp_path):
    assert cache_path(tmp_path, KV) == tmp_path / "azure" / "security-service-color" / "key-vaults.svg"
    nsg = "azure/Networking Service Color/Network Security Groups (Classic).svg"
    assert cache_path(tmp_path, nsg).name == "network-security-groups-classic.svg"


def test_resolver_returns_absolute_paths_for_relative_dirs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cache_path(Path("icons"), KV).parent.mkdir(parents=True)
    cache_path(Path("icons"), KV).write_bytes(SVG)
    assert Path(IconResolver(Path("."), Path("icons"))(KV)).is_absolute()


import shutil

import pytest

from infradiag.render_d2 import render_svg


@pytest.mark.skipif(shutil.which("d2") is None, reason="d2 not installed")
def test_real_d2_bundles_cached_icon(tmp_path):
    cached = cache_path(tmp_path / "icons", KV)
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10'><rect width='10' height='10'/></svg>")
    view = View("T", [ViewNode("n0", "kv", icon=KV)], [])
    render_svg(render_d2(view, IconResolver(tmp_path, tmp_path / "icons")), tmp_path / "out" / "v.d2", tmp_path / "out" / "v.svg")
    assert 'href="data:image/svg+xml;base64' in (tmp_path / "out" / "v.svg").read_text()
