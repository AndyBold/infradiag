"""Monthly cost estimates from Azure list prices, per subscription and resource group (environment).

Actual spend from Cost Management (actuals.py) can be shown alongside; estimates stay the default.

Every resource infradiag extracted is priced: Resource Graph rows as deployed, plus what-if resources not yet in Azure
(planned). Each resource type maps its SKU and size to Retail Prices API meters below. Usage-billed meters are priced
only from the usage the config states under cost.usage; anything else is listed as not estimated, never guessed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase

from .actuals import NO_GROUP, Actuals, resource_group_of
from .config import CostConfig, Target, UsageRule
from .diff import EXISTS_PER_WHATIF
from .extract_azure import load_arg_cache
from .extract_bicep import load_whatif_cache
from .ids import normalise_id, parse_id
from .normalise import NODE_CHANGE_TYPES
from .prices import Meter, NoPrice, PriceBook, PriceQuery

HOURS = 730  # Azure bills a month as 730 hours
DAYS = HOURS / 24
DNS_ZONE = "Zone 1"  # Azure DNS prices by zone; Zone 1 covers North America and Europe

DEPLOYED, PLANNED, UNKNOWN = "deployed", "planned", "unknown"
ESTIMATED, FREE, NOT_ESTIMATED = "estimated", "no charge", "not estimated"
# Where a charge's quantity comes from: the billing period and the resource's own settings, a cost.usage rule, or a
# default the estimator assumed because the resource doesn't say.
FIXED, USAGE_RULE, DEFAULT = "fixed", "usage rule", "default"


@dataclass
class Resource:
    id: str
    type: str
    name: str
    resource_group: str
    location: str
    sku: dict
    kind: str
    properties: dict
    status: str  # deployed (in Azure, or what-if says it exists), planned (the next deployment creates it), unknown

    @property
    def region(self) -> str:
        return self.location.replace(" ", "").lower()


def _resource(rid: str, raw: dict, status: str) -> Resource | None:
    parts = parse_id(rid)
    if parts is None:
        return None
    return Resource(normalise_id(rid), parts.type, (raw.get("name") or parts.name).split("/")[-1],
                    parts.resource_group, raw.get("location") or "", raw.get("sku") or {}, raw.get("kind") or "",
                    raw.get("properties") or {}, status)


def collect_resources(cfg: Target) -> list[Resource]:
    """Resource Graph rows, then what-if resources Resource Graph lacks. Inline subnets and peerings carry no charge."""
    found: dict[str, Resource] = {}
    for row in load_arg_cache(cfg):
        if (r := _resource(row.get("id", ""), row, DEPLOYED)) is not None:
            found[r.id] = r
    if cfg.entry_points:
        for raw in load_whatif_cache(cfg).values():
            if raw.get("error") or raw.get("status") == "Failed":
                continue
            for change in raw.get("changes") or []:
                change_type = change.get("changeType")
                if change_type not in NODE_CHANGE_TYPES and change_type != "Unsupported":
                    continue
                status = (DEPLOYED if change_type in EXISTS_PER_WHATIF
                          else UNKNOWN if change_type == "Unsupported" else PLANNED)
                r = _resource(change.get("resourceId", ""), change.get("after") or {}, status)
                if r is not None and r.id not in found:
                    found[r.id] = r
    return sorted(found.values(), key=lambda r: (r.resource_group, r.type, r.name))


# --- mapping resources to meters ------------------------------------------------------------------------------------

class NotEstimated(Exception):
    pass


@dataclass(frozen=True)
class Charge:
    label: str
    meter: Meter
    quantity: float                # meter units per month
    basis: str                     # how the quantity was reached, for the report
    source: str = FIXED            # fixed, usage rule or default
    rule: UsageRule | None = None  # the cost.usage rule a usage-rule quantity comes from


@dataclass
class Plan:
    charges: list[Charge] = field(default_factory=list)
    missing_usage: list[str] = field(default_factory=list)  # usage-billed parts the config gives no usage for
    notes: list[str] = field(default_factory=list)          # parts the estimate leaves out
    free: str = ""                                          # why the resource itself costs nothing


def _hourly(label: str, meter: Meter, count: float = 1, source: str = FIXED) -> Charge:
    return Charge(label, meter, HOURS * count, f"{HOURS} h" + (f" × {count:g}" if count != 1 else ""), source)


def _daily(label: str, meter: Meter) -> Charge:
    return Charge(label, meter, DAYS, f"{DAYS:.2f} days")


def _monthly(label: str, meter: Meter) -> Charge:
    """A meter priced per item per month."""
    return Charge(label, meter, 1, "1 month")


class Usage:
    """Usage the config states for one resource; asking for a key it lacks records the part as not estimated."""

    def __init__(self, rules: dict[str, UsageRule], plan: Plan):
        self.rules, self.plan = rules, plan
        self.values = {key: rule.values[key] for key, rule in rules.items()}

    def get(self, key: str, what: str) -> float | None:
        if key not in self.values:
            self.plan.missing_usage.append(f"{what} (cost.usage `{key}`)")
            return None
        return self.values[key]

    def charge(self, key: str, what: str, meter: Meter, scale: float = 1, unit: str = "") -> None:
        value = self.get(key, what)
        if value is not None:
            self.plan.charges.append(Charge(what, meter, value * scale, f"{value:g} {unit or key} (assumed)", USAGE_RULE,
                                            self.rules[key]))


def _sku_name(r: Resource) -> str:
    return str(r.sku.get("name") or "")


def _vm(region: str, size: str, label: str, hours: float, basis: str, source: str = FIXED,
        rule: UsageRule | None = None) -> Charge:
    """A Linux pay-as-you-go VM: Spot, low-priority, Windows and Cloud Services prices are other items for the size."""
    meter = Meter(PriceQuery("Virtual Machines", region, size), "1 Hour",
                  reject=("Windows", "Spot", "Low Priority", "Cloud Services", "Dedicated Host"))
    return Charge(label, meter, hours, basis, source, rule)


def _registry(r: Resource, plan: Plan, usage: Usage) -> None:
    tier = _sku_name(r).title()
    meter = Meter(PriceQuery("Container Registry", r.region), "1/Day", "Container Registry", tier,
                  f"{tier} Registry Unit")
    plan.charges.append(_daily(f"{tier} registry", meter))
    plan.notes.append("storage above the tier's included allowance and build tasks not included")


def _vpn_gateway(r: Resource, plan: Plan, usage: Usage) -> None:
    sku = (r.properties.get("sku") or r.sku or {}).get("name") or ""
    if (r.properties.get("gatewayType") or "Vpn").lower() != "vpn":
        raise NotEstimated("ExpressRoute gateways aren't mapped")
    meter_name = "Basic Gateway" if sku.lower() == "basic" else sku
    plan.charges.append(_hourly(f"{sku} gateway", Meter(PriceQuery("VPN Gateway", r.region), "1 Hour",
                                                        "VPN Gateway", sku, meter_name)))
    plan.notes.append("P2S/S2S connection hours and data transfer not included")


def _public_ip(r: Resource, plan: Plan, usage: Usage) -> None:
    sku, tier = _sku_name(r) or "Basic", (r.sku.get("tier") or "Regional")
    if tier.lower() == "global":
        sku_name, meter_name = "Global", "Global IPv4 Static Public IP"
    elif sku.lower() == "basic":
        allocation = (r.properties.get("publicIPAllocationMethod") or "Dynamic").title()
        sku_name, meter_name = "Basic", f"Basic IPv4 {allocation} Public IP"
    else:
        sku_name, meter_name = sku, f"{sku} IPv4 Static Public IP"
    plan.charges.append(_hourly(f"{sku} public IP", Meter(PriceQuery("Virtual Network", r.region), "1 Hour",
                                                          "IP Addresses", sku_name, meter_name)))


def _nat_gateway(r: Resource, plan: Plan, usage: Usage) -> None:
    sku = _sku_name(r) or "Standard"
    query = PriceQuery("NAT Gateway", "Global")
    plan.charges.append(_hourly(f"{sku} NAT gateway", Meter(query, "1 Hour", "NAT Gateway", sku, f"{sku} Gateway")))
    usage.charge("data_processed_gb", "data processed",
                 Meter(query, "1 GB", "NAT Gateway", sku, f"{sku} Data Processed"), unit="GB")


def _private_endpoint(r: Resource, plan: Plan, usage: Usage) -> None:
    meter = Meter(PriceQuery("Virtual Network", "Global"), "1 Hour", "Virtual Network Private Link", "Standard",
                  "Standard Private Endpoint")
    plan.charges.append(_hourly("private endpoint", meter))
    plan.notes.append("data processed not included")


def _private_dns_zone(r: Resource, plan: Plan, usage: Usage) -> None:
    query = PriceQuery("Azure DNS", DNS_ZONE)
    plan.charges.append(_monthly("private zone", Meter(query, "1", "Azure DNS", "Private", "Private Zone")))
    usage.charge("queries_millions", "DNS queries", Meter(query, "1M", "Azure DNS", "Private", "Private Queries"),
                 unit="million queries")


def _resolver_endpoint(direction: str):
    def estimate(r: Resource, plan: Plan, usage: Usage) -> None:
        meter = Meter(PriceQuery("Azure DNS", DNS_ZONE), "1", "Azure DNS", "Private Resolver",
                      f"Private Resolver {direction} Endpoint")
        plan.charges.append(_monthly(f"{direction.lower()} endpoint", meter))
    return estimate


def _forwarding_ruleset(r: Resource, plan: Plan, usage: Usage) -> None:
    meter = Meter(PriceQuery("Azure DNS", DNS_ZONE), "1", "Azure DNS", "Private Resolver",
                  "Private Resolver DNS Forwarding Ruleset")
    plan.charges.append(_monthly("forwarding ruleset", meter))


def _event_hubs(r: Resource, plan: Plan, usage: Usage) -> None:
    tier = _sku_name(r).title()
    units = float(r.sku.get("capacity") or 1)
    source = FIXED if r.sku.get("capacity") else DEFAULT
    query = PriceQuery("Event Hubs", r.region)
    unit_meter = {"Basic": "Basic Throughput Unit", "Standard": "Standard Throughput Unit",
                  "Premium": "Premium Processing Unit"}.get(tier)
    if unit_meter is None:
        raise NotEstimated(f"Event Hubs {tier or 'unknown'} tier isn't mapped")
    plan.charges.append(_hourly(unit_meter.lower() + "s", Meter(query, "1 Hour", "Event Hubs", tier, unit_meter), units,
                                source))
    if tier != "Premium":
        usage.charge("ingress_events_millions", "ingress events",
                     Meter(query, "1M", "Event Hubs", tier, f"{tier} Ingress Events"), unit="million events")


def _service_bus(r: Resource, plan: Plan, usage: Usage) -> None:
    tier = _sku_name(r).title()
    query = PriceQuery("Service Bus", r.region)
    if tier == "Standard":
        plan.charges.append(_hourly("base unit", Meter(query, "1/Hour", "Service Bus", tier, "Standard Base Unit")))
    elif tier == "Premium":
        units = float(r.sku.get("capacity") or 1)
        plan.charges.append(_hourly("messaging units", Meter(query, "1/Hour", "Service Bus", tier,
                                                             "Premium Messaging Unit"), units,
                                    FIXED if r.sku.get("capacity") else DEFAULT))
        return
    elif tier != "Basic":
        raise NotEstimated(f"Service Bus {tier or 'unknown'} tier isn't mapped")
    usage.charge("operations_millions", "messaging operations",
                 Meter(query, "1M", "Service Bus", tier, f"{tier} Messaging Operations"), unit="million operations")


def _app_configuration(r: Resource, plan: Plan, usage: Usage) -> None:
    tier = _sku_name(r).title()
    if tier == "Free":
        plan.free = "Free tier"
        return
    meter = Meter(PriceQuery("App Configuration", r.region), "1/Day", "App Configuration", tier, f"{tier} Instance")
    plan.charges.append(_daily(f"{tier} store", meter))
    plan.notes.append("operations above the daily allowance not included")


def _key_vault(r: Resource, plan: Plan, usage: Usage) -> None:
    tier = ((r.properties.get("sku") or r.sku or {}).get("name") or "standard").title()
    usage.charge("operations", "operations", Meter(PriceQuery("Key Vault", r.region), "10K", "Key Vault", tier,
                                                   "Operations"), scale=1 / 10_000)


DTU_OBJECTIVES = {10: "S0", 20: "S1", 50: "S2", 100: "S3", 200: "S4", 400: "S6", 800: "S7", 1600: "S9", 3000: "S12",
                  125: "P1", 250: "P2", 500: "P4", 1000: "P6", 1750: "P11", 4000: "P15"}


def _sql_database(r: Resource, plan: Plan, usage: Usage) -> None:
    tier = (r.sku.get("tier") or "").lower()
    if tier == "system" or "system" in r.kind.lower():
        plan.free = "system database"
        return
    name = _sku_name(r)
    query = PriceQuery("SQL Database", r.region)
    if tier in ("basic", "standard", "premium"):
        objective = (r.properties.get("currentServiceObjectiveName")
                     or r.properties.get("requestedServiceObjectiveName") or name)
        if objective.lower() == tier:
            objective = "B" if tier == "basic" else DTU_OBJECTIVES.get(int(r.sku.get("capacity") or 0), objective)
        sku = "B" if tier == "basic" else objective
        meter = Meter(query, "1/Day", f"SQL Database Single {tier.title()}", sku,
                      "B DTU" if tier == "basic" else f"{sku} DTUs")
        plan.charges.append(_daily(f"{objective} database", meter))
        return
    if name.upper().startswith("GP_S_GEN5"):
        usage.charge("vcore_hours", "serverless compute",
                     Meter(query, "1 Hour", "SQL Database General Purpose - Serverless - Compute Gen5", "1 vCore",
                           "vCore"), unit="vCore hours")
        plan.notes.append("data storage not included")
        return
    raise NotEstimated(f"SQL Database SKU {name or 'unknown'} isn't mapped")


def _postgres(r: Resource, plan: Plan, usage: Usage) -> None:
    name, tier = _sku_name(r), (r.sku.get("tier") or "").lower()
    query = PriceQuery("Azure Database for PostgreSQL", r.region)
    ha = (r.properties.get("highAvailability") or {}).get("mode") or "Disabled"
    servers = 1 if ha.lower() == "disabled" else 2
    if tier == "burstable":
        meter = Meter(query, "1 Hour", "Azure Database for PostgreSQL Flexible Server Burstable BS Series Compute",
                      arm_skus=(name, name.removeprefix("Standard_")))
    elif tier in ("generalpurpose", "memoryoptimized"):
        meter = Meter(query, "1 Hour", arm_skus=(name,), reject=("Reserved",))
    else:
        raise NotEstimated(f"PostgreSQL {tier or 'unknown'} tier isn't mapped")
    plan.charges.append(_hourly(f"{name} compute" + (" (with HA standby)" if servers > 1 else ""), meter, servers))
    storage = r.properties.get("storage") or {}
    if "v2" in str(storage.get("type") or "").lower():
        plan.notes.append("Premium SSD v2 storage not included")
    elif size := storage.get("storageSizeGB"):
        meter = Meter(query, "1 GB/Month", "Azure Database for PostgreSQL Flex Server Storage", "Storage",
                      "Storage Data Stored")
        plan.charges.append(Charge("storage", meter, float(size) * servers, f"{size} GB" +
                                   (f" × {servers}" if servers > 1 else "")))
    plan.notes.append("backup storage above the provisioned size not included")


def _aks(r: Resource, plan: Plan, usage: Usage) -> None:
    tier = (r.sku.get("tier") or "Free").title()
    if tier == "Standard":
        plan.charges.append(_hourly("Standard tier control plane", Meter(
            PriceQuery("Azure Kubernetes Service", r.region), "1 Hour", "Azure Kubernetes Service", "Standard",
            "Standard Uptime SLA")))
    elif tier != "Free":
        plan.notes.append(f"{tier} tier control plane not included")
    for pool in r.properties.get("agentPoolProfiles") or []:
        size, count = pool.get("vmSize"), pool.get("count") or 0
        if (pool.get("osType") or "Linux").lower() != "linux":
            plan.notes.append(f"{pool.get('osType')} node pool {pool.get('name')} not included")
        elif size and count:
            plan.charges.append(_vm(r.region, size, f"{pool.get('name')} nodes ({size})", HOURS * count,
                                    f"{HOURS} h × {count} nodes"))
    plan.notes.append("node OS disks not included; autoscaling pools priced at their current node count")


def _devops_pool(r: Resource, plan: Plan, usage: Usage) -> None:
    size = ((r.properties.get("fabricProfile") or {}).get("sku") or {}).get("name")
    if not size:
        raise NotEstimated("agent VM size not found")
    hours = usage.get("agent_hours", f"agent VMs ({size})")
    if hours is not None:
        plan.charges.append(_vm(r.region, size, f"agent VMs ({size})", hours, f"{hours:g} agent hours (assumed)",
                                USAGE_RULE, usage.rules["agent_hours"]))
        plan.notes.append("agent OS disks not included")


def _log_analytics(r: Resource, plan: Plan, usage: Usage) -> None:
    sku = ((r.properties.get("sku") or {}).get("name") or "pergb2018").lower()
    if sku == "free":
        plan.free = "Free tier"
        return
    if sku != "pergb2018":
        raise NotEstimated(f"Log Analytics {sku} pricing tier isn't mapped")
    usage.charge("ingestion_gb", "data ingestion", Meter(PriceQuery("Log Analytics", r.region), "1 GB",
                                                          "Log Analytics", "Analytics Logs",
                                                          "Analytics Logs Data Ingestion"), unit="GB")
    if (r.properties.get("retentionInDays") or 0) > 31:
        plan.notes.append("retention beyond the included period not included")


def _app_insights(r: Resource, plan: Plan, usage: Usage) -> None:
    if r.properties.get("WorkspaceResourceId") or r.properties.get("IngestionMode") == "LogAnalytics":
        plan.free = "billed through its Log Analytics workspace"
        return
    raise NotEstimated("classic (non-workspace) Application Insights is usage-billed and isn't mapped")


def _storage_account(r: Resource, plan: Plan, usage: Usage) -> None:
    sku = _sku_name(r)
    performance, _, redundancy = sku.partition("_")
    if performance.lower() != "standard" or r.kind not in ("StorageV2", "BlobStorage"):
        raise NotEstimated(f"{r.kind or 'storage'} {sku or 'unknown SKU'} isn't mapped")
    access = (r.properties.get("accessTier") or "Hot").title()
    meter = Meter(PriceQuery("Storage", r.region), "1 GB/Month", "General Block Blob v2", f"{access} {redundancy}",
                  f"{access} {redundancy} Data Stored")
    usage.charge("stored_gb", f"{access} {redundancy} blob storage", meter, unit="GB")
    plan.notes.append("transactions, files, queues and tables not included")


def _load_balancer(r: Resource, plan: Plan, usage: Usage) -> None:
    sku = _sku_name(r) or "Basic"
    if sku.lower() == "basic":
        plan.free = "Basic SKU"
        return
    if sku.lower() != "standard":
        raise NotEstimated(f"{sku} load balancer isn't mapped")
    rules = len(r.properties.get("loadBalancingRules") or []) + len(r.properties.get("outboundRules") or [])
    query = PriceQuery("Load Balancer", "Global")
    if rules == 0:
        plan.free = "no load-balancing or outbound rules"
        return
    plan.charges.append(_hourly("first 5 rules", Meter(query, "1 Hour", "Load Balancer", "Standard",
                                                       "Standard Included LB Rules and Outbound Rules")))
    if rules > 5:
        plan.charges.append(_hourly("additional rules", Meter(query, "1/Hour", "Load Balancer", "Standard",
                                                              "Standard Overage LB Rules and Outbound Rules"),
                                    rules - 5))
    plan.notes.append("data processed not included")


def _cognitive(r: Resource, plan: Plan, usage: Usage) -> None:
    if _sku_name(r).upper() == "F0":
        plan.free = "F0 (free) tier"
        return
    raise NotEstimated(f"{r.kind or 'Cognitive Services'} {_sku_name(r)} is billed per transaction and isn't mapped")


def _container_environment(r: Resource, plan: Plan, usage: Usage) -> None:
    profiles = r.properties.get("workloadProfiles") or []
    if any((p.get("workloadProfileType") or "").lower() != "consumption" for p in profiles):
        raise NotEstimated("dedicated workload profiles aren't mapped")
    plan.free = "no charge for a Consumption-only environment; its apps are billed per use"


def _container_app(r: Resource, plan: Plan, usage: Usage) -> None:
    profile = r.properties.get("workloadProfileName")
    if profile and profile.lower() != "consumption":
        plan.free = f"runs on dedicated workload profile {profile}, billed with its environment"
        return
    raise NotEstimated("billed per use (vCPU and memory seconds, requests) on the Consumption plan")


ESTIMATORS = {
    "microsoft.containerregistry/registries": _registry,
    "microsoft.network/virtualnetworkgateways": _vpn_gateway,
    "microsoft.network/publicipaddresses": _public_ip,
    "microsoft.network/natgateways": _nat_gateway,
    "microsoft.network/privateendpoints": _private_endpoint,
    "microsoft.network/privatednszones": _private_dns_zone,
    "microsoft.network/dnsresolvers/inboundendpoints": _resolver_endpoint("Inbound"),
    "microsoft.network/dnsresolvers/outboundendpoints": _resolver_endpoint("Outbound"),
    "microsoft.network/dnsforwardingrulesets": _forwarding_ruleset,
    "microsoft.network/loadbalancers": _load_balancer,
    "microsoft.eventhub/namespaces": _event_hubs,
    "microsoft.servicebus/namespaces": _service_bus,
    "microsoft.appconfiguration/configurationstores": _app_configuration,
    "microsoft.keyvault/vaults": _key_vault,
    "microsoft.sql/servers/databases": _sql_database,
    "microsoft.dbforpostgresql/flexibleservers": _postgres,
    "microsoft.containerservice/managedclusters": _aks,
    "microsoft.devopsinfrastructure/pools": _devops_pool,
    "microsoft.operationalinsights/workspaces": _log_analytics,
    "microsoft.insights/components": _app_insights,
    "microsoft.storage/storageaccounts": _storage_account,
    "microsoft.cognitiveservices/accounts": _cognitive,
    "microsoft.app/managedenvironments": _container_environment,
    "microsoft.app/containerapps": _container_app,
    "microsoft.app/jobs": _container_app,
}

# Types with no charge of their own (patterns, checked after ESTIMATORS). Child resources here are billed via their parent.
NO_CHARGE = {
    "microsoft.authorization/*": "no charge",
    "microsoft.managedidentity/*": "no charge",
    "microsoft.insights/diagnosticsettings": "no charge (the destination bills ingestion)",
    "microsoft.insights/privatelinkscopes": "no charge (its private endpoint is billed)",
    "microsoft.insights/privatelinkscopes/*": "no charge",
    "microsoft.portal/dashboards": "no charge",
    "microsoft.devcenter/devcenters": "no charge without dev boxes",
    "microsoft.devcenter/projects": "no charge without dev boxes",
    "microsoft.network/virtualnetworks": "no charge",
    "microsoft.network/virtualnetworks/subnets": "no charge",
    "microsoft.network/networksecuritygroups": "no charge",
    "microsoft.network/networkinterfaces": "no charge",
    "microsoft.network/routetables": "no charge",
    "microsoft.network/networkwatchers": "no charge",
    "microsoft.network/dnsresolvers": "billed per endpoint",
    "microsoft.network/privatednszones/*": "billed with the zone",
    "microsoft.network/privateendpoints/privatednszonegroups": "no charge",
    "microsoft.sql/servers": "billed per database",
    "microsoft.sql/servers/firewallrules": "no charge",
    "microsoft.sql/servers/virtualnetworkrules": "no charge",
    "microsoft.dbforpostgresql/flexibleservers/*": "billed with the server",
    "microsoft.keyvault/vaults/*": "billed with the vault",
    "microsoft.eventhub/namespaces/*": "billed with the namespace",
    "microsoft.servicebus/namespaces/*": "billed with the namespace",
    "microsoft.storage/storageaccounts/*": "billed with the account",
}

# Usage-billed types with nothing mapped: say why, rather than "not mapped".
USAGE_BILLED = {
    "microsoft.network/virtualnetworks/virtualnetworkpeerings": "billed per GB transferred",
    "microsoft.insights/actiongroups": "billed per notification",
    "microsoft.insights/scheduledqueryrules": "billed per rule evaluated",
    "microsoft.insights/metricalerts": "billed per metric monitored",
}


@dataclass
class Line:
    label: str
    basis: str
    quantity: float
    unit: str
    unit_price: float
    meter: str = ""                # the Retail Prices item's meterName and productName
    product: str = ""
    source: str = FIXED            # where the quantity comes from (see Charge)
    rule: UsageRule | None = None

    @property
    def monthly(self) -> float:
        return self.quantity * self.unit_price


@dataclass
class Estimate:
    resource: Resource
    state: str                                        # estimated, no charge, not estimated
    lines: list[Line] = field(default_factory=list)
    missing_usage: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    reason: str = ""                                  # why it's free or not estimated

    @property
    def monthly(self) -> float:
        return sum(line.monthly for line in self.lines)


def _lookup(table: dict[str, str], resource_type: str) -> str | None:
    return next((v for k, v in table.items() if fnmatchcase(resource_type, k)), None)


def estimate(r: Resource, book: PriceBook, cost: CostConfig) -> Estimate:
    estimator = ESTIMATORS.get(r.type)
    if estimator is None:
        if (free := _lookup(NO_CHARGE, r.type)) is not None:
            return Estimate(r, FREE, reason=free)
        return Estimate(r, NOT_ESTIMATED, reason=_lookup(USAGE_BILLED, r.type) or "resource type isn't mapped")
    if r.status == UNKNOWN:
        return Estimate(r, NOT_ESTIMATED, reason="what-if couldn't evaluate it")
    plan = Plan()
    try:
        estimator(r, plan, Usage(cost.usage_rules_for(r.type, r.name), plan))
        lines = []
        for c in plan.charges:
            p = book.price(c.meter)
            lines.append(Line(c.label, c.basis, c.quantity, p.unit, p.unit_price, p.meter, p.product, c.source, c.rule))
    except NotEstimated as exc:
        return Estimate(r, NOT_ESTIMATED, reason=str(exc))
    except NoPrice as exc:
        return Estimate(r, NOT_ESTIMATED, reason=str(exc))
    if plan.free:
        return Estimate(r, FREE, reason=plan.free)
    if not lines:
        return Estimate(r, NOT_ESTIMATED, missing_usage=plan.missing_usage, notes=plan.notes,
                        reason="usage-billed; no usage in the config for " + "; ".join(plan.missing_usage))
    return Estimate(r, ESTIMATED, lines, plan.missing_usage, plan.notes)


# --- the report -----------------------------------------------------------------------------------------------------

@dataclass
class GroupTotal:
    resource_group: str
    deployed: float = 0.0
    planned: float = 0.0
    estimated: int = 0
    free: int = 0
    not_estimated: int = 0

    @property
    def total(self) -> float:
        return self.deployed + self.planned


def totals(estimates: list[Estimate]) -> list[GroupTotal]:
    """One row per resource group. Shared resource groups keep their own row; nothing is split between rows."""
    groups: dict[str, GroupTotal] = {}
    for e in estimates:
        g = groups.setdefault(e.resource.resource_group, GroupTotal(e.resource.resource_group))
        if e.state == ESTIMATED:
            g.estimated += 1
            if e.resource.status == PLANNED:
                g.planned += e.monthly
            else:
                g.deployed += e.monthly
        elif e.state == FREE:
            g.free += 1
        else:
            g.not_estimated += 1
    return sorted(groups.values(), key=lambda g: g.resource_group)


@dataclass
class CostReport:
    subscription: str
    currency: str
    estimates: list[Estimate]
    price_dates: list[str]
    actuals: Actuals | None = None
    detail: bool = False  # add the per-resource, per-meter breakdown

    @property
    def groups(self) -> list[GroupTotal]:
        return totals(self.estimates)

    @property
    def billed(self) -> dict[str, dict[str, float]] | None:
        """Actual cost per resource and meter, when the detail query ran and succeeded."""
        a = self.actuals
        return a.by_resource if self.detail and a is not None and not a.error and not a.detail_error else None


def build_report(cfg: Target, book: PriceBook, cost: CostConfig, actuals: Actuals | None = None,
                 detail: bool = False) -> CostReport:
    estimates = [estimate(r, book, cost) for r in collect_resources(cfg)]
    return CostReport(cfg.label, book.currency, estimates, sorted(book.fetched), actuals, detail)


# --- the detailed breakdown -----------------------------------------------------------------------------------------

def sku_label(r: Resource) -> str:
    """The SKU, tier and capacity Azure reports for the resource."""
    sku = r.sku or r.properties.get("sku") or {}
    name, tier, capacity = (str(sku.get(k) or "") for k in ("name", "tier", "capacity"))
    parts = [name, tier if tier.lower() != name.lower() else "", f"capacity {capacity}" if capacity else ""]
    return " / ".join(p for p in parts if p)


@dataclass
class MeterDetail:
    """One row of a resource's breakdown: an estimated meter, a billed meter, or both when they are the same meter."""
    line: Line | None = None
    billed_meter: str = ""
    actual: float | None = None


