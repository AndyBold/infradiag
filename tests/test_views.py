from infradiag.model import AZURE, BICEP, COLLAPSED, ENDPOINT, REF, STUB, Edge, Graph
from infradiag.rules import CollapseRule, Rules
from infradiag.views import build_detailed, filter_graph
from tests.factories import DEV, SHARED, SUB, node, rid

VNET = rid(DEV, "Microsoft.Network/virtualNetworks/vnet-app-dev-weu")
SUBNET = VNET + "/subnets/snet-pe"
HUB = rid(SHARED, "Microsoft.Network/virtualNetworks/vnet-app-shared-weu")
PE = rid(DEV, "Microsoft.Network/privateEndpoints/pe-kv")
KV = rid(DEV, "Microsoft.KeyVault/vaults/kv-app-dev-weu")
SQL = rid(DEV, "Microsoft.Sql/servers/sql-app-dev-weu")
DB = SQL + "/databases/app-db"
NIC = rid(DEV, "Microsoft.Network/networkInterfaces/pe-kv.nic.1")
ZONE = rid(SHARED, "Microsoft.Network/privateDnsZones/privatelink.vaultcore.azure.net")
LINK = ZONE + "/virtualNetworkLinks/link-dev"
PEER_A = VNET + "/virtualNetworkPeerings/peer-dev-to-hub"
PEER_B = HUB + "/virtualNetworkPeerings/peer-hub-to-dev"
ROLE = rid(DEV, "Microsoft.Authorization/roleAssignments/ra1")
PLATFORM = rid("rg-app-platform-weu", "Microsoft.Network/dnsZones/example.org")

RULES = Rules(
    hide_types=("microsoft.authorization/*", "microsoft.network/networkinterfaces"),
    hide_managed=True,
    collapse=(
        CollapseRule("microsoft.network/privatednszones/virtualnetworklinks", "virtualNetwork.id", "dns link"),
        CollapseRule("microsoft.network/virtualnetworks/virtualnetworkpeerings", "remoteVirtualNetwork.id", "peering"),
    ),
    icons={"microsoft.keyvault/vaults": "azure/Security Service Color/Key Vaults.svg"},
)


def build_graph():
    g = Graph()
    for n in (
        node(VNET), node(SUBNET), node(HUB), node(KV), node(SQL), node(DB),
        node(PE, sources=[BICEP]),
        node(NIC, sources=[AZURE], managed_by=PE),
        node(ROLE),
        node(ZONE), node(LINK, properties={"virtualNetwork": {"id": VNET}}),
        node(PEER_A, properties={"remoteVirtualNetwork": {"id": HUB}}),
        node(PEER_B, properties={"remoteVirtualNetwork": {"id": VNET}}),
        node(PLATFORM, sources=(), flags={STUB}),
        node(rid(DEV, "Microsoft.App/containerApps/ca-web"), sources=[AZURE]),
    ):
        g.add_node(n)
    g.edges |= {
        Edge(PE.lower(), SUBNET.lower(), REF, "properties.subnet.id"),
        Edge(PE.lower(), KV.lower(), REF, "properties.privateLinkServiceConnections[].properties.privateLinkServiceId"),
        Edge(ROLE.lower(), KV.lower(), REF, "properties.scope"),
        Edge(KV.lower(), PLATFORM.lower(), REF, "properties.x.id"),
        Edge(rid(DEV, "Microsoft.App/containerApps/ca-web").lower(), KV.lower(), ENDPOINT, "endpoint"),
    }
    return g


def test_filter_hides_types_and_managed_and_collapses():
    g = filter_graph(build_graph(), RULES)
    assert ROLE.lower() not in g.nodes
    assert NIC.lower() not in g.nodes
    assert LINK.lower() not in g.nodes and PEER_A.lower() not in g.nodes
    collapsed = {(e.source, e.target, e.label) for e in g.edges if e.kind == COLLAPSED}
    assert collapsed == {
        tuple(sorted((ZONE.lower(), VNET.lower()))) + ("dns link",),
        tuple(sorted((VNET.lower(), HUB.lower()))) + ("peering",),
    }
    assert not any(e.source == ROLE.lower() for e in g.edges)


def find(view_nodes, label):
    for n in view_nodes:
        if n.label == label:
            return n
        if (hit := find(n.children, label)) is not None:
            return hit
    return None


