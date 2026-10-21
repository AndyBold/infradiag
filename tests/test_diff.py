from pathlib import Path

from infradiag.config import DEFAULT_VIEWS, EntryPoint, Target
from infradiag.diff import compute_drift, render_markdown
from infradiag.model import AZURE, BICEP, REF, Edge, Graph
from infradiag.normalise import PropertyChange, WhatIfSignals
from infradiag.rules import Rules, load_rules, matches
from tests.factories import DEV, SHARED, node, rid

KV = rid(DEV, "Microsoft.KeyVault/vaults/kv-app-dev-weu")
SQL = rid(DEV, "Microsoft.Sql/servers/sql-app-dev-weu")
WEB = rid(DEV, "Microsoft.App/containerApps/ca-web-dev-weu")
NIC = rid(DEV, "Microsoft.Network/networkInterfaces/pe-kv.nic.1")
DASH = rid(SHARED, "Microsoft.Portal/dashboards/dash-app-shared-weu")
HUB = rid(SHARED, "Microsoft.Network/virtualNetworks/vnet-app-shared-weu")
NEW = rid(DEV, "Microsoft.Storage/storageAccounts/stnew")
TOPIC = rid(SHARED, "Microsoft.ServiceBus/namespaces/sb/topics/t1")
ELSEWHERE = rid("rg-app-platform-weu", "Microsoft.Network/privateDnsZones/z")


def cfg(failed_rgs=("rg-app-dev-weu",), rgs=("rg-app-dev-weu", "rg-app-shared-weu")) -> Target:
    eps = (
        EntryPoint("main-dev", "infra-dev", "infra/main.bicep", (), tuple(failed_rgs)),
        EntryPoint("shared", "infra-shared", "infra/shared.bicep", (), ("rg-app-shared-weu",)),
    )
    return Target(Path("."), "sub", "Sub", "westeurope", rgs, eps, Path("cache"), Path("out"))


def graphs(bicep_nodes, azure_nodes):
    b, a = Graph(), Graph()
    for n in bicep_nodes:
        b.add_node(n)
    for n in azure_nodes:
        a.add_node(n)
    return b, a


RULES = Rules(implicit_types=("microsoft.portal/*",),
              ignore_modify_paths=("properties.virtualNetworkPeerings*",))


def test_matches_is_case_insensitive_glob():
    assert matches("Microsoft.Portal/dashboards", ("microsoft.portal/*",))
    assert not matches("microsoft.keyvault/vaults", ())


def test_classifies_azure_only_resources():
    b, a = graphs(
        [node(KV, sources=[BICEP])],
        [node(KV, sources=[AZURE]), node(WEB, sources=[AZURE]), node(NIC, sources=[AZURE], managed_by="pe"),
         node(DASH, sources=[AZURE])],
    )
    signals = WhatIfSignals(ignored={WEB.lower(): {"main-dev"}}, change_types={KV.lower(): "NoChange"})
    report = compute_drift(b, a, signals, RULES, cfg())
    assert report.matched == 1
    assert [(i.name, i.note) for i in report.azure_only] == [("ca-web-dev-weu", "confirmed by what-if Ignore")]
    assert sorted(i.name for i in report.expected_implicit) == ["dash-app-shared-weu", "pe-kv.nic.1"]


def test_failed_entry_point_makes_its_rgs_unverified():
    b, a = graphs([], [node(WEB, sources=[AZURE]), node(HUB, sources=[AZURE])])
    signals = WhatIfSignals(failed={"main-dev": "InvalidTemplate"})
    report = compute_drift(b, a, signals, RULES, cfg())
    assert [(i.name, i.note) for i in report.unverified] == [("ca-web-dev-weu", "main-dev what-if failed")]
    assert [i.name for i in report.azure_only] == ["vnet-app-shared-weu"]
    assert report.failed == {"main-dev": "InvalidTemplate"}


def test_bicep_only_split_by_whatif_change_type_and_scope():
    b, a = graphs([node(NEW, sources=[BICEP]), node(TOPIC, sources=[BICEP]), node(ELSEWHERE, sources=[BICEP])], [])
    signals = WhatIfSignals(change_types={NEW.lower(): "Create", TOPIC.lower(): "NoChange",
                                          ELSEWHERE.lower(): "NoChange"})
    report = compute_drift(b, a, signals, RULES, cfg())
    assert [i.name for i in report.bicep_only] == ["stnew"]
    assert [i.name for i in report.not_indexed] == ["sb/t1"]
    assert [i.name for i in report.out_of_scope] == ["z"]


def test_every_rg_in_scope_when_subscription_lists_none():
    b, a = graphs([node(ELSEWHERE, sources=[BICEP])], [])
    signals = WhatIfSignals(change_types={ELSEWHERE.lower(): "Create"})
    report = compute_drift(b, a, signals, RULES, cfg(rgs=()))
    assert [i.name for i in report.bicep_only] == ["z"]
    assert report.out_of_scope == []


def test_modified_paths_filtered_by_ignore_rules():
    b, a = graphs([node(HUB, sources=[BICEP])], [node(HUB, sources=[AZURE])])
    signals = WhatIfSignals(modified={
        HUB.lower(): [PropertyChange("properties.virtualNetworkPeerings", "Delete"),
                      PropertyChange("properties.addressSpace.addressPrefixes.0", "Modify")],
        KV.lower(): [PropertyChange("properties.virtualNetworkPeerings", "Delete")],
    })
    report = compute_drift(b, a, signals, RULES, cfg())
    assert report.modified == {HUB.lower(): [PropertyChange("properties.addressSpace.addressPrefixes.0", "Modify")]}