@dataclass
class ResourceDetail:
    resource_id: str
    estimate: Estimate | None      # None: billed, but no extracted resource has this ID
    meters: list[MeterDetail]
    actual: float | None = None    # None without actual detail; 0 when nothing was billed to it
    unmatched: str = ""            # why a billed resource matched nothing extracted

    @property
    def name(self) -> str:
        if self.estimate is not None:
            return self.estimate.resource.name
        parts = parse_id(self.resource_id)
        return parts.name if parts else self.resource_id or "(no resource)"

    @property
    def type(self) -> str:
        if self.estimate is not None:
            return self.estimate.resource.type
        parts = parse_id(self.resource_id)
        return parts.type if parts else ""


@dataclass
class GroupDetail:
    resource_group: str
    resources: list[ResourceDetail] = field(default_factory=list)  # extracted resources, with what was billed to them
    unmatched: list[ResourceDetail] = field(default_factory=list)  # billed resources matching nothing extracted
    note: str = ""                                                 # set for a resource group infradiag didn't extract


# Resource groups Azure creates and manages for a resource elsewhere, whose charges estimates put with that resource.
MANAGED_GROUPS = {
    "mc_*": "Azure-managed resource group holding an AKS cluster's nodes, disks and load balancer;"
            " the estimate prices nodes with the cluster",
    "me_*": "Azure-managed resource group holding a Container Apps environment's infrastructure",
}


