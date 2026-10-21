import json
from pathlib import Path

from infradiag import actuals, cli, render_d2
from infradiag.azcli import AzError
from infradiag.icons import cache_path
from infradiag.subscriptions import Subscription
from tests.factories import DEV, SUB, arg_row, rid, whatif_change, whatif_result

KV = rid(DEV, "Microsoft.KeyVault/vaults/kv-app-dev-weu")
WEB = rid(DEV, "Microsoft.App/containerApps/ca-web-dev-weu")

TEST_SUB = "0a1b2c3d-0000-4000-8000-000000000002"
TEST_KV = f"/subscriptions/{TEST_SUB}/resourceGroups/rg-test/providers/Microsoft.KeyVault/vaults/kv-test"

CONFIG = f"""
bicep_root: /nonexistent
location: westeurope
subscriptions:
  - subscription: contoso-dev
    resource_groups: [{DEV}]
    entry_points:
      - name: main-dev
        template: infra/main.bicep
        resource_groups: [{DEV}]
"""

VIEWS = """
hide_types: []
summary:
  groups:
    Data: [microsoft.keyvault/vaults]
"""


def setup(tmp_path: Path, test_sub: bool = False) -> list[str]:
    """Contoso-Dev (configured, extracted) and Contoso-Test (unconfigured; extracted only if `test_sub`)."""
    (tmp_path / "config.yaml").write_text(CONFIG)
    (tmp_path / "views.yaml").write_text(VIEWS)
    cache = tmp_path / "cache"
    (cache / SUB / "whatif").mkdir(parents=True)
    (cache / "subscriptions.json").write_text(json.dumps([{"id": SUB, "name": "Contoso-Dev"},
                                                          {"id": TEST_SUB, "name": "Contoso-Test"}]))
    (cache / SUB / "whatif" / "main-dev.json").write_text(
        json.dumps(whatif_result(whatif_change("NoChange", KV), whatif_change("Ignore", WEB))))
    (cache / SUB / "arg.json").write_text(json.dumps(
        {"resource_groups": [DEV], "rows": [arg_row(KV), arg_row(WEB)]}))
    if test_sub:
        (cache / TEST_SUB).mkdir()
        (cache / TEST_SUB / "arg.json").write_text(json.dumps({"resource_groups": None, "rows": [arg_row(TEST_KV)]}))
    return ["--config", str(tmp_path / "config.yaml"), "--views", str(tmp_path / "views.yaml")]


DEV_OUT = Path("out") / "Contoso-Dev"


def test_diff_writes_report(tmp_path, capsys):
    assert cli.main(setup(tmp_path) + ["diff"]) == 0
    report = (tmp_path / DEV_OUT / "drift-report.md").read_text()
    assert f"Subscription: Contoso-Dev ({SUB})" in report
    assert "| Matched | 1 |" in report
    assert "| ca-web-dev-weu |" in report
    assert "confirmed by what-if Ignore" in report
    assert "drift-report.md" in capsys.readouterr().out


def test_render_without_d2_writes_sources_and_reports_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(render_d2.shutil, "which", lambda _: None)
    assert cli.main(setup(tmp_path) + ["render"]) == 2
    assert (tmp_path / DEV_OUT / "detailed.d2").exists()
    assert "error: d2 not installed" in capsys.readouterr().err


def test_render_single_view(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "render_svg", lambda text, d2, svg: calls.append(svg.name))
    assert cli.main(setup(tmp_path) + ["render", "--view", "summary"]) == 0
    assert calls == ["summary.svg"]


def test_missing_cache_is_a_friendly_error(tmp_path, capsys):
    args = setup(tmp_path)
    (tmp_path / "cache" / SUB / "arg.json").unlink()
    assert cli.main(args + ["diff"]) == 2
    assert "infradiag extract --azure" in capsys.readouterr().err


def test_extract_reports_partial_failure(tmp_path, monkeypatch):
    args = setup(tmp_path)
    monkeypatch.setattr(cli, "discover", lambda: [Subscription(SUB, "Contoso-Dev")])
    monkeypatch.setattr(cli, "extract_bicep", lambda cfg, **kw: {"main-dev": "boom"})
    monkeypatch.setattr(cli, "extract_azure", lambda cfg, **kw: 2)
    assert cli.main(args + ["extract"]) == 1


