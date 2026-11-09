import pytest

from infradiag.applications import build_application
from infradiag.normalise import graph_from_arg
from infradiag.overlays import apply_overlay
from infradiag.rules import Rules
from infradiag.security import Grant, SecurityEvidence
from infradiag.views import build_summary, project
from tests.factories import SUB, rid


def fixture():
    mi, api, vault = [rid('rg-a', p) for p in ('Microsoft.ManagedIdentity/userAssignedIdentities/mi-orders',
        'Microsoft.App/containerApps/api', 'Microsoft.KeyVault/vaults/vault')]
    graph = graph_from_arg([
        {'id': mi, 'properties': {'principalId': 'p'}},
        {'id': api, 'tags': {'application': 'Orders'}, 'identity': {'userAssignedIdentities': {mi: {'principalId': 'p'}}}},
        {'id': vault, 'tags': {'application': 'Data'}, 'properties': {'publicNetworkAccess': 'Enabled'}},
    ])
    return graph, mi.lower(), api.lower(), vault.lower()


def test_identity_grants_retain_provenance_scope_condition_and_do_not_claim_usage():
    graph, _mi, _api, _vault = fixture()
    rules = Rules(summary_hide_types=('microsoft.managedidentity/userassignedidentities',))
    projection = project(build_summary(graph, rules), graph)
    grants = [Grant('p', 'role', 'Secrets User', f'/subscriptions/{SUB}', 'x', source) for source in ('azure', 'bicep')]
    view = apply_overlay(projection, graph, SecurityEvidence(grants=grants), mode='identity', rules=rules)
    labels = '\n'.join(e.label for e in view.edges)
    assert 'mi-orders' in labels and 'Secrets User' in labels
    assert 'inherited' in labels and 'condition: x' in labels
    assert 'azure' in labels and 'bicep' in labels
    assert 'configured use' not in labels
    assert 'attachment' in labels
    assert not projection.view.notes
    assert any('not effective' in n.lower() for n in view.notes)


def test_context_retains_workload_attribution_for_shared_identity():
    graph, mi, _api, vault = fixture()
    worker = rid('rg-a', 'Microsoft.App/jobs/worker').lower()
    extra = graph_from_arg([{'id': worker, 'tags': {'application': 'Orders'},
                            'identity': {'userAssignedIdentities': {mi: {'principalId': 'p'}}}}])
    graph.nodes.update(extra.nodes)
    p = build_application(graph, Rules(), level='context', subscription_names={})
    view = apply_overlay(p, graph, SecurityEvidence(grants=[Grant('p', 'r', 'Read', vault)]), mode='identity', rules=Rules())
    labels = '\n'.join(e.label for e in view.edges)
    assert 'api: attachment' in labels and 'worker: attachment' in labels
    assert len([n for n in view.nodes if n.kind == 'identity']) == 1


def test_network_does_not_equate_private_endpoint_to_disabled_public_access():
    graph, _, api, vault = fixture()
    subnet = rid('rg-a', 'Microsoft.Network/virtualNetworks/vnet/subnets/apps').lower()
    pe = rid('rg-a', 'Microsoft.Network/privateEndpoints/vault-pe').lower()
    extra = graph_from_arg([
        {'id': rid('rg-a', 'Microsoft.Network/networkSecurityGroups/nsg')},
        {'id': subnet, 'properties': {'networkSecurityGroup': {'id': rid('rg-a', 'Microsoft.Network/networkSecurityGroups/nsg')}}},
        {'id': pe, 'properties': {'subnet': {'id': subnet}, 'privateLinkServiceConnections': [
            {'properties': {'privateLinkServiceId': vault}}]}},
    ])
    graph.nodes.update(extra.nodes)
    graph.nodes[api].metadata['azure']['properties'] = {'configuration': {'subnetId': subnet}}
    p = build_application(graph, Rules(), level='context', subscription_names={})
    view = apply_overlay(p, graph, SecurityEvidence(), mode='network', rules=Rules())
    labels = '\n'.join(e.label for e in view.edges)
    annotations = '\n'.join(a for n in view.nodes for a in n.annotations)
    assert 'private endpoint' in labels
    assert 'integration association' in labels
    assert 'public access: Enabled' in annotations
    assert 'public access: unknown' in annotations
    assert 'networkSecurityGroup' in labels + annotations


