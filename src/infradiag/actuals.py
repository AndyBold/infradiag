"""Actual spend per resource group from Azure Cost Management (a read-only query), cached per subscription and period."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta

from .azcli import AzError, run_az
from .cache import read_json, write_json
from .config import Target

API_VERSION = "2023-11-01"
ACCESS_DENIED = ("rbacaccessdenied", "authorizationfailed", "unauthorized", "forbidden")


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

    @property
    def total(self) -> float:
        return sum(self.by_group.values())


def query_body(period: Period) -> dict:
    return {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {"from": f"{period.start.isoformat()}T00:00:00Z", "to": f"{period.end.isoformat()}T23:59:59Z"},
        "dataset": {
            "granularity": "None",
            "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
            "grouping": [{"type": "Dimension", "name": "ResourceGroupName"}],
        },
    }


def fetch_actuals(subscription: str, period: Period, run=None) -> dict:
    """Cost Management's query API is a POST but only reads; az rest reuses the az login."""
    run = run or run_az
    url = (f"https://management.azure.com/subscriptions/{subscription}/providers/Microsoft.CostManagement/query"
           f"?api-version={API_VERSION}")
    body = json.dumps(query_body(period))
    columns, rows = [], []
    while url:
        page = (run(["rest", "--method", "post", "--url", url, "--body", body]) or {}).get("properties") or {}
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
        rg = str(row[group] or "(no resource group)").lower()
        actuals.by_group[rg] = actuals.by_group.get(rg, 0.0) + float(row[cost] or 0)
        if currency is not None and row[currency]:
            actuals.currency = str(row[currency])
    return actuals


def _refusal(exc: AzError) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else "az rest failed"
    if any(word in text.lower() for word in ACCESS_DENIED):
        return f"no billing read access (Cost Management Reader or Billing Reader needed): {text}"
    return f"Cost Management query failed: {text}"


def load_actuals(cfg: Target, period: Period, refresh: bool = False, run=None) -> Actuals:
    """Cached actuals for the period, else a fresh query. A refused or failed query yields Actuals with an error."""
    path = cfg.cache_dir / "actual-cost" / f"{period.start.isoformat()}_{period.end.isoformat()}.json"
    if path.exists() and not refresh:
        return parse(read_json(path, "infradiag cost --actual --refresh"), period)
    try:
        raw = fetch_actuals(cfg.subscription, period, run)
    except AzError as exc:
        return Actuals(period, error=_refusal(exc))
    write_json(path, raw)
    return parse(raw, period)
