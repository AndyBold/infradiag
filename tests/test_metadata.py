from infradiag.model import merge_graphs
from infradiag.normalise import graph_from_arg, graph_from_whatif
from tests.factories import rid

RID = rid('rg-a', 'Microsoft.App/containerApps/api')


def test_source_metadata_survives_merge_without_inventing_observed_identity():
    before = {'id': RID, 'tags': {'Application': 'Orders'}, 'identity': {'type': 'SystemAssigned', 'principalId': 'old'},
              'properties': {'publicNetworkAccess': 'Enabled'}}
    after = {**before, 'identity': {'type': 'SystemAssigned', 'principalId': 'new'}}
    azure = graph_from_arg([before])
    bicep, _ = graph_from_whatif({'main': {'changes': [{'resourceId': RID, 'changeType': 'Modify', 'after': after}]}})
    merged = merge_graphs(bicep, azure)
    n = merged.nodes[RID.lower()]
    assert n.tags == {'Application': 'Orders'}
    assert n.identity['principalId'] == 'old'
    assert n.metadata['azure']['identity']['principalId'] == 'old'
    assert n.metadata['bicep']['identity']['principalId'] == 'new'
    n.metadata['azure']['identity']['principalId'] = 'changed'
    assert azure.nodes[RID.lower()].identity['principalId'] == 'old'


def test_old_cache_has_empty_metadata_values():
    n = graph_from_arg([{'id': RID}]).nodes[RID.lower()]
    assert n.tags == {}
    assert n.identity == {}