def test_render_marks_failed_entry_point_resources_unverified(tmp_path, monkeypatch, capsys):
    args = setup(tmp_path)
    (tmp_path / "cache" / SUB / "whatif" / "main-dev.json").write_text(json.dumps({"error": "boom"}))
    monkeypatch.setattr(cli, "render_svg", lambda text, d2, svg: d2.write_text(text) if d2.parent.mkdir(parents=True, exist_ok=True) is None else None)
    assert cli.main(args + ["render", "--view", "detailed"]) == 0
    d2 = (tmp_path / DEV_OUT / "detailed.d2").read_text()
    assert "class: unverified" in d2
    assert "class: azure_only" not in d2
    assert "main-dev" in capsys.readouterr().err


ICON = "azure/Security Service Color/Key Vaults.svg"


def capture_d2(monkeypatch):
    def fake(text, d2, svg):
        d2.parent.mkdir(parents=True, exist_ok=True)
        d2.write_text(text)
    monkeypatch.setattr(cli, "render_svg", fake)


def test_render_uses_cached_icons_and_hints_when_missing(tmp_path, monkeypatch, capsys):
    args = setup(tmp_path)
    (tmp_path / "views.yaml").write_text(VIEWS + f"icons:\n  microsoft.keyvault/vaults: {ICON}\n"
                                         "  microsoft.app/containerapps: azure/x/missing.svg\n")
    cached = cache_path(tmp_path / "cache" / "icons", ICON)
    cached.parent.mkdir(parents=True)
    cached.write_text("<svg/>")
    capture_d2(monkeypatch)
    assert cli.main(args + ["render", "--view", "detailed"]) == 0
    d2 = (tmp_path / DEV_OUT / "detailed.d2").read_text()
    assert f'icon: "{cached}"' in d2
    assert "https://icons.terrastruct.com/azure%2Fx%2Fmissing.svg" in d2
    assert "infradiag icons" in capsys.readouterr().err


def test_icons_command_reports_failures(tmp_path, monkeypatch, capsys):
    args = setup(tmp_path)
    calls = []
    monkeypatch.setattr(cli, "update_icons", lambda rules, icon_dir, **kw: calls.append(icon_dir) or {"a.svg": "404"})
    assert cli.main(args + ["icons"]) == 1
    assert calls == [tmp_path / "cache" / "icons"]
    assert "a.svg: 404" in capsys.readouterr().err


def test_diff_and_render_skip_subscriptions_not_extracted(tmp_path, monkeypatch, capsys):
    args = setup(tmp_path)
    capture_d2(monkeypatch)
    assert cli.main(args + ["diff"]) == 0
    assert cli.main(args + ["render"]) == 0
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["Contoso-Dev"]
    assert f"Contoso-Test ({TEST_SUB}) not extracted, skipped — run `infradiag extract --subscription {TEST_SUB}`" in capsys.readouterr().err


def test_unconfigured_subscription_gets_azure_only_outputs(tmp_path, monkeypatch, capsys):
    args = setup(tmp_path, test_sub=True)
    capture_d2(monkeypatch)
    assert cli.main(args + ["diff"]) == 0
    assert cli.main(args + ["render", "--view", "detailed"]) == 0
    out = tmp_path / "out" / "Contoso-Test"
    report = (out / "drift-report.md").read_text()
    assert f"Subscription: Contoso-Test ({TEST_SUB})" in report and "**Azure only:**" in report
    d2 = (out / "detailed.d2").read_text()
    assert "Detailed topology (Azure only)" in d2
    assert f"Contoso-Test ({TEST_SUB})" in d2 and "kv-test" in d2
    assert "class: azure_only" not in d2
    captured = capsys.readouterr()
    assert "Contoso-Test/drift-report.md: Azure only" in captured.out
    assert f"note: Contoso-Test ({TEST_SUB}): no Bicep entry points" in captured.err
    assert (tmp_path / DEV_OUT / "detailed.d2").exists()


def test_render_labels_subscription_with_name_and_id(tmp_path, monkeypatch):
    args = setup(tmp_path)
    capture_d2(monkeypatch)
    assert cli.main(args + ["render", "--view", "detailed"]) == 0
    assert f"Contoso-Dev ({SUB})" in (tmp_path / DEV_OUT / "detailed.d2").read_text()


