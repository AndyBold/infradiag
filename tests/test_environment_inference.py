import csv
import io

import pytest

from infradiag.access import build_access_rows
from infradiag.applications import build_application
from infradiag.config import ConfigError
from infradiag.normalise import graph_from_arg
from infradiag.render_d2 import render_d2
from infradiag.rules import Rules, _applications
from infradiag.security import Grant, SecurityEvidence
from infradiag.split import environments
from infradiag.untagged import build_untagged_rows, write_untagged
from infradiag.views import ViewProjection
from tests.factories import SUB, rid


def graph_of(*items):
    """(resource group, type/name, tags) -> graph."""
    return graph_from_arg([{'id': rid(rg, path), 'tags': tags} for rg, path, tags in items])


def decisions_for(graph, **applications):
    p = build_application(graph, Rules(applications=applications), level='application', subscription_names={})
    return {graph.nodes[r].name: d for r, d in p.env_decisions.items()}, p


def test_environment_tag_is_never_overridden_by_a_name_or_resource_group_pattern():
    graph = graph_of(('rg-app-qa-scus', 'Microsoft.KeyVault/vaults/kv-dev0-keys', {'environment': 'prod'}))
    (d,) = decisions_for(graph)[0].values()
    assert (d.environment, d.source, d.detail) == ('prod', 'tag', 'environment=prod')


def test_priority_is_override_then_tag_then_name_pattern_then_resource_group_then_unspecified():
    graph = graph_of(
        ('rg-x-dev1', 'Microsoft.KeyVault/vaults/kv-forced', {'environment': 'qa'}),       # override beats the tag
        ('rg-x-dev1', 'Microsoft.KeyVault/vaults/kv-tagged', {'environment': 'uat'}),      # tag beats the patterns
        ('rg-x-dev1', 'Microsoft.KeyVault/vaults/kv-qa-named', {}),                        # name beats resource group
        ('rg-x-dev1', 'Microsoft.KeyVault/vaults/kv-plain', {}),                           # resource group
        ('rg-x', 'Microsoft.KeyVault/vaults/kv-plain', {}),                                # nothing matches
    )
    d, _ = decisions_for(graph, overrides=[{'match': {'name': 'kv-forced'}, 'environment': 'int'}])
    assert (d['kv-forced'].environment, d['kv-forced'].source) == ('int', 'override')
    assert d['kv-forced'].detail == 'applications.overrides[0]'
    assert (d['kv-tagged'].environment, d['kv-tagged'].source) == ('uat', 'tag')
    assert (d['kv-qa-named'].environment, d['kv-qa-named'].source) == ('qa', 'name pattern')
    plain = [x for r, x in build_application(graph, Rules(), level='application', subscription_names={}).env_decisions.items()
             if graph.nodes[r].name == 'kv-plain']
    assert sorted((x.environment, x.source) for x in plain) == [('dev1', 'resource group pattern'), ('unspecified', 'unspecified')]


def test_decision_records_the_rule_and_what_it_matched():
    graph = graph_of(('rg-x', 'Microsoft.Network/virtualNetworks/vnet-usa-dev0-scus', {}))
    d, _ = decisions_for(graph)
    (decision,) = d.values()
    assert decision.source == 'name pattern' and "matched 'dev0' in vnet-usa-dev0-scus" in decision.detail
    assert decision.text().startswith('dev0 (name pattern: ')


def test_default_rules_match_whole_words_and_do_not_read_non_prod_as_prod():
    names = {'rg-usa-shared-scus': 'shared', 'id-api-usa-lower-scus': 'lower', 'link-vnet-usa-qa-scus': 'qa',
             'kv-usa-non-prod-scus': 'unspecified', 'devices-hub': 'unspecified', 'aks-prod': 'prod', 'DEV2-web': 'dev2'}  # ids are lower-cased
    graph = graph_of(*[('rg', f'Microsoft.KeyVault/vaults/{n}', {}) for n in names])
    d, _ = decisions_for(graph)
    assert {n: x.environment for n, x in d.items()} == {n.lower(): e for n, e in names.items()}


def test_configured_rules_replace_the_defaults_and_can_target_the_resource_group_only():
    graph = graph_of(('rg-payments-green', 'Microsoft.KeyVault/vaults/kv-dev0-a', {}),
                     ('rg-other', 'Microsoft.KeyVault/vaults/kv-b', {}))
    rules = [{'pattern': r'-(green|blue)$', 'environment': r'slot-\1', 'on': ['resource_group']}]
    d, _ = decisions_for(graph, environment_rules=rules)
    assert (d['kv-dev0-a'].environment, d['kv-dev0-a'].source) == ('slot-green', 'resource group pattern')
    assert d['kv-b'].environment == 'unspecified'
    d, _ = decisions_for(graph, environment_rules=[])  # an empty list switches inference off
    assert {x.environment for x in d.values()} == {'unspecified'}


