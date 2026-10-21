"""Compare the Bicep (what-if) graph with the Azure (Resource Graph) graph."""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Target
from .ids import parse_id
from .model import REF, STUB, UNSUPPORTED, Edge, Graph, Node
from .normalise import PropertyChange, WhatIfSignals
from .rules import Rules, matches

# what-if compared these against live state, so the resource exists even if Resource Graph lacks it
EXISTS_PER_WHATIF = {"NoChange", "Modify", "NoEffect", "Deploy"}


@dataclass(frozen=True)
class DriftItem:
    id: str
    type: str
    name: str
    resource_group: str
    note: str = ""


@dataclass
class DriftReport:
    matched: int = 0
    azure_only: list[DriftItem] = field(default_factory=list)
    unverified: list[DriftItem] = field(default_factory=list)
    expected_implicit: list[DriftItem] = field(default_factory=list)
    bicep_only: list[DriftItem] = field(default_factory=list)
    not_indexed: list[DriftItem] = field(default_factory=list)
    out_of_scope: list[DriftItem] = field(default_factory=list)
    modified: dict[str, list[PropertyChange]] = field(default_factory=dict)
    edges_bicep_only: list[Edge] = field(default_factory=list)
    edges_azure_only: list[Edge] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)


def display_name(n: Node) -> str:
    """Child resources share leaf names (a DNS zone's VNet link is often named after the VNet), so prefix the parent."""
    parent = parse_id(n.parent_id) if n.parent_id else None
    return f"{parent.name}/{n.name}" if parent else n.name


def _item(n: Node, note: str = "") -> DriftItem:
    return DriftItem(n.id, n.type, display_name(n), n.resource_group, note)


def _real(graph: Graph) -> dict[str, Node]:
    return {i: n for i, n in graph.nodes.items() if STUB not in n.flags}


def compute_drift(bicep: Graph, azure: Graph, signals: WhatIfSignals, rules: Rules, cfg: Target) -> DriftReport:
    report = DriftReport(failed=dict(signals.failed))
    b, a = _real(bicep), _real(azure)
    both = b.keys() & a.keys()
    report.matched = len(both)

    failed_by_rg: dict[str, list[str]] = {}
    for ep in cfg.entry_points:
        if ep.name in signals.failed:
            for rg in ep.resource_groups:
                failed_by_rg.setdefault(rg, []).append(ep.name)

    for rid in sorted(a.keys() - b.keys()):
        n = a[rid]
        if matches(n.name, rules.out_of_scope_names):
            report.out_of_scope.append(_item(n))
        elif n.managed_by or matches(n.type, rules.implicit_types):
            report.expected_implicit.append(_item(n))
        elif n.resource_group in failed_by_rg:
            report.unverified.append(_item(n, f"{', '.join(failed_by_rg[n.resource_group])} what-if failed"))
        else:
            report.azure_only.append(_item(n, "confirmed by what-if Ignore" if rid in signals.ignored else ""))

    scope = set(cfg.resource_groups)
    for rid in sorted(b.keys() - a.keys()):
        n = b[rid]
        if (scope and n.resource_group not in scope) or matches(n.name, rules.out_of_scope_names):
            report.out_of_scope.append(_item(n))
        elif signals.change_types.get(rid) in EXISTS_PER_WHATIF:
            report.not_indexed.append(_item(n))
        else:
            report.bicep_only.append(_item(n, signals.change_types.get(rid, "")))

    for rid in sorted(signals.modified):
        kept = [c for c in signals.modified[rid] if not matches(c.path, rules.ignore_modify_paths)]
        if kept and rid in b:
            report.modified[rid] = kept

    def comparable(graph: Graph, nodes: dict[str, Node]) -> dict[tuple[str, str], Edge]:
        return {
            (e.source, e.target): e
            for e in sorted(graph.edges)
            if e.kind == REF and e.source in both and e.target in both and UNSUPPORTED not in nodes[e.source].flags
        }

    eb, ea = comparable(bicep, b), comparable(azure, a)
    report.edges_bicep_only = [eb[k] for k in sorted(eb.keys() - ea.keys())]
    report.edges_azure_only = [ea[k] for k in sorted(ea.keys() - eb.keys())]
    return report


