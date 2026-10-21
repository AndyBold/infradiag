import pytest

from infradiag.model import AZURE, BICEP, REF, STUB, Edge, Graph, Node, merge_graphs
from tests.factories import DEV, rid

VNET = rid(DEV, "Microsoft.Network/virtualNetworks/vnet-app-dev-weu")


def test_from_id_fills_parts_and_lowercases():
    n = Node.from_id(VNET, name="vnet-app-dev-weu")
    assert n.id == VNET.lower()
    assert n.type == "microsoft.network/virtualnetworks"
    assert n.resource_group == DEV
    assert n.name == "vnet-app-dev-weu"


def test_from_id_uses_last_name_segment():
    db = rid(DEV, "Microsoft.Sql/servers/sql-1/databases/app-db")
    assert Node.from_id(db, name="sql-1/app-db").name == "app-db"
    assert Node.from_id(db).name == "app-db"


def test_from_id_rejects_non_resource_ids():
    with pytest.raises(ValueError):
        Node.from_id("/subscriptions/x/resourceGroups/rg")


def test_parent_id():
    assert Node.from_id(VNET + "/subnets/snet-aca").parent_id == VNET.lower()
    assert Node.from_id(VNET).parent_id is None


def test_add_node_merges_sources_and_keeps_non_empty_properties():
    g = Graph()
    g.add_node(Node.from_id(VNET, sources={BICEP}, entry_points={"network-dev"}, properties={"a": 1}))
    merged = g.add_node(Node.from_id(VNET.upper(), sources={AZURE}, properties={"b": 2}, managed_by="x"))
    assert merged.sources == {BICEP, AZURE}
    assert merged.entry_points == {"network-dev"}
    assert merged.properties == {"a": 1}
    assert merged.managed_by == "x"
    assert len(g.nodes) == 1


def test_add_node_fills_empty_properties():
    g = Graph()
    g.add_node(Node.from_id(VNET, sources={BICEP}))
    assert g.add_node(Node.from_id(VNET, sources={AZURE}, properties={"b": 2})).properties == {"b": 2}


def test_stub_flag_dropped_when_real_source_arrives():
    g = Graph()
    g.add_node(Node.from_id(VNET, flags={STUB}))
    assert g.add_node(Node.from_id(VNET, sources={AZURE})).flags == set()


def test_merge_graphs_does_not_mutate_inputs():
    a, b = Graph(), Graph()
    a.add_node(Node.from_id(VNET, sources={BICEP}))
    b.add_node(Node.from_id(VNET, sources={AZURE}))
    b.edges.add(Edge(VNET.lower(), VNET.lower() + "/subnets/s", REF, "x"))
    merged = merge_graphs(a, b)
    assert merged.nodes[VNET.lower()].sources == {BICEP, AZURE}
    assert a.nodes[VNET.lower()].sources == {BICEP}
    assert len(merged.edges) == 1