def test_edge_differences_only_between_nodes_on_both_sides():
    b, a = graphs([node(KV, sources=[BICEP]), node(SQL, sources=[BICEP])],
                  [node(KV, sources=[AZURE]), node(SQL, sources=[AZURE]), node(WEB, sources=[AZURE])])
    b.edges.add(Edge(KV.lower(), SQL.lower(), REF, "properties.x.id"))
    a.edges.add(Edge(SQL.lower(), KV.lower(), REF, "properties.y.id"))
    a.edges.add(Edge(WEB.lower(), KV.lower(), REF, "properties.z.id"))
    report = compute_drift(b, a, WhatIfSignals(), RULES, cfg())
    assert [(e.source, e.target) for e in report.edges_bicep_only] == [(KV.lower(), SQL.lower())]
    assert [(e.source, e.target) for e in report.edges_azure_only] == [(SQL.lower(), KV.lower())]


def test_markdown_has_all_sections_and_failure_banner():
    b, a = graphs([node(NEW, sources=[BICEP])], [node(WEB, sources=[AZURE])])
    signals = WhatIfSignals(failed={"shared": "boom"}, change_types={NEW.lower(): "Create"})
    report = compute_drift(b, a, signals, RULES, cfg())
    md = render_markdown(report, {NEW.lower(): "stnew", WEB.lower(): "ca-web-dev-weu"})
    for heading in ("# Drift report", "## Summary", "## Failed entry points", "## In Azure, not in Bicep",
                    "## Unverified", "## Expected implicit", "## In Bicep, not in Azure",
                    "## Not indexed by Resource Graph", "## Outside query scope",
                    "## Configuration drift", "## Edge differences"):
        assert heading in md
    assert "`shared`: boom" in md
    assert "| ca-web-dev-weu |" in md


def test_default_views_are_generic_azure_rules():
    rules = load_rules(DEFAULT_VIEWS)
    assert rules.hide_managed is True
    assert rules.collapse[0].label == "dns link"
    assert rules.summary_groups[0][0] == "Connectivity"
    # Name patterns are project-specific, so the defaults have none.
    assert rules.out_of_scope_names == ()
    assert rules.summary_name_overrides == ()
    assert rules.summary_collapse_names == ()


def test_other_environment_names_are_out_of_scope():
    other = rid(SHARED, "Microsoft.Network/privateDnsZones/privatelink.vaultcore.azure.net/virtualNetworkLinks/link-vnet-app-test-weu")
    b, a = graphs([], [node(other, sources=[AZURE]), node(WEB, sources=[AZURE])])
    rules = Rules(out_of_scope_names=("*-test-*",))
    report = compute_drift(b, a, WhatIfSignals(), rules, cfg())
    assert [i.name for i in report.out_of_scope] == ["privatelink.vaultcore.azure.net/link-vnet-app-test-weu"]
    assert [i.name for i in report.azure_only] == ["ca-web-dev-weu"]


def test_load_rules_reads_out_of_scope_names(tmp_path):
    path = tmp_path / "views.yaml"
    path.write_text("out_of_scope_names: ['*-qa-*']\n")
    assert load_rules(path).out_of_scope_names == ("*-qa-*",)


from infradiag.diff import display_name
from infradiag.model import Node


def test_display_name_prefixes_parent_for_child_and_extension_resources():
    diag = KV + "/providers/Microsoft.Insights/diagnosticSettings/default"
    assert display_name(Node.from_id(diag)) == "kv-app-dev-weu/default"
    assert display_name(Node.from_id(KV)) == "kv-app-dev-weu"


def test_markdown_lists_changes_and_summarises_template_unset_properties():
    b, a = graphs([node(HUB, sources=[BICEP])], [node(HUB, sources=[AZURE])])
    signals = WhatIfSignals(modified={HUB.lower(): [
        PropertyChange("properties.defaultAction", "Modify"),
        PropertyChange("properties.zones", "Delete"),
        PropertyChange("properties.vpnStack", "Delete"),
    ]})
    md = render_markdown(compute_drift(b, a, signals, RULES, cfg()), {HUB.lower(): "vnet-app-shared-weu"})
    section = md.split("## Configuration drift", 1)[1].split("## Edge differences", 1)[0]
    assert "- `properties.defaultAction` — Modify" in section
    assert "`properties.zones`" not in section
    assert "2 properties set in Azure but not in the template" in section


from infradiag.diff import node_styles


def test_node_styles_follow_drift_categories():
    b, a = graphs([node(NEW, sources=[BICEP]), node(TOPIC, sources=[BICEP])],
                  [node(WEB, sources=[AZURE]), node(HUB, sources=[AZURE]), node(NIC, sources=[AZURE], managed_by="pe")])
    signals = WhatIfSignals(failed={"shared": "boom"},
                            change_types={NEW.lower(): "Create", TOPIC.lower(): "NoChange"})
    styles = node_styles(compute_drift(b, a, signals, RULES, cfg()))
    assert styles == {
        NEW.lower(): "bicep_only",          # would be created
        WEB.lower(): "azure_only",        # real drift
        HUB.lower(): "unverified",          # its entry point failed
    }                                       # TOPIC (not indexed) and NIC (implicit) stay normal
