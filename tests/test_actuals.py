import json
from datetime import date
from pathlib import Path

import pytest

from infradiag.actuals import (
    BY_RESOURCE_AND_METER,
    NO_GROUP,
    Period,
    last_full_month,
    load_actuals,
    parse,
    parse_detail,
    query_body,
    resource_group_of,
)
from infradiag.azcli import AzError
from infradiag.config import Target
from tests.factories import DEV, SUB, rid

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


DETAIL_COLUMNS = [{"name": "Cost"}, {"name": "ResourceId"}, {"name": "Meter"}, {"name": "Currency"}]
VGW = rid(DEV, "Microsoft.Network/virtualNetworkGateways/vgw-app-dev-weu")


def test_detail_query_groups_by_resource_and_meter():
    assert query_body(SEPT, BY_RESOURCE_AND_METER)["dataset"]["grouping"] == [
        {"type": "Dimension", "name": "ResourceId"}, {"type": "Dimension", "name": "Meter"}]


def test_parse_detail_sums_per_lowercased_resource_and_meter():
    raw = {"columns": DETAIL_COLUMNS, "rows": [[100.0, VGW, "VpnGw1AZ", "GBP"], [10.0, VGW.lower(), "VpnGw1AZ", "GBP"],
                                               [2.5, VGW, "Data Transfer Out", "GBP"], [1.0, "", "Support", "GBP"]]}
    assert parse_detail(raw) == {VGW.lower(): {"VpnGw1AZ": 110.0, "Data Transfer Out": 2.5}, "": {"Support": 1.0}}


def test_resource_group_of_billed_ids():
    assert resource_group_of(VGW) == DEV
    assert resource_group_of("") == NO_GROUP
    assert resource_group_of(f"/subscriptions/{SUB}/providers/Microsoft.Capacity/reservations/r1") == NO_GROUP


def test_detail_runs_a_second_query_cached_beside_the_first(tmp_path):
    groupings = []

    def run(args):
        grouping = json.loads(args[6])["dataset"]["grouping"]
        groupings.append([g["name"] for g in grouping])
        if len(grouping) == 1:
            return {"properties": {"columns": COLUMNS, "rows": [[12.5, DEV, "GBP"]]}}
        return {"properties": {"columns": DETAIL_COLUMNS, "rows": [[12.5, VGW, "VpnGw1AZ", "GBP"]]}}

    actuals = load_actuals(target(tmp_path), SEPT, run=run, detail=True)
    assert groupings == [["ResourceGroupName"], ["ResourceId", "Meter"]]
    assert actuals.by_group == {DEV: 12.5} and actuals.by_resource == {VGW.lower(): {"VpnGw1AZ": 12.5}}
    assert (tmp_path / SUB / "actual-cost" / "2026-09-01_2026-09-30-by-resource.json").exists()

    def offline(args):
        raise AssertionError("should read the cache")

    assert load_actuals(target(tmp_path), SEPT, run=offline, detail=True).by_resource == actuals.by_resource
    assert load_actuals(target(tmp_path), SEPT, run=offline).by_resource is None


def test_failed_detail_query_keeps_the_resource_group_totals(tmp_path):
    def run(args):
        if len(json.loads(args[6])["dataset"]["grouping"]) == 2:
            raise AzError("BadRequest: invalid grouping")
        return {"properties": {"columns": COLUMNS, "rows": [[12.5, DEV, "GBP"]]}}

    actuals = load_actuals(target(tmp_path), SEPT, run=run, detail=True)
    assert actuals.by_group == {DEV: 12.5} and not actuals.error
    assert actuals.by_resource is None
    assert actuals.detail_error == "Cost Management query failed: BadRequest: invalid grouping"


def test_throttled_queries_are_retried_with_backoff(tmp_path):
    calls, waits = [], []

    def run(args):
        calls.append(args)
        if len(calls) < 3:
            raise AzError('Too Many Requests({"error":{"code":"429","message":"Too many requests. Please retry."}})')
        return {"properties": {"columns": COLUMNS, "rows": [[5.0, "rg-a", "GBP"]]}}

    actuals = load_actuals(target(tmp_path), SEPT, run=run, sleep=waits.append)
    assert actuals.by_group == {"rg-a": 5.0} and not actuals.error
    assert waits == [4, 8]


def test_throttling_that_never_clears_is_reported(tmp_path):
    def run(args):
        raise AzError("Too Many Requests(429)")

    actuals = load_actuals(target(tmp_path), SEPT, run=run, sleep=lambda s: None)
    assert actuals.error == "Cost Management query failed: Too Many Requests(429)"
