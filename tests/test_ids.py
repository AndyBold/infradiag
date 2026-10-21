from infradiag.ids import find_ids, normalise_id, parse_id, resolve, top_level_id

SUB = "0a1b2c3d-0000-4000-8000-000000000001"
VNET = f"/subscriptions/{SUB}/resourceGroups/rg-app-dev-weu/providers/Microsoft.Network/virtualNetworks/vnet-app-dev-weu"


def test_normalise_lowercases_and_strips_trailing_slash():
    assert normalise_id(VNET + "/ ") == VNET.lower()


def test_parse_top_level():
    parts = parse_id(VNET)
    assert parts.subscription == SUB
    assert parts.resource_group == "rg-app-dev-weu"
    assert parts.type == "microsoft.network/virtualnetworks"
    assert parts.name == "vnet-app-dev-weu"
    assert parts.parent_id is None


def test_parse_child():
    parts = parse_id(VNET + "/subnets/snet-aca")
    assert parts.type == "microsoft.network/virtualnetworks/subnets"
    assert parts.name == "snet-aca"
    assert parts.parent_id == VNET.lower()


def test_parse_extension_resource_uses_last_provider():
    kv = f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.KeyVault/vaults/kv"
    parts = parse_id(kv + "/providers/Microsoft.Insights/diagnosticSettings/diag")
    assert parts.type == "microsoft.insights/diagnosticsettings"
    assert parts.name == "diag"
    assert parts.parent_id == kv.lower()


def test_parse_rejects_non_resource_ids():
    assert parse_id(f"/subscriptions/{SUB}/resourceGroups/rg-app-dev-weu") is None
    assert parse_id(f"/subscriptions/{SUB}/providers/Microsoft.Authorization/roleDefinitions/abc") is None
    assert parse_id("not an id") is None


def test_find_ids_in_free_text():
    text = f"see https://management.azure.com{VNET}/subnets/snet-pe?api-version=1 and {VNET.upper()}"
    assert find_ids(text) == [VNET.lower() + "/subnets/snet-pe", VNET.lower()]


def test_find_ids_trims_dangling_type_segment():
    assert find_ids(VNET + "/subnets") == [VNET.lower()]


def test_top_level_id():
    assert top_level_id(VNET + "/subnets/snet-aca") == VNET.lower()


def test_resolve_prefers_longest_known_prefix():
    subnet = VNET.lower() + "/subnets/snet-aca"
    assert resolve(subnet + "/whatever/x", {VNET.lower(), subnet}) == subnet
    assert resolve(subnet, {VNET.lower()}) == VNET.lower()
    assert resolve(subnet, set()) == VNET.lower()