def test_detailed_nests_sub_rg_vnet_subnet_resource():
    view = build_detailed(build_graph(), RULES, subscription_names={SUB: "Contoso-Dev"})
    sub = find(view.nodes, f"Contoso-Dev ({SUB})")
    assert sub.sublabel == "subscription"
    rg = find(sub.children, DEV)
    vnet = find(rg.children, "vnet-app-dev-weu")
    subnet = find(vnet.children, "snet-pe")
    assert [c.label for c in subnet.children] == ["pe-kv"]
    assert [c.label for c in find(rg.children, "sql-app-dev-weu").children] == ["app-db"]


def test_detailed_labels_unknown_subscription_by_id():
    view = build_detailed(build_graph(), RULES)
    assert find(view.nodes, SUB).sublabel == "subscription"


def test_detailed_styles_and_icons():
    view = build_detailed(build_graph(), RULES)
    assert find(view.nodes, "pe-kv").style == "bicep_only"
    assert find(view.nodes, "ca-web").style == "azure_only"
    assert find(view.nodes, "kv-app-dev-weu").style == "normal"
    assert find(view.nodes, "kv-app-dev-weu").icon == "azure/Security Service Color/Key Vaults.svg"
    assert find(view.nodes, "kv-app-dev-weu").sublabel == "vaults"


def test_stubs_grouped_under_external():
    view = build_detailed(build_graph(), RULES)
    external = find(view.nodes, "External / out of scope")
    assert [c.label for c in external.children] == ["example.org"]
    assert find(external.children, "example.org").style == "stub"


def test_detailed_edges_drop_placement_duplicates_and_label_collapsed():
    view = build_detailed(build_graph(), RULES)
    keys = {}

    def index(nodes):
        for n in nodes:
            keys[n.key] = n.label
            index(n.children)

    index(view.nodes)
    named = {(keys[e.source], keys[e.target], e.label, e.style) for e in view.edges}
    assert ("pe-kv", "snet-pe", "", "solid") not in named
    assert ("pe-kv", "kv-app-dev-weu", "", "solid") in named
    assert ("ca-web", "kv-app-dev-weu", "", "dashed") in named
    assert any(label == "peering" for *_, label, _ in named)


def test_detailed_is_deterministic():
    assert build_detailed(build_graph(), RULES) == build_detailed(build_graph(), RULES)


from infradiag.rules import NameCollapse
from infradiag.views import build_summary

ENV = rid(DEV, "Microsoft.App/managedEnvironments/cae-app-dev-weu")
WRK1 = rid(DEV, "Microsoft.App/containerApps/ca-worker-a-dev-weu")
WRK2 = rid(DEV, "Microsoft.App/containerApps/ca-worker-b-dev-weu")
API = rid(DEV, "Microsoft.App/containerApps/ca-api-dev-weu")
APPCFG = rid(DEV, "Microsoft.AppConfiguration/configurationStores/appcfg-app-dev-weu")
VGW = rid(SHARED, "Microsoft.Network/virtualNetworkGateways/vgw-app-shared-weu")
PIP = rid(SHARED, "Microsoft.Network/publicIPAddresses/pip-app-vgw-shared-weu")
WEIRD = rid(DEV, "Microsoft.Weird/things/thing1")

SUMMARY_RULES = Rules(
    hide_types=RULES.hide_types,
    hide_managed=True,
    summary_groups=(
        ("Connectivity", ("microsoft.network/virtualnetworkgateways",)),
        ("Networking", ("microsoft.network/virtualnetworks", "microsoft.network/publicipaddresses")),
        ("Workloads", ("microsoft.app/*",)),
        ("Data & integration", ("microsoft.keyvault/vaults", "microsoft.appconfiguration/*", "microsoft.sql/servers")),
    ),
    summary_name_overrides=(("pip-app-vgw-*", "Connectivity"),),
    summary_hide_types=("microsoft.network/privateendpoints",),
    summary_nest_into_types=("microsoft.app/managedenvironments",),
    summary_collapse_names=(NameCollapse("ca-worker-*", "Workers"),),
)


def build_summary_graph():
    g = build_graph()
    for n in (node(ENV), node(WRK1), node(WRK2), node(API), node(APPCFG), node(VGW), node(PIP), node(WEIRD)):
        g.add_node(n)
    g.edges |= {
        Edge(WRK1.lower(), ENV.lower(), REF, "properties.environmentId"),
        Edge(WRK2.lower(), ENV.lower(), REF, "properties.environmentId"),
        Edge(API.lower(), ENV.lower(), REF, "properties.environmentId"),
        Edge(API.lower(), APPCFG.lower(), ENDPOINT, "endpoint"),
        Edge(WRK1.lower(), KV.lower(), ENDPOINT, "endpoint"),
        Edge(WRK2.lower(), KV.lower(), ENDPOINT, "endpoint"),
        Edge(WRK1.lower(), WRK1.lower() + "x", REF, "ignored-missing-target"),
    }
    g.edges = {e for e in g.edges if e.target in g.nodes}
    return g


