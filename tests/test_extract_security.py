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