def _group_note(rg: str) -> str:
    if rg == NO_GROUP:
        return "charges Cost Management ties to no resource group"
    return _lookup(MANAGED_GROUPS, rg) or "resource group infradiag didn't extract"


def _unmatched_reason(rid: str, rg: str, extracted: set[str]) -> str:
    managed = _lookup(MANAGED_GROUPS, rg) is not None
    if not rid:
        return "charge tied to no resource"
    if rg in extracted:
        return "not in the extract (" + ("Azure-managed resource group" if managed
                                         else "deleted since, or not returned by Resource Graph") + ")"
    return "in an Azure-managed resource group" if managed else "resource group not extracted"


def _matched(e: Estimate, billed: dict[str, float] | None) -> ResourceDetail:
    """Pair an estimated meter with a billed one only when the names are equal and no other estimated line shares it;
    everything else billed is listed on its own."""
    names = [line.meter.lower() for line in e.lines if line.meter]
    remaining = {m.lower(): (m, cost) for m, cost in (billed or {}).items()}
    rows = []
    for line in e.lines:
        key = line.meter.lower()
        hit = remaining.pop(key, None) if key and names.count(key) == 1 else None
        rows.append(MeterDetail(line, *(hit or ("", None))))
    rows += [MeterDetail(billed_meter=m, actual=cost) for m, cost in sorted(remaining.values(), key=lambda mc: -mc[1])]
    return ResourceDetail(e.resource.id, e, rows, None if billed is None else sum(billed.values()))


