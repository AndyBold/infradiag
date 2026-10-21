"""Load the config (infradiag.yaml): where the Bicep project is, and per subscription which entry points to what-if
and which RGs to read from Azure.

Subscriptions themselves are discovered from `az login`; the config only names (by display name or ID) the ones that
need settings, and they are matched to the discovered ones at run time. Relative paths are relative to the config file,
so one install serves any number of projects, each with its own config, cache and output.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

import yaml

from .subscriptions import Subscription, matching, subscription_label

CONFIG_NAME = "infradiag.yaml"
DEFAULTS_DIR = Path(__file__).parent / "defaults"
DEFAULT_VIEWS = DEFAULTS_DIR / "views.yaml"
KNOWN_KEYS = ("bicep_root", "location", "views", "cache_dir", "out_dir", "subscriptions", "cost")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class EntryPoint:
    name: str
    deployment_name: str
    template: str
    parameters: tuple[str, ...]
    resource_groups: tuple[str, ...]


@dataclass(frozen=True)
class SubscriptionConfig:
    location: str | None
    resource_groups: tuple[str, ...]  # empty = every resource group
    entry_points: tuple[EntryPoint, ...]


@dataclass(frozen=True)
class UsageRule:
    """Monthly usage to assume for usage-billed resources whose type (and name) match these fnmatch patterns."""
    type: str
    name: str
    values: dict[str, float]


@dataclass(frozen=True)
class CostConfig:
    currency: str = "GBP"
    usage: tuple[UsageRule, ...] = ()

    def usage_for(self, resource_type: str, name: str) -> dict[str, float]:
        """Usage values for one resource; for each key the first matching rule wins."""
        return {key: rule.values[key] for key, rule in self.usage_rules_for(resource_type, name).items()}

    def usage_rules_for(self, resource_type: str, name: str) -> dict[str, UsageRule]:
        """The rule each of one resource's usage values comes from."""
        found: dict[str, UsageRule] = {}
        for rule in self.usage:
            if fnmatchcase(resource_type.lower(), rule.type) and fnmatchcase(name.lower(), rule.name):
                for key in rule.values:
                    found.setdefault(key, rule)
        return found


@dataclass(frozen=True)
class Target:
    """Everything one subscription's extract, diff and render need."""
    bicep_root: Path
    subscription: str
    subscription_name: str
    location: str | None
    resource_groups: tuple[str, ...]  # empty = every resource group
    entry_points: tuple[EntryPoint, ...]
    cache_dir: Path
    out_dir: Path

    @property
    def label(self) -> str:
        return subscription_label(self.subscription, self.subscription_name)


@dataclass(frozen=True)
class Config:
    bicep_root: Path
    subscriptions: dict[str, SubscriptionConfig]  # keyed by the display name or ID the config gives, as written
    cache_dir: Path
    out_dir: Path
    views_path: Path
    cost: CostConfig = field(default_factory=CostConfig)

    def match(self, subs: list[Subscription]) -> tuple[dict[str, SubscriptionConfig], list[str]]:
        """Settings per discovered subscription ID, and the configured subscriptions none of `subs` matches."""
        found: dict[str, SubscriptionConfig] = {}
        unmatched: list[str] = []
        for key, sc in self.subscriptions.items():
            hits = matching(subs, key)
            if not hits:
                unmatched.append(key)
                continue
            if len(hits) > 1:
                raise ConfigError(f"config subscription '{key}' matches {len(hits)} subscriptions, use the ID: "
                                  f"{', '.join(s.label for s in hits)}")
            if hits[0].id in found:
                raise ConfigError(f"config configures {hits[0].label} twice")
            found[hits[0].id] = sc
        return found, unmatched

    def target(self, sub: Subscription, out_folder: str, sc: SubscriptionConfig | None) -> Target:
        """Without settings a subscription gets every resource group and no entry points (Azure only)."""
        sc = sc or SubscriptionConfig(None, (), ())
        return Target(self.bicep_root, sub.id, sub.name, sc.location, sc.resource_groups, sc.entry_points,
                      self.cache_dir / sub.id, self.out_dir / out_folder)


