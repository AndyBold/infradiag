import json

from infradiag import cli
from infradiag.access import build_access_rows
from infradiag.applications import build_application
from infradiag.filters import GrantFilter, filter_evidence
from infradiag.overlays import apply_overlay
from infradiag.rules import Rules
from infradiag.security import Grant, SecurityEvidence, classify_role, grants_from_rows
from tests.factories import SUB, rid
from tests.test_cli import SUB as CLI_SUB
from tests.test_cli import capture_d2, setup
from tests.test_overlays import many_resource_graph

SUBSCOPE = f'/subscriptions/{SUB}'
MG = '/providers/microsoft.management/managementgroups/root'
RG = f'{SUBSCOPE}/resourcegroups/rg-a'
VAULT = rid('rg-a', 'Microsoft.KeyVault/vaults/v0').lower()


def evidence():
    return SecurityEvidence(grants=[
        Grant('user1', '/providers/microsoft.authorization/roledefinitions/aaa', 'Reader', SUBSCOPE, principal_type='User',
              role_kind='control'),
        Grant('grp1', '/providers/microsoft.authorization/roledefinitions/aaa', 'Reader', MG, subscription=SUB,
              principal_type='Group', role_kind='control'),
        Grant('sp1', '/providers/microsoft.authorization/roledefinitions/bbb', 'Key Vault Secrets User', VAULT,
              principal_type='ServicePrincipal', role_kind='data'),
        Grant('ghost', '/providers/microsoft.authorization/roledefinitions/ccc', 'Mystery', RG, role_kind=''),
        Grant('sp1', '/providers/microsoft.authorization/roledefinitions/bbb', 'Key Vault Secrets User', VAULT,
              source='bicep', role_kind='data'),
        Grant('sp1', '/providers/microsoft.authorization/roledefinitions/aaa', 'Reader', VAULT, source='bicep',
              role_kind='control'),
    ])


def names(ev):
    return sorted((g.principal_id, g.role_name, g.source) for g in ev.grants)


def run(flt, ev=None):
    return filter_evidence(ev or evidence(), many_resource_graph(), flt)[0]


def test_inactive_filter_returns_the_same_evidence_and_no_annotations():
    ev = evidence()
    out, report = filter_evidence(ev, many_resource_graph(), GrantFilter())
    assert out is ev and report.lines == [] and GrantFilter().suffix() == '' and GrantFilter().title_suffix() == ''


def test_hide_scope_drops_grants_at_those_scopes():
    assert {g.principal_id for g in run(GrantFilter(hide_scopes=frozenset({'subscription'}))).grants} == {'grp1', 'sp1', 'ghost'}
    kept = run(GrantFilter(hide_scopes=frozenset({'subscription', 'management-group', 'resource-group'}))).grants
    assert {g.principal_id for g in kept} == {'sp1'}


def test_role_plane_keeps_data_or_control_and_reports_unclassified_roles():
    data, report = filter_evidence(evidence(), many_resource_graph(), GrantFilter(role_plane='data'))
    assert {g.role_name for g in data.grants} == {'Key Vault Secrets User'}
    assert any('Mystery' in line and 'unknown plane' in line for line in report.lines)
    control = run(GrantFilter(role_plane='control'))
    assert {g.role_name for g in control.grants} == {'Reader'}


def test_unclassifiable_role_is_hidden_by_either_plane_never_silently():
    for plane in ('data', 'control'):
        out, report = filter_evidence(evidence(), many_resource_graph(), GrantFilter(role_plane=plane))
        assert 'Mystery' not in {g.role_name for g in out.grants}
        assert 'Mystery' in report.text
    # without a plane filter it stays
    assert 'Mystery' in {g.role_name for g in run(GrantFilter(hide_scopes=frozenset({'tenant'}))).grants}


def test_plane_and_name_are_filled_from_another_grant_of_the_same_role_definition():
    declared = Grant('p', '/providers/microsoft.authorization/roledefinitions/bbb', 'bbb', VAULT, source='bicep')
    observed = Grant('p', '/providers/microsoft.authorization/roledefinitions/bbb', 'Key Vault Secrets User', VAULT,
                     role_kind='data')
    out = run(GrantFilter(role_plane='data', roles=frozenset({'key vault secrets user'})), SecurityEvidence(grants=[declared, observed]))
    assert [g.source for g in out.grants] == ['bicep', 'azure']