@pytest.mark.parametrize('rule', [
    {'pattern': '(', 'environment': 'x'}, {'pattern': 'a', 'environment': r'\1'}, {'pattern': 'a'},
    {'pattern': 'a', 'environment': 'x', 'on': ['tags']}, {'pattern': 'a', 'environment': 'x', 'extra': 1}, 'dev'])
def test_bad_environment_rules_are_rejected(rule):
    with pytest.raises(ConfigError, match='environment_rules'):
        _applications({'environment_rules': [rule]})


def test_inferred_resources_move_out_of_unspecified_into_their_environment():
    graph = graph_of(('rg-app-dev0-scus', 'Microsoft.KeyVault/vaults/kv-a', {'environment': 'dev0', 'application': 'App'}),
                     ('rg-app-dev0-scus', 'Microsoft.Network/privateDnsZones/zone.example', {'application': 'App'}),
                     ('rg-misc', 'Microsoft.Network/networkWatchers/nw', {}))
    _, p = decisions_for(graph)
    assert environments(p.view) == ['dev0', 'unspecified']
    assert any('dev0' in c.tooltip and 'resource group pattern' in c.tooltip for n in p.view.nodes for c in n.children)
    off = build_application(graph, Rules(applications={'environment_rules': []}), level='application', subscription_names={})
    assert environments(off.view) == ['dev0', 'unspecified']
    assert len(off.view.nodes) == 3 and len(p.view.nodes) == 2


def test_hover_tooltip_reaches_the_d2_source():
    graph = graph_of(('rg-app-dev0-scus', 'Microsoft.KeyVault/vaults/kv-a', {}))
    _, p = decisions_for(graph)
    text = render_d2(p.view, style='c4')
    assert 'tooltip: "Environment: dev0 (resource group pattern: ' in text


def test_untagged_report_lists_inferred_and_unspecified_with_the_rule_and_a_suggested_tag(tmp_path):
    graph = graph_of(('rg-app-dev0-scus', 'Microsoft.KeyVault/vaults/kv-tagged', {'environment': 'dev0'}),
                     ('rg-app-dev0-scus', 'Microsoft.KeyVault/vaults/kv-inferred', {}),
                     ('rg-misc', 'Microsoft.Network/networkWatchers/nw', {}),
                     ('rg-misc', 'Microsoft.KeyVault/vaults/kv-forced', {}))
    d, p = decisions_for(graph, overrides=[{'match': {'name': 'kv-forced'}, 'environment': 'qa'}])
    path = tmp_path / 'x-untagged.csv'
    report = write_untagged(p.env_decisions, graph, path)
    rows = list(csv.reader(io.StringIO(path.read_text())))
    assert rows[0][:2] == ['Resource', 'Resource type'] and len(rows) == 3  # tagged and overridden are not listed
    by_name = {r[0]: r for r in rows[1:]}
    inferred, unspecified = by_name['kv-inferred'], by_name['nw']
    assert inferred[4] == 'dev0' and inferred[5] == 'resource group pattern' and 'rg-app-dev0-scus' in inferred[6] and inferred[7] == 'environment=dev0'
    assert unspecified[4:6] == ['unspecified', 'unspecified'] and unspecified[7] == ''
    assert (report.inferred, report.unspecified, report.by_source) == (1, 1, {'resource group pattern': 1})
    assert '1 resource(s)' in report.summary('x-untagged.csv') and '1 stay unspecified' in report.summary('x-untagged.csv')
    assert build_untagged_rows(p.env_decisions, graph, 'env')[0][7].startswith('env=')


def test_access_rows_say_how_each_environment_was_decided():
    graph = graph_of(('rg-app-dev0-scus', 'Microsoft.KeyVault/vaults/kv-tagged', {'environment': 'dev0'}),
                     ('rg-app-dev0-scus', 'Microsoft.KeyVault/vaults/kv-inferred', {}),
                     ('rg-misc', 'Microsoft.KeyVault/vaults/kv-none', {}))
    _, p = decisions_for(graph)
    inferred = rid('rg-app-dev0-scus', 'Microsoft.KeyVault/vaults/kv-inferred').lower()
    grants = [Grant('p', 'r', 'Reader', inferred), Grant('p', 'r', 'Reader', f'/subscriptions/{SUB}')]
    rows = build_access_rows(ViewProjection(p.view, p.resources, p.env_decisions), graph, SecurityEvidence(grants=grants), Rules())
    resource = next(r for r in rows if r.scope_kind == 'resource')
    assert (resource.environment, resource.environment_source) == ('dev0', 'resource group pattern')
    sub = {r.environment: r.environment_source for r in rows if r.scope_kind == 'subscription'}
    assert sub == {'dev0': 'resource group pattern, tag', 'unspecified': 'unspecified'}