def breakdown(report: CostReport) -> list[GroupDetail]:
    """Per resource group: each priced or unpriced resource (and free ones that were billed anyway), its meters and
    what was billed to it, then billed resources that match nothing extracted. Extracted groups come first."""
    billed = dict(report.billed) if report.billed is not None else None
    extracted = {e.resource.resource_group for e in report.estimates}
    groups = {rg: GroupDetail(rg) for rg in sorted(extracted)}
    for e in report.estimates:
        meters = billed.pop(e.resource.id, {}) if billed is not None else None
        if e.state != FREE or meters:
            groups[e.resource.resource_group].resources.append(_matched(e, meters))
    for rid, meters in (billed or {}).items():
        rg = resource_group_of(rid)
        g = groups.setdefault(rg, GroupDetail(rg, note=_group_note(rg)))
        rows = [MeterDetail(billed_meter=m, actual=cost) for m, cost in sorted(meters.items(), key=lambda mc: -mc[1])]
        g.unmatched.append(ResourceDetail(rid, None, rows, sum(meters.values()), _unmatched_reason(rid, rg, extracted)))
    for g in groups.values():
        g.resources.sort(key=lambda d: (-(d.estimate.monthly if d.estimate.state == ESTIMATED else 0),
                                        -(d.actual or 0), d.name))
        g.unmatched.sort(key=lambda d: (-(d.actual or 0), d.resource_id))
    return sorted(groups.values(), key=lambda g: (bool(g.note), g.resource_group))


SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€"}


def _money(amount: float, currency: str) -> str:
    symbol = SYMBOLS.get(currency)
    return f"{symbol}{amount:,.2f}" if symbol else f"{amount:,.2f} {currency}".rstrip()


def _unit_price(amount: float, unit: str, currency: str) -> str:
    """e.g. "£0.1585 per hour" for a "1 Hour" meter, "£0.0226 per 10K" for a "10K" one."""
    symbol = SYMBOLS.get(currency)
    text = f"{amount:.4f}".rstrip("0").rstrip(".")
    price = f"{symbol}{text}" if symbol else f"{text} {currency}"
    per = unit[2:] if unit[:2] in ("1 ", "1/") else unit
    per = per.lower().replace("gb", "GB").replace("gib", "GiB")
    return f"{price} each" if per == "1" else f"{price} per {per}"


def _environment_table(report: CostReport) -> list[str]:
    cur, actuals = report.currency, report.actuals
    money = lambda amount: _money(amount, cur)  # noqa: E731
    groups = {g.resource_group: g for g in report.groups}
    show_actual = actuals is not None and not actuals.error
    names = sorted(groups.keys() | (actuals.by_group.keys() if show_actual else set()))
    head = "| Environment (resource group) | Estimate: deployed | Estimate: planned | **Estimate / month** |"
    rule = "|---|--:|--:|--:|"
    if show_actual:
        head += f" **Actual {actuals.period}** |"
        rule += "--:|"
    lines = [head + " Estimated | No charge | Not estimated |", rule + "--:|--:|--:|"]

    def actual_cell(name: str | None) -> str:
        if not show_actual:
            return ""
        amount = actuals.total if name is None else actuals.by_group.get(name)
        return f" {'—' if amount is None else _money(amount, actuals.currency)} |"

    for name in names:
        g = groups.get(name)
        if g is None:
            lines.append(f"| {name} | — | — | — |{actual_cell(name)} — | — | — |")
            continue
        lines.append(f"| {name} | {money(g.deployed)} | {money(g.planned)} | **{money(g.total)}** |{actual_cell(name)}"
                     f" {g.estimated} | {g.free} | {g.not_estimated} |")
    rows = groups.values()
    deployed, planned = sum(g.deployed for g in rows), sum(g.planned for g in rows)
    lines.append(f"| **Subscription total** | **{money(deployed)}** | **{money(planned)}** | **{money(deployed + planned)}**"
                 f" |{actual_cell(None)} {sum(g.estimated for g in rows)} | {sum(g.free for g in rows)}"
                 f" | {sum(g.not_estimated for g in rows)} |")
    return lines + [""]


