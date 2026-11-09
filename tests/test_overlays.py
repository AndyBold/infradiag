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
    assert 'Secrets User' in labels and 'mi-orders' not in '\n'.join(e.label for e in view.edges if e.kind == 'grant')
    assert 'subscription scope' in labels and 'conditional' in labels and 'condition: x' not in labels
    (grant,) = [e for e in view.edges if e.kind == 'grant']
    assert 'mi-orders' in grant.tooltip and 'Condition: x' in grant.tooltip and 'Roles: Secrets User' in grant.tooltip
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


def many_resource_graph(count=6):
    return graph_from_arg([{'id': rid('rg-a', f'Microsoft.KeyVault/vaults/v{i}'), 'tags': {'application': 'Data'}}
                           for i in range(count)] +
                          [{'id': rid('rg-b', 'Microsoft.KeyVault/vaults/other'), 'tags': {'application': 'Data'}}])


def grant_edges(view):
    return [e for e in view.edges if e.kind == 'grant']


def test_subscription_scope_grant_is_one_edge_to_a_subscription_node_not_one_per_resource():
    graph = many_resource_graph()
    p = build_application(graph, Rules(), level='application', subscription_names={})
    # components are distinct per resource, so a per-resource fan-out would draw seven edges
    assert len({k for k in p.resources.values()}) == 7
    view = apply_overlay(p, graph, SecurityEvidence(grants=[Grant('u', 'r', 'Reader', f'/subscriptions/{SUB}')]),
                         mode='identity', rules=Rules(), subscription_names={SUB: 'Contoso'})
    edges = grant_edges(view)
    assert len(edges) == 1
    sub = next(n for n in view.nodes if n.kind == 'subscription')
    assert edges[0].target == sub.key and f'Contoso ({SUB})' == sub.label
    assert 'subscription scope' in edges[0].label


def test_management_group_grant_targets_the_subscription_node():
    graph = many_resource_graph()
    p = build_application(graph, Rules(), level='application', subscription_names={})
    mg = '/providers/microsoft.management/managementgroups/root'
    view = apply_overlay(p, graph, SecurityEvidence(grants=[Grant('u', 'r', 'Reader', mg, subscription=SUB)]),
                         mode='identity', rules=Rules())
    edges = grant_edges(view)
    assert len(edges) == 1 and 'management group scope' in edges[0].label
    assert next(n for n in view.nodes if n.key == edges[0].target).kind == 'subscription'


def test_resource_group_grant_gets_one_edge_to_a_resource_group_node():
    graph = many_resource_graph()
    p = build_application(graph, Rules(), level='application', subscription_names={})
    view = apply_overlay(p, graph, SecurityEvidence(grants=[
        Grant('u', 'r', 'Reader', f'/subscriptions/{SUB}/resourceGroups/rg-a')]), mode='identity', rules=Rules())
    edges = grant_edges(view)
    assert len(edges) == 1
    target = next(n for n in view.nodes if n.key == edges[0].target)
    assert (target.kind, target.label) == ('resource_group', 'rg-a')


def test_resource_group_grant_covering_one_element_stays_on_the_resource():
    graph = many_resource_graph()
    p = build_application(graph, Rules(), level='application', subscription_names={})
    view = apply_overlay(p, graph, SecurityEvidence(grants=[
        Grant('u', 'r', 'Reader', f'/subscriptions/{SUB}/resourceGroups/rg-b')]), mode='identity', rules=Rules())
    edges = grant_edges(view)
    assert len(edges) == 1
    assert not any(n.kind == 'resource_group' for n in view.nodes)


def test_resource_scope_grant_stays_on_the_resource():
    graph = many_resource_graph()
    p = build_application(graph, Rules(), level='application', subscription_names={})
    scope = rid('rg-a', 'Microsoft.KeyVault/vaults/v0').lower()
    view = apply_overlay(p, graph, SecurityEvidence(grants=[Grant('u', 'r', 'Reader', scope)]), mode='identity', rules=Rules())
    edges = grant_edges(view)
    assert len(edges) == 1 and 'resource scope' in edges[0].label
    assert not any(n.kind in ('subscription', 'resource_group') for n in view.nodes)


def test_parallel_grants_between_the_same_pair_merge_into_one_edge_listing_roles():
    graph = many_resource_graph()
    p = build_application(graph, Rules(), level='application', subscription_names={})
    roles = [f'Role {i}' for i in range(7)]
    grants = [Grant('u', f'r{i}', role, f'/subscriptions/{SUB}') for i, role in enumerate(roles)]
    view = apply_overlay(p, graph, SecurityEvidence(grants=grants), mode='identity', rules=Rules())
    (edge,) = grant_edges(view)
    first = edge.label.splitlines()[0]
    assert 'Role 0, Role 1, Role 2 +4 more' in first and 'Role 3' not in edge.label


