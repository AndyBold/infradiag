import io
import json
from urllib.error import HTTPError

import pytest

from infradiag import prices
from infradiag.prices import Meter, NoPrice, PriceBook, PriceError, PriceQuery, fetch, match

VPN = PriceQuery("VPN Gateway", "westeurope")


def item(meter, price, unit="1 Hour", product="VPN Gateway", sku=None, tier=0.0, arm_sku="", **extra):
    return {"productName": product, "skuName": sku or meter, "meterName": meter, "unitOfMeasure": unit,
            "retailPrice": price, "tierMinimumUnits": tier, "armSkuName": arm_sku, **extra}


def test_match_by_names_and_unit_ignoring_case():
    items = [item("VpnGw1", 0.1434), item("VpnGw1AZ", 0.1585), item("P2S Connection", 0.0075, sku="VpnGw1AZ")]
    p = match(items, Meter(VPN, "1/hour", "vpn gateway", "VPNGW1AZ", "VpnGw1AZ"))
    assert (p.unit_price, p.unit) == (0.1585, "1 Hour")


def test_tiered_meter_uses_first_paid_tier():
    items = [item("Analytics Logs Data Ingestion", 0.0, unit="1 GB"),
             item("Analytics Logs Data Ingestion", 2.0831, unit="1 GB", tier=5.0),
             item("Analytics Logs Data Ingestion", 1.5, unit="1 GB", tier=100.0)]
    assert match(items, Meter(VPN, "1 GB", meter="Analytics Logs Data Ingestion")).unit_price == 2.0831


def test_free_meter_prices_at_zero():
    assert match([item("Free Instance", 0.0, unit="1/Day")], Meter(VPN, "1/Day", meter="Free Instance")).unit_price == 0


def test_reject_and_arm_sku_pick_the_linux_pay_as_you_go_vm():
    q = PriceQuery("Virtual Machines", "westeurope", "Standard_D2als_v7")
    items = [item("D2als v7", 0.0726, product="Virtual Machines Dalsv7 Series", arm_sku="Standard_D2als_v7"),
             item("D2als v7 Spot", 0.0134, product="Virtual Machines Dalsv7 Series", arm_sku="Standard_D2als_v7"),
             item("D2als v7", 0.1419, product="Virtual Machines Dalsv7 Series Windows", arm_sku="Standard_D2als_v7")]
    m = Meter(q, "1 Hour", arm_skus=("standard_d2als_v7",), reject=("Windows", "Spot"))
    assert match(items, m).unit_price == 0.0726


def test_no_matching_item_raises_with_the_meter_sought():
    with pytest.raises(NoPrice, match="no list price for VPN Gateway / StandardV2"):
        match([item("VpnGw1", 0.1)], Meter(VPN, "1 Hour", sku="StandardV2"))


def test_several_prices_for_one_meter_are_not_guessed_between():
    with pytest.raises(NoPrice, match="2 different list prices"):
        match([item("Gw", 0.1), item("Gw", 0.2)], Meter(VPN, "1 Hour", meter="Gw"))


def test_same_price_listed_twice_is_fine():
    assert match([item("vCore", 0.47), item("vCore", 0.47)], Meter(VPN, "1 Hour", meter="vCore")).unit_price == 0.47


def test_fetch_follows_next_page_link():
    pages = {"first": {"Items": [1, 2], "NextPageLink": "second"}, "second": {"Items": [3], "NextPageLink": None}}
    urls = []

    def get(url):
        urls.append(url)
        return pages["first" if len(urls) == 1 else "second"]

    assert fetch(VPN, "GBP", get) == [1, 2, 3]
    assert "currencyCode=GBP" in urls[0] and "VPN+Gateway" in urls[0] and urls[1] == "second"


def test_price_book_caches_and_rereads_offline(tmp_path):
    calls = []

    def get(url):
        calls.append(url)
        return {"Items": [item("VpnGw1AZ", 0.1585)]}

    m = Meter(VPN, "1 Hour", meter="VpnGw1AZ")
    assert PriceBook(tmp_path, "gbp", get=get).price(m).unit_price == 0.1585
    cached = json.loads((tmp_path / "prices" / "GBP" / "VPN-Gateway--westeurope.json").read_text())
    assert cached["currency"] == "GBP" and cached["items"][0]["meterName"] == "VpnGw1AZ"

    def offline(url):
        raise PriceError("network down")

    book = PriceBook(tmp_path, "GBP", get=offline)
    assert book.price(m).unit_price == 0.1585
    assert book.fetched == {cached["fetched"]}
    assert len(calls) == 1


def test_price_book_refresh_refetches_once_per_run(tmp_path):
    calls = []

    def get(url):
        calls.append(url)
        return {"Items": [item("VpnGw1AZ", 0.2 if len(calls) > 1 else 0.1585)]}

    m = Meter(VPN, "1 Hour", meter="VpnGw1AZ")
    PriceBook(tmp_path, "GBP", get=get).price(m)
    book = PriceBook(tmp_path, "GBP", refresh=True, get=get)
    assert book.price(m).unit_price == 0.2
    book.price(m)
    assert len(calls) == 2


def test_throttled_requests_are_retried(monkeypatch):
    responses = [HTTPError("u", 429, "Too Many Requests", {"Retry-After": "3"}, None),
                 io.BytesIO(json.dumps({"Items": []}).encode())]

    def fake_urlopen(url, timeout):
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    slept = []
    monkeypatch.setattr(prices, "urlopen", fake_urlopen)
    assert prices._get_json("u", sleep=slept.append) == {"Items": []}
    assert slept == [3.0]


def test_other_http_errors_fail(monkeypatch):
    def fake_urlopen(url, timeout):
        raise HTTPError("u", 400, "Bad Request", {}, None)

    monkeypatch.setattr(prices, "urlopen", fake_urlopen)
    with pytest.raises(PriceError, match="400"):
        prices._get_json("u", sleep=lambda s: None)