def _actuals_intro(report: CostReport) -> list[str]:
    actuals = report.actuals
    if actuals is None:
        return []
    if actuals.error:
        return [f"**Actual cost ({actuals.period}): not available for this subscription** — {actuals.error}."
                " The estimates below still apply.", ""]
    currency = f" ({actuals.currency})" if actuals.currency else ""
    text = (f"**Actual cost** is what Azure Cost Management billed for {actuals.period} (its ActualCost view: negotiated"
            f" prices and credits as billed, reservation purchases not amortised), in the billing currency{currency}."
            " It covers every resource group in the subscription, including ones infradiag doesn't extract"
            " (shown with no estimate). Azure bills some resources in the resource groups it manages (AKS nodes in"
            " `mc_*`, Container Apps load balancers in `me_*`); estimates put them with the resource that owns them.")
    if actuals.currency and actuals.currency != report.currency:
        text += f" Estimates are in {report.currency}, so compare the two with the exchange rate in mind."
    if not actuals.by_group:
        text += " Cost Management returned no charges for this period."
    lines = [text, ""]
    if report.detail and actuals.detail_error:
        lines += [f"**Actual cost per resource: not available** — {actuals.detail_error}. The detailed breakdowns"
                  " below show estimates only.", ""]
    return lines


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def _quantity(line: Line) -> str:
    return f"{line.quantity:,.2f}".rstrip("0").rstrip(".") + f" × {line.unit}"