def node_styles(report: DriftReport) -> dict[str, str]:
    """Diagram style per node id, so the drawing agrees with the report. Unlisted ids are drawn normally."""
    styles = {i.id: "azure_only" for i in report.azure_only}
    styles |= {i.id: "bicep_only" for i in report.bicep_only}
    styles |= {i.id: "unverified" for i in report.unverified}
    return styles


def _table(items: list[DriftItem]) -> list[str]:
    if not items:
        return ["None.", ""]
    lines = ["| Resource | Type | Resource group | Note |", "|---|---|---|---|"]
    lines += [f"| {i.name} | `{i.type}` | {i.resource_group} | {i.note} |" for i in items]
    return lines + [""]


def _edges(edges: list[Edge], names: dict[str, str]) -> list[str]:
    if not edges:
        return ["None.", ""]
    return [f"- {names.get(e.source, e.source)} → {names.get(e.target, e.target)} (`{e.label}`)" for e in edges] + [""]


def render_azure_only(subscription: str) -> str:
    return "\n".join([
        "# Drift report",
        "",
        f"Subscription: {subscription}",
        "",
        "**Azure only:** the config has no Bicep entry points for this subscription, so there is no what-if"
        " and no drift comparison. The diagrams show what Resource Graph returned.",
        "",
    ])


def render_markdown(report: DriftReport, names: dict[str, str], subscription: str = "") -> str:
    lines = [
        "# Drift report",
        "",
        *([f"Subscription: {subscription}", ""] if subscription else []),
        "_Bicep (what-if) compared with Azure (Resource Graph)._",
        "",
        "## Summary",
        "",
        "| Category | Count |",
        "|---|---|",
        f"| Matched | {report.matched} |",
        f"| In Azure, not in Bicep | {len(report.azure_only)} |",
        f"| Unverified (entry point failed) | {len(report.unverified)} |",
        f"| Expected implicit | {len(report.expected_implicit)} |",
        f"| In Bicep, not in Azure | {len(report.bicep_only)} |",
        f"| Not indexed by Resource Graph | {len(report.not_indexed)} |",
        f"| Outside query scope | {len(report.out_of_scope)} |",
        f"| Configuration drift | {len(report.modified)} |",
        f"| Edge differences | {len(report.edges_bicep_only) + len(report.edges_azure_only)} |",
        "",
    ]
    if report.failed:
        lines += ["## Failed entry points", "",
                  "> Bicep data for these entry points is missing; resources in their resource groups are"
                  " listed as Unverified instead of drift.", ""]
        lines += [f"- `{name}`: {error}" for name, error in sorted(report.failed.items())] + [""]
    else:
        lines += ["## Failed entry points", "", "None.", ""]
    lines += ["## In Azure, not in Bicep", ""] + _table(report.azure_only)
    lines += ["## Unverified", ""] + _table(report.unverified)
    lines += ["## Expected implicit", "", "_Created by Azure for another resource; not drift._", ""]
    lines += _table(report.expected_implicit)
    lines += ["## In Bicep, not in Azure", "", "_The next deployment would create these._", ""]
    lines += _table(report.bicep_only)
    lines += ["## Not indexed by Resource Graph", "",
              "_what-if says these exist, but Resource Graph's `resources` table doesn't list their type._", ""]
    lines += _table(report.not_indexed)
    lines += ["## Outside query scope", ""] + _table(report.out_of_scope)
    lines += ["## Configuration drift", "",
              "_Properties the next deployment would change (what-if `Modify`). Properties Azure holds but the"
              " template doesn't set are only counted: what-if lists them as `Delete`, but they are mostly"
              " read-only or defaulted._", ""]
    if not report.modified:
        lines += ["None.", ""]
    for rid, changes in report.modified.items():
        lines += [f"### {names.get(rid, rid)}", ""]
        changed = [c for c in changes if c.change != "Delete"]
        unset = len(changes) - len(changed)
        lines += [f"- `{c.path}` — {c.change}" for c in changed]
        if unset:
            lines.append(f"- {unset} properties set in Azure but not in the template")
        lines.append("")
    lines += ["## Edge differences", "", "### Only in Bicep", ""] + _edges(report.edges_bicep_only, names)
    lines += ["### Only in Azure", ""] + _edges(report.edges_azure_only, names)
    return "\n".join(lines)