def test_distinct_principals_and_targets_stay_distinct_edges():
    graph = many_resource_graph()
    p = build_application(graph, Rules(), level='application', subscription_names={})
    grants = [Grant('u1', 'r', 'Reader', f'/subscriptions/{SUB}'), Grant('u2', 'r', 'Reader', f'/subscriptions/{SUB}')]
    view = apply_overlay(p, graph, SecurityEvidence(grants=grants), mode='identity', rules=Rules())
    assert len(grant_edges(view)) == 2


@pytest.mark.parametrize("scope", [f'/subscriptions/{SUB}', f'/subscriptions/{SUB}/resourceGroups/rg-a',
                                   '/providers/microsoft.management/managementgroups/root'])
def test_grant_drawn_to_a_scope_node_leaves_no_disconnected_identity_resource_nodes(scope):
    graph = many_resource_graph()
    mi = rid('rg-a', 'Microsoft.ManagedIdentity/userAssignedIdentities/hidden-mi').lower()
    graph.nodes.update(graph_from_arg([{'id': mi, 'properties': {'principalId': 'x'}}]).nodes)
    p = build_application(graph, Rules(summary_hide_types=('microsoft.managedidentity/userassignedidentities',)),
                          level='application', subscription_names={})
    view = apply_overlay(p, graph, SecurityEvidence(grants=[Grant('u', 'r', 'Reader', scope, subscription=SUB)]),
                         mode='identity', rules=Rules())
    assert not any(n.kind == 'identity_resource' for n in view.nodes)
    connected = {e.source for e in view.edges} | {e.target for e in view.edges}
    assert all(n.key in connected for n in view.nodes if n.kind in ('subscription', 'resource_group', 'principal'))


def declared_and_observed(*, in_bicep=True, in_azure=True, attach_in=('bicep', 'azure')):
    """An API and its user-assigned identity as what-if declares them (no principal ID: it is only known after
    deployment) and as Resource Graph observes them (principal ID filled in)."""
    from infradiag.model import merge_graphs
    from infradiag.normalise import graph_from_whatif
    mi, api = (rid('rg-a', p) for p in ('Microsoft.ManagedIdentity/userAssignedIdentities/mi-orders',
                                         'Microsoft.App/containerApps/api'))
    declared = [{'changeType': 'NoChange', 'resourceId': api, 'after': {
        'id': api, 'name': 'api', 'tags': {'application': 'Orders'}, 'properties': {},
        'identity': {'type': 'UserAssigned', 'userAssignedIdentities': {mi: {}} if 'bicep' in attach_in else {}}}}]
    observed = [{'id': api, 'tags': {'application': 'Orders'}, 'identity': {
        'type': 'UserAssigned',
        'userAssignedIdentities': {mi: {'principalId': 'P-1'}} if 'azure' in attach_in else {}}}]
    if in_bicep:
        declared.append({'changeType': 'NoChange', 'resourceId': mi,
                         'after': {'id': mi, 'name': 'mi-orders', 'properties': {}}})
    if in_azure:
        observed.append({'id': mi, 'properties': {'principalId': 'P-1'}})
    bicep, _ = graph_from_whatif({'app': {'status': 'Succeeded', 'changes': declared}})
    return merge_graphs(bicep, graph_from_arg(observed)), mi.lower()


def identity_nodes(view):
    return [n for n in view.nodes if n.kind in ('identity', 'principal')]


def application_overlay(graph, grants=()):
    p = build_application(graph, Rules(), level='application', subscription_names={})
    return apply_overlay(p, graph, SecurityEvidence(grants=list(grants)), mode='identity', rules=Rules())


def test_declared_and_observed_copies_of_an_identity_are_one_node_with_both_provenances():
    # Regression: USA-NonProd drew mi-app-api-usa-dev0-scus twice, as `azure | principal: <guid>` and
    # `bicep | principal: unknown`, because overlay nodes were keyed by (source, principal or identity ID).
    graph, _mi = declared_and_observed()
    view = application_overlay(graph)
    (node,) = identity_nodes(view)
    assert node.label == 'mi-orders'
    assert node.sublabel == 'azure, bicep | principal: p-1'
    attachments = [e for e in view.edges if e.kind == 'attachment']
    assert len(attachments) == 1 and attachments[0].label.endswith('attachment (azure, bicep)')


def test_grants_from_either_source_reach_the_same_identity_by_principal_id():
    graph, _mi = declared_and_observed()
    vault = rid('rg-a', 'Microsoft.KeyVault/vaults/vault').lower()
    graph.nodes.update(graph_from_arg([{'id': vault, 'tags': {'application': 'Data'}}]).nodes)
    # what-if resolves an existing identity's principal ID in a role assignment even though the identity's own
    # after-state omits it; Resource Graph sees the same assignment.
    grants = [Grant('p-1', 'r', 'Secrets User', vault, source=s) for s in ('azure', 'bicep')]
    view = application_overlay(graph, grants)
    (node,) = identity_nodes(view)
    (edge,) = [e for e in view.edges if e.kind == 'grant']
    assert edge.source == node.key and 'mi-orders' in edge.tooltip and 'azure, bicep' in edge.label