def _source(line: Line) -> str:
    if line.source == USAGE_RULE and line.rule is not None:
        name = "" if line.rule.name == "*" else f", name `{line.rule.name}`"
        return f"usage rule (`{line.rule.type}`{name}): {line.basis}"
    return f"{line.source}: {line.basis}"


def _detail_lines(g: GroupDetail, report: CostReport) -> list[str]:
    """The breakdown for one resource group, collapsed under <details> so the summary stays readable."""
    cur, actuals = report.currency, report.actuals
    billed = report.billed is not None
    period = f" {actuals.period}" if actuals is not None else ""
    money = lambda amount: _money(amount, cur)  # noqa: E731
    paid = lambda amount: "" if amount is None else _money(amount, actuals.currency)  # noqa: E731
    lines: list[str] = []
    if g.resources:
        head = ["Resource / meter", "Type", "SKU / size", "Unit price", "Quantity", "Quantity source", "Estimate / month"]
        width = len(head) + billed

        def row(*cells: str) -> str:
            return "| " + " | ".join([*cells, *[""] * (width - len(cells))]) + " |"

        lines += [row(*head, *([f"Actual{period}"] if billed else [])),
                  "|---|---|---|--:|--:|---|--:|" + ("--:|" if billed else "")]
        for d in g.resources:
            e = d.estimate
            r = e.resource
            total = f"**{money(e.monthly)}**" if e.state == ESTIMATED else e.state
            nothing_billed = all(m.actual is None for m in d.meters)
            actual = ["—" if nothing_billed else f"**{paid(d.actual)}**"] if billed else []
            lines.append(row(f"**{_cell(r.name)}** ({r.status})", f"`{r.type}`", _cell(sku_label(r)) or "—", "", "",
                             "", total, *actual))
            for m in d.meters:
                if m.line is None and round(m.actual or 0, 2) == 0:
                    continue  # free-grant meters billed at zero; the JSON keeps them
                if m.line is None:
                    lines.append(row(f"↳ billed: {_cell(m.billed_meter)}", "", "", "", "", "", "", paid(m.actual)))
                    continue
                meter = f"{_cell(m.line.label)} — {_cell(m.line.meter)}" if m.line.meter else _cell(m.line.label)
                lines.append(row(f"↳ {meter}", "", "", _unit_price(m.line.unit_price, m.line.unit, cur),
                                 _quantity(m.line), _cell(_source(m.line)), money(m.line.monthly),
                                 *([paid(m.actual)] if billed else [])))
            if e.state != ESTIMATED:
                lines.append(row(f"↳ _{e.state}: {_cell(e.reason)}_"))
            else:
                lines += [row(f"↳ _not estimated: {_cell(u)} — no usage stated_") for u in e.missing_usage]
                lines += [row(f"↳ _{_cell(n)}_") for n in e.notes]
        lines.append("")
    if g.unmatched:
        if g.resources:
            lines += [f"Billed in {g.resource_group} but not matched to an extracted resource:", ""]
        lines += [f"| Billed resource / meter | Type | Actual{period} | Why unmatched |", "|---|---|--:|---|"]
        for d in g.unmatched:
            lines.append(f"| **{_cell(d.name)}** | {f'`{d.type}`' if d.type else '—'} | **{paid(d.actual)}**"
                         f" | {d.unmatched} |")
            lines += [f"| ↳ {_cell(m.billed_meter)} | | {paid(m.actual)} | |" for m in d.meters
                      if round(m.actual or 0, 2) != 0]
        lines.append("")
    if not lines:
        return []
    count = len(g.resources) + len(g.unmatched)
    return ["<details>", f"<summary>Detailed breakdown: {count} resource{'s' if count != 1 else ''}</summary>", "",
            *lines, "</details>", ""]