def labels(nodes):
    return [n.label for n in nodes]


def test_summary_groups_in_configured_order_with_other_last():
    view = build_summary(build_summary_graph(), SUMMARY_RULES)
    assert labels(view.nodes) == ["Connectivity", "Networking", "Workloads", "Data & integration", "Other"]
    assert labels(view.nodes[0].children) == ["pip-app-vgw-shared-weu", "vgw-app-shared-weu"]
    assert "thing1" in labels(view.nodes[-1].children)


def test_summary_hides_children_stubs_and_hidden_types():
    view = build_summary(build_summary_graph(), SUMMARY_RULES)
    assert find(view.nodes, "pe-kv") is None
    assert find(view.nodes, "snet-pe") is None
    assert find(view.nodes, "app-db") is None
    assert find(view.nodes, "example.org") is None
    assert find(view.nodes, "sql-app-dev-weu") is not None


def test_summary_collapses_workers_and_nests_apps_in_environment():
    view = build_summary(build_summary_graph(), SUMMARY_RULES)
    env = find(view.nodes, "cae-app-dev-weu")
    assert sorted(labels(env.children)) == ["Workers ×2", "ca-api-dev-weu"]
    assert find(view.nodes, "ca-worker-a-dev-weu") is None


def test_summary_edges_are_merged_and_styled():
    view = build_summary(build_summary_graph(), SUMMARY_RULES)
    keys = {}

    def index(nodes):
        for n in nodes:
            keys[n.key] = n.label
            index(n.children)

    index(view.nodes)
    named = {(keys[e.source], keys[e.target], e.style) for e in view.edges}
    # edges from apps nested in the environment are drawn once, from the environment
    assert ("cae-app-dev-weu", "kv-app-dev-weu", "private") in named
    assert ("cae-app-dev-weu", "appcfg-app-dev-weu", "dashed") in named
    assert not any(s in ("Workers ×2", "ca-api-dev-weu") for s, _, _ in named)
    assert not any(t == "cae-app-dev-weu" for _, t, _ in named)
    assert len([e for e in view.edges if keys[e.source] == "cae-app-dev-weu" and keys[e.target] == "kv-app-dev-weu"]) == 1
    assert all(n.style == "normal" for n in view.nodes)


def test_detailed_uses_drift_styles_when_given():
    styles = {KV.lower(): "unverified"}
    view = build_detailed(build_graph(), RULES, drift_styles=styles)
    assert find(view.nodes, "kv-app-dev-weu").style == "unverified"
    assert find(view.nodes, "pe-kv").style == "normal"            # Bicep-only by source, but not drift
    assert find(view.nodes, "example.org").style == "stub"


MI = rid(DEV, "Microsoft.ManagedIdentity/userAssignedIdentities/mi-api-dev-weu")
TEST_VNET = rid("rg-app-test-weu", "Microsoft.Network/virtualNetworks/vnet-app-test-weu")


def test_filter_drops_out_of_scope_names_and_their_collapsed_edges():
    g = build_graph()
    g.add_node(node(TEST_VNET, sources=(), flags={STUB}))
    g.add_node(node(ZONE + "/virtualNetworkLinks/link-test", properties={"virtualNetwork": {"id": TEST_VNET}}))
    rules = Rules(collapse=RULES.collapse, out_of_scope_names=("*-test*",))
    out = filter_graph(g, rules)
    assert TEST_VNET.lower() not in out.nodes
    assert not any(TEST_VNET.lower() in (e.source, e.target) for e in out.edges)


def test_filter_drops_edges_to_hidden_edge_target_types_but_keeps_nodes():
    g = build_graph()
    g.add_node(node(MI))
    g.edges.add(Edge(PE.lower(), MI.lower(), REF, "identity.userAssignedIdentities"))
    out = filter_graph(g, Rules(hide_edges_to_types=("microsoft.managedidentity/userassignedidentities",)))
    assert MI.lower() in out.nodes
    assert not any(e.target == MI.lower() for e in out.edges)
