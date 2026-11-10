"""Principal names and kinds from Microsoft Graph: extraction, cache, overlay labels, access table and filters."""
import json
from dataclasses import replace
from types import SimpleNamespace

from infradiag.access import build_access_rows, render_csv
from infradiag.applications import build_application
from infradiag.azcli import AzError
from infradiag.extract_security import _directory_principal, extract_security, load_security, resolve_directory
from infradiag.filters import GrantFilter, filter_evidence
from infradiag.normalise import graph_from_arg
from infradiag.overlays import apply_overlay
from infradiag.rules import Rules
from infradiag.security import DirectoryPrincipal, Grant, SecurityEvidence
from tests.factories import SUB, rid

SP, GRP, USR, MI, GONE = (f'{n:08x}-0000-0000-0000-000000000000' for n in (1, 2, 3, 4, 5))
SCOPE = f'/subscriptions/{SUB}'


def graph_object(kind, pid, name, **extra):
    return {'@odata.type': f'#microsoft.graph.{kind}', 'id': pid, 'displayName': name, **extra}


DIRECTORY = {
    SP: graph_object('servicePrincipal', SP, 'CrowdStrike Falcon Cloud Security', servicePrincipalType='Application',
                     appId='app-1', alternativeNames=[]),
    GRP: graph_object('group', GRP, 'Platform Admins'),
    USR: graph_object('user', USR, 'Jane Doe', userPrincipalName='jane@example.com'),
    MI: graph_object('servicePrincipal', MI, 'mi-workflow', servicePrincipalType='ManagedIdentity',
                     alternativeNames=['isExplicit=True', rid('rg-a', 'Microsoft.ManagedIdentity/userAssignedIdentities/mi-other-name')]),
}


def fake_az(directory=DIRECTORY, calls=None, graph_error=None):
    def run(args):
        if args[0] == 'rest':
            assert args[args.index('--method') + 1] == 'POST'
            assert args[args.index('--url') + 1] == 'https://graph.microsoft.com/v1.0/directoryObjects/getByIds'
            body = json.loads(args[args.index('--body') + 1])
            assert body['types'] == ['user', 'group', 'servicePrincipal', 'device'] and len(body['ids']) <= 1000
            if calls is not None:
                calls.append(body['ids'])
            if graph_error:
                raise AzError(graph_error)
            return {'value': [directory[i] for i in body['ids'] if i in directory]}
        if args[:3] == ['role', 'assignment', 'list']:
            return []
        return {'data': [{'type': 'microsoft.authorization/roleassignments', 'properties': {
            'principalId': pid.upper(), 'scope': SCOPE, 'roleDefinitionId': 'r', 'principalType': 'ServicePrincipal'}}
            for pid in (SP, GRP, USR, MI, GONE, SP)]}
    return run


def cfg_for(tmp_path):
    return SimpleNamespace(cache_dir=tmp_path, subscription=SUB)


def test_extract_resolves_every_distinct_principal_once_and_caches_directory_metadata(tmp_path):
    calls = []
    evidence = extract_security(cfg_for(tmp_path), fake_az(calls=calls))
    assert calls == [sorted([SP, GRP, USR, MI, GONE])]
    sp, usr, mi = evidence.principals[SP], evidence.principals[USR], evidence.principals[MI]
    assert (sp.name, sp.kind, sp.sp_type, sp.app_id) == ('CrowdStrike Falcon Cloud Security', 'service principal', 'Application', 'app-1')
    assert (usr.upn, usr.kind) == ('jane@example.com', 'user') and evidence.principals[GRP].kind == 'group'
    assert mi.kind == 'managed identity (user-assigned)' and mi.resource_id.endswith('/userassignedidentities/mi-other-name')
    assert load_security(cfg_for(tmp_path)).principals == evidence.principals
    raw = json.loads((tmp_path / 'security.json').read_text())
    assert raw['version'] == 2 and set(raw['principals'][SP]) == {
        'principal_id', 'name', 'object_type', 'sp_type', 'app_id', 'resource_id', 'upn', 'deleted'}


def test_ids_graph_does_not_return_are_recorded_as_deleted(tmp_path):
    evidence = extract_security(cfg_for(tmp_path), fake_az())
    assert evidence.principals[GONE].deleted and evidence.principals[GONE].kind == 'deleted'
    assert evidence.principals[GONE].label == f'Deleted principal {GONE}'
    assert load_security(cfg_for(tmp_path)).principals[GONE].deleted


