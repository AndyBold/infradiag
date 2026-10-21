"""Query Azure Resource Graph for everything in a subscription's configured RGs (or all of them) and cache the rows."""

from __future__ import annotations

from .azcli import run_az
from .cache import CacheMissing, read_json, write_json
from .config import Target


def _hint(cfg: Target) -> str:
    return f"infradiag extract --azure --subscription {cfg.subscription}"


def _scope(rgs) -> str:
    return ", ".join(rgs) if rgs else "every resource group"


def build_query(cfg: Target) -> str:
    where = f"subscriptionId =~ '{cfg.subscription}'"
    if cfg.resource_groups:
        rgs = ", ".join(f"'{rg}'" for rg in cfg.resource_groups)
        where += f" and resourceGroup in~ ({rgs})"
    return (
        "resources"
        f" | where {where}"
        " | project id, name, type, resourceGroup, subscriptionId, location, kind, managedBy, sku, identity, properties"
        " | order by id asc"
    )


def extract_azure(cfg: Target, run=None) -> int:
    run = run or run_az
    rows: list[dict] = []
    skip_token = None
    while True:
        args = ["graph", "query", "-q", build_query(cfg), "--first", "1000", "--subscriptions", cfg.subscription]
        if skip_token:
            args += ["--skip-token", skip_token]
        page = run(args) or {}
        rows.extend(page.get("data") or [])
        skip_token = page.get("skip_token")
        if not skip_token:
            break
    # resource_groups: null = every resource group
    write_json(cfg.cache_dir / "arg.json", {"resource_groups": list(cfg.resource_groups) or None, "rows": rows})
    return len(rows)


def load_arg_cache(cfg: Target) -> list[dict]:
    cached = read_json(cfg.cache_dir / "arg.json", _hint(cfg))
    cached_rgs = cached.get("resource_groups")  # None = every resource group
    if (cached_rgs is None) != (not cfg.resource_groups):
        raise CacheMissing(f"Resource Graph cache was read for {_scope(cached_rgs)}, the config now asks for "
                           f"{_scope(cfg.resource_groups)} — run `{_hint(cfg)}`")
    missing = sorted(set(cfg.resource_groups) - set(cached_rgs or []))
    if missing:
        raise CacheMissing(f"Resource Graph cache lacks {', '.join(missing)} — run `{_hint(cfg)}`")
    return cached.get("rows") or []
