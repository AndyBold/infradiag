import shutil
import subprocess

import pytest

from infradiag import render_d2 as r
from infradiag.views import View, ViewEdge, ViewNode

VIEW = View(
    "T",
    [ViewNode("c0", "rg-a", "resource group", children=[
        ViewNode("n0", "kv-1", "vaults", icon="azure/Security Service Color/Key Vaults.svg", style="azure_only"),
        ViewNode("n1", "privatelink.vaultcore.azure.net", "privatednszones"),
    ])],
    [ViewEdge("n1", "n0", "dns link", "solid"), ViewEdge("n0", "n1", "", "dashed")],
)


def test_render_snapshot():
    text = r.render_d2(VIEW)
    body = text[text.index("title: "):]
    assert body == (
        'title: "T" {near: top-center; shape: text; style.font-size: 28}\n'
        'c0: "rg-a\\nresource group" {\n'
        '  n0: "kv-1\\nvaults" {\n'
        '    icon: "https://icons.terrastruct.com/azure%2FSecurity%20Service%20Color%2FKey%20Vaults.svg"\n'
        '    class: azure_only\n'
        '  }\n'
        '  n1: "privatelink.vaultcore.azure.net\\nprivatednszones"\n'
        '}\n'
        'c0.n1 -> c0.n0: "dns link"\n'
        'c0.n0 -> c0.n1: {class: dashed}\n'
    )


def test_header_defines_every_style_class():
    text = r.render_d2(VIEW)
    for cls in ("bicep_only", "azure_only", "unverified", "stub", "dashed", "private"):
        assert f"  {cls}: " in text


def test_labels_with_quotes_and_backslashes_are_escaped():
    view = View('a "b"', [ViewNode("n0", 'x"y\\z')], [])
    text = r.render_d2(view)
    assert 'title: "a \\"b\\""' in text
    assert 'n0: "x\\"y\\\\z"' in text


def test_render_svg_without_d2_writes_source_and_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(r.shutil, "which", lambda _: None)
    with pytest.raises(r.D2NotFound, match="brew install d2"):
        r.render_svg("x: y\n", tmp_path / "out" / "v.d2", tmp_path / "out" / "v.svg")
    assert (tmp_path / "out" / "v.d2").read_text() == "x: y\n"


def test_render_svg_surfaces_d2_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(r.shutil, "which", lambda _: "/bin/d2")
    monkeypatch.setattr(r.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "bad syntax"))
    with pytest.raises(RuntimeError, match="bad syntax"):
        r.render_svg("x", tmp_path / "v.d2", tmp_path / "v.svg")


@pytest.mark.skipif(shutil.which("d2") is None, reason="d2 not installed")
def test_real_d2_accepts_output(tmp_path):
    view = View("T", [ViewNode("c0", "rg", children=[ViewNode("n0", "a.b.c"), ViewNode("n1", 'q"x')])],
                [ViewEdge("n0", "n1", "", "private"), ViewEdge("n1", "n0", "peering", "solid")])
    r.render_svg(r.render_d2(view), tmp_path / "v.d2", tmp_path / "v.svg")
    assert "<svg" in (tmp_path / "v.svg").read_text()


def test_c4_presentation_composes_with_drift_and_overlay_annotations():
    view = View('C4', [ViewNode('app', 'Orders', kind='application', children=[
        ViewNode('api', 'Orders API', 'containerapps', style='azure_only', kind='component',
                 annotations=('public access: unknown',))]), ViewNode('mi', 'mi-orders', kind='identity')],
        [ViewEdge('api', 'mi', 'attachment', 'dashed', 'attachment')], ['Coverage unknown'])
    text = r.render_d2(view, style='c4')
    assert '[Application]' in text
    assert 'class: azure_only' in text
    assert 'public access: unknown' in text
    assert 'Coverage unknown' in text
    assert 'Legend' in text
    assert '#7b1fa2' in text


def test_c4_long_boundary_and_permission_labels_are_wrapped():
    scope = '/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-orders/providers/Microsoft.KeyVault/vaults/orders'
    view = View('T', [ViewNode('a', 'Orders', 'production | A subscription with a very long display name',
                               children=[ViewNode('api', 'API')], kind='application'),
                      ViewNode('mi', 'mi-orders', kind='identity')],
                [ViewEdge('mi', 'api', 'Secrets User; scope: ' + scope, kind='grant')])
    text = r.render_d2(view, style='c4')
    assert 'production | A subscription with a very long display name' not in text
    assert scope not in text
    assert 'Secrets User' in text
    assert 'width:' in text
