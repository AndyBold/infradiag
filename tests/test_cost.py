import json
from datetime import date
from pathlib import Path

import pytest

from infradiag.actuals import Actuals, Period
from infradiag.config import CostConfig, EntryPoint, Target, UsageRule
from infradiag.cost import (
    DEFAULT,
    DEPLOYED,
    ESTIMATED,
    FIXED,
    FREE,
    NOT_ESTIMATED,
    PLANNED,
    USAGE_RULE,
    CostReport,
    Resource,
    breakdown,
    collect_resources,
    estimate,
    render_json,
    render_markdown,
    totals,
)
from infradiag.prices import PriceQuery, match
from tests.factories import DEV, SHARED, SUB, rid, whatif_change, whatif_result

REGION = "westeurope"


def price(service, meter, value, unit="1 Hour", product=None, sku=None, region=REGION, arm_sku="", tier=0.0):
    return {"serviceName": service, "armRegionName": region, "productName": product or service,
            "skuName": sku or meter, "meterName": meter, "unitOfMeasure": unit, "retailPrice": value,
            "tierMinimumUnits": tier, "armSkuName": arm_sku}


PRICES = [
    price("VPN Gateway", "VpnGw1AZ", 0.16),
    price("SQL Database", "S2 DTUs", 1.8, unit="1/Day", product="SQL Database Single Standard", sku="S2"),
    price("SQL Database", "vCore", 0.5, product="SQL Database General Purpose - Serverless - Compute Gen5",
          sku="1 vCore"),
    price("Azure Database for PostgreSQL", "B1MS", 0.02,
          product="Azure Database for PostgreSQL Flexible Server Burstable BS Series Compute", arm_sku="B1MS"),
    price("Azure Database for PostgreSQL", "Storage Data Stored", 0.1, unit="1 GB/Month",
          product="Azure Database for PostgreSQL Flex Server Storage", sku="Storage"),
    price("Virtual Machines", "D2als v7", 0.07, product="Virtual Machines Dalsv7 Series", arm_sku="Standard_D2als_v7"),
    price("Virtual Machines", "D2als v7", 0.14, product="Virtual Machines Dalsv7 Series Windows",
          arm_sku="Standard_D2als_v7"),
    price("Virtual Machines", "D2als v7", 0.14, product="Dalsv7 Series Cloud Services", arm_sku="Standard_D2als_v7"),
    price("Virtual Machines", "D2als v7 Spot", 0.01, product="Virtual Machines Dalsv7 Series",
          arm_sku="Standard_D2als_v7"),
    price("Virtual Network", "Standard Private Endpoint", 0.01, product="Virtual Network Private Link",
          sku="Standard", region="Global"),
    price("Log Analytics", "Analytics Logs Data Ingestion", 0.0, unit="1 GB", product="Log Analytics",
          sku="Analytics Logs"),
    price("Log Analytics", "Analytics Logs Data Ingestion", 2.0, unit="1 GB", product="Log Analytics",
          sku="Analytics Logs", tier=5.0),
    price("Event Hubs", "Standard Throughput Unit", 0.03, product="Event Hubs", sku="Standard"),
]


class FakeBook:
    """A PriceBook over PRICES, recording the queries it was asked for."""

    currency = "GBP"
    fetched = {"2026-10-21"}

    def __init__(self):
        self.queries = []

    def items(self, query: PriceQuery):
        self.queries.append(query)
        return [i for i in PRICES if i["serviceName"] == query.service and i["armRegionName"] == query.region
                and (not query.arm_sku or i["armSkuName"] == query.arm_sku)]

    def price(self, meter):
        return match(self.items(meter.query), meter)


def res(rtype, name, rg=DEV, sku=None, properties=None, kind="", status=DEPLOYED, location=REGION):
    provider = "/".join(f"{p}/{name}" for p in rtype.split("/")[1:])
    return Resource(rid(rg, f"{rtype.split('/')[0]}/{provider}").lower(), rtype, name, rg, location, sku or {}, kind,
                    properties or {}, status)