def render_markdown(report: CostReport) -> str:
    cur = report.currency
    money = lambda amount: _money(amount, cur)  # noqa: E731
    groups = report.groups
    details = {d.resource_group: d for d in breakdown(report)} if report.detail else {}
    lines = [
        "# Cost estimate",
        "",
        f"Subscription: {report.subscription}",
        "",
        f"_Estimated monthly cost in {cur} from Azure Retail Prices API pay-as-you-go list prices"
        f" ({', '.join(report.price_dates) or 'no prices fetched'}). Estimates are not actual spend: list prices"
        " exclude discounts, reservations, savings plans, free monthly grants and tax. A month is 730 hours; tiered"
        " meters use their first paid tier. Usage-billed parts are included only where the config states usage"
        " (`cost.usage`); data transfer and per-request charges are otherwise left out._",
        "",
        *_actuals_intro(report),
        "## By environment",
        "",
        "_One row per resource group; shared resource groups keep their own row. Planned = in Bicep, not yet in Azure._",
        "",
        *_environment_table(report),
    ]

    for g in groups:
        priced = sorted((e for e in report.estimates if e.resource.resource_group == g.resource_group
                         and e.state == ESTIMATED), key=lambda e: -e.monthly)
        free = [e for e in report.estimates if e.resource.resource_group == g.resource_group and e.state == FREE]
        lines += [f"## {g.resource_group}", ""]
        if priced:
            lines += ["| Resource | Type | Status | Per month | Basis | Not included |", "|---|---|---|--:|---|---|"]
            for e in priced:
                basis = "; ".join(f"{line.label}: {line.basis} × {_unit_price(line.unit_price, line.unit, cur)}"
                                  for line in e.lines)
                left_out = "; ".join([*(f"{m} — no usage stated" for m in e.missing_usage), *e.notes])
                lines.append(f"| {e.resource.name} | `{e.resource.type}` | {e.resource.status} | {money(e.monthly)}"
                             f" | {basis} | {left_out} |")
            lines.append("")
        else:
            lines += ["No estimated resources.", ""]
        if free:
            counts: dict[str, int] = {}
            for e in free:
                counts[e.resource.type] = counts.get(e.resource.type, 0) + 1
            lines += ["No charge: " + ", ".join(f"`{t}` ×{n}" for t, n in sorted(counts.items())) + ".", ""]
        if report.detail:
            lines += _detail_lines(details[g.resource_group], report)

    for d in (d for d in details.values() if d.note):
        lines += [f"## {d.resource_group}", "", f"_Not extracted: {d.note}. Billed charges only, no estimate._", "",
                  *_detail_lines(d, report)]

    missing = [e for e in report.estimates if e.state == NOT_ESTIMATED]
    lines += ["## Not estimated", "",
              "_Not in the totals above: the type isn't mapped to list prices, or it is billed by usage that"
              " the config doesn't state._", ""]
    if not missing:
        lines += ["None.", ""]
    else:
        lines += ["| Resource | Type | Resource group | Status | Reason |", "|---|---|---|---|---|"]
        lines += [f"| {e.resource.name} | `{e.resource.type}` | {e.resource.resource_group} | {e.resource.status}"
                  f" | {e.reason} |" for e in missing]
        lines.append("")
    return "\n".join(lines)


def _actuals_json(actuals: Actuals | None) -> dict | None:
    if actuals is None:
        return None
    return {"period": {"from": actuals.period.start.isoformat(), "to": actuals.period.end.isoformat()},
            "currency": actuals.currency or None, "error": actuals.error or None,
            "by_resource_group": {rg: round(v, 2) for rg, v in sorted(actuals.by_group.items())},
            "total": None if actuals.error else round(actuals.total, 2)}


def _line_json(line: Line) -> dict:
    return {"label": line.label, "meter": line.meter or None, "product": line.product or None,
            "unit_price": line.unit_price, "unit": line.unit, "quantity": round(line.quantity, 4),
            "quantity_source": line.source, "basis": line.basis,
            "usage_rule": {"type": line.rule.type, "name": line.rule.name} if line.rule else None,
            "monthly": round(line.monthly, 2)}


def _meter_json(m: MeterDetail) -> dict:
    return {"estimate": _line_json(m.line) if m.line else None, "billed_meter": m.billed_meter or None,
            "actual": None if m.actual is None else round(m.actual, 2)}


def _resource_detail_json(d: ResourceDetail) -> dict:
    data = {"id": d.resource_id or None, "name": d.name, "type": d.type or None}
    if d.estimate is not None:
        e = d.estimate
        data.update({"sku": sku_label(e.resource) or None, "status": e.resource.status, "state": e.state,
                     "monthly": round(e.monthly, 2) if e.state == ESTIMATED else None, "reason": e.reason or None,
                     "missing_usage": e.missing_usage, "notes": e.notes})
    else:
        data["unmatched"] = d.unmatched
    data["actual"] = None if d.actual is None else round(d.actual, 2)
    data["meters"] = [_meter_json(m) for m in d.meters]
    return data


def _detail_json(report: CostReport) -> list[dict]:
    return [{"resource_group": g.resource_group, "extracted": not g.note, "note": g.note or None,
             "resources": [_resource_detail_json(d) for d in g.resources],
             "unmatched_actual": [_resource_detail_json(d) for d in g.unmatched]}
            for g in breakdown(report)]


def render_json(report: CostReport) -> dict:
    data = {
        "subscription": report.subscription,
        "currency": report.currency,
        "basis": "estimates: Azure Retail Prices API pay-as-you-go list prices, 730 hours per month;"
                 " actual: Azure Cost Management ActualCost in the billing currency",
        "price_dates": report.price_dates,
        "environments": [
            {"resource_group": g.resource_group, "deployed": round(g.deployed, 2), "planned": round(g.planned, 2),
             "total": round(g.total, 2), "estimated": g.estimated, "no_charge": g.free,
             "not_estimated": g.not_estimated}
            for g in report.groups
        ],
        "total": round(sum(g.total for g in report.groups), 2),
        "actual": _actuals_json(report.actuals),
        "resources": [
            {"id": e.resource.id, "name": e.resource.name, "type": e.resource.type,
             "resource_group": e.resource.resource_group, "status": e.resource.status, "state": e.state,
             "monthly": round(e.monthly, 2) if e.state == ESTIMATED else None, "reason": e.reason or None,
             "lines": [{"label": line.label, "basis": line.basis, "quantity": round(line.quantity, 4),
                        "unit": line.unit, "unit_price": line.unit_price, "monthly": round(line.monthly, 2)}
                       for line in e.lines],
             "missing_usage": e.missing_usage, "notes": e.notes}
            for e in report.estimates
        ],
    }
    if report.detail:
        data["detail"] = _detail_json(report)
        if report.actuals is not None:
            data["actual"]["detail_error"] = report.actuals.detail_error or None
    return data