def test_principal_kind_uses_visible_identities_then_recorded_type_then_unresolved():
    graph = many_resource_graph()
    ev = evidence()
    def kinds(*k):
        return {g.principal_id for g in filter_evidence(ev, graph, GrantFilter(principal_kinds=frozenset(k)))[0].grants}
    assert kinds('user') == {'user1'} and kinds('group') == {'grp1'}
    assert kinds('service-principal') == {'sp1'}  # the bicep copy has no type: it is unresolved, not a service principal
    assert kinds('unresolved') == {'ghost', 'sp1'}
    assert kinds('user', 'group') == {'user1', 'grp1'}


def test_managed_identity_kind_comes_from_attachments():
    graph, mi = many_resource_graph(), 'mi-principal'
    from infradiag.security import Attachment
    ev = SecurityEvidence(attachments=[Attachment(VAULT, mi, 'mi-orders', '/x/mi-orders')],
                          grants=[Grant(mi, 'r', 'Reader', VAULT), Grant('other', 'r', 'Reader', VAULT)])
    out = filter_evidence(ev, graph, GrantFilter(principal_kinds=frozenset({'managed-identity'})))[0]
    assert [g.principal_id for g in out.grants] == [mi]


def test_role_names_are_case_insensitive_and_repeatable():
    out = run(GrantFilter(roles=frozenset({'key vault secrets user', 'mystery'})))
    assert {g.role_name for g in out.grants} == {'Key Vault Secrets User', 'Mystery'}


def test_drift_only_keeps_grants_one_source_evidences():
    out = run(GrantFilter(drift_only=True))
    # the vault secrets grant is evidenced by azure and bicep; every other grant by one source
    assert ('sp1', 'Key Vault Secrets User', 'azure') not in names(out) and ('sp1', 'Key Vault Secrets User', 'bicep') not in names(out)
    assert ('sp1', 'Reader', 'bicep') in names(out) and ('user1', 'Reader', 'azure') in names(out)


def test_drift_is_judged_before_other_filters_remove_a_source():
    # hiding nothing but the azure copy's scope kind must not turn a two-source grant into drift
    ev = SecurityEvidence(grants=[Grant('p', 'r', 'Reader', VAULT, source='azure'), Grant('p', 'r', 'Reader', VAULT, source='bicep')])
    assert run(GrantFilter(drift_only=True, roles=frozenset({'reader'})), ev).grants == []


def test_filters_compose_with_and():
    out = run(GrantFilter(hide_scopes=frozenset({'subscription'}), principal_kinds=frozenset({'group', 'user'}),
                          role_plane='control'))
    assert {g.principal_id for g in out.grants} == {'grp1'}


def test_stem_suffix_title_and_report_describe_the_filters_and_are_distinct():
    a = GrantFilter(hide_scopes=frozenset({'subscription', 'management-group'}), role_plane='data')
    b = GrantFilter(hide_scopes=frozenset({'subscription'}), role_plane='data')
    c = GrantFilter(roles=frozenset({'a'})), GrantFilter(roles=frozenset({'b'}))
    assert a.suffix() == '-no-sub-no-mg-data' and b.suffix() == '-no-sub-data'
    assert c[0].suffix() != c[1].suffix() and c[0].suffix().startswith('-roles-')
    assert GrantFilter(principal_kinds=frozenset({'managed-identity', 'unresolved'}), drift_only=True).suffix() == '-p-mi-unres-drift'
    assert a.title_suffix() == ' [filtered: no subscription/management-group scope; data-plane roles]'
    assert 'Showing 2 of 6 grants' in filter_evidence(evidence(), many_resource_graph(), GrantFilter(role_plane='data'))[1].text


def test_classify_role_from_definition_permissions():
    assert classify_role([{'actions': ['*'], 'dataActions': []}]) == 'control'
    assert classify_role([{'actions': ['a/read'], 'dataActions': ['b/read']}]) == 'data'
    assert classify_role([{'actions': [], 'dataActions': ['b/read']}]) == 'data'
    assert classify_role([{'actions': [], 'dataActions': []}]) == '' and classify_role(None) == ''