def est(r, usage=()):
    return estimate(r, FakeBook(), CostConfig("GBP", tuple(usage)))


# --- meter mapping ---------------------------------------------------------------------------------------------------

def test_vpn_gateway_priced_hourly_from_its_sku():
    e = est(res("microsoft.network/virtualnetworkgateways", "vgw",
                properties={"sku": {"name": "VpnGw1AZ"}, "gatewayType": "Vpn"}))
    assert e.state == ESTIMATED
    assert e.monthly == pytest.approx(730 * 0.16)
    assert e.lines[0].basis == "730 h"


def test_standard_sql_database_maps_dtu_capacity_to_its_service_objective():
    e = est(res("microsoft.sql/servers/databases", "db", sku={"name": "Standard", "tier": "Standard", "capacity": 50}))
    assert e.state == ESTIMATED
    assert e.lines[0].label == "S2 database"
    assert e.monthly == pytest.approx(730 / 24 * 1.8)


def test_serverless_sql_priced_from_stated_vcore_hours():
    db = res("microsoft.sql/servers/databases", "ledger-db", sku={"name": "GP_S_Gen5", "tier": "GeneralPurpose"})
    e = est(db, [UsageRule("microsoft.sql/servers/databases", "ledger-*", {"vcore_hours": 100})])
    assert e.state == ESTIMATED
    assert e.monthly == pytest.approx(50)
    assert e.lines[0].basis == "100 vCore hours (assumed)"
    assert "data storage not included" in e.notes


def test_postgres_burstable_compute_and_storage():
    e = est(res("microsoft.dbforpostgresql/flexibleservers", "psql", sku={"name": "Standard_B1ms", "tier": "Burstable"},
                properties={"storage": {"storageSizeGB": 64}, "highAvailability": {"mode": "Disabled"}}))
    assert e.state == ESTIMATED
    assert e.monthly == pytest.approx(730 * 0.02 + 64 * 0.1)


def test_aks_prices_linux_nodes_and_skips_free_control_plane():
    e = est(res("microsoft.containerservice/managedclusters", "aks", sku={"name": "Base", "tier": "Free"},
                properties={"agentPoolProfiles": [{"name": "system", "vmSize": "Standard_D2als_v7", "count": 2,
                                                   "osType": "Linux"}]}))
    assert e.state == ESTIMATED
    assert e.monthly == pytest.approx(730 * 2 * 0.07)


def test_global_meters_query_the_global_region():
    book = FakeBook()
    e = estimate(res("microsoft.network/privateendpoints", "pe"), book, CostConfig())
    assert e.monthly == pytest.approx(7.3)
    assert book.queries == [PriceQuery("Virtual Network", "Global")]


def test_tiered_usage_meter_ignores_free_grant():
    ws = res("microsoft.operationalinsights/workspaces", "log", properties={"sku": {"name": "PerGB2018"}})
    e = est(ws, [UsageRule("microsoft.operationalinsights/workspaces", "*", {"ingestion_gb": 10})])
    assert e.monthly == pytest.approx(20)


def test_no_charge_types_and_free_tiers():
    assert est(res("microsoft.network/virtualnetworks", "vnet")).state == FREE
    assert est(res("microsoft.managedidentity/userassignedidentities", "mi")).state == FREE
    master = est(res("microsoft.sql/servers/databases", "master", sku={"name": "System", "tier": "System"}))
    assert (master.state, master.reason) == (FREE, "system database")
    assert est(res("microsoft.cognitiveservices/accounts", "di", sku={"name": "F0"})).state == FREE


# --- not estimated ---------------------------------------------------------------------------------------------------

def test_unmapped_type_is_not_estimated():
    e = est(res("microsoft.web/sites", "app"))
    assert (e.state, e.reason) == (NOT_ESTIMATED, "resource type isn't mapped")


