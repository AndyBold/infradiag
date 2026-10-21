from pathlib import Path

import pytest

from infradiag.cache import CacheMissing, read_json, write_json
from infradiag.config import CONFIG_NAME, DEFAULT_VIEWS, ConfigError, CostConfig, find_config, load_config
from infradiag.subscriptions import Subscription

VALID = """
bicep_root: ~/Projects/bicep-project
location: westeurope
subscriptions:
  - subscription: contoso-dev
    resource_groups: [RG-APP-DEV-WEU]
    entry_points:
      - name: main-dev
        deployment_name: infra-dev
        template: infra/main.bicep
        parameters: [infra/env/dev.bicepparam, imageTag=1]
        resource_groups: [rg-app-dev-weu]
      - name: network-dev
        template: infra/network.bicep
        resource_groups: [rg-app-dev-weu]
  - subscription: 0A1B2C3D-0000-4000-8000-000000000002
    location: eastus2
  - subscription: Contoso-Prod
    location: eastus2
"""
DEV_SUB = Subscription("0a1b2c3d-0000-4000-8000-000000000001", "Contoso-Dev")
TEST_SUB = Subscription("0a1b2c3d-0000-4000-8000-000000000002", "Contoso-Test")
OTHER = Subscription("0a1b2c3d-0000-4000-8000-000000000003", "Contoso Sandbox")


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / CONFIG_NAME
    path.write_text(text)
    return path


def test_loads_subscriptions_keyed_as_written(tmp_path):
    cfg = load_config(write(tmp_path, VALID))
    assert list(cfg.subscriptions) == ["contoso-dev", TEST_SUB.id.upper(), "Contoso-Prod"]
    dev_sub = cfg.subscriptions["contoso-dev"]
    assert dev_sub.resource_groups == ("rg-app-dev-weu",)
    assert dev_sub.location == "westeurope"
    assert cfg.bicep_root == Path("~/Projects/bicep-project").expanduser()
    main = dev_sub.entry_points[0]
    assert main.parameters == ("infra/env/dev.bicepparam", "imageTag=1")
    assert main.deployment_name == "infra-dev"


def test_subscription_location_overrides_default_and_entry_points_are_optional(tmp_path):
    test_sub = load_config(write(tmp_path, VALID)).subscriptions[TEST_SUB.id.upper()]
    assert test_sub.location == "eastus2"
    assert test_sub.entry_points == () and test_sub.resource_groups == ()


def test_defaults_deployment_name_and_parameters(tmp_path):
    cfg = load_config(write(tmp_path, VALID))
    network = cfg.subscriptions["contoso-dev"].entry_points[1]
    assert network.deployment_name == "infra-network-dev"
    assert network.parameters == ()


def test_cache_and_out_dirs_are_relative_to_config_file(tmp_path):
    cfg = load_config(write(tmp_path, VALID))
    assert cfg.cache_dir == tmp_path / "cache"
    assert cfg.out_dir == tmp_path / "out"


def test_minimal_config_keeps_everything_beside_the_config_file(tmp_path):
    cfg = load_config(write(tmp_path, ""))
    assert cfg.bicep_root == tmp_path
    assert (cfg.cache_dir, cfg.out_dir) == (tmp_path / "cache", tmp_path / "out")
    assert cfg.views_path == DEFAULT_VIEWS
    assert cfg.subscriptions == {} and cfg.cost == CostConfig()


def test_paths_are_relative_to_the_config_file_unless_absolute(tmp_path):
    text = "bicep_root: ../infra-repo\nviews: my-views.yaml\ncache_dir: /var/tmp/infradiag\nout_dir: ~/diagrams\n"
    cfg = load_config(write(tmp_path, text))
    assert cfg.bicep_root == tmp_path / ".." / "infra-repo"
    assert cfg.views_path == tmp_path / "my-views.yaml"
    assert cfg.cache_dir == Path("/var/tmp/infradiag")
    assert cfg.out_dir == Path("~/diagrams").expanduser()


def test_unknown_keys_are_rejected_with_the_known_ones(tmp_path):
    with pytest.raises(ConfigError, match="unknown key\\(s\\) project_path; expected bicep_root, location"):
        load_config(write(tmp_path, "project_path: ~/x\n"))


def test_find_config_uses_the_given_path_else_the_current_directory(tmp_path):
    assert find_config(tmp_path / "elsewhere.yaml", cwd=tmp_path) == tmp_path / "elsewhere.yaml"
    (tmp_path / CONFIG_NAME).write_text("")
    assert find_config(None, cwd=tmp_path) == tmp_path / CONFIG_NAME


def test_find_config_without_one_says_how_to_start(tmp_path):
    with pytest.raises(ConfigError, match="no infradiag.yaml in .*pass --config <path>, or run `infradiag init`"):
        find_config(None, cwd=tmp_path)


def test_match_by_name_ignoring_case_or_by_id_and_reports_unseen(tmp_path):
    cfg = load_config(write(tmp_path, VALID))
    settings, unmatched = cfg.match([DEV_SUB, TEST_SUB, OTHER])
    assert settings == {DEV_SUB.id: cfg.subscriptions["contoso-dev"], TEST_SUB.id: cfg.subscriptions[TEST_SUB.id.upper()]}
    assert unmatched == ["Contoso-Prod"]


def test_match_rejects_ambiguous_name(tmp_path):
    twin = Subscription("11111111-1111-1111-1111-111111111111", "Contoso-Dev")
    with pytest.raises(ConfigError, match="'contoso-dev' matches 2 subscriptions, use the ID"):
        load_config(write(tmp_path, VALID)).match([DEV_SUB, twin])