def test_subscription_option_targets_by_name_or_id(tmp_path, monkeypatch):
    args = setup(tmp_path, test_sub=True)
    calls = []
    monkeypatch.setattr(cli, "_diff", lambda cfg, rules: calls.append(cfg.subscription) or 0)
    assert cli.main(args + ["diff", "--subscription", "contoso-test"]) == 0
    assert cli.main(args + ["diff", "--subscription", SUB, "--subscription", "Contoso-Test"]) == 0
    assert calls == [TEST_SUB, SUB, TEST_SUB]


def test_targeting_a_subscription_that_was_not_extracted_is_an_error(tmp_path, capsys):
    args = setup(tmp_path)
    assert cli.main(args + ["render", "--subscription", "Contoso-Test"]) == 2
    assert f"infradiag extract --azure --subscription {TEST_SUB}" in capsys.readouterr().err


def test_unknown_subscription_lists_candidates(tmp_path, capsys):
    args = setup(tmp_path)
    assert cli.main(args + ["diff", "--subscription", "Contoso-Prod"]) == 2
    err = capsys.readouterr().err
    assert "no subscription matches 'Contoso-Prod'" in err
    assert f"Contoso-Dev ({SUB})" in err and f"Contoso-Test ({TEST_SUB})" in err


def test_diff_before_any_extract_says_to_extract(tmp_path, capsys):
    args = setup(tmp_path)
    (tmp_path / "cache" / "subscriptions.json").unlink()
    assert cli.main(args + ["diff"]) == 2
    assert "run `infradiag extract`" in capsys.readouterr().err


def test_extract_discovers_every_subscription_and_skips_whatif_for_unconfigured(tmp_path, monkeypatch, capsys):
    args = setup(tmp_path)
    monkeypatch.setattr(cli, "discover", lambda: [Subscription(SUB, "Contoso-Dev"), Subscription(TEST_SUB, "Contoso-Test")])
    bicep, azure = [], []
    monkeypatch.setattr(cli, "extract_bicep", lambda cfg, **kw: bicep.append(cfg.subscription) or {})
    monkeypatch.setattr(cli, "extract_azure", lambda cfg, **kw: azure.append(cfg.subscription) or 0)
    assert cli.main(args + ["extract"]) == 0
    assert bicep == [SUB] and azure == [SUB, TEST_SUB]
    out = capsys.readouterr().out
    assert f"== Contoso-Test ({TEST_SUB}) ==" in out and "Azure only, no what-if" in out
    cached = json.loads((tmp_path / "cache" / "subscriptions.json").read_text())
    assert [row["name"] for row in cached] == ["Contoso-Dev", "Contoso-Test"]


def test_extract_with_subscription_option_runs_only_that_one(tmp_path, monkeypatch):
    args = setup(tmp_path)
    monkeypatch.setattr(cli, "discover", lambda: [Subscription(SUB, "Contoso-Dev"), Subscription(TEST_SUB, "Contoso-Test")])
    azure = []
    monkeypatch.setattr(cli, "extract_bicep", lambda cfg, **kw: {})
    monkeypatch.setattr(cli, "extract_azure", lambda cfg, **kw: azure.append(cfg.subscription) or 0)
    assert cli.main(args + ["extract", "--azure", "--subscription", TEST_SUB.upper()]) == 0
    assert azure == [TEST_SUB]


def test_one_subscription_failing_does_not_stop_the_others(tmp_path, monkeypatch, capsys):
    args = setup(tmp_path)
    monkeypatch.setattr(cli, "discover", lambda: [Subscription(SUB, "Contoso-Dev"), Subscription(TEST_SUB, "Contoso-Test")])
    done = []

    def fake_azure(cfg, **kw):
        if cfg.subscription == SUB:
            raise AzError("AuthorizationFailed")
        done.append(cfg.subscription)
        return 0

    monkeypatch.setattr(cli, "extract_azure", fake_azure)
    assert cli.main(args + ["extract", "--azure"]) == 2
    assert done == [TEST_SUB]
    assert f"error: Contoso-Dev ({SUB}): AuthorizationFailed" in capsys.readouterr().err