def _require(data: dict, key: str, where: str):
    if key not in data or data[key] in (None, "", []):
        raise ConfigError(f"{where}: missing required key '{key}'")
    return data[key]


def _entry_points(raw_list: list, where: str) -> tuple[EntryPoint, ...]:
    entry_points = []
    for i, raw in enumerate(raw_list):
        ep_where = f"{where}.entry_points[{i}]"
        name = _require(raw, "name", ep_where)
        entry_points.append(
            EntryPoint(
                name=name,
                deployment_name=raw.get("deployment_name") or f"infra-{name}",
                template=_require(raw, "template", ep_where),
                parameters=tuple(str(p) for p in raw.get("parameters") or []),
                resource_groups=tuple(rg.lower() for rg in _require(raw, "resource_groups", ep_where)),
            )
        )
    names = [ep.name for ep in entry_points]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ConfigError(f"{where}: duplicate entry point names: {', '.join(dupes)}")
    return tuple(entry_points)


def _cost(raw: dict | None) -> CostConfig:
    raw = raw or {}
    rules = []
    for i, rule in enumerate(raw.get("usage") or []):
        where = f"cost.usage[{i}]"
        rtype = str(_require(rule, "type", where)).lower()
        values = {k: v for k, v in rule.items() if k not in ("type", "name")}
        bad = sorted(k for k, v in values.items() if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0)
        if bad:
            raise ConfigError(f"{where}: usage values must be non-negative numbers: {', '.join(bad)}")
        if not values:
            raise ConfigError(f"{where}: no usage values (e.g. ingestion_gb: 10)")
        rules.append(UsageRule(rtype, str(rule.get("name") or "*").lower(), {k: float(v) for k, v in values.items()}))
    currency = str(raw.get("currency") or "GBP").upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ConfigError(f"cost.currency: expected a 3-letter currency code, got '{currency}'")
    return CostConfig(currency, tuple(rules))


def find_config(path: Path | None, cwd: Path | None = None) -> Path:
    """The config to use: the one given, else infradiag.yaml in the current directory."""
    if path is not None:
        return Path(path)
    found = (cwd or Path.cwd()) / CONFIG_NAME
    if not found.is_file():
        raise ConfigError(f"no {CONFIG_NAME} in {found.parent} — pass --config <path>, or run `infradiag init` to"
                          " create one")
    return found


def _path(base: Path, value) -> Path:
    return base / Path(str(value)).expanduser()


def load_config(path: Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping of settings")
    if "subscription" in data or "entry_points" in data:
        raise ConfigError("config: 'subscription' and top-level 'entry_points' are replaced by a 'subscriptions'"
                          " list (subscription name or ID, location, resource_groups, entry_points per subscription)")
    unknown = sorted(k for k in data if k not in KNOWN_KEYS)
    if unknown:
        raise ConfigError(f"{path}: unknown key(s) {', '.join(unknown)}; expected {', '.join(KNOWN_KEYS)}")

    subscriptions: dict[str, SubscriptionConfig] = {}
    for i, raw in enumerate(data.get("subscriptions") or []):
        where = f"subscriptions[{i}]"
        key = str(_require(raw, "subscription", where)).strip()
        if key.lower() in (k.lower() for k in subscriptions):
            raise ConfigError(f"{where}: subscription '{key}' is listed twice")
        entry_points = _entry_points(raw.get("entry_points") or [], where)
        location = raw.get("location") or data.get("location")
        if entry_points and not location:
            raise ConfigError(f"{where}: missing required key 'location' (needed for what-if)")
        subscriptions[key] = SubscriptionConfig(
            location=location,
            resource_groups=tuple(rg.lower() for rg in raw.get("resource_groups") or []),
            entry_points=entry_points,
        )

    base = path.parent
    return Config(
        bicep_root=_path(base, data.get("bicep_root") or "."),
        subscriptions=subscriptions,
        cache_dir=_path(base, data.get("cache_dir") or "cache"),
        out_dir=_path(base, data.get("out_dir") or "out"),
        views_path=_path(base, data["views"]) if data.get("views") else DEFAULT_VIEWS,
        cost=_cost(data.get("cost")),
    )
