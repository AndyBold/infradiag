import csv
import io
import re

from infradiag.access import IndexEntry, build_access_rows, for_environment, render_csv, render_html, render_index
from infradiag.applications import EXTERNAL, build_application
from infradiag.model import STUB
from infradiag.normalise import graph_from_arg
from infradiag.overlays import apply_overlay
from infradiag.rules import Rules
from infradiag.security import Grant, SecurityEvidence
from infradiag.split import environments, split_by_environment
from tests.factories import SUB, node, rid

OTHER_SUB = '0a1b2c3d-0000-4000-8000-0000000000ff'


def fixture():
    mi = rid('rg-a', 'Microsoft.ManagedIdentity/userAssignedIdentities/mi-orders')
    api0, api1 = rid('rg-a', 'Microsoft.App/containerApps/api0'), rid('rg-b', 'Microsoft.App/containerApps/api1')
    vault = rid('rg-s', 'Microsoft.KeyVault/vaults/vault')
    ext = rid('rg-x', 'Microsoft.Storage/storageAccounts/outside')
    graph = graph_from_arg([
        {'id': mi, 'properties': {'principalId': 'p'}},
        {'id': api0, 'tags': {'application': 'Orders', 'environment': 'dev0'},
         'identity': {'userAssignedIdentities': {mi: {'principalId': 'p'}}}},
        {'id': api1, 'tags': {'application': 'Orders', 'environment': 'dev1'}},
        {'id': vault, 'tags': {'application': 'Data', 'environment': 'shared'}},
    ])
    graph.nodes[ext.lower()] = node(ext.lower(), sources=(), flags={STUB})
    from infradiag.model import REF, Edge
    api0, api1, vault, ext = api0.lower(), api1.lower(), vault.lower(), ext.lower()
    graph.edges |= {Edge(api0, vault, REF, 'ref'), Edge(api0, api1, REF, 'ref'), Edge(api1, vault, REF, 'ref'),
                    Edge(api1, ext, REF, 'ref')}
    return graph, {'mi': mi.lower(), 'api0': api0, 'api1': api1, 'vault': vault, 'ext': ext}


def grants(ids):
    return SecurityEvidence(grants=[
        Grant('p', '/providers/microsoft.authorization/roledefinitions/r1', 'Secrets User', ids['vault'], source='azure'),
        Grant('p', '/providers/microsoft.authorization/roledefinitions/r2', 'Reader', f'/subscriptions/{SUB}', source='bicep'),
        Grant('p', '/providers/microsoft.authorization/roledefinitions/r3', 'Contributor',
              f'/subscriptions/{SUB}/resourcegroups/rg-b', 'cond', 'azure', principal_type='ServicePrincipal'),
        Grant('q', '/providers/microsoft.authorization/roledefinitions/r4', 'Owner',
              '/providers/microsoft.management/managementgroups/mg1', source='azure', subscription=SUB),
        Grant('nobody', '/providers/microsoft.authorization/roledefinitions/r5', 'Reader',
              f'/subscriptions/{OTHER_SUB}', source='azure', principal_type='User'),
    ])


def build(level='application'):
    graph, ids = fixture()
    p = build_application(graph, Rules(), level=level, subscription_names={SUB: 'Contoso'})
    view = apply_overlay(p, graph, grants(ids), mode='identity', rules=Rules(), subscription_names={SUB: 'Contoso'})
    return graph, ids, p, view


def labels(view):
    return {n.key: n.label for n in view.nodes}


def test_environment_comes_from_the_model_not_labels():
    _g, _i, p, _v = build()
    assert environments(p.view) == ['dev0', 'dev1', 'shared', 'unspecified']  # the untagged identity
    external = next(n for n in p.view.nodes if n.label == EXTERNAL)
    assert external.environment == ''


def test_each_environment_diagram_holds_only_its_own_containers_with_stubs_for_the_rest():
    _g, _i, _p, view = build()
    parts = {e.environment: e.view for e in split_by_environment(view)}
    dev0 = parts['dev0']
    own = [n for n in dev0.nodes if n.environment]
    assert [n.environment for n in own] == ['dev0'] and own[0].children
    stubs = {n.label: n for n in dev0.nodes if n.kind == 'stub'}
    assert set(stubs) == {'Orders', 'Data'}  # dev1's and shared's containers, collapsed
    assert all(not s.children and s.style == 'stub' for s in stubs.values())
    assert any('environment: dev1' in s.sublabel for s in stubs.values())
    assert any('environment: shared' in s.sublabel for s in stubs.values())


