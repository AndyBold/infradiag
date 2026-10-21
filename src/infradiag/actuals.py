"""Actual spend per resource group from Azure Cost Management (a read-only query), cached per subscription and period.

With detail, a second query breaks the same spend down per resource and meter.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

from .azcli import AzError, run_az
from .cache import read_json, write_json
from .config import Target
from .ids import normalise_id

API_VERSION = "2023-11-01"
ACCESS_DENIED = ("rbacaccessdenied", "authorizationfailed", "unauthorized", "forbidden")
THROTTLED = ("too many requests", "429")
RETRIES = 5  # Cost Management throttles bursts of queries with 429s
NO_GROUP = "(no resource group)"
BY_GROUP = [{"type": "Dimension", "name": "ResourceGroupName"}]
BY_RESOURCE_AND_METER = [{"type": "Dimension", "name": "ResourceId"}, {"type": "Dimension", "name": "Meter"}]


@dataclass(frozen=True)
class Period:
    start: date
    end: date  # inclusive

    def __str__(self) -> str:
        return f"{self.start.isoformat()} to {self.end.isoformat()}"


def last_full_month(today: date | None = None) -> Period:
    first = (today or date.today()).replace(day=1)
    end = first - timedelta(days=1)
    return Period(end.replace(day=1), end)


@dataclass
class Actuals:
    period: Period
    currency: str = ""
    by_group: dict[str, float] = field(default_factory=dict)  # lowercased resource group -> cost
    error: str = ""                                           # why there are no actuals for this subscription
    # lowercased resource ID ("" for charges tied to no resource) -> meter -> cost; None unless detail was asked for
    by_resource: dict[str, dict[str, float]] | None = None
    detail_error: str = ""                                    # why the per-resource breakdown is missing

    @property
    def total(self) -> float:
        return sum(self.by_group.values())


def query_body(period: Period, grouping: list[dict] = BY_GROUP) -> dict:
    return {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {"from": f"{period.start.isoformat()}T00:00:00Z", "to": f"{period.end.isoformat()}T23:59:59Z"},
        "dataset": {
            "granularity": "None",
            "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
            "grouping": grouping,
        },
    }


def _post(run, url: str, body: str, sleep) -> dict:
    for attempt in range(RETRIES):
        try:
            return run(["rest", "--method", "post", "--url", url, "--body", body]) or {}
        except AzError as exc:
            if attempt == RETRIES - 1 or not any(word in str(exc).lower() for word in THROTTLED):
                raise
            sleep(2 ** (attempt + 2))  # az rest doesn't pass Retry-After through
    raise AssertionError("unreachable")


def fetch_actuals(subscription: str, period: Period, run=None, grouping: list[dict] = BY_GROUP,
                  sleep=time.sleep) -> dict:
    """Cost Management's query API is a POST but only reads; az rest reuses the az login."""
    run = run or run_az
    url = (f"https://management.azure.com/subscriptions/{subscription}/providers/Microsoft.CostManagement/query"
           f"?api-version={API_VERSION}")
    body = json.dumps(query_body(period, grouping))
    columns, rows = [], []
    while url:
        page = _post(run, url, body, sleep).get("properties") or {}
        columns = page.get("columns") or columns
        rows.extend(page.get("rows") or [])
        url = page.get("nextLink")
    return {"columns": columns, "rows": rows}


def parse(raw: dict, period: Period) -> Actuals:
    names = [c.get("name", "").lower() for c in raw.get("columns") or []]
    cost, group, currency = (names.index(n) if n in names else None for n in ("cost", "resourcegroupname", "currency"))
    actuals = Actuals(period)
    if cost is None or group is None:
        return actuals
    for row in raw.get("rows") or []:
        rg = str(row[group] or NO_GROUP).lower()
        actuals.by_group[rg] = actuals.by_group.get(rg, 0.0) + float(row[cost] or 0)
        if currency is not None and row[currency]:
            actuals.currency = str(row[currency])
    return actuals


def parse_detail(raw: dict) -> dict[str, dict[str, float]]:
    """Cost per resource ID and meter. IDs are lowercased to match extracted ones."""
    names = [c.get("name", "").lower() for c in raw.get("columns") or []]
    cost, resource, meter = (names.index(n) if n in names else None for n in ("cost", "resourceid", "meter"))
    found: dict[str, dict[str, float]] = {}
    if cost is None or resource is None:
        return found
    for row in raw.get("rows") or []:
        meters = found.setdefault(normalise_id(str(row[resource] or "")), {})
        name = str(row[meter] or "") if meter is not None else ""
        meters[name or "(no meter)"] = meters.get(name or "(no meter)", 0.0) + float(row[cost] or 0)
    return found


def resource_group_of(resource_id: str) -> str:
    """The lowercased resource group a billed resource ID sits in, if any."""
    parts = normalise_id(resource_id).split("/")
    return parts[parts.index("resourcegroups") + 1] if "resourcegroups" in parts[:-1] else NO_GROUP


def _refusal(exc: AzError) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else "az rest failed"
    if any(word in text.lower() for word in ACCESS_DENIED):
        return f"no billing read access (Cost Management Reader or Billing Reader needed): {text}"
    return f"Cost Management query failed: {text}"


def _cached(cfg: Target, period: Period, suffix: str, grouping: list[dict], refresh: bool, run, sleep) -> dict:
    path = cfg.cache_dir / "actual-cost" / f"{period.start.isoformat()}_{period.end.isoformat()}{suffix}.json"
    if path.exists() and not refresh:
        return read_json(path, "infradiag cost --actual --refresh")
    raw = fetch_actuals(cfg.subscription, period, run, grouping, sleep)
    write_json(path, raw)
    return raw


def load_actuals(cfg: Target, period: Period, refresh: bool = False, run=None, detail: bool = False,
                 sleep=time.sleep) -> Actuals:
    """Cached actuals for the period, else a fresh query. A refused or failed query yields Actuals with an error.

    detail adds the per-resource, per-meter breakdown, a second query cached beside the first.
    """
    try:
        actuals = parse(_cached(cfg, period, "", BY_GROUP, refresh, run, sleep), period)
    except AzError as exc:
        return Actuals(period, error=_refusal(exc))
    if detail:
        try:
            actuals.by_resource = parse_detail(_cached(cfg, period, "-by-resource", BY_RESOURCE_AND_METER, refresh,
                                                       run, sleep))
        except AzError as exc:
            actuals.detail_error = _refusal(exc)
    return actuals