def test_unresolved_principal_granted_in_both_sources_is_one_node():
    graph, _mi = declared_and_observed()
    vault = rid('rg-a', 'Microsoft.KeyVault/vaults/vault').lower()
    graph.nodes.update(graph_from_arg([{'id': vault, 'tags': {'application': 'Data'}}]).nodes)
    view = application_overlay(graph, [Grant('user-guid', 'r', 'Reader', vault, source=s) for s in ('azure', 'bicep')])
    unresolved = [n for n in identity_nodes(view) if n.kind == 'principal']
    assert [(n.label, n.sublabel) for n in unresolved] == [('Unresolved principal user-guid', 'azure, bicep')]


@pytest.mark.parametrize('in_bicep, in_azure, attach_in, sublabel, attachment', [
    (True, False, ('bicep',), 'bicep | principal: unknown', 'attachment (bicep)'),
    (False, True, ('azure',), 'azure | principal: p-1', 'attachment (azure)'),
    (True, True, ('azure',), 'azure, bicep | principal: p-1', 'attachment (azure)'),
])
def test_identity_drift_between_sources_stays_visible(in_bicep, in_azure, attach_in, sublabel, attachment):
    graph, _mi = declared_and_observed(in_bicep=in_bicep, in_azure=in_azure, attach_in=attach_in)
    view = application_overlay(graph)
    (node,) = identity_nodes(view)
    assert node.sublabel == sublabel
    assert [e.label.split(': ', 1)[1] for e in view.edges if e.kind == 'attachment'] == [attachment]


def test_identity_bicep_references_but_does_not_declare_keeps_bicep_provenance():
    # e.g. a shared identity in another resource group that the template attaches as an `existing` resource
    graph, _mi = declared_and_observed(in_bicep=False)
    (node,) = identity_nodes(application_overlay(graph))
    assert node.sublabel == 'azure, bicep | principal: p-1'


def test_grant_label_omits_the_scope_path_and_the_tooltip_lists_every_scope():
    graph = many_resource_graph()
    p = build_application(graph, Rules(), level='application', subscription_names={})
    scopes = [f'/subscriptions/{SUB}', '/providers/Microsoft.Management/managementGroups/root']
    grants = [Grant('u', f'r{i}', f'Role {i}', scope, subscription=SUB) for i, scope in enumerate(scopes)]
    view = apply_overlay(p, graph, SecurityEvidence(grants=grants), mode='identity', rules=Rules())
    (edge,) = grant_edges(view)
    assert '/subscriptions/' not in edge.label and 'scope:' not in edge.label and 'more' not in edge.label.splitlines()[1]
    assert 'subscription scope / management group scope' in edge.label
    assert all(s in edge.tooltip for s in scopes)


def test_single_scope_tooltip_is_that_scope():
    graph = many_resource_graph()
    p = build_application(graph, Rules(), level='application', subscription_names={})
    view = apply_overlay(p, graph, SecurityEvidence(grants=[Grant('u', 'r', 'Reader', f'/subscriptions/{SUB}')]),
                         mode='identity', rules=Rules())
    (edge,) = grant_edges(view)
    assert edge.tooltip.splitlines()[1:] == ['Roles: Reader', f'Scope: /subscriptions/{SUB}']


def test_long_condition_unresolved_role_paths_and_extra_roles_move_to_the_tooltip():
    graph = many_resource_graph()
    p = build_application(graph, Rules(), level='application', subscription_names={})
    role_path = f'/subscriptions/{SUB}/providers/Microsoft.Authorization/roleDefinitions/acdd72a7-3385'
    cond = 'ForAnyOfAnyValues:GuidEquals {' + ', '.join(f'guid-{i}' for i in range(80)) + '}'
    grants = [Grant('u', 'r1', 'Reader', f'/subscriptions/{SUB}', condition=cond),
              Grant('u', 'r2', role_path, f'/subscriptions/{SUB}')] + [
        Grant('u', f'r{i}', f'Role {i}', f'/subscriptions/{SUB}') for i in range(3, 8)]
    view = apply_overlay(p, graph, SecurityEvidence(grants=grants), mode='identity', rules=Rules())
    (edge,) = grant_edges(view)
    assert len(edge.label) < 120 and 'guid-1' not in edge.label and SUB not in edge.label
    assert 'Reader, acdd72a7-3385, Role 3 +4 more' in edge.label and 'conditional' in edge.label
    assert 'Role 7' in edge.tooltip and role_path in edge.tooltip and cond in edge.tooltip
