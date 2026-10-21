"""Azure Retail Prices API (pay-as-you-go list prices, no auth), cached under cache/prices/ so reports re-render offline."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from .cache import read_json, write_json

API = "https://prices.azure.com/api/retail/prices"
RETRIES = 5  # the API throttles bursts with 429s


class PriceError(Exception):
    pass


class NoPrice(Exception):
    """No single list price matches a meter."""


@dataclass(frozen=True)
class PriceQuery:
    """One fetch from the API: a service's consumption prices in one region (optionally one VM size)."""
    service: str
    region: str
    arm_sku: str | None = None

    def filter(self) -> str:
        f = (f"serviceName eq '{self.service}' and armRegionName eq '{self.region}'"
             " and priceType eq 'Consumption'")
        return f + (f" and armSkuName eq '{self.arm_sku}'" if self.arm_sku else "")

    def slug(self) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "-", "--".join(p for p in (self.service, self.region, self.arm_sku) if p))


@dataclass(frozen=True)
class Meter:
    """Which price list item to use. Names compare ignoring case; unset fields match anything."""
    query: PriceQuery
    unit: str                       # the item's unitOfMeasure, e.g. "1 Hour", "1/Day", "1 GB/Month"
    product: str | None = None
    sku: str | None = None
    meter: str | None = None
    arm_skus: tuple[str, ...] = ()  # any of these armSkuNames
    reject: tuple[str, ...] = ()    # rule out items whose product, sku or meter name contains one of these

    def describe(self) -> str:
        names = [self.query.service, self.product, self.sku, self.meter, *self.arm_skus[:1]]
        return " / ".join(dict.fromkeys(n for n in names if n))


@dataclass(frozen=True)
class Price:
    unit_price: float
    unit: str


def _norm_unit(unit: str) -> str:
    return re.sub(r"[\s/]+", "", unit).lower()


def _eq(a: str | None, b: str) -> bool:
    return (a or "").lower() == b.lower()


def match(items: list[dict], m: Meter) -> Price:
    """The list price for meter `m`. Tiered meters use their first paid tier, so free grants aren't applied."""
    found = [
        i for i in items
        if _norm_unit(i.get("unitOfMeasure", "")) == _norm_unit(m.unit)
        and (m.product is None or _eq(i.get("productName"), m.product))
        and (m.sku is None or _eq(i.get("skuName"), m.sku))
        and (m.meter is None or _eq(i.get("meterName"), m.meter))
        and (not m.arm_skus or any(_eq(i.get("armSkuName"), s) for s in m.arm_skus))
        and not any(r.lower() in f"{i.get('productName')} {i.get('skuName')} {i.get('meterName')}".lower()
                    for r in m.reject)
    ]
    if not found:
        raise NoPrice(f"no list price for {m.describe()} ({m.unit})")
    paid = [i for i in found if i["retailPrice"] > 0]
    if not paid:
        return Price(0.0, found[0]["unitOfMeasure"])
    first_tier = min(i["tierMinimumUnits"] for i in paid)
    prices = {i["retailPrice"] for i in paid if i["tierMinimumUnits"] == first_tier}
    if len(prices) > 1:
        raise NoPrice(f"{len(prices)} different list prices match {m.describe()}")
    return Price(prices.pop(), paid[0]["unitOfMeasure"])


def _get_json(url: str, sleep=time.sleep) -> dict:
    for attempt in range(RETRIES):
        try:
            with urlopen(url, timeout=60) as resp:
                return json.load(resp)
        except HTTPError as exc:
            if exc.code != 429 or attempt == RETRIES - 1:
                raise PriceError(f"Retail Prices API: {exc}") from exc
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            sleep(float(retry_after) if retry_after and retry_after.isdigit() else 2 ** (attempt + 1))
        except (URLError, OSError, ValueError) as exc:
            raise PriceError(f"Retail Prices API unreachable ({exc}); cached prices are used when present") from exc
    raise AssertionError("unreachable")


def fetch(query: PriceQuery, currency: str, get=None) -> list[dict]:
    get = get or _get_json
    url: str | None = f"{API}?{urlencode({'currencyCode': currency, '$filter': query.filter()})}"
    items: list[dict] = []
    while url:
        page = get(url)
        items.extend(page.get("Items") or [])
        url = page.get("NextPageLink")
    return items


class PriceBook:
    """Prices per query, read from cache/prices/<currency>/ or fetched once (and cached) when missing or refreshing."""

    def __init__(self, cache_dir: Path, currency: str, refresh: bool = False, get=None):
        self.currency = currency.upper()
        self.dir = cache_dir / "prices" / self.currency
        self.refresh = refresh
        self.get = get
        self.fetched: set[str] = set()  # dates of the price lists used
        self._items: dict[PriceQuery, list[dict]] = {}

    def items(self, query: PriceQuery) -> list[dict]:
        if query not in self._items:
            path = self.dir / f"{query.slug()}.json"
            if path.exists() and not self.refresh:
                data = read_json(path, "infradiag cost --refresh-prices")
            else:
                data = {"filter": query.filter(), "currency": self.currency, "fetched": date.today().isoformat(),
                        "items": fetch(query, self.currency, self.get)}
                write_json(path, data)
            self.fetched.add(data["fetched"])
            self._items[query] = data["items"]
        return self._items[query]

    def price(self, meter: Meter) -> Price:
        return match(self.items(meter.query), meter)
