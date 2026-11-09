from dataclasses import replace

from infradiag.applications import build_application
from infradiag.normalise import graph_from_arg
from infradiag.overlays import apply_overlay
from infradiag.render_d2 import render_d2
from infradiag.rules import Rules
from infradiag.security import Grant, SecurityEvidence
from infradiag.split import split_by_environment
from infradiag.views import DRIFT_NOTE, View, ViewEdge, ViewNode, edge_label, mark_drift
from tests.factories import rid


def grant(i, *sources, kind='grant'):
    return ViewEdge(f'p{i}', f't{i}', 'Reader\nsubscription scope', 'solid', kind, '', sources)


def marked(view):
    return {e.source: e.drift for e in view.edges}


def view_of(*edges):
    return mark_drift(View('v', [], list(edges)))


def test_dominant_single_source_marker_is_dropped_for_one_note_and_minority_markers_stay():
    v = view_of(*[grant(i, 'azure') for i in range(8)], grant(8, 'bicep'), grant(9, 'azure', 'bicep'))
    assert marked(v) == {**{f'p{i}': '' for i in range(8)}, 'p8': 'bicep only', 'p9': ''}
    (note,) = [n for n in v.notes if n.startswith(DRIFT_NOTE)]
    assert note.startswith(DRIFT_NOTE + '8 of 10 grant edges are Azure-only (not declared in Bicep)')
    assert 'hover' in note


def test_bicep_only_dominance_gets_its_own_wording_and_the_azure_minority_keeps_its_marker():
    v = view_of(*[grant(i, 'bicep') for i in range(6)], grant(6, 'azure'))
    assert marked(v)['p6'] == 'azure only' and not any(v.edges[i].drift for i in range(6))
    assert 'Bicep-only (declared in Bicep, not found in Azure)' in v.notes[-1]


def test_no_note_and_every_marker_when_no_source_is_the_majority():
    v = view_of(*[grant(i, 'azure') for i in range(3)], *[grant(i, 'bicep') for i in range(3, 6)], grant(6, 'azure', 'bicep'))
    assert not v.notes and sum(bool(e.drift) for e in v.edges) == 6


def test_a_diagram_with_too_few_grants_marks_each_one_source_edge_and_has_no_note():
    v = view_of(*[grant(i, 'azure') for i in range(4)])
    assert not v.notes and all(e.drift == 'azure only' for e in v.edges)


def test_attachment_and_configured_use_edges_keep_their_marker_even_in_a_dominated_diagram():
    v = view_of(*[grant(i, 'azure') for i in range(6)], grant(7, 'azure', kind='attachment'), grant(8, 'azure', kind='configured'))
    assert marked(v)['p7'] == marked(v)['p8'] == 'azure only' and v.notes


def test_marking_is_repeatable_and_replaces_the_previous_note():
    v = view_of(*[grant(i, 'azure') for i in range(6)])
    v.edges = [replace(e, sources=('bicep',)) if e.source == 'p0' else e for e in v.edges]
    mark_drift(mark_drift(v))
    assert len([n for n in v.notes if n.startswith(DRIFT_NOTE)]) == 1 and '5 of 6' in v.notes[-1]
    assert marked(v)['p0'] == 'bicep only'


def test_edge_label_joins_the_marker_to_the_detail_line_or_brackets_a_single_line():
    assert edge_label(replace(grant(0, 'azure'), drift='azure only')) == 'Reader\nazure only | subscription scope'
    assert edge_label(ViewEdge('a', 'b', 'api: attachment', drift='bicep only')) == 'api: attachment (bicep only)'
    assert edge_label(grant(0, 'azure')) == 'Reader\nsubscription scope'


def test_d2_draws_markers_only_where_decided():
    v = view_of(*[grant(i, 'azure') for i in range(6)], grant(6, 'bicep'))
    v.nodes = [ViewNode(k, k) for k in sorted({x for e in v.edges for x in (e.source, e.target)})]
    text = render_d2(v, style='c4')
    assert text.count('azure only') == 0 and text.count('bicep only | subscription scope') == 1
    assert 'Drift note: 6 of 7 grant edges are Azure-only' in text