def test_cross_environment_edges_become_stub_edges_and_unrelated_edges_are_dropped():
    _g, _i, p, view = build()
    parts = {e.environment: e.view for e in split_by_environment(view)}
    keys = {n.label + n.sublabel.split('\n')[0]: n.key for n in p.view.nodes}
    dev1, shared = parts['dev1'], parts['shared']
    names = {k: n for n in dev1.nodes for k in [n.key]}
    # dev1's diagram: edge to shared (stub), edge from dev0 (stub), edge to the external dependency
    edge_kinds = {(names[e.source].label if e.source in names else '?', names[e.target].label if e.target in names else '?')
                  for e in dev1.edges}
    assert ('?', 'Data') in edge_kinds        # api1 -> shared stub
    assert ('Orders', '?') in edge_kinds      # dev0 stub -> api1
    ext = next(n for n in dev1.nodes if n.label == 'outside')
    assert ext.kind != 'stub' or not ext.children
    # shared's diagram has no edge between the two other environments
    shared_nodes = {n.key for n in shared.nodes} | {c.key for n in shared.nodes for c in n.children}
    assert all(e.source in shared_nodes and e.target in shared_nodes for e in shared.edges)
    assert len([e for e in shared.edges if e.kind == 'dependency']) == 2
    assert keys  # model keys exist


def test_overlay_grants_are_filtered_to_the_environments_they_touch():
    _g, _i, _p, view = build()
    parts = {e.environment: e.view for e in split_by_environment(view)}
    def grant_labels(v):
        return '\n'.join(e.label for e in v.edges if e.kind == 'grant')
    assert 'Secrets User' in grant_labels(parts['shared']) and 'Secrets User' not in grant_labels(parts['dev0'])
    # subscription-scope grant (Reader) and management-group grant (Owner) reach every environment in SUB
    for env in ('dev0', 'dev1', 'shared'):
        assert 'Reader' in grant_labels(parts[env]) and 'Owner' in grant_labels(parts[env])
    # the resource-group grant covers rg-b (dev1) only
    assert 'Contributor' in grant_labels(parts['dev1'])
    assert 'Contributor' not in grant_labels(parts['dev0']) and 'Contributor' not in grant_labels(parts['shared'])
    # the grant in a subscription with no visible resources touches no environment
    assert all('nobody' not in e.label and 'unresolved' not in e.label.lower() for e in parts['dev0'].edges
               if e.kind == 'grant' and 'Reader' in e.label and OTHER_SUB in e.label)


def test_scope_stub_says_how_much_of_the_environment_it_covers():
    _g, _i, _p, view = build()
    dev0 = {e.environment: e.view for e in split_by_environment(view)}['dev0']
    scope = next(n for n in dev0.nodes if n.kind == 'subscription')
    assert scope.annotations == ('covers 1 element(s) in this environment',)
    assert not scope.children


def test_principal_is_a_flat_copy_in_each_environment_it_touches():
    _g, _i, _p, view = build()
    for e in split_by_environment(view):
        principals = [n for n in e.view.nodes if n.kind in ('identity', 'principal')]
        assert principals and all(not n.children for n in principals)


def test_split_diagrams_render_with_d2_and_keep_unique_keys():
    from infradiag.render_d2 import render_d2
    _g, _i, _p, view = build()
    for e in split_by_environment(view):
        text = render_d2(e.view, style='c4')
        assert e.environment in text
        keys = [n.key for n in e.view.nodes]
        assert len(keys) == len(set(keys))


# --- access table -----------------------------------------------------------------------------------------------

def rows_for(level='application'):
    graph, ids, p, _view = build(level)
    return build_access_rows(p, graph, grants(ids), Rules(), {SUB: 'Contoso'}), ids