def test_usage_billed_without_stated_usage_is_not_estimated():
    e = est(res("microsoft.operationalinsights/workspaces", "log", properties={"sku": {"name": "PerGB2018"}}))
    assert e.state == NOT_ESTIMATED
    assert "cost.usage `ingestion_gb`" in e.reason


def test_usage_rule_for_another_name_does_not_apply():
    db = res("microsoft.sql/servers/databases", "orders-db", sku={"name": "GP_S_Gen5"})
    e = est(db, [UsageRule("microsoft.sql/servers/databases", "ledger-*", {"vcore_hours": 100})])
    assert e.state == NOT_ESTIMATED


def test_missing_list_price_is_not_estimated_not_guessed():
    e = est(res("microsoft.network/virtualnetworkgateways", "vgw",
                properties={"sku": {"name": "VpnGw9"}, "gatewayType": "Vpn"}))
    assert e.state == NOT_ESTIMATED
    assert "no list price for VPN Gateway" in e.reason


def test_unmapped_sku_of_mapped_type_is_not_estimated():
    e = est(res("microsoft.sql/servers/databases", "db", sku={"name": "HS_Gen5", "tier": "Hyperscale"}))
    assert (e.state, e.reason) == (NOT_ESTIMATED, "SQL Database SKU HS_Gen5 isn't mapped")


def test_consumption_container_apps_are_not_estimated():
    e = est(res("microsoft.app/containerapps", "ca", properties={"workloadProfileName": "Consumption"}))
    assert e.state == NOT_ESTIMATED and "billed per use" in e.reason


def test_resource_what_if_could_not_evaluate_is_not_estimated():
    e = est(res("microsoft.network/privateendpoints", "pe", status="unknown"))
    assert e.state == NOT_ESTIMATED


# --- aggregation by environment --------------------------------------------------------------------------------------

def _estimates():
    vgw = res("microsoft.network/virtualnetworkgateways", "vgw", rg=SHARED,
              properties={"sku": {"name": "VpnGw1AZ"}, "gatewayType": "Vpn"})
    pe_dev = res("microsoft.network/privateendpoints", "pe-dev")
    pe_new = res("microsoft.network/privateendpoints", "pe-new", status=PLANNED)
    pe_shared = res("microsoft.network/privateendpoints", "pe-shared", rg=SHARED)
    return [est(r) for r in (vgw, pe_dev, pe_new, pe_shared, res("microsoft.network/virtualnetworks", "vnet"),
                             res("microsoft.web/sites", "app"))]


def test_totals_by_resource_group_split_deployed_and_planned():
    groups = {g.resource_group: g for g in totals(_estimates())}
    assert set(groups) == {DEV, SHARED}
    dev, shared = groups[DEV], groups[SHARED]
    assert (dev.deployed, dev.planned) == (pytest.approx(7.3), pytest.approx(7.3))
    assert (dev.estimated, dev.free, dev.not_estimated) == (2, 1, 1)
    # shared keeps its own row, nothing divided across environments
    assert shared.deployed == pytest.approx(730 * 0.16 + 7.3)
    assert shared.planned == 0


def _report(actuals=None):
    return CostReport(f"Contoso-Dev ({SUB})", "GBP", _estimates(), ["2026-10-21"], actuals)


def test_markdown_report_lists_environments_totals_and_not_estimated():
    md = render_markdown(_report())
    assert "| rg-app-dev-weu | £7.30 | £7.30 | **£14.60** |" in md
    assert "| **Subscription total** | **£131.40** | **£7.30** | **£138.70** |" in md
    assert "exclude discounts, reservations, savings plans" in md
    assert "| app | `microsoft.web/sites` | rg-app-dev-weu | deployed | resource type isn't mapped |" in md
    assert "private endpoint: 730 h × £0.01 per hour" in md
    assert "Actual" not in md