def test_match_rejects_same_subscription_by_name_and_id(tmp_path):
    text = VALID.replace("subscription: Contoso-Prod", f"subscription: {DEV_SUB.id}")
    with pytest.raises(ConfigError, match="configures Contoso-Dev .* twice"):
        load_config(write(tmp_path, text)).match([DEV_SUB])


def test_target_for_configured_subscription(tmp_path):
    cfg = load_config(write(tmp_path, VALID))
    t = cfg.target(DEV_SUB, "Contoso-Dev", cfg.match([DEV_SUB])[0][DEV_SUB.id])
    assert t.subscription == DEV_SUB.id and t.subscription_name == "Contoso-Dev"
    assert t.label == f"Contoso-Dev ({DEV_SUB.id})"
    assert [ep.name for ep in t.entry_points] == ["main-dev", "network-dev"]
    assert t.resource_groups == ("rg-app-dev-weu",)
    assert t.cache_dir == tmp_path / "cache" / DEV_SUB.id
    assert t.out_dir == tmp_path / "out" / "Contoso-Dev"


def test_target_for_unconfigured_subscription_is_azure_only(tmp_path):
    t = load_config(write(tmp_path, VALID)).target(OTHER, "Contoso-Sandbox", None)
    assert t.entry_points == () and t.resource_groups == ()
    assert t.cache_dir == tmp_path / "cache" / OTHER.id


def test_subscriptions_are_optional(tmp_path):
    cfg = load_config(write(tmp_path, "bicep_root: ~/x\n"))
    assert cfg.subscriptions == {}


def test_entry_points_need_a_location(tmp_path):
    text = VALID.replace("location: westeurope\n", "", 1)
    with pytest.raises(ConfigError, match="subscriptions\\[0\\]: missing required key 'location'"):
        load_config(write(tmp_path, text))


def test_duplicate_subscription_rejected(tmp_path):
    text = VALID.replace("subscription: Contoso-Prod", "subscription: Contoso-Dev")
    with pytest.raises(ConfigError, match="subscriptions\\[2\\]: subscription 'Contoso-Dev' is listed twice"):
        load_config(write(tmp_path, text))


def test_old_single_subscription_layout_rejected_with_hint(tmp_path):
    text = "bicep_root: ~/x\nsubscription: abc\nentry_points: []\n"
    with pytest.raises(ConfigError, match="replaced by a 'subscriptions' list"):
        load_config(write(tmp_path, text))


def test_missing_key_names_the_key(tmp_path):
    with pytest.raises(ConfigError, match="subscriptions\\[0\\].entry_points\\[0\\]: missing required key 'template'"):
        load_config(write(tmp_path, VALID.replace("        template: infra/main.bicep\n", "")))


def test_duplicate_entry_point_names_rejected(tmp_path):
    text = VALID.replace("name: network-dev", "name: main-dev")
    with pytest.raises(ConfigError, match="subscriptions\\[0\\]: duplicate entry point names: main-dev"):
        load_config(write(tmp_path, text))


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="config file not found"):
        load_config(tmp_path / "nope.yaml")


def test_cache_round_trip_and_missing_hint(tmp_path):
    write_json(tmp_path / "a" / "b.json", {"x": 1})
    assert read_json(tmp_path / "a" / "b.json", "unused") == {"x": 1}
    with pytest.raises(CacheMissing, match="run `infradiag extract --azure`"):
        read_json(tmp_path / "missing.json", "infradiag extract --azure")


COST = VALID + """
cost:
  currency: usd
  usage:
    - type: microsoft.sql/servers/databases
      name: ledger-*
      vcore_hours: 100
    - type: microsoft.sql/servers/*
      vcore_hours: 20
      stored_gb: 5
"""


def test_cost_defaults_to_gbp_with_no_usage(tmp_path):
    cost = load_config(write(tmp_path, VALID)).cost
    assert (cost.currency, cost.usage) == ("GBP", ())
    assert cost.usage_for("microsoft.sql/servers/databases", "ledger-db") == {}


def test_cost_usage_first_matching_rule_wins_per_key(tmp_path):
    cost = load_config(write(tmp_path, COST)).cost
    assert cost.currency == "USD"
    assert cost.usage_for("Microsoft.Sql/servers/databases", "Ledger-DB") == {"vcore_hours": 100, "stored_gb": 5}
    assert cost.usage_for("microsoft.sql/servers/databases", "orders-db") == {"vcore_hours": 20, "stored_gb": 5}
    assert cost.usage_for("microsoft.keyvault/vaults", "kv") == {}
    rules = cost.usage_rules_for("microsoft.sql/servers/databases", "ledger-db")
    assert {key: (rule.type, rule.name) for key, rule in rules.items()} == {
        "vcore_hours": ("microsoft.sql/servers/databases", "ledger-*"), "stored_gb": ("microsoft.sql/servers/*", "*")}


@pytest.mark.parametrize("rule, message", [
    ("- type: microsoft.keyvault/vaults\n      operations: lots", "non-negative numbers: operations"),
    ("- type: microsoft.keyvault/vaults\n      operations: -1", "non-negative numbers: operations"),
    ("- type: microsoft.keyvault/vaults", "no usage values"),
    ("- operations: 5", "missing required key 'type'"),
])
def test_cost_usage_rejects_bad_rules(tmp_path, rule, message):
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, VALID + f"cost:\n  usage:\n    {rule}\n"))


def test_cost_currency_must_be_a_code(tmp_path):
    with pytest.raises(ConfigError, match="3-letter currency code"):
        load_config(write(tmp_path, VALID + "cost:\n  currency: pounds\n"))