def test_lookups_are_batched_at_1000_and_partial_failure_keeps_answered_batches():
    ids = [f'{n:08x}-0000-0000-0000-000000000000' for n in range(1, 2502)]
    calls = []
    found, failure = resolve_directory(ids, fake_az({i: graph_object('group', i, f'g{i}') for i in ids}, calls))
    assert [len(c) for c in calls] == [1000, 1000, 501] and len(found) == 2501 and not failure
    answered = []
    def flaky(args):
        if len(answered) == 1:
            answered.append(0)
            raise AzError('Forbidden\nsecond line is dropped')
        answered.append(1)
        return {'value': []}
    found, failure = resolve_directory(ids, flaky)
    assert failure == 'Forbidden' and all(p.deleted for p in found.values())
    assert len(found) == 1501 and len(answered) == 3  # the failed middle batch records nothing


def test_graph_failure_warns_once_on_stderr_and_does_not_fail_the_extract(tmp_path, capsys):
    evidence = extract_security(cfg_for(tmp_path), fake_az(graph_error='AADSTS: insufficient privileges'))
    assert len(evidence.grants) == 5 and evidence.principals == {}
    err = capsys.readouterr().err
    assert err.count('warning: Microsoft Graph principal lookup failed') == 1 and 'insufficient privileges' in err
    assert any('Graph principal lookup failed' in c for c in evidence.coverage)
    assert load_security(cfg_for(tmp_path)).principals == {}


def test_cache_from_before_principal_names_loads_unchanged(tmp_path):
    (tmp_path / 'security.json').write_text(json.dumps({'version': 1, 'subscription': SUB, 'coverage': ['c'], 'grants': [
        {'principal_id': SP, 'role_id': 'r', 'role_name': 'Reader', 'scope': SCOPE}]}))
    evidence = load_security(cfg_for(tmp_path))
    assert [g.principal_id for g in evidence.grants] == [SP] and evidence.principals == {} and evidence.coverage == ['c']


def test_extract_makes_no_graph_call_without_guid_principals(tmp_path):
    calls = []
    def run(args):
        calls.append(args)
        if args[:3] == ['role', 'assignment', 'list']:
            return []
        return {'data': [{'type': 'microsoft.authorization/roleassignments', 'properties': {
            'principalId': 'p', 'scope': SCOPE, 'roleDefinitionId': 'r'}}]}
    extract_security(cfg_for(tmp_path), run)
    assert not any(c[0] == 'rest' for c in calls)


# --- rendering -------------------------------------------------------------------------------------------------------

def world():
    mi = rid('rg-a', 'Microsoft.ManagedIdentity/userAssignedIdentities/mi-other-name')
    vault = rid('rg-a', 'Microsoft.KeyVault/vaults/vault')
    api = rid('rg-a', 'Microsoft.App/containerApps/api')
    graph = graph_from_arg([{'id': mi, 'properties': {}}, {'id': api, 'tags': {'application': 'Orders'}},
                            {'id': vault, 'tags': {'application': 'Orders'}}])
    return graph, vault.lower()


def directory_evidence(vault, ids=(SP, GRP, USR, MI, GONE)):
    principals = {i: _directory_principal(DIRECTORY[i]) if i in DIRECTORY else DirectoryPrincipal(i, deleted=True) for i in ids}
    return SecurityEvidence(grants=[Grant(i, 'r', 'Reader', vault, principal_type='ServicePrincipal') for i in ids],
                            principals=principals)


def overlay(evidence, graph):
    p = build_application(graph, Rules(), level='application', subscription_names={})
    return p, apply_overlay(p, graph, evidence, mode='identity', rules=Rules())


def test_principal_nodes_are_named_with_kind_and_keep_the_guid_on_hover():
    graph, vault = world()
    _p, view = overlay(directory_evidence(vault), graph)
    nodes = {n.label: n for n in view.nodes if n.kind == 'principal'}
    assert set(nodes) == {'CrowdStrike Falcon Cloud Security [Application]', 'Platform Admins [Group]',
                          'jane@example.com [User]', f'Deleted principal {GONE}'}
    assert not any(SP in n.label or GRP in n.label or USR in n.label for n in view.nodes if n.kind == 'principal' and 'Deleted' not in n.label)
    assert f'Principal ID: {SP}' in nodes['CrowdStrike Falcon Cloud Security [Application]'].tooltip
    assert 'Application ID: app-1' in nodes['CrowdStrike Falcon Cloud Security [Application]'].tooltip
    assert not any('Unresolved' in n.label for n in view.nodes)


