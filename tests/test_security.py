from infradiag.normalise import graph_from_arg
from infradiag.security import declared_security, identity_attachments, scope_contains
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
