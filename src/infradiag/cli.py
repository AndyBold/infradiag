"""infradiag: diagrams, a drift report and cost estimates from Bicep what-if and Azure Resource Graph."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import date
from pathlib import Path

from .actuals import Period, last_full_month, load_actuals
from .azcli import AzError
from .cache import CacheMissing
from .config import CONFIG_NAME, DEFAULTS_DIR, Config, ConfigError, CostConfig, Target, find_config, load_config
from .cost import build_report
from .cost import render_json as render_cost_json
from .cost import render_markdown as render_cost_markdown
from .diff import compute_drift, display_name, node_styles, render_azure_only, render_markdown
from .edges import add_endpoint_edges, add_reference_edges
from .extract_azure import extract_azure, load_arg_cache
from .extract_bicep import extract_bicep, load_whatif_cache
from .icons import IconResolver, update_icons
from .model import Graph, merge_graphs
from .normalise import WhatIfSignals, graph_from_arg, graph_from_whatif
from .prices import PriceBook, PriceError
from .render_d2 import D2NotFound, render_d2, render_svg
from .rules import load_rules
from .subscriptions import SubscriptionError, discover, folder_names, load_subscriptions, select, write_subscriptions
from .views import build_detailed, build_summary

VIEWS = {"detailed": "Detailed topology", "summary": "Architecture overview"}


def build_graphs(cfg: Target) -> tuple[Graph, Graph, WhatIfSignals]:
    bicep, signals = graph_from_whatif(load_whatif_cache(cfg))
    azure = graph_from_arg(load_arg_cache(cfg))
    for graph in (bicep, azure):
        add_reference_edges(graph)
        add_endpoint_edges(graph)
    return bicep, azure, signals


def _targets(cfg: Config, args) -> tuple[list[Target], dict[str, str]]:
    """The subscriptions to work on, and every known subscription's name by ID (for labels).

    extract asks az which subscriptions exist and caches the list; diff and render read it back, so they stay offline.
    """
    if args.command == "extract":
        subs = discover()
        if not subs:
            raise SubscriptionError("az login can see no enabled subscriptions")
        write_subscriptions(cfg.cache_dir, subs)
    else:
        subs = load_subscriptions(cfg.cache_dir)
    settings, unmatched = cfg.match(subs)
    if not args.subscriptions:
        for key in unmatched:
            print(f"note: config subscription '{key}' isn't visible to az login; skipped", file=sys.stderr)
    folders = folder_names(subs)
    chosen = [cfg.target(s, folders[s.id], settings.get(s.id)) for s in select(subs, args.subscriptions)]
    if args.command != "extract" and not args.subscriptions:
        for t in chosen:
            if not t.cache_dir.exists():
                print(f"note: {t.label} not extracted, skipped — run `infradiag extract --subscription {t.subscription}`",
                      file=sys.stderr)
        chosen = [t for t in chosen if t.cache_dir.exists()]
        if not chosen:
            raise CacheMissing("no subscription has been extracted — run `infradiag extract`")
    return chosen, {s.id: s.name for s in subs}


def _extract(cfg: Target, args) -> int:
    print(f"== {cfg.label} ==")
    both = not args.bicep and not args.azure
    failures = {}
    if (args.bicep or both) and not cfg.entry_points:
        print("no Bicep entry points in the config: Azure only, no what-if")
    elif args.bicep or both:
        failures = extract_bicep(cfg)
    if args.azure or both:
        print(f"Resource Graph: {extract_azure(cfg)} rows")
    if failures:
        print(f"{len(failures)} what-if failure(s): {', '.join(sorted(failures))}", file=sys.stderr)
        return 1
    return 0


def _diff(cfg: Target, rules) -> int:
    out = cfg.out_dir / "drift-report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    if not cfg.entry_points:
        out.write_text(render_azure_only(cfg.label))
        print(f"{out}: Azure only — no Bicep entry points in the config, no drift comparison")
        return 0
    bicep, azure, signals = build_graphs(cfg)
    report = compute_drift(bicep, azure, signals, rules, cfg)
    names = {rid: display_name(n) for rid, n in merge_graphs(bicep, azure).nodes.items()}
    out.write_text(render_markdown(report, names, cfg.label))
    print(f"{out}: {report.matched} matched, {len(report.azure_only)} Azure-only, "
          f"{len(report.bicep_only)} Bicep-only, {len(report.modified)} modified")
    return 0


def _cost(cfg: Target, cost: CostConfig, book: PriceBook, period: Period | None, refresh: bool,
          detail: bool = False) -> int:
    actuals = load_actuals(cfg, period, refresh, detail=detail) if period else None
    report = build_report(cfg, book, cost, actuals, detail)
    out = cfg.out_dir / "cost-report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_cost_markdown(report))
    (cfg.out_dir / "cost-report.json").write_text(json.dumps(render_cost_json(report), indent=1))
    groups = report.groups
    summary = f"estimate {sum(g.total for g in groups):,.2f} {report.currency}/month"
    if actuals and actuals.error:
        print(f"note: {cfg.label}: actual cost unavailable — {actuals.error}", file=sys.stderr)
    elif actuals:
        summary += f", actual {actuals.total:,.2f}{' ' + actuals.currency if actuals.currency else ''} for {actuals.period}"
        if actuals.detail_error:
            print(f"note: {cfg.label}: actual cost per resource unavailable — {actuals.detail_error}", file=sys.stderr)
    print(f"{out}: {summary}, {sum(g.not_estimated for g in groups)} resource(s) not estimated")
    return 0


def _period(args) -> Period | None:
    if not args.actual:
        if args.start or args.end:
            raise ConfigError("--from/--to need --actual")
        return None
    default = last_full_month()
    period = Period(args.start or default.start, args.end or default.end)
    if period.start > period.end:
        raise ConfigError(f"--from {period.start} is after --to {period.end}")
    return period


def _init(directory: Path, force: bool) -> int:
    """Write a starter config and a copy of the default views next to it."""
    files = {CONFIG_NAME: directory / CONFIG_NAME, "views.yaml": directory / "views.yaml"}
    existing = [str(p) for p in files.values() if p.exists()]
    if existing and not force:
        raise ConfigError(f"{', '.join(existing)} already exist(s); use --force to overwrite")
    directory.mkdir(parents=True, exist_ok=True)
    for name, target in files.items():
        shutil.copyfile(DEFAULTS_DIR / name, target)
        print(target)
    print(f"next: set bicep_root and subscriptions in {files[CONFIG_NAME]}, then run `infradiag extract`"
          + ("" if directory.resolve() == Path.cwd().resolve() else f" with --config {files[CONFIG_NAME]}"))
    return 0


def _icons(rules, views_path: Path, icon_dir: Path) -> int:
    failures = update_icons(rules, icon_dir, base_dir=views_path.parent)
    for path, error in sorted(failures.items()):
        print(f"{path}: {error}", file=sys.stderr)
    print(f"{icon_dir}: {len(set(rules.icons.values())) - len(failures)} icons ok, {len(failures)} failed")
    return 1 if failures else 0


def _render(cfg: Target, rules, which: str, views_path: Path, icon_dir: Path, sub_names: dict[str, str]) -> int:
    bicep, azure, signals = build_graphs(cfg)
    merged = merge_graphs(bicep, azure)
    if signals.failed:
        print(f"warning: what-if failed for {', '.join(sorted(signals.failed))}; "
              "their resources are drawn as unverified", file=sys.stderr)
    if cfg.entry_points:
        styles, suffix = node_styles(compute_drift(bicep, azure, signals, rules, cfg)), ""
    else:
        print(f"note: {cfg.label}: no Bicep entry points in the config; drawn from Azure only", file=sys.stderr)
        styles, suffix = {}, " (Azure only)"
    icons = IconResolver(views_path.parent, icon_dir)
    for name in (VIEWS if which == "all" else [which]):
        title = VIEWS[name] + suffix
        view = (build_detailed(merged, rules, title, drift_styles=styles, subscription_names=sub_names)
                if name == "detailed" else build_summary(merged, rules, title))
        render_svg(render_d2(view, icons), cfg.out_dir / f"{name}.d2", cfg.out_dir / f"{name}.svg")
        print(cfg.out_dir / f"{name}.svg")
    if icons.missing:
        print(f"note: {len(icons.missing)} icon(s) not cached locally, fetched from the internet — "
              "run `infradiag icons` to cache them", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="infradiag", description=__doc__)
    parser.add_argument("--config", type=Path, metavar="PATH",
                        help=f"the project's config (default: ./{CONFIG_NAME})")
    parser.add_argument("--views", type=Path, metavar="PATH",
                        help="views file (default: the config's `views`, else infradiag's generic Azure rules)")
    per_sub = argparse.ArgumentParser(add_help=False)
    per_sub.add_argument("--subscription", action="append", dest="subscriptions", metavar="NAME_OR_ID",
                         help="only this subscription (display name or ID); repeat for more. Default: all of them")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help=f"write a starter {CONFIG_NAME} and views.yaml for a new project")
    init.add_argument("directory", nargs="?", type=Path, default=Path("."),
                      help="where to write them (default: the current directory)")
    init.add_argument("--force", action="store_true", help="overwrite existing files")
    ex = sub.add_parser("extract", parents=[per_sub], help="run what-if and Resource Graph (needs az login)")
    group = ex.add_mutually_exclusive_group()
    group.add_argument("--bicep", action="store_true")
    group.add_argument("--azure", action="store_true")
    sub.add_parser("diff", parents=[per_sub], help="write out/<subscription>/drift-report.md from the cache")
    rd = sub.add_parser("render", parents=[per_sub], help="write out/<subscription>/<view>.d2 and .svg from the cache")
    rd.add_argument("--view", choices=["detailed", "summary", "all"], default="all")
    co = sub.add_parser("cost", parents=[per_sub],
                        help="write out/<subscription>/cost-report.md (list-price estimates) from the cache")
    co.add_argument("--actual", action="store_true",
                    help="also show actual spend from Azure Cost Management (read-only; needs billing read access)")
    co.add_argument("--from", dest="start", type=date.fromisoformat, metavar="YYYY-MM-DD",
                    help="actual-cost period start (default: first day of last month)")
    co.add_argument("--to", dest="end", type=date.fromisoformat, metavar="YYYY-MM-DD",
                    help="actual-cost period end, inclusive (default: last day of last month)")
    co.add_argument("--detail", action="store_true",
                    help="add a per-resource, per-meter breakdown under each resource group (with --actual, what was"
                         " billed per resource and meter too)")
    co.add_argument("--refresh", action="store_true",
                    help="fetch prices (and actual cost) again instead of using the cache")
    sub.add_parser("icons", help="download the views file's icons into <cache>/icons/ (re-run to refresh)")
    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            return _init(args.directory, args.force)
        cfg = load_config(find_config(args.config))
        views_path = args.views or cfg.views_path
        icon_dir = cfg.cache_dir / "icons"
        rules = None if args.command in ("extract", "cost") else load_rules(views_path)
        if args.command == "icons":
            return _icons(rules, views_path, icon_dir)
        chosen, sub_names = _targets(cfg, args)
        if args.command == "cost":
            period = _period(args)
            book = PriceBook(cfg.cache_dir, cfg.cost.currency, refresh=args.refresh)
        rc = 0
        for target in chosen:
            try:
                if args.command == "extract":
                    rc = max(rc, _extract(target, args))
                elif args.command == "diff":
                    rc = max(rc, _diff(target, rules))
                elif args.command == "cost":
                    rc = max(rc, _cost(target, cfg.cost, book, period, args.refresh, args.detail))
                else:
                    rc = max(rc, _render(target, rules, args.view, views_path, icon_dir, sub_names))
            except (CacheMissing, AzError, PriceError) as exc:
                # One subscription's missing cache or az failure shouldn't stop the others.
                print(f"error: {target.label}: {exc}", file=sys.stderr)
                rc = 2
        return rc
    except (ConfigError, SubscriptionError, CacheMissing, AzError, D2NotFound, RuntimeError,
            FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