def test_explicit_out_of_scope_identity_stays_excluded():
    graph, _, _, vault = fixture()
    rules = Rules(out_of_scope_names=('mi-*',))
    p = build_application(graph, rules, level='application', subscription_names={})
    view = apply_overlay(p, graph, SecurityEvidence(grants=[Grant('p', 'r', 'Read', vault)]), mode='identity', rules=rules)
    assert not any(n.kind == 'identity' for n in view.nodes)


def test_network_overlay_preserves_context_application_kind():
    import json

    from tests.test_c4_integration import FIXTURES
    graph = graph_from_arg(json.loads((FIXTURES / 'resources.json').read_text()))
    projection = build_application(graph, Rules(), level='context', subscription_names={})
    original = {n.key: n.kind for n in projection.view.nodes}
    view = apply_overlay(projection, graph, SecurityEvidence(), mode='network', rules=Rules())
    assert {n.key: n.kind for n in view.nodes if n.key in original} == original


@pytest.mark.parametrize("scope_kind", ["identity", "resource_group", "subscription"])
def test_role_grant_to_managed_identity_resource_is_visible(scope_kind):
    graph, mi, _, _ = fixture()
    rules = Rules(summary_hide_types=('microsoft.managedidentity/userassignedidentities',))
    projection = project(build_summary(graph, rules), graph)
    view = apply_overlay(projection, graph, SecurityEvidence(grants=[
        Grant('operator', 'r', 'Managed Identity Operator',
              {'identity': mi, 'resource_group': f'/subscriptions/{SUB}/resourceGroups/rg-a',
               'subscription': f'/subscriptions/{SUB}'}[scope_kind])]), mode='identity', rules=rules)
    assert any('Managed Identity Operator' in e.label for e in view.edges)


@pytest.mark.parametrize("view_level", ["summary", "application"])
def test_collapsed_same_named_workloads_keep_resource_attribution(view_level):
    from infradiag.model import merge_graphs
    from infradiag.rules import NameCollapse
    graph, _mi, _api, _ = fixture()
    other = rid('rg-other', 'Microsoft.App/containerApps/api')
    graph = merge_graphs(graph, graph_from_arg([{'id': other, 'identity': {'principalId': 'other-principal'},
                                             'properties': {'publicNetworkAccess': 'Disabled'}}]))
    rules = Rules(summary_collapse_names=(NameCollapse('api', 'APIs'),), applications={'overrides': [
        {'match': {'name': 'api'}, 'application': 'APIs', 'component': 'API replicas'}]})
    projection = (project(build_summary(graph, rules), graph) if view_level == 'summary' else
                  build_application(graph, rules, level='application', subscription_names={}))
    view = apply_overlay(projection, graph, SecurityEvidence(), mode='all', rules=rules)
    labels = '\n'.join(e.label for e in view.edges)
    assert 'rg-a' in labels and 'rg-other' in labels
    def annotations(nodes):
        return '\n'.join(a for n in nodes for a in (*n.annotations, annotations(n.children)))
    assert 'rg-other' in annotations(view.nodes)


@pytest.mark.parametrize("access", ["Enabled", "Disabled"])
def test_postgres_public_access_uses_service_network_properties(access):
    graph = graph_from_arg([{'id': rid('rg-data', 'Microsoft.DBforPostgreSQL/flexibleServers/db'),
                            'properties': {'network': {'publicNetworkAccess': access}}}])
    p = build_application(graph, Rules(), level='context', subscription_names={})
    view = apply_overlay(p, graph, SecurityEvidence(), mode='network', rules=Rules())
    assert any(f'public access: {access}' in a for n in view.nodes for a in n.annotations)


def test_unknown_principal_is_not_asserted_to_be_managed_identity():
    graph, _, _, vault = fixture()
    p = build_application(graph, Rules(), level='context', subscription_names={})
    view = apply_overlay(p, graph, SecurityEvidence(grants=[Grant('user-guid', 'r', 'Reader', vault)]),
                         mode='identity', rules=Rules())
    unknown = next(n for n in view.nodes if 'Unresolved principal user-guid' in n.label)
    assert unknown.kind == 'principal'
