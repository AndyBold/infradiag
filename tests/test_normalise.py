from infradiag.model import AZURE, BICEP, UNSUPPORTED
from infradiag.normalise import PropertyChange, flatten_delta, graph_from_whatif
from tests.factories import DEV, SHARED, rid, whatif_change, whatif_result

VNET = rid(DEV, "Microsoft.Network/virtualNetworks/vnet-app-dev-weu")
HUB = rid(SHARED, "Microsoft.Network/virtualNetworks/vnet-app-shared-weu")
KV = rid(DEV, "Microsoft.KeyVault/vaults/kv-app-dev-weu")
NSG = rid(DEV, "Microsoft.Network/networkSecurityGroups/nsg-1")


def test_node_change_types_become_bicep_nodes():
    graph, signals = graph_from_whatif({"main": whatif_result(
        whatif_change("Create", KV),
        whatif_change("NoChange", NSG),
    )})
    assert set(graph.nodes) == {KV.lower(), NSG.lower()}
    assert graph.nodes[KV.lower()].sources == {BICEP}
    assert graph.nodes[KV.lower()].entry_points == {"main"}
    assert signals.change_types == {KV.lower(): "Create", NSG.lower(): "NoChange"}


def test_resource_group_changes_are_skipped():
    rg = f"/subscriptions/x/resourceGroups/{DEV}"
    graph, _ = graph_from_whatif({"net": whatif_result(whatif_change("NoChange", rg))})
    assert graph.nodes == {}


def test_ignore_is_a_signal_not_a_node():
    graph, signals = graph_from_whatif({"net": whatif_result(whatif_change("Ignore", KV))})
    assert graph.nodes == {}
    assert signals.ignored == {KV.lower(): {"net"}}


def test_unsupported_is_flagged_node():
    graph, _ = graph_from_whatif({"main": whatif_result(whatif_change("Unsupported", KV))})
    assert graph.nodes[KV.lower()].flags == {UNSUPPORTED}


def test_inline_subnets_without_id_get_synthesised_ids():
    vnet = whatif_change("Modify", VNET, properties={
        "subnets": [{"name": "snet-aca", "properties": {"networkSecurityGroup": {"id": NSG}}}],
    })
    graph, _ = graph_from_whatif({"net": whatif_result(vnet)})
    subnet = graph.nodes[VNET.lower() + "/subnets/snet-aca"]
    assert subnet.type == "microsoft.network/virtualnetworks/subnets"
    assert subnet.name == "snet-aca"
    assert subnet.properties == {"networkSecurityGroup": {"id": NSG}}
    assert subnet.sources == {BICEP}


def test_same_resource_from_two_entry_points_and_inline_is_one_node():
    peering_id = HUB + "/virtualNetworkPeerings/peer-hub-to-dev"
    shared = whatif_result(whatif_change("NoChange", HUB, properties={
        "virtualNetworkPeerings": [{"name": "peer-hub-to-dev", "properties": {}}],
    }))
    peering = whatif_result(
        whatif_change("NoChange", peering_id, name="vnet-app-shared-weu/peer-hub-to-dev",
                      properties={"remoteVirtualNetwork": {"id": VNET}}),
        whatif_change("NoChange", HUB),
    )
    graph, _ = graph_from_whatif({"shared": shared, "peering": peering})
    assert [i for i in graph.nodes if "peerings" in i] == [peering_id.lower()]
    peer = graph.nodes[peering_id.lower()]
    assert peer.entry_points == {"shared", "peering"}
    assert peer.properties == {"remoteVirtualNetwork": {"id": VNET}}
    assert graph.nodes[HUB.lower()].entry_points == {"shared", "peering"}


def test_modify_records_flattened_delta():
    delta = [
        {"path": "properties.virtualNetworkPeerings", "propertyChangeType": "Delete", "children": None},
        {"path": "properties.subnets", "propertyChangeType": "Array", "children": [
            {"path": "0", "propertyChangeType": "Modify", "children": [
                {"path": "properties.addressPrefix", "propertyChangeType": "Modify", "children": None},
            ]},
        ]},
    ]
    _, signals = graph_from_whatif({"net": whatif_result(whatif_change("Modify", VNET, delta=delta))})
    assert signals.modified[VNET.lower()] == [
        PropertyChange("properties.virtualNetworkPeerings", "Delete"),
        PropertyChange("properties.subnets.0.properties.addressPrefix", "Modify"),
    ]


def test_failed_entry_points_are_recorded_and_skipped():
    graph, signals = graph_from_whatif({
        "main": {"error": "InvalidTemplate: boom"},
        "workflow": {"status": "Failed", "error": {"code": "X", "message": "denied"}},
        "net": whatif_result(whatif_change("Create", KV)),
    })
    assert signals.failed == {"main": "InvalidTemplate: boom", "workflow": "denied"}
    assert set(graph.nodes) == {KV.lower()}


def test_flatten_delta_handles_none():
    assert flatten_delta(None) == []


from infradiag.normalise import graph_from_arg
from tests.factories import arg_row


def test_arg_rows_become_azure_nodes_with_managed_by():
    nic = rid(DEV, "Microsoft.Network/networkInterfaces/pe-kv.nic.1")
    graph = graph_from_arg([arg_row(KV), arg_row(nic, managed_by="/subscriptions/x/pe")])
    assert graph.nodes[KV.lower()].sources == {AZURE}
    assert graph.nodes[KV.lower()].managed_by is None
    assert graph.nodes[nic.lower()].managed_by == "/subscriptions/x/pe"


def test_arg_inline_subnets_keep_their_ids():
    subnet_id = VNET + "/subnets/snet-pe"
    graph = graph_from_arg([arg_row(VNET, properties={
        "subnets": [{"id": subnet_id, "name": "snet-pe", "properties": {"privateEndpoints": []}}],
    })])
    assert graph.nodes[subnet_id.lower()].sources == {AZURE}


def test_whatif_and_arg_ids_match_despite_case():
    lower_vnet = VNET.lower().replace("/resourcegroups/", "/resourceGroups/")
    bicep, _ = graph_from_whatif({"net": whatif_result(whatif_change("NoChange", VNET, properties={
        "subnets": [{"name": "snet-aca", "properties": {}}],
    }))})
    azure = graph_from_arg([arg_row(lower_vnet, properties={
        "subnets": [{"id": lower_vnet + "/subnets/SNET-ACA", "name": "snet-aca", "properties": {}}],
    })])
    assert set(bicep.nodes) == set(azure.nodes)


def test_delta_skips_noeffect_and_unevaluated_after_values():
    delta = [
        {"path": "sku.tier", "propertyChangeType": "NoEffect", "before": "Basic", "after": "Basic", "children": None},
        {"path": "properties.env.0.value", "propertyChangeType": "Modify", "before": "x",
         "after": "[reference('/subscriptions/x').outputs.y]", "children": None},
        {"path": "properties.defaultAction", "propertyChangeType": "Modify", "before": "Allow",
         "after": "Deny", "children": None},
    ]
    assert flatten_delta(delta) == [PropertyChange("properties.defaultAction", "Modify")]
