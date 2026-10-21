import pytest

from infradiag.subscriptions import (
    Subscription,
    SubscriptionError,
    discover,
    folder_names,
    load_subscriptions,
    select,
    subscription_label,
    write_subscriptions,
)

DEV_SUB = Subscription("0a1b2c3d-0000-4000-8000-000000000001", "Contoso-Dev")
TEST_SUB = Subscription("0a1b2c3d-0000-4000-8000-000000000002", "Contoso-Test")
SANDBOX = Subscription("0a1b2c3d-0000-4000-8000-000000000003", "Contoso Sandbox")
ALL = [SANDBOX, DEV_SUB, TEST_SUB]


def test_label_is_name_then_id():
    assert DEV_SUB.label == "Contoso-Dev (0a1b2c3d-0000-4000-8000-000000000001)"
    assert subscription_label(TEST_SUB.id, None) == TEST_SUB.id
    assert subscription_label(TEST_SUB.id, TEST_SUB.id) == TEST_SUB.id


def test_discover_keeps_enabled_lowercases_and_sorts_by_name():
    rows = [
        {"id": "0A1B2C3D-0000-4000-8000-000000000002", "name": "Contoso-Test", "state": "Enabled"},
        {"id": DEV_SUB.id, "name": "Contoso-Dev", "state": "Enabled"},
        {"id": "00000000-0000-0000-0000-000000000000", "name": "Old", "state": "Disabled"},
    ]
    calls = []
    assert discover(run=lambda args: calls.append(args) or rows) == [DEV_SUB, TEST_SUB]
    assert calls == [["account", "list"]]


def test_select_defaults_to_every_subscription():
    assert select(ALL, None) == ALL
    assert select(ALL, []) == ALL


def test_select_by_id_or_case_insensitive_name_in_given_order():
    assert select(ALL, ["contoso-test", DEV_SUB.id.upper()]) == [TEST_SUB, DEV_SUB]
    assert select(ALL, ["  contoso sandbox "]) == [SANDBOX]


def test_select_drops_repeats():
    assert select(ALL, ["Contoso-Dev", DEV_SUB.id]) == [DEV_SUB]


def test_select_unknown_lists_candidates():
    with pytest.raises(SubscriptionError, match="no subscription matches 'Contoso-Prod'; available: .*Contoso-Dev \\(0a1b2c3d-0000-4000-8000-000000000001"):
        select(ALL, ["Contoso-Prod"])


def test_select_ambiguous_name_lists_matches():
    twin = Subscription("11111111-1111-1111-1111-111111111111", "contoso-test")
    with pytest.raises(SubscriptionError, match="'Contoso-Test' matches 2 subscriptions, use the ID: .*000000000002.*1111"):
        select(ALL + [twin], ["Contoso-Test"])
    assert select(ALL + [twin], [twin.id]) == [twin]


def test_folder_names_are_path_safe_and_unique():
    twin = Subscription("11111111-1111-1111-1111-111111111111", "contoso-test")
    folders = folder_names(ALL + [twin])
    assert folders[DEV_SUB.id] == "Contoso-Dev"
    assert folders[SANDBOX.id] == "Contoso-Sandbox"
    assert folders[TEST_SUB.id] == f"Contoso-Test-{TEST_SUB.id}"
    assert folders[twin.id] == f"contoso-test-{twin.id}"


def test_subscriptions_cache_round_trip(tmp_path):
    write_subscriptions(tmp_path, ALL)
    assert load_subscriptions(tmp_path) == ALL
