from infradiag.normalise import graph_from_arg
from infradiag.security import declared_security, grants_from_rows, identity_attachments, scope_contains
from tests.factories import SUB, rid


def test_scope_matching_respects_segments():
    assert scope_contains('/subscriptions/a', '/subscriptions/a/resourceGroups/b')
    assert not scope_contains('/subscriptions/a', '/subscriptions/ab/resourceGroups/b')
    assert scope_contains('/subscriptions/A/', '/subscriptions/a')


def test_attachments_resolve_shared_user_and_system_identities():
    mi = rid('rg-a', 'Microsoft.ManagedIdentity/userAssignedIdentities/orders')
    api = rid('rg-a', 'Microsoft.App/containerApps/api')
    worker = rid('rg-a', 'Microsoft.App/jobs/worker')
    graph = graph_from_arg([
        {'id': mi, 'properties': {'principalId': 'SHARED'}},
        {'id': api, 'identity': {'type': 'SystemAssigned, UserAssigned', 'principalId': 'SYSTEM',
                                'userAssignedIdentities': {mi: {}}}},
        {'id': worker, 'identity': {'userAssignedIdentities': {mi: {'principalId': 'SHARED'}}}},
    ])
    attachments = identity_attachments(graph)
    assert {(a.workload, a.principal_id, a.name) for a in attachments} == {
        (api.lower(), 'system', 'api (system-assigned)'),
        (api.lower(), 'shared', 'orders'), (worker.lower(), 'shared', 'orders')}


def test_declared_subscription_grant_does_not_require_resource_group_id():
    scope = f'/subscriptions/{SUB}'
    results = {'main': {'changes': [
        {'resourceId': scope + '/providers/Microsoft.Authorization/roleAssignments/a', 'changeType': 'Create',
         'after': {'type': 'Microsoft.Authorization/roleAssignments', 'properties': {
             'principalId': '[reference(unknown)]', 'roleDefinitionId': scope + '/providers/Microsoft.Authorization/roleDefinitions/r',
             'condition': 'condition text'}}},
        {'resourceId': scope + '/providers/Microsoft.Authorization/roleAssignments/b', 'changeType': 'Delete'},
    ]}}
    evidence = declared_security(results)
    assert len(evidence.grants) == 1
    grant = evidence.grants[0]
    assert grant.source == 'bicep'
    assert grant.scope == scope.lower()
    assert grant.condition == 'condition text'
    assert grant.principal_id == '[reference(unknown)]'
    assert any('unresolved' in c.lower() for c in evidence.coverage)


TENANT_ROLE = '/providers/Microsoft.Authorization/RoleDefinitions/B24988AC-6180-42A0-AB88-20F7382DD24C'
SUB_ROLE = f'/subscriptions/{SUB}/providers/Microsoft.Authorization/roleDefinitions/b24988ac-6180-42a0-ab88-20f7382dd24c'


def _assignment(role_id, **extra):
    return {'type': 'microsoft.authorization/roleassignments', 'id': 'a1', 'properties': {
        'principalId': 'P', 'scope': f'/subscriptions/{SUB}', 'roleDefinitionId': role_id, **extra}}


def test_tenant_form_assignment_resolves_against_subscription_form_definition():
    rows = [{'type': 'microsoft.authorization/roledefinitions', 'id': SUB_ROLE, 'properties': {'roleName': 'Contributor'}},
            _assignment(TENANT_ROLE)]
    evidence = grants_from_rows(rows)
    assert [g.role_name for g in evidence.grants] == ['Contributor']
    assert not any('name unavailable' in c for c in evidence.coverage)


def test_absent_role_definition_still_notes_unavailable_name():
    evidence = grants_from_rows([_assignment(TENANT_ROLE)])
    assert evidence.grants[0].role_name == TENANT_ROLE
    assert any('role definition name unavailable' in c for c in evidence.coverage)


def test_tenant_and_subscription_form_duplicates_collapse():
    named = {**_assignment(SUB_ROLE), 'roleDefinitionName': 'Contributor'}
    for rows in ([_assignment(TENANT_ROLE), named], [named, _assignment(TENANT_ROLE)]):
        grants = grants_from_rows(rows).grants
        assert len(grants) == 1
        assert grants[0].role_name == 'Contributor'
        assert grants[0].role_id == TENANT_ROLE.lower()