def test_grants_from_rows_records_the_role_plane_from_cached_definitions():
    rows = [
        {'type': 'microsoft.authorization/roledefinitions', 'id': '/subscriptions/s/providers/microsoft.authorization/roledefinitions/aaa',
         'properties': {'roleName': 'Vault User', 'permissions': [{'actions': [], 'dataActions': ['x']}]}},
        {'type': 'microsoft.authorization/roleassignments', 'id': '/x/1', 'properties': {
            'principalId': 'p', 'roleDefinitionId': '/providers/Microsoft.Authorization/roleDefinitions/AAA', 'scope': SUBSCOPE}},
        {'type': 'microsoft.authorization/roleassignments', 'id': '/x/2', 'properties': {
            'principalId': 'p', 'roleDefinitionId': '/providers/Microsoft.Authorization/roleDefinitions/ccc', 'scope': SUBSCOPE}},
    ]
    kinds = {g.role_id.rsplit('/', 1)[-1]: g.role_kind for g in grants_from_rows(rows).grants}
    assert kinds == {'aaa': 'data', 'ccc': ''}


def test_old_cache_grants_load_without_a_role_plane():
    assert Grant('p', 'r', 'n', 's').role_kind == ''


def overlay(ev, flt, graph=None):
    graph = graph or many_resource_graph()
    p = build_application(graph, Rules(), level='application', subscription_names={})
    ev, _ = filter_evidence(ev, graph, flt)
    return p, graph, ev, apply_overlay(p, graph, ev, mode='identity', rules=Rules(), prune_ungranted=flt.active)


def test_nodes_left_with_no_edges_are_not_drawn():
    flt = GrantFilter(hide_scopes=frozenset({'subscription', 'management-group', 'resource-group'}))
    _p, _g, _ev, view = overlay(evidence(), flt)
    assert not any(n.kind in ('subscription', 'resource_group') for n in view.nodes)
    assert not any(n.label.startswith('Unresolved principal ghost') for n in view.nodes)  # its only grant was RG scope
    used = {k for e in view.edges for k in (e.source, e.target)}
    assert all(n.key in used for n in view.nodes if n.kind in ('identity', 'principal', 'subscription', 'resource_group'))


def test_identity_with_only_an_attachment_is_pruned_once_a_filter_removes_its_grants():
    graph = many_resource_graph()
    mi = rid('rg-a', 'Microsoft.ManagedIdentity/userAssignedIdentities/mi').lower()
    api = rid('rg-a', 'Microsoft.App/containerApps/api').lower()
    from infradiag.normalise import graph_from_arg
    graph = graph_from_arg([
        {'id': mi, 'properties': {'principalId': 'pm'}},
        {'id': api, 'tags': {'application': 'Orders'}, 'identity': {'userAssignedIdentities': {mi: {'principalId': 'pm'}}}},
        {'id': VAULT, 'tags': {'application': 'Data'}}])
    ev = SecurityEvidence(grants=[Grant('pm', 'r', 'Reader', SUBSCOPE), Grant('u', 'r', 'Key Vault Secrets User', VAULT, role_kind='data')])
    p = build_application(graph, Rules(), level='application', subscription_names={})
    plain = apply_overlay(p, graph, ev, mode='identity', rules=Rules())
    assert any(e.kind == 'attachment' for e in plain.edges)
    flt = GrantFilter(hide_scopes=frozenset({'subscription'}))
    filtered_ev, _ = filter_evidence(ev, graph, flt)
    view = apply_overlay(p, graph, filtered_ev, mode='identity', rules=Rules(), prune_ungranted=True)
    assert not any(e.kind in ('attachment', 'configured') for e in view.edges)
    assert [n.kind for n in view.nodes if n.kind in ('identity', 'principal')] == ['principal']


def test_overlay_without_a_filter_still_draws_attachment_only_identities():
    graph, _mi, _api, _vault = __import__('tests.test_overlays', fromlist=['fixture']).fixture()
    p = build_application(graph, Rules(), level='application', subscription_names={})
    view = apply_overlay(p, graph, SecurityEvidence(), mode='identity', rules=Rules())
    assert any(n.kind == 'identity' for n in view.nodes) and any(e.kind == 'attachment' for e in view.edges)


def test_access_table_rows_follow_the_filtered_evidence():
    p, graph, ev, _view = overlay(evidence(), GrantFilter(hide_scopes=frozenset({'subscription'})))
    rows = build_access_rows(p, graph, ev, Rules())
    assert 'subscription' not in {r.scope_kind for r in rows} and {r.scope_kind for r in rows} >= {'management group'}