def test_actuals_shown_side_by_side_including_groups_without_estimates():
    actuals = Actuals(Period(date(2026, 9, 1), date(2026, 9, 30)), "USD",
                      {DEV: 20.0, SHARED: 150.0, "rg-app-test-weu": 30.0})
    md = render_markdown(_report(actuals))
    assert "**Actual 2026-09-01 to 2026-09-30** |" in md
    assert "| rg-app-dev-weu | £7.30 | £7.30 | **£14.60** | $20.00 |" in md
    assert "| rg-app-test-weu | — | — | — | $30.00 |" in md
    assert "| **Subscription total** | **£131.40** | **£7.30** | **£138.70** | $200.00 |" in md
    assert "Estimates are in GBP" in md
    data = render_json(_report(actuals))
    assert data["actual"]["total"] == 200.0 and data["actual"]["currency"] == "USD"


def test_refused_actuals_are_reported_and_estimates_kept():
    actuals = Actuals(Period(date(2026, 9, 1), date(2026, 9, 30)), error="no billing read access (...)")
    md = render_markdown(_report(actuals))
    assert "not available for this subscription** — no billing read access" in md
    assert "| **Subscription total** | **£131.40** | **£7.30** | **£138.70** | 4 |" in md
    assert render_json(_report(actuals))["actual"]["error"].startswith("no billing read access")


def test_json_report_marks_estimates_and_not_estimated():
    data = render_json(_report())
    assert data["currency"] == "GBP" and data["total"] == 138.7
    by_name = {r["name"]: r for r in data["resources"]}
    assert by_name["pe-new"]["status"] == "planned" and by_name["pe-new"]["monthly"] == 7.3
    assert by_name["app"]["state"] == "not estimated" and by_name["app"]["monthly"] is None


# --- resources from the extract cache --------------------------------------------------------------------------------

def test_collect_resources_merges_azure_and_bicep_only(tmp_path):
    vgw, pe = rid(SHARED, "Microsoft.Network/virtualNetworkGateways/vgw"), rid(DEV, "Microsoft.Network/privateEndpoints/pe")
    new = rid(DEV, "Microsoft.Network/privateEndpoints/pe-new")
    cache = tmp_path / SUB
    (cache / "whatif").mkdir(parents=True)
    (cache / "arg.json").write_text(json.dumps({"resource_groups": [DEV, SHARED], "rows": [
        {"id": vgw, "name": "vgw", "location": REGION, "sku": None, "properties": {"sku": {"name": "VpnGw1AZ"}}},
        {"id": pe, "name": "pe", "location": REGION, "properties": {}},
    ]}))
    changes = [whatif_change("NoChange", pe), whatif_change("Create", new)]
    changes[1]["after"]["location"] = REGION
    (cache / "whatif" / "main.json").write_text(json.dumps(whatif_result(*changes)))
    cfg = Target(Path("/nonexistent"), SUB, "Contoso-Dev", REGION, (DEV, SHARED),
                 (EntryPoint("main", "infra-main", "main.bicep", (), (DEV,)),), cache, tmp_path / "out")
    found = {r.name: r for r in collect_resources(cfg)}
    assert set(found) == {"vgw", "pe", "pe-new"}
    assert found["pe"].status == DEPLOYED and found["pe-new"].status == PLANNED
    assert found["vgw"].properties["sku"]["name"] == "VpnGw1AZ" and found["pe-new"].region == REGION


# --- detailed breakdown ----------------------------------------------------------------------------------------------

SEPT = Period(date(2026, 9, 1), date(2026, 9, 30))
LOG_USAGE = UsageRule("microsoft.operationalinsights/workspaces", "log-*", {"ingestion_gb": 10})