def test_every_grant_is_in_the_table_including_subscription_and_mg_scope_and_uncovered_ones():
    rows, _ids = rows_for()
    by_role = {}
    for r in rows:
        by_role.setdefault(r.role, []).append(r)
    assert set(by_role) == {'Secrets User', 'Reader', 'Contributor', 'Owner'} and len(rows) >= 5
    assert [r for r in rows if r.principal_id == 'nobody']  # listed although no resource is in the view
    reader = [r for r in by_role['Reader'] if r.scope_kind == 'subscription' and r.source == 'bicep']
    assert {r.environment for r in reader} == {'dev0', 'dev1', 'shared', 'unspecified'}
    assert all(r.resource.startswith('Contoso') and r.covers == 1 for r in reader)
    owner = by_role['Owner']
    assert {r.scope_kind for r in owner} == {'management group'} and {r.environment for r in owner} == {'dev0', 'dev1', 'shared', 'unspecified'}
    secrets = by_role['Secrets User'][0]
    assert (secrets.resource, secrets.resource_type, secrets.scope_kind, secrets.environment) == (
        'vault', 'microsoft.keyvault/vaults', 'resource', 'shared')
    contributor = by_role['Contributor'][0]
    assert (contributor.resource, contributor.scope_kind, contributor.environment, contributor.condition) == (
        'rg-b', 'resource group', 'dev1', 'cond')


def test_principal_name_type_and_id_are_resolved():
    rows, _ = rows_for()
    p = next(r for r in rows if r.role == 'Secrets User')
    assert (p.principal_name, p.principal_type, p.principal_id) == ('mi-orders', 'managed identity (user-assigned)', 'p')
    q = next(r for r in rows if r.principal_id == 'nobody')
    assert (q.principal_name, q.principal_type) == ('unresolved principal', 'user')
    assert next(r for r in rows if r.principal_id == 'q').principal_type == 'unresolved'


def test_per_environment_rows_and_csv():
    rows, _ = rows_for()
    dev1 = for_environment(rows, 'DEV1')
    assert dev1 and {r.environment for r in dev1} == {'dev1'}
    assert {'Contributor', 'Reader', 'Owner'} <= {r.role for r in dev1}
    parsed = list(csv.reader(io.StringIO(render_csv(rows))))
    assert parsed[0][:3] == ['Principal', 'Principal type', 'Principal ID'] and len(parsed) == len(rows) + 1
    assert all(len(line) == len(parsed[0]) for line in parsed)


def test_access_html_is_self_contained_and_filterable():
    rows, _ = rows_for()
    page = render_html(rows, 'Access', 'x-access.csv', back='x-index.html')
    assert not re.search(r'https?://|//cdn|<link|src=', page)
    assert page.count('<tr>') == len(rows) + 1
    assert page.count('class="colfilter"') == 13 and 'id="q"' in page and 'Showing' in page
    assert 'x-access.csv' in page and 'x-index.html' in page
    assert 'Secrets User' in page


def test_html_escapes_cell_content():
    rows, _ = rows_for()
    from dataclasses import replace
    page = render_html([replace(rows[0], condition='<script>alert(1)</script>')], 'T', 'c.csv')
    assert '<script>alert(1)</script>' not in page and '&lt;script&gt;alert(1)&lt;/script&gt;' in page


def test_index_links_each_environment():
    page = render_index('Idx', [IndexEntry('All', 9, 8, '', '', 'a-access.html', 5),
                               IndexEntry('dev0', 3, 2, 'x-dev0.svg', 'x-dev0.html', 'x-dev0-access.html', 2)], ['note'])
    assert 'x-dev0.svg' in page and 'x-dev0.html' in page and 'access table (2)' in page and not re.search(r'https?://', page)


def test_a_real_application_named_like_the_external_root_keeps_its_environment():
    graph, _ids = fixture()
    for n in graph.nodes.values():
        if n.name == 'vault':
            n.tags['application'] = EXTERNAL
    p = build_application(graph, Rules(), level='application', subscription_names={})
    assert 'shared' in environments(p.view)


def test_elements_covered_counts_distinct_drawn_elements_not_resources():
    graph, ids = fixture()
    rules = Rules(applications={'overrides': [{'match': {'name': 'api*'}, 'application': 'Orders', 'environment': 'dev0',
                                               'component': 'api'}]})
    p = build_application(graph, rules, level='application', subscription_names={})
    rows = build_access_rows(p, graph, grants(ids), rules, {})
    reader = [r for r in rows if r.role == 'Reader' and r.scope_kind == 'subscription' and r.environment == 'dev0']
    assert [r.covers for r in reader] == [1]  # api0 and api1 are one drawn component