def test_managed_identity_merges_into_its_resource_by_graph_arm_id_without_a_duplicate_node():
    graph, vault = world()
    _p, view = overlay(directory_evidence(vault), graph)
    identities = [n for n in view.nodes if n.kind in ('identity', 'identity_resource') and 'mi-other-name' in n.label]
    assert len(identities) == 1 and identities[0].kind == 'identity'  # named after the resource, not Graph's display name
    assert not any('mi-workflow' in n.label for n in view.nodes)
    (grant,) = [e for e in view.edges if e.kind == 'grant' and e.source == identities[0].key]
    assert grant.target


def test_managed_identity_without_a_visible_resource_is_a_named_principal():
    graph, vault = world()
    ev = directory_evidence(vault, (MI,))
    ev.principals[MI] = replace(ev.principals[MI], resource_id=rid('rg-z', 'Microsoft.ManagedIdentity/userAssignedIdentities/elsewhere').lower())
    _p, view = overlay(ev, graph)
    assert [n.label for n in view.nodes if n.kind == 'principal'] == ['mi-workflow [Managed identity]']


def test_without_graph_data_principals_stay_unresolved_as_before():
    graph, vault = world()
    ev = SecurityEvidence(grants=[Grant(SP, 'r', 'Reader', vault)])
    _p, view = overlay(ev, graph)
    assert [n.label for n in view.nodes if n.kind == 'principal'] == [f'Unresolved principal {SP}']


def test_access_table_has_graph_names_and_types():
    graph, vault = world()
    ev = directory_evidence(vault)
    p, _view = overlay(ev, graph)
    rows = build_access_rows(p, graph, ev, Rules())
    got = {r.principal_id: (r.principal_name, r.principal_type) for r in rows}
    assert got[SP] == ('CrowdStrike Falcon Cloud Security', 'service principal')
    assert got[GRP] == ('Platform Admins', 'group') and got[USR] == ('jane@example.com', 'user')
    assert got[MI] == ('mi-other-name', 'managed identity (user-assigned)')
    assert got[GONE] == ('deleted principal', 'deleted')
    assert 'CrowdStrike Falcon Cloud Security' in render_csv(rows)


def test_principal_kind_filter_uses_graph_kinds():
    graph, vault = world()
    ev = directory_evidence(vault)
    def kept(*kinds):
        out, _ = filter_evidence(ev, graph, GrantFilter(principal_kinds=frozenset(kinds)))
        return {g.principal_id for g in out.grants}
    assert kept('user') == {USR} and kept('group') == {GRP} and kept('service-principal') == {SP}
    assert kept('managed-identity') == {MI} and kept('deleted') == {GONE}
    assert kept('unresolved') == set()
    assert 'deleted' in GrantFilter(principal_kinds=frozenset({'deleted'})).phrases()[0]


def test_filtered_evidence_keeps_graph_names():
    # Regression: the drift / data-plane builds draw filtered evidence, which dropped the principals and left every
    # principal "Unresolved".
    graph, vault = world()
    ev = directory_evidence(vault)
    out, _ = filter_evidence(ev, graph, GrantFilter(roles=frozenset({'reader'})))
    assert out.principals == ev.principals
    _p, view = overlay(out, graph)
    assert not any('Unresolved' in n.label for n in view.nodes)


def test_device_is_looked_up_and_foreign_group_is_never_marked_deleted(tmp_path):
    dev, foreign = '0000000a-0000-0000-0000-000000000000', '0000000b-0000-0000-0000-000000000000'
    directory = {dev: graph_object('device', dev, 'LAPTOP-1')}
    def run(args):
        if args[0] == 'rest':
            body = json.loads(args[args.index('--body') + 1])
            assert 'device' in body['types']
            return {'value': [directory[i] for i in body['ids'] if i in directory]}
        if args[:3] == ['role', 'assignment', 'list']:
            return []
        return {'data': [{'type': 'microsoft.authorization/roleassignments', 'properties': {
            'principalId': pid, 'scope': SCOPE, 'roleDefinitionId': 'r', 'principalType': t}}
            for pid, t in ((dev, 'Device'), (foreign, 'ForeignGroup'))]}
    evidence = extract_security(cfg_for(tmp_path), run)
    assert evidence.principals[dev].kind == 'device' and not evidence.principals[dev].deleted
    assert foreign not in evidence.principals  # not returned, but its type was not searched: not "deleted"
    graph, vault = world()
    out, _ = filter_evidence(SecurityEvidence(grants=[Grant(dev, 'r', 'Reader', vault, principal_type='Device')],
                                              principals=evidence.principals), graph,
                             GrantFilter(principal_kinds=frozenset({'device'})))
    assert [g.principal_id for g in out.grants] == [dev]