def test_lines_record_the_matched_meter_and_where_the_quantity_comes_from():
    vgw = est(res("microsoft.network/virtualnetworkgateways", "vgw",
                  properties={"sku": {"name": "VpnGw1AZ"}, "gatewayType": "Vpn"}))
    assert (vgw.lines[0].meter, vgw.lines[0].source, vgw.lines[0].rule) == ("VpnGw1AZ", FIXED, None)
    log = est(res("microsoft.operationalinsights/workspaces", "log-app", properties={"sku": {"name": "PerGB2018"}}),
              [LOG_USAGE])
    assert (log.lines[0].meter, log.lines[0].source, log.lines[0].rule) == (
        "Analytics Logs Data Ingestion", USAGE_RULE, LOG_USAGE)
    hub = est(res("microsoft.eventhub/namespaces", "evh", sku={"name": "Standard"}))
    assert (hub.lines[0].source, hub.lines[0].quantity) == (DEFAULT, 730)  # no capacity set: one throughput unit


def _detail_estimates():
    log = res("microsoft.operationalinsights/workspaces", "log-app", properties={"sku": {"name": "PerGB2018"}})
    return _estimates() + [est(log, [LOG_USAGE])]


VGW_ID = rid(SHARED, "microsoft.network/virtualnetworkgateways/vgw").lower()
PE_DEV_ID = rid(DEV, "microsoft.network/privateendpoints/pe-dev").lower()
VNET_ID = rid(DEV, "microsoft.network/virtualnetworks/vnet").lower()
GONE_ID = rid(DEV, "microsoft.storage/storageaccounts/stgone").lower()
AKS_DISK_ID = rid("mc_rg-app-dev-weu_aks-app_westeurope", "microsoft.compute/disks/aks-os-1").lower()
BILLED = {
    VGW_ID: {"VpnGw1AZ": 110.0, "Data Transfer Out": 3.5, "Data Transfer Out - Free": 0.0},
    PE_DEV_ID: {"Standard Private Endpoint": 7.0},
    VNET_ID: {"Virtual Network Peering Ingress": 0.4},
    GONE_ID: {"Hot LRS Data Stored": 1.25},
    AKS_DISK_ID: {"E10 LRS Disk": 9.0},
}


def _detail_report(by_resource=None, detail_error=""):
    actuals = None
    if by_resource is not None or detail_error:
        actuals = Actuals(SEPT, "GBP", {DEV: 8.65, SHARED: 113.5, "mc_rg-app-dev-weu_aks-app_westeurope": 9.0},
                          by_resource=by_resource, detail_error=detail_error)
    return CostReport(f"Contoso-Dev ({SUB})", "GBP", _detail_estimates(), ["2026-10-21"], actuals, detail=True)


def test_default_report_has_no_breakdown():
    report = _detail_report(BILLED)
    report.detail = False
    md = render_markdown(report)
    assert "<details>" not in md and "Quantity source" not in md and "## mc_rg-app-dev-weu" not in md
    assert "detail" not in render_json(report)


def test_estimate_breakdown_lists_meters_quantities_and_sources():
    md = render_markdown(_detail_report())
    assert "<summary>Detailed breakdown: 4 resources</summary>" in md  # dev: log, pe-dev, pe-new, app
    assert ("| Resource / meter | Type | SKU / size | Unit price | Quantity | Quantity source | Estimate / month |"
            in md)
    assert "Actual 2026" not in md
    assert "| **vgw** (deployed) | `microsoft.network/virtualnetworkgateways` | VpnGw1AZ |  |  |  | **£116.80** |" in md
    assert "| ↳ VpnGw1AZ gateway — VpnGw1AZ |  |  | £0.16 per hour | 730 × 1 Hour | fixed: 730 h | £116.80 |" in md
    assert ("| ↳ data ingestion — Analytics Logs Data Ingestion |  |  | £2 per GB | 10 × 1 GB |"
            " usage rule (`microsoft.operationalinsights/workspaces`, name `log-*`): 10 GB (assumed) | £20.00 |") in md
    assert "| ↳ _data processed not included_ |" in md
    assert "| **app** (deployed) | `microsoft.web/sites` | — |  |  |  | not estimated |" in md
    assert "| ↳ _not estimated: resource type isn't mapped_ |" in md
    assert "vnet" not in md.split("## Not estimated")[0].split("<details>")[1]  # free and not billed: summary only