def test_configured_subscription_not_visible_is_skipped_with_note(tmp_path, monkeypatch, capsys):
    args = setup(tmp_path)
    (tmp_path / "config.yaml").write_text(CONFIG + "  - subscription: Contoso-Prod\n")
    assert cli.main(args + ["diff"]) == 0
    assert "config subscription 'Contoso-Prod' isn't visible to az login; skipped" in capsys.readouterr().err


VGW = rid(DEV, "Microsoft.Network/virtualNetworkGateways/vgw-app-dev-weu")
VGW_PRICE = {"productName": "VPN Gateway", "skuName": "VpnGw1AZ", "meterName": "VpnGw1AZ", "unitOfMeasure": "1 Hour",
             "retailPrice": 0.2, "tierMinimumUnits": 0.0, "armSkuName": ""}


def setup_cost(tmp_path: Path, test_sub: bool = False) -> list[str]:
    """setup() plus a VPN gateway in Resource Graph and its list price already cached, so cost runs offline."""
    args = setup(tmp_path, test_sub)
    cache = tmp_path / "cache"
    (cache / SUB / "arg.json").write_text(json.dumps({"resource_groups": [DEV], "rows": [
        arg_row(KV), arg_row(WEB),
        {"id": VGW, "name": "vgw-app-dev-weu", "location": "westeurope",
         "properties": {"sku": {"name": "VpnGw1AZ"}, "gatewayType": "Vpn"}}]}))
    prices = cache / "prices" / "GBP"
    prices.mkdir(parents=True)
    (prices / "VPN-Gateway--westeurope.json").write_text(json.dumps(
        {"filter": "", "currency": "GBP", "fetched": "2026-10-21", "items": [VGW_PRICE]}))
    return args


def test_cost_writes_estimate_reports_from_cached_prices(tmp_path, capsys):
    assert cli.main(setup_cost(tmp_path) + ["cost"]) == 0
    md = (tmp_path / DEV_OUT / "cost-report.md").read_text()
    assert f"Subscription: Contoso-Dev ({SUB})" in md
    assert "| rg-app-dev-weu | £146.00 | £0.00 | **£146.00** |" in md
    assert "| kv-app-dev-weu | `microsoft.keyvault/vaults` |" in md      # usage-billed, no usage stated
    assert "Actual" not in md
    data = json.loads((tmp_path / DEV_OUT / "cost-report.json").read_text())
    assert data["total"] == 146.0 and data["actual"] is None
    assert "estimate 146.00 GBP/month, 2 resource(s) not estimated" in capsys.readouterr().out


def test_cost_actual_side_by_side_and_refused_subscription_carries_on(tmp_path, monkeypatch, capsys):
    def fake_az(args):
        if TEST_SUB in args[4]:
            raise AzError('Unauthorized({"error":{"code":"RBACAccessDenied"}})')
        return {"properties": {"columns": [{"name": "Cost"}, {"name": "ResourceGroupName"}, {"name": "Currency"}],
                               "rows": [[180.0, DEV, "USD"], [40.0, "rg-app-test-weu", "USD"]]}}

    monkeypatch.setattr(actuals, "run_az", fake_az)
    args = setup_cost(tmp_path, test_sub=True) + ["cost", "--actual", "--from", "2026-09-01", "--to", "2026-09-30"]
    assert cli.main(args) == 0
    md = (tmp_path / DEV_OUT / "cost-report.md").read_text()
    assert "| rg-app-dev-weu | £146.00 | £0.00 | **£146.00** | $180.00 |" in md
    assert "| rg-app-test-weu | — | — | — | $40.00 |" in md
    test_md = (tmp_path / "out" / "Contoso-Test" / "cost-report.md").read_text()
    assert "not available for this subscription** — no billing read access" in test_md
    captured = capsys.readouterr()
    assert "actual 220.00 USD for 2026-09-01 to 2026-09-30" in captured.out
    assert "Contoso-Test" in captured.err and "actual cost unavailable" in captured.err


def test_cost_period_needs_actual_and_must_be_ordered(tmp_path, capsys):
    args = setup_cost(tmp_path)
    assert cli.main(args + ["cost", "--from", "2026-09-01"]) == 2
    assert "--from/--to need --actual" in capsys.readouterr().err
    assert cli.main(args + ["cost", "--actual", "--from", "2026-09-30", "--to", "2026-09-01"]) == 2
    assert "is after --to" in capsys.readouterr().err
