"""Run what-if for each of a subscription's Bicep entry points and cache the raw result."""

from __future__ import annotations

from .azcli import AzError, run_az
from .cache import CacheMissing, read_json, write_json
from .config import ConfigError, EntryPoint, Target


def whatif_args(cfg: Target, ep: EntryPoint) -> list[str]:
    args = [
        "deployment", "sub", "what-if",
        "--subscription", cfg.subscription,
        "--name", ep.deployment_name,
        "--location", cfg.location,
        "--template-file", ep.template,
        "--result-format", "FullResourcePayloads",
        "--no-pretty-print",
    ]
    for param in ep.parameters:
        args += ["--parameters", param]
    return args


def _cache_path(cfg: Target, ep: EntryPoint):
    return cfg.cache_dir / "whatif" / f"{ep.name}.json"


def extract_bicep(cfg: Target, run=None, log=print) -> dict[str, str]:
    run = run or run_az
    if not cfg.bicep_root.is_dir():
        raise ConfigError(f"bicep_root {cfg.bicep_root} is not a directory; point it at your Bicep project")
    failures: dict[str, str] = {}
    for ep in cfg.entry_points:
        log(f"what-if {ep.name} ({ep.template}) ...")
        try:
            raw = run(whatif_args(cfg, ep), cwd=cfg.bicep_root) or {}
        except AzError as exc:
            raw = {"error": str(exc)}
        if raw.get("error"):
            failures[ep.name] = raw["error"] if isinstance(raw["error"], str) else str(raw["error"])
            log(f"  FAILED: {failures[ep.name]}")
        write_json(_cache_path(cfg, ep), raw)
    return failures


def load_whatif_cache(cfg: Target) -> dict[str, dict]:
    hint = f"infradiag extract --bicep --subscription {cfg.subscription}"
    missing = [ep.name for ep in cfg.entry_points if not _cache_path(cfg, ep).exists()]
    if missing:
        raise CacheMissing(f"no what-if cache for: {', '.join(missing)} — run `{hint}`")
    return {ep.name: read_json(_cache_path(cfg, ep), hint) for ep in cfg.entry_points}