def overlay(grants, level='application'):
    graph = graph_from_arg([{'id': rid('rg-a', f'Microsoft.KeyVault/vaults/v{i}'), 'tags': {'application': 'Data', 'environment': 'dev0'}}
                            for i in range(3)])
    p = build_application(graph, Rules(), level=level, subscription_names={})
    return apply_overlay(p, graph, SecurityEvidence(grants=grants), mode='identity', rules=Rules())


def vault_grants(n, source, role='Reader'):
    return [Grant(f'u{i}', 'r', role, rid('rg-a', 'Microsoft.KeyVault/vaults/v0').lower(), source=source) for i in range(n)]


def test_overlay_applies_the_note_to_the_diagram_it_draws():
    view = overlay(vault_grants(6, 'azure') + [
        Grant('x', 'r', 'Owner', rid('rg-a', 'Microsoft.KeyVault/vaults/v1').lower(), source='bicep')])
    edges = [e for e in view.edges if e.kind == 'grant']
    assert len(edges) == 7 and sum(bool(e.drift) for e in edges) == 1
    assert next(e for e in edges if e.drift).drift == 'bicep only' and 'Sources: bicep' in next(e for e in edges if e.drift).tooltip
    assert any(n.startswith(DRIFT_NOTE + '6 of 7') for n in view.notes)
    assert all('Sources: azure' in e.tooltip for e in edges if not e.drift)  # per-edge sources stay on hover


def test_the_note_is_decided_per_environment_diagram_after_a_split():
    graph = graph_from_arg([{'id': rid('rg-a', f'Microsoft.KeyVault/vaults/{n}'), 'tags': {'application': 'Data', 'environment': env}}
                            for n, env in (('a', 'dev0'), ('b', 'qa'))])
    p = build_application(graph, Rules(), level='application', subscription_names={})
    def vault(n):
        return rid('rg-a', f'Microsoft.KeyVault/vaults/{n}').lower()
    grants = [Grant(f'u{i}', 'r', 'Reader', vault('a'), source='azure') for i in range(6)] + [
        Grant(f'q{i}', 'r', 'Reader', vault('b'), source='bicep') for i in range(6)]
    view = apply_overlay(p, graph, SecurityEvidence(grants=grants), mode='identity', rules=Rules())
    assert not [n for n in view.notes if n.startswith(DRIFT_NOTE)]  # 6 + 6: neither source is the majority overall
    parts = {x.environment: x.view for x in split_by_environment(view)}
    for env, word in (('dev0', 'Azure-only'), ('qa', 'Bicep-only')):
        notes = [n for n in parts[env].notes if n.startswith(DRIFT_NOTE)]
        assert len(notes) == 1 and f'6 of 6 grant edges are {word}' in notes[0]
        assert not any(e.drift for e in parts[env].edges if e.kind == 'grant')


def test_with_the_drift_only_filter_every_kept_grant_is_one_source_so_the_note_still_summarises_them():
    from infradiag.filters import GrantFilter, filter_evidence
    graph = graph_from_arg([{'id': rid('rg-a', 'Microsoft.KeyVault/vaults/v0'), 'tags': {'application': 'Data', 'environment': 'dev0'}}])
    p = build_application(graph, Rules(), level='application', subscription_names={})
    scope = rid('rg-a', 'Microsoft.KeyVault/vaults/v0').lower()
    grants = [Grant(f'u{i}', 'r', 'Reader', scope, source='azure') for i in range(6)] + [
        Grant('b', 'r', 'Reader', scope, source='bicep'), Grant('b', 'r', 'Reader', scope, source='azure'),
        *[Grant(f'both{i}', 'r', 'Reader', scope, source=s) for i in range(3) for s in ('azure', 'bicep')]]
    evidence, _report = filter_evidence(SecurityEvidence(grants=grants), graph, GrantFilter(drift_only=True))
    view = apply_overlay(p, graph, evidence, mode='identity', rules=Rules(), prune_ungranted=True)
    edges = [e for e in view.edges if e.kind == 'grant']
    assert len(edges) == 6 and not any(e.drift for e in edges)
    assert any(n.startswith(DRIFT_NOTE + '6 of 6') for n in view.notes)
