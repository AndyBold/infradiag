from infradiag.edges import add_endpoint_edges, add_reference_edges, get_path
from infradiag.model import ENDPOINT, REF, STUB, Edge, Graph
from tests.factories import DEV, SHARED, node, rid

VNET = rid(DEV, "Microsoft.Network/virtualNetworks/vnet-app-dev-weu")
SUBNET = VNET + "/subnets/snet-pe"
NSG = rid(DEV, "Microsoft.Network/networkSecurityGroups/nsg-1")
KV = rid(DEV, "Microsoft.KeyVault/vaults/kv-app-dev-weu")
PE = rid(DEV, "Microsoft.Network/privateEndpoints/pe-kv-app-dev-weu")
APP = rid(DEV, "Microsoft.App/containerApps/ca-api-dev-weu")
APPCFG = rid(DEV, "Microsoft.AppConfiguration/configurationStores/appcfg-app-dev-weu")
ACR = rid(SHARED, "Microsoft.ContainerRegistry/registries/acrappsharedweu")
APPI = rid(DEV, "Microsoft.Insights/components/appi-app-dev-weu")


def graph_of(*nodes):
    g = Graph()
    for n in nodes:
        g.add_node(n)
    return g


def test_get_path():
    assert get_path({"a": {"b": "x"}}, "a.b") == "x"
    assert get_path({"a": "x"}, "a.b") is None


def test_private_endpoint_edges_to_subnet_and_target():
    pe = node(PE, properties={
        "subnet": {"id": SUBNET},
        "privateLinkServiceConnections": [{"properties": {"privateLinkServiceId": KV}}],
    })
    g = graph_of(pe, node(SUBNET), node(KV))
    add_reference_edges(g)
    assert g.edges == {
        Edge(PE.lower(), SUBNET.lower(), REF, "properties.subnet.id"),
        Edge(PE.lower(), KV.lower(), REF,
             "properties.privateLinkServiceConnections[].properties.privateLinkServiceId"),
    }


def test_self_parent_and_child_references_are_dropped():
    vnet = node(VNET, properties={"self": VNET, "subnets": [{"id": SUBNET}], "other": {"id": SUBNET}})
    subnet = node(SUBNET, properties={"parent": VNET, "networkSecurityGroup": {"id": NSG}})
    g = graph_of(vnet, subnet, node(NSG))
    add_reference_edges(g)
    assert g.edges == {Edge(SUBNET.lower(), NSG.lower(), REF, "properties.networkSecurityGroup.id")}


def test_runtime_back_references_are_ignored():
    subnet = node(SUBNET, properties={
        "privateEndpoints": [{"id": PE}],
        "ipConfigurations": [{"id": PE + "/ipconfig"}],
    })
    g = graph_of(subnet, node(PE))
    add_reference_edges(g)
    assert g.edges == set()


def test_unevaluated_whatif_expressions_are_skipped():
    pe = node(PE, properties={"subnet": {"id": f"[reference('{SUBNET}').id]"}})
    g = graph_of(pe, node(SUBNET))
    add_reference_edges(g)
    assert g.edges == set()


def test_targets_outside_graph_become_stubs():
    platform = rid("rg-app-platform-weu", "Microsoft.Network/privateDnsZones/zone.example")
    pe = node(PE, properties={"dns": {"id": platform + "/A/rec"}})
    g = graph_of(pe)
    add_reference_edges(g)
    assert g.nodes[platform.lower()].flags == {STUB}
    assert g.edges == {Edge(PE.lower(), platform.lower(), REF, "properties.dns.id")}


def test_endpoint_edges_from_container_apps():
    app = node(APP, properties={
        "template": {"containers": [{
            "image": "acrappsharedweu.azurecr.io/api:1.2.3",
            "env": [
                {"name": "AppConfiguration__Endpoint", "value": "https://APPCFG-app-dev-weu.azconfig.io"},
                {"name": "APPLICATIONINSIGHTS_CONNECTION_STRING",
                 "value": "InstrumentationKey=09129bc7-0b1c;IngestionEndpoint=https://x/"},
            ],
        }]},
        "configuration": {"secrets": [{"keyVaultUrl": "https://kv-app-dev-weu.vault.azure.net/secrets/s"}]},
    })
    g = graph_of(
        app,
        node(APPCFG, properties={"endpoint": "https://appcfg-app-dev-weu.azconfig.io"}),
        node(KV, properties={"vaultUri": "https://kv-app-dev-weu.vault.azure.net/"}),
        node(ACR, properties={"loginServer": "acrappsharedweu.azurecr.io"}),
        node(APPI, properties={"InstrumentationKey": "09129bc7-0b1c"}),
    )
    add_endpoint_edges(g)
    assert {(e.target, e.kind) for e in g.edges} == {
        (APPCFG.lower(), ENDPOINT), (KV.lower(), ENDPOINT), (ACR.lower(), ENDPOINT), (APPI.lower(), ENDPOINT),
    }


def test_endpoint_edges_only_from_consumers():
    other = node(PE, properties={"note": "https://kv-app-dev-weu.vault.azure.net/"})
    g = graph_of(other, node(KV, properties={"vaultUri": "https://kv-app-dev-weu.vault.azure.net/"}))
    add_endpoint_edges(g)
    assert g.edges == set()


def test_runtime_back_references_match_case_insensitively():
    ampls = rid(SHARED, "Microsoft.Insights/privateLinkScopes/ampls-1")
    appi = node(APPI, properties={"PrivateLinkScopedResources": [{"ResourceId": ampls + "/scopedresources/x"}]})
    g = graph_of(appi, node(ampls))
    add_reference_edges(g)
    assert g.edges == set()
