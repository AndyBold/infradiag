import pytest

from infradiag.applications import build_application
from infradiag.config import ConfigError
from infradiag.model import Edge, Graph
from infradiag.rules import Rules, load_rules
from tests.factories import SUB, node, rid


def resource(name, env='dev', app='Orders', **tags):
    return node(rid('rg-a', f'Microsoft.App/containerApps/{name}'), tags={'Application': app, 'environment': env, **tags})


def walk(nodes):
    for n in nodes:
        yield n
        yield from walk(n.children)


def test_application_keeps_components_and_context_collapses_internal_edges():
    api, worker = resource('api'), resource('worker')
    store = node(rid('rg-a', 'Microsoft.Sql/servers/db'), tags={'application': 'orders', 'environment': 'DEV'})
    graph = Graph({n.id: n for n in (api, worker, store)}, {Edge(api.id, store.id, 'ref', '')})
    app = build_application(graph, Rules(), level='application', subscription_names={SUB: 'Dev'})
    assert {n.label for n in walk(app.view.nodes)} >= {'api', 'worker', 'db'}
    assert len(app.view.edges) == 1
    context = build_application(graph, Rules(), level='context', subscription_names={})
    assert len(context.view.nodes) == 1
    assert set(context.resources.values()) == {context.view.nodes[0].key}
    assert context.view.edges == []


def test_environment_subscription_and_missing_tags_are_not_merged():
    dev, prod = resource('dev'), resource('prod', 'production')
    other = resource('other')
    other.id = other.id.replace(SUB, '11111111-1111-1111-1111-111111111111')
    other.subscription = '11111111-1111-1111-1111-111111111111'
    unknown = node(rid('rg-a', 'Microsoft.Storage/storageAccounts/unknown'))
    p = build_application(Graph({n.id: n for n in (dev, prod, other, unknown)}), Rules(), level='context', subscription_names={})
    assert len(p.view.nodes) == 4
    assert len(set(p.resources.values())) == 4
    assert any('Unclassified' in n.label and 'unspecified' in n.sublabel for n in p.view.nodes)


def test_overrides_cross_subscription_component_grouping_and_mixed_annotations(tmp_path):
    path = tmp_path / 'views.yaml'
    path.write_text('''applications:
  annotations: [owner]
  overrides:
    - match: {name: "api*"}
      application: Platform
      group: shared-platform
      shared: true
      component: API replicas
      role: API
    - match: {name: "*"}
      application: Wrong
''')
    a, b = resource('api-a', owner='Alice'), resource('api-b', owner='Bob')
    b.id = b.id.replace(SUB, '11111111-1111-1111-1111-111111111111')
    b.subscription = '11111111-1111-1111-1111-111111111111'
    p = build_application(Graph({a.id:a, b.id:b}), load_rules(path), level='application', subscription_names={})
    assert len(p.view.nodes) == 1
    assert len(p.view.nodes[0].children) == 1
    assert 'Platform' in p.view.nodes[0].label
    assert 'shared' in p.view.nodes[0].sublabel.lower()
    assert 'mixed' in p.view.nodes[0].sublabel
    assert 'API replicas' in p.view.nodes[0].children[0].label
    assert p.resources[a.id] == p.resources[b.id]


@pytest.mark.parametrize('yaml', ['applications: []', 'applications: {oops: x}',
                                  'applications: {overrides: [{match: {oops: x}}]}',
                                  'applications: {overrides: [{match: {name: x}, shared: yesplease}]}'])
def test_bad_application_rules_are_rejected(tmp_path, yaml):
    p = tmp_path / 'views.yaml'
    p.write_text(yaml)
    with pytest.raises(ConfigError, match='applications'):
        load_rules(p)
