import json
from datetime import date
from pathlib import Path

import pytest

from infradiag.actuals import Period, last_full_month, load_actuals, parse, query_body
from infradiag.azcli import AzError
from infradiag.config import Target
from tests.factories import SUB

SEPT = Period(date(2026, 9, 1), date(2026, 9, 30))
COLUMNS = [{"name": "Cost", "type": "Number"}, {"name": "ResourceGroupName", "type": "String"},
           {"name": "Currency", "type": "String"}]
REFUSED = ('ERROR: Unauthorized({"error":{"code":"RBACAccessDenied","message":"The client does not have'
           ' authorization to perform action."}})\nInteractive authentication is needed.')


def target(tmp_path: Path) -> Target:
    return Target(Path("/nonexistent"), SUB, "Contoso-Dev", None, (), (), tmp_path / SUB, tmp_path / "out")


@pytest.mark.parametrize("today, start, end", [
    (date(2026, 10, 21), date(2026, 9, 1), date(2026, 9, 30)),
    (date(2026, 1, 1), date(2025, 12, 1), date(2025, 12, 31)),
    (date(2028, 3, 15), date(2028, 2, 1), date(2028, 2, 29)),
])
def test_default_period_is_last_full_month(today, start, end):
    assert last_full_month(today) == Period(start, end)


def test_query_asks_for_actual_cost_by_resource_group():
    body = query_body(SEPT)
    assert body["type"] == "ActualCost"
    assert body["timePeriod"] == {"from": "2026-09-01T00:00:00Z", "to": "2026-09-30T23:59:59Z"}
    assert body["dataset"]["grouping"] == [{"type": "Dimension", "name": "ResourceGroupName"}]


def test_parse_sums_rows_per_resource_group_ignoring_case():
    actuals = parse({"columns": COLUMNS, "rows": [[10.5, "RG-APP-DEV-WEU", "USD"], [2.0, "rg-app-dev-weu", "USD"],
                                                  [100.0, "rg-app-shared-weu", "USD"], [1.0, "", "USD"]]}, SEPT)
    assert actuals.by_group == {"rg-app-dev-weu": 12.5, "rg-app-shared-weu": 100.0, "(no resource group)": 1.0}
    assert actuals.currency == "USD" and actuals.total == 113.5 and not actuals.error


def test_load_actuals_queries_read_only_follows_pages_and_caches(tmp_path):
    calls = []

    def run(args):
        calls.append(args)
        if len(calls) == 1:
            return {"properties": {"columns": COLUMNS, "rows": [[5.0, "rg-a", "GBP"]], "nextLink": "https://next"}}
        return {"properties": {"columns": COLUMNS, "rows": [[7.0, "rg-b", "GBP"]], "nextLink": None}}

    actuals = load_actuals(target(tmp_path), SEPT, run=run)
    assert actuals.by_group == {"rg-a": 5.0, "rg-b": 7.0}
    assert calls[0][:4] == ["rest", "--method", "post", "--url"]
    assert f"/subscriptions/{SUB}/providers/Microsoft.CostManagement/query" in calls[0][4]
    assert calls[1][4] == "https://next"
    assert json.loads(calls[0][6])["type"] == "ActualCost"

    def offline(args):
        raise AssertionError("should read the cache")

    assert load_actuals(target(tmp_path), SEPT, run=offline).by_group == {"rg-a": 5.0, "rg-b": 7.0}
    assert (tmp_path / SUB / "actual-cost" / "2026-09-01_2026-09-30.json").exists()


def test_refused_billing_query_is_reported_not_raised_or_cached(tmp_path):
    def run(args):
        raise AzError(REFUSED)

    actuals = load_actuals(target(tmp_path), SEPT, run=run)
    assert actuals.error.startswith("no billing read access (Cost Management Reader or Billing Reader needed)")
    assert "RBACAccessDenied" in actuals.error and "Interactive" not in actuals.error
    assert actuals.by_group == {}
    assert not (tmp_path / SUB / "actual-cost").exists()


def test_other_query_failures_are_reported_too(tmp_path):
    def run(args):
        raise AzError("az rest timed out after 900s")

    assert load_actuals(target(tmp_path), SEPT, run=run).error == "Cost Management query failed: az rest timed out after 900s"


def test_empty_period_has_no_charges(tmp_path):
    actuals = load_actuals(target(tmp_path), SEPT, run=lambda args: {"properties": {"columns": COLUMNS, "rows": []}})
    assert actuals.by_group == {} and actuals.total == 0 and not actuals.error