def test_breakdown_matches_billed_meters_to_estimated_ones():
    groups = {g.resource_group: g for g in breakdown(_detail_report(BILLED))}
    vgw = next(d for d in groups[SHARED].resources if d.name == "vgw")
    assert vgw.actual == pytest.approx(113.5)
    assert [(m.line.meter if m.line else None, m.billed_meter, m.actual) for m in vgw.meters] == [
        ("VpnGw1AZ", "VpnGw1AZ", 110.0), (None, "Data Transfer Out", 3.5), (None, "Data Transfer Out - Free", 0.0)]
    pe_new = next(d for d in groups[DEV].resources if d.name == "pe-new")
    assert pe_new.actual == 0 and all(m.actual is None for m in pe_new.meters)
    vnet = next(d for d in groups[DEV].resources if d.name == "vnet")  # free, but billed: shown
    assert vnet.estimate.state == FREE and vnet.actual == pytest.approx(0.4)


def test_breakdown_side_by_side_in_markdown():
    md = render_markdown(_detail_report(BILLED))
    assert "| Quantity source | Estimate / month | Actual 2026-09-01 to 2026-09-30 |" in md
    assert "| **vgw** (deployed) | `microsoft.network/virtualnetworkgateways` | VpnGw1AZ |  |  |  | **£116.80** | **£113.50** |" in md
    assert "| ↳ VpnGw1AZ gateway — VpnGw1AZ |  |  | £0.16 per hour | 730 × 1 Hour | fixed: 730 h | £116.80 | £110.00 |" in md
    assert "| ↳ billed: Data Transfer Out |  |  |  |  |  |  | £3.50 |" in md
    assert "Data Transfer Out - Free" not in md  # billed at zero: JSON only
    assert "| **pe-new** (planned) | `microsoft.network/privateendpoints` | — |  |  |  | **£7.30** | — |" in md
    assert "| **vnet** (deployed) | `microsoft.network/virtualnetworks` | — |  |  |  | no charge | **£0.40** |" in md


def test_billed_resources_matching_nothing_extracted_are_listed_separately():
    groups = {g.resource_group: g for g in breakdown(_detail_report(BILLED))}
    [gone] = groups[DEV].unmatched
    assert (gone.name, gone.type, gone.actual) == ("stgone", "microsoft.storage/storageaccounts", 1.25)
    assert gone.unmatched.startswith("not in the extract")
    managed = groups["mc_rg-app-dev-weu_aks-app_westeurope"]
    assert managed.note.startswith("Azure-managed resource group holding an AKS cluster's nodes")
    assert not managed.resources and managed.unmatched[0].name == "aks-os-1"
    assert list(groups)[-1] == "mc_rg-app-dev-weu_aks-app_westeurope"  # extracted groups first

    md = render_markdown(_detail_report(BILLED))
    assert "Billed in rg-app-dev-weu but not matched to an extracted resource:" in md
    assert ("| **stgone** | `microsoft.storage/storageaccounts` | **£1.25** | not in the extract (deleted since, or"
            " not returned by Resource Graph) |") in md
    assert "| ↳ Hot LRS Data Stored | | £1.25 | |" in md
    assert "## mc_rg-app-dev-weu_aks-app_westeurope\n\n_Not extracted: Azure-managed resource group" in md
    assert "| **aks-os-1** | `microsoft.compute/disks` | **£9.00** | in an Azure-managed resource group |" in md