# --- CLI ---------------------------------------------------------------------------------------------------------

def write_security(tmp_path):
    grants = [
        {'principal_id': 'u1', 'role_id': '/providers/microsoft.authorization/roledefinitions/aaa', 'role_name': 'Reader',
         'scope': f'/subscriptions/{CLI_SUB}', 'condition': '', 'source': 'azure', 'subscription': CLI_SUB,
         'principal_type': 'User', 'role_kind': 'control'},
        {'principal_id': 'u2', 'role_id': '/providers/microsoft.authorization/roledefinitions/bbb',
         'role_name': 'Key Vault Secrets User', 'scope': f'/subscriptions/{CLI_SUB}', 'condition': '', 'source': 'azure',
         'subscription': CLI_SUB, 'principal_type': 'User', 'role_kind': 'data'}]
    (tmp_path / 'cache' / CLI_SUB / 'security.json').write_text(json.dumps(
        {'version': 1, 'subscription': CLI_SUB, 'coverage': [], 'grants': grants}))


def prepare(tmp_path, monkeypatch):
    capture_d2(monkeypatch)
    args = setup(tmp_path)
    write_security(tmp_path)
    return args


def render(args, *extra):
    return cli.main(args + ['render', '--view', 'application', '--security-overlay', 'identity', '--split', 'environment',
                            '--subscription', CLI_SUB, *extra])


def test_filters_need_an_identity_overlay(tmp_path, capsys):
    args = setup(tmp_path)
    assert cli.main(args + ['render', '--view', 'application', '--drift-only']) == 2
    assert '--security-overlay identity' in capsys.readouterr().err
    assert cli.main(args + ['render', '--view', 'application', '--security-overlay', 'network',
                                        '--role-plane', 'data']) == 2


def test_filtered_split_annotates_title_index_and_tables_and_uses_its_own_file_names(tmp_path, monkeypatch):
    args = prepare(tmp_path, monkeypatch)
    assert render(args) == 0
    out = tmp_path / 'out/applications'
    plain = {p.name for p in out.iterdir()}
    plain_index = (out / 'application-identity-index.html').read_text()
    plain_table = (out / 'application-identity-access.html').read_text()
    assert 'filtered' not in plain_index.lower() and 'filters-active' not in plain_table
    assert render(args, '--role-plane', 'data', '--hide-scope', 'resource-group') == 0
    stem = 'application-identity-no-rg-data'
    names = {p.name for p in out.iterdir()}
    assert plain <= names and f'{stem}-index.html' in names and f'{stem}-access.html' in names
    index = (out / f'{stem}-index.html').read_text()
    assert 'filtered: no resource-group scope; data-plane roles' in index and f'{stem}-unspecified.svg' in index
    assert 'application-identity-unspecified' not in index.replace(stem, '')
    table = (out / f'{stem}-access.html').read_text()
    assert 'filters-active' in table and 'same filters as the diagram' in table
    assert 'Key Vault Secrets User' in table and '>Reader<' not in table
    assert (out / f'{stem}-access.csv').read_text().count('\n') == 2  # header + the one data-plane grant
    assert (out / 'application-identity-index.html').read_text() == plain_index  # unfiltered files untouched
    assert (out / 'application-identity-access.html').read_text() == plain_table
    assert 'Filters:' in (out / f'{stem}-unspecified.d2').read_text()


def test_unfiltered_render_output_has_no_filter_text(tmp_path, monkeypatch):
    assert render(prepare(tmp_path, monkeypatch)) == 0
    out = tmp_path / 'out/applications'
    for path in out.iterdir():
        if path.suffix in ('.d2', '.html', '.csv'):
            assert 'Filters:' not in path.read_text() and 'filtered' not in path.name


def test_filters_work_on_the_per_subscription_views_too(tmp_path, monkeypatch):
    args = prepare(tmp_path, monkeypatch)
    assert cli.main(args + ['render', '--view', 'summary', '--security-overlay', 'identity',
                            '--role', 'reader', '--role', 'nothing', '--subscription', CLI_SUB]) == 0
    files = {p.name for p in (tmp_path / 'out').rglob('*') if p.is_file()}
    assert any(f.startswith('summary-identity-roles-') and f.endswith('-access.html') for f in files)
