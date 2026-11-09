from types import SimpleNamespace

from infradiag.azcli import AzError
from infradiag.extract_security import extract_security, load_security
from tests.factories import SUB


def test_security_paginates_and_combines_inherited_grants(tmp_path):
    cfg = SimpleNamespace(cache_dir=tmp_path, subscription=SUB)
    scope = f'/subscriptions/{SUB}'
    def run(args):
        if args[:3] == ['role', 'assignment', 'list']:
            assert '--include-inherited' in args
            return [{'properties': {'principalId': 'p', 'scope': '/providers/Microsoft.Management/managementGroups/root',
                                     'roleDefinitionId': 'r'}}]
        assert args[:2] == ['graph', 'query']
        if '--skip-token' not in args:
            return {'data': [{'type': 'microsoft.authorization/roledefinitions', 'id': 'r',
                              'properties': {'roleName': 'Reader'}}], 'skip_token': 'next'}
        return {'data': [{'type': 'microsoft.authorization/roleassignments', 'properties': {
            'principalId': 'P', 'scope': scope, 'roleDefinitionId': 'r', 'condition': 'x'}}]}
    evidence = extract_security(cfg, run)
    assert len(evidence.grants) == 2
    assert {g.role_name for g in evidence.grants} == {'Reader'}
    assert {g.source for g in evidence.grants} == {'azure'}
    assert any(g.condition == 'x' for g in evidence.grants)
    assert load_security(cfg).grants == evidence.grants


def test_denied_and_missing_cache_are_unknown(tmp_path):
    cfg = SimpleNamespace(cache_dir=tmp_path, subscription=SUB)
    assert 'not extracted' in ' '.join(load_security(cfg).coverage)
    def denied(args):
        raise AzError('Forbidden')
    evidence = extract_security(cfg, denied)
    assert evidence.grants == []
    assert any('Forbidden' in c for c in evidence.coverage)
    assert any('incomplete' in c.lower() for c in load_security(cfg).coverage)


BUILTIN = 'b24988ac-6180-42a0-ab88-20f7382dd24c'
TENANT_BUILTIN = f'/providers/Microsoft.Authorization/RoleDefinitions/{BUILTIN}'


def _builtin_run(lookup, calls):
    """Resource Graph returns custom role definitions only, never built-ins."""
    def run(args):
        calls.append(args)
        if args[:3] == ['role', 'assignment', 'list']:
            return []
        if args[:3] == ['role', 'definition', 'list']:
            return lookup(args)
        return {'data': [
            {'type': 'microsoft.authorization/roledefinitions', 'id': f'/subscriptions/{SUB}/providers/'
             'Microsoft.Authorization/roleDefinitions/11111111-1111-1111-1111-111111111111',
             'properties': {'roleName': 'Custom', 'type': 'CustomRole'}},
            {'type': 'microsoft.authorization/roleassignments', 'properties': {
                'principalId': 'p', 'scope': f'/subscriptions/{SUB}', 'roleDefinitionId': TENANT_BUILTIN}}]}
    return run


def test_builtin_role_absent_from_resource_graph_resolves_via_cli(tmp_path):
    cfg = SimpleNamespace(cache_dir=tmp_path, subscription=SUB)
    calls = []
    def lookup(args):
        assert args[args.index('--name') + 1] == BUILTIN
        assert args[args.index('--subscription') + 1] == SUB
        return [{'roleName': 'Contributor', 'name': BUILTIN, 'id': f'/subscriptions/{SUB}/providers/'
                 f'Microsoft.Authorization/roleDefinitions/{BUILTIN}'}]
    evidence = extract_security(cfg, _builtin_run(lookup, calls))
    assert [g.role_name for g in evidence.grants] == ['Contributor']
    assert not any('name unavailable' in c for c in evidence.coverage)
    assert load_security(cfg).grants[0].role_name == 'Contributor'
    assert sum(a[:3] == ['role', 'definition', 'list'] for a in calls) == 1


def test_failed_role_lookup_notes_coverage_and_extract_continues(tmp_path):
    cfg = SimpleNamespace(cache_dir=tmp_path, subscription=SUB)
    def lookup(args):
        raise AzError('Forbidden')
    evidence = extract_security(cfg, _builtin_run(lookup, []))
    assert [g.role_name for g in evidence.grants] == [TENANT_BUILTIN]
    assert any('lookup failed' in c and BUILTIN in c for c in evidence.coverage)
    assert any('name unavailable' in c for c in evidence.coverage)
    assert (tmp_path / 'security.json').exists()


def test_no_role_lookup_when_everything_resolves(tmp_path):
    cfg = SimpleNamespace(cache_dir=tmp_path, subscription=SUB)
    calls = []
    def run(args):
        calls.append(args)
        if args[:3] == ['role', 'assignment', 'list']:
            return [{'roleDefinitionName': 'Reader', 'properties': {
                'principalId': 'p', 'scope': f'/subscriptions/{SUB}', 'roleDefinitionId': TENANT_BUILTIN}}]
        return {'data': [
            {'type': 'microsoft.authorization/roledefinitions', 'id': TENANT_BUILTIN.replace('b24988ac', '22222222')
             .replace('6180-42a0-ab88-20f7382dd24c', '2222-2222-2222-222222222222'),
             'properties': {'roleName': 'Custom'}},
            {'type': 'microsoft.authorization/roleassignments', 'properties': {
                'principalId': 'p', 'scope': f'/subscriptions/{SUB}',
                'roleDefinitionId': TENANT_BUILTIN.replace('b24988ac', '22222222')
                .replace('6180-42a0-ab88-20f7382dd24c', '2222-2222-2222-222222222222')}}]}
    evidence = extract_security(cfg, run)
    assert {g.role_name for g in evidence.grants} == {'Reader', 'Custom'}
    assert not any(a[:3] == ['role', 'definition', 'list'] for a in calls)