def test_unmatched_charge_in_an_extracted_managed_group_says_so():
    node = res("microsoft.compute/virtualmachinescalesets", "aks-pool-vmss", rg="mc_rg-app-dev-weu_aks-app_westeurope")
    gone = rid("mc_rg-app-dev-weu_aks-app_westeurope", "microsoft.compute/disks/aks-os-old").lower()
    report = CostReport("Contoso-Dev", "GBP", [est(node)], [], Actuals(SEPT, "GBP", {}, by_resource={gone: {"P4": 2.0}}),
                        detail=True)
    [g] = breakdown(report)
    assert not g.note and g.unmatched[0].unmatched == "not in the extract (Azure-managed resource group)"


def test_charges_tied_to_no_resource_get_their_own_section():
    groups = {g.resource_group: g for g in breakdown(_detail_report({"": {"Support plan": 25.0}}))}
    other = groups["(no resource group)"]
    assert other.note == "charges Cost Management ties to no resource group"
    assert (other.unmatched[0].name, other.unmatched[0].unmatched) == ("(no resource)", "charge tied to no resource")


def test_estimated_lines_sharing_a_meter_are_not_paired_with_one_bill():
    aks = res("microsoft.containerservice/managedclusters", "aks", sku={"name": "Base", "tier": "Free"},
              properties={"agentPoolProfiles": [
                  {"name": "system", "vmSize": "Standard_D2als_v7", "count": 1, "osType": "Linux"},
                  {"name": "user", "vmSize": "Standard_D2als_v7", "count": 2, "osType": "Linux"}]})
    report = CostReport("Contoso-Dev", "GBP", [est(aks)], [], Actuals(SEPT, "GBP", {DEV: 150.0},
                        by_resource={aks.id: {"D2als v7": 150.0}}), detail=True)
    [d] = breakdown(report)[0].resources
    assert [m.actual for m in d.meters] == [None, None, 150.0]
    assert d.meters[2].billed_meter == "D2als v7" and d.meters[2].line is None


def test_missing_per_resource_actuals_say_so_and_keep_estimate_detail():
    report = _detail_report(detail_error="Cost Management query failed: BadRequest")
    md = render_markdown(report)
    assert "**Actual cost per resource: not available** — Cost Management query failed: BadRequest." in md
    assert "<summary>Detailed breakdown: 4 resources</summary>" in md and "| Quantity source | Estimate / month |\n" in md
    assert render_json(report)["actual"]["detail_error"] == "Cost Management query failed: BadRequest"


def test_json_detail_carries_meters_estimates_actuals_and_unmatched():
    data = render_json(_detail_report(BILLED))
    groups = {g["resource_group"]: g for g in data["detail"]}
    vgw = next(r for r in groups[SHARED]["resources"] if r["name"] == "vgw")
    assert (vgw["sku"], vgw["monthly"], vgw["actual"]) == ("VpnGw1AZ", 116.8, 113.5)
    gateway, transfer, free = vgw["meters"]
    assert gateway["estimate"]["meter"] == "VpnGw1AZ" and gateway["estimate"]["quantity_source"] == "fixed"
    assert (gateway["billed_meter"], gateway["actual"]) == ("VpnGw1AZ", 110.0)
    assert transfer == {"estimate": None, "billed_meter": "Data Transfer Out", "actual": 3.5}
    assert free == {"estimate": None, "billed_meter": "Data Transfer Out - Free", "actual": 0.0}
    log = next(r for r in groups[DEV]["resources"] if r["name"] == "log-app")
    assert log["meters"][0]["estimate"]["usage_rule"] == {"type": "microsoft.operationalinsights/workspaces",
                                                         "name": "log-*"}
    assert groups[DEV]["unmatched_actual"][0]["unmatched"].startswith("not in the extract")
    managed = groups["mc_rg-app-dev-weu_aks-app_westeurope"]
    assert managed["extracted"] is False and managed["resources"] == []
    assert data["actual"]["detail_error"] is None
