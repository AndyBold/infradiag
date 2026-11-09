"""Turn the merged graph into renderer-agnostic View trees (detailed and summary)."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from fnmatch import fnmatchcase

from .edges import get_path
from .ids import resolve
from .model import AZURE, BICEP, COLLAPSED, ENDPOINT, REF, STUB, Edge, Graph, Node
from .rules import Rules, matches
from .subscriptions import subscription_label

SUBNET = "microsoft.network/virtualnetworks/subnets"


@dataclass
class ViewNode:
    key: str
    label: str
    sublabel: str = ""
    icon: str | None = None
    style: str = "normal"
    children: list[ViewNode] = field(default_factory=list)
    resource_ids: tuple[str, ...] = ()
    kind: str = "resource"
    annotations: tuple[str, ...] = ()
    # Application model: the environment a top-level application container belongs to ("" for anything else).
    environment: str = ""
    # Overlay scope nodes (subscription / resource group): keys of the view elements the grants beneath them cover.
    covers: tuple[str, ...] = ()
    # Hover text for the node (d2 draws it as a tooltip).
    tooltip: str = ""


@dataclass(frozen=True, order=True)
class ViewEdge:
    source: str
    target: str
    label: str = ""
    style: str = "solid"
    kind: str = "dependency"
    # Shown on hover rather than drawn on the label (the SVG gets it as a <title>; see render_d2.add_edge_tooltips).
    tooltip: str = ""
    # The sources (bicep / azure) that evidence an overlay edge, and the per-edge drift marker mark_drift decided on
    # for the diagram this edge is drawn in ("azure only"; empty when both sources agree or the note covers it).
    sources: tuple[str, ...] = ()
    drift: str = ""


def edge_label(edge: ViewEdge) -> str:
    """The label as drawn: the drift marker joins the detail line of a grant ("azure only | subscription scope"),
    or follows a single-line label in brackets."""
    if not edge.drift:
        return edge.label
    lines = edge.label.split("\n")
    if len(lines) > 1:
        lines[1] = f"{edge.drift} | {lines[1]}"
    else:
        lines[0] += f" ({edge.drift})"
    return "\n".join(lines)


DRIFT_NOTE = "Drift note: "
DRIFT_MIN_GRANTS = 5  # a diagram with fewer grant edges marks every one-source edge, as there is no pattern to summarise
DRIFT_MAJORITY = 0.5  # one source evidencing more than this share of a diagram's grant edges is its dominant pattern
DRIFT_MEANING = {"azure": "Azure-only (not declared in Bicep)", "bicep": "Bicep-only (declared in Bicep, not found in Azure)"}


def mark_drift(view: View) -> View:
    """Decide, for this diagram, which overlay edges carry a one-source drift marker (in place; safe to repeat).

    When more than DRIFT_MAJORITY of a diagram's grant edges (at least DRIFT_MIN_GRANTS of them) are evidenced by the same
    single source, that is the diagram's normal state (most Azure role assignments have no Bicep declaration), so those
    edges lose the marker and one note says so; only the other one-source edges keep theirs and stand out. Attachment and
    configured-use edges, and every edge below the threshold, keep their marker. Sources stay in each edge's hover."""
    grants = [e for e in view.edges if e.kind == "grant"]
    alone = Counter(next(iter(set(e.sources))) for e in grants if len(set(e.sources)) == 1)
    dominant = ""
    if len(grants) >= DRIFT_MIN_GRANTS and alone:
        source, count = alone.most_common(1)[0]
        if count / len(grants) > DRIFT_MAJORITY:
            dominant = source
    edges = []
    for e in view.edges:
        only = next(iter(set(e.sources))) if len(set(e.sources)) == 1 else ""
        edges.append(replace(e, drift=f"{only} only" if only and not (e.kind == "grant" and only == dominant) else ""))
    view.edges = sorted(edges)
    view.notes = [n for n in view.notes if not n.startswith(DRIFT_NOTE)]
    if dominant:
        view.notes.append(f"{DRIFT_NOTE}{alone[dominant]} of {len(grants)} grant edges are {DRIFT_MEANING[dominant]}, so they are "
                          "not marked one by one (hover an edge for its sources). Any edge marked with a source is "
                          "evidenced by that source alone, against the pattern.")
    return view


@dataclass
class View:
    title: str
    nodes: list[ViewNode]
    edges: list[ViewEdge]
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EnvDecision:
    """How a resource's environment was decided. `source` is one of tag, override, name pattern, resource group
    pattern, or unspecified; `detail` names the tag, override or rule (and what it matched) that decided it."""
    environment: str
    source: str
    detail: str = ""

    @property
    def inferred(self) -> bool:
        return self.source in ("name pattern", "resource group pattern")

    def text(self) -> str:
        return f"{self.environment} ({self.source}{': ' + self.detail if self.detail else ''})"


@dataclass
class ViewProjection:
    view: View
    resources: dict[str, str]
    # Application views: the environment decision for every resource drawn (resource ID -> decision).
    env_decisions: dict[str, EnvDecision] = field(default_factory=dict)


def project(view: View, graph: Graph) -> ViewProjection:
    resources = {}
    def walk(nodes):
        for n in nodes:
            resources.update((rid, n.key) for rid in n.resource_ids)
            walk(n.children)
    walk(view.nodes)
    # Map hidden child resources to their closest visible ancestor.
    for rid, n in graph.nodes.items():
        parent = n.parent_id
        while parent and parent not in resources:
            node = graph.nodes.get(parent)
            parent = node.parent_id if node else None
        if rid not in resources and parent in resources:
            resources[rid] = resources[parent]
    return ViewProjection(view, resources)


def short_type(resource_type: str) -> str:
    return resource_type.rsplit("/", 1)[-1]


def filter_graph(graph: Graph, rules: Rules) -> Graph:
    out = Graph()
    collapsing = {}
    for rid, n in graph.nodes.items():
        if (matches(n.type, rules.hide_types) or (rules.hide_managed and n.managed_by)
                or matches(n.name, rules.out_of_scope_names)):
            continue
        rule = next((r for r in rules.collapse if r.type == n.type), None)
        if rule:
            collapsing[rid] = rule
            continue
        out.nodes[rid] = n
    known = set(out.nodes)
    for rid, rule in sorted(collapsing.items()):
        n = graph.nodes[rid]
        target, parent = get_path(n.properties, rule.target_path), n.parent_id
        if not isinstance(target, str) or parent not in known:
            continue
        resolved = resolve(target, known)
        if resolved in known and resolved != parent:
            a, b = sorted((parent, resolved))
            out.edges.add(Edge(a, b, COLLAPSED, rule.label))
    out.edges |= {
        e for e in graph.edges
        if e.source in known and e.target in known
        and not matches(out.nodes[e.target].type, rules.hide_edges_to_types)
    }
    return out


def _style(n: Node) -> str:
    if STUB in n.flags:
        return "stub"
    if n.sources == {BICEP}:
        return "bicep_only"
    if n.sources == {AZURE}:
        return "azure_only"
    return "normal"


def _edge_style(e: Edge) -> str:
    return "dashed" if e.kind == ENDPOINT else "solid"


def _break_cycles(placement: dict[str, str | None]) -> None:
    for start in list(placement):
        seen, current = set(), start
        while current is not None:
            if current in seen:
                placement[start] = None
                break
            seen.add(current)
            current = placement.get(current)


def build_detailed(graph: Graph, rules: Rules, title: str = "Detailed topology",
                   drift_styles: dict[str, str] | None = None,
                   subscription_names: dict[str, str] | None = None) -> View:
    g = filter_graph(graph, rules)
    sub_names = subscription_names or {}

    def style(n: Node) -> str:
        if drift_styles is None or STUB in n.flags:
            return _style(n)
        return drift_styles.get(n.id, "normal")

    ids = sorted(g.nodes)
    keys = {rid: f"n{i}" for i, rid in enumerate(ids)}
    vnodes = {
        rid: ViewNode(keys[rid], g.nodes[rid].name, short_type(g.nodes[rid].type),
                      rules.icons.get(g.nodes[rid].type), style(g.nodes[rid]), resource_ids=(rid,))
        for rid in ids
    }

    subnet_of: dict[str, str] = {}
    for e in sorted(g.edges):
        if (e.kind != COLLAPSED and g.nodes[e.target].type == SUBNET
                and g.nodes[e.source].type != SUBNET and e.source not in subnet_of):
            subnet_of[e.source] = e.target

    placement: dict[str, str | None] = {}
    for rid in ids:
        n = g.nodes[rid]
        if n.type != SUBNET and rid in subnet_of:
            placement[rid] = subnet_of[rid]
        elif n.parent_id in g.nodes:
            placement[rid] = n.parent_id
        else:
            placement[rid] = None
    _break_cycles(placement)

    roots: list[ViewNode] = []
    containers: dict[str, ViewNode] = {}

    def container(cid: str, label: str, sublabel: str, icon: str | None, parent: ViewNode | None) -> ViewNode:
        if cid not in containers:
            containers[cid] = ViewNode(f"c{len(containers)}", label, sublabel, icon, kind="deployment")
            (parent.children if parent else roots).append(containers[cid])
        return containers[cid]

    for rid in ids:
        parent = placement[rid]
        if parent is not None:
            vnodes[parent].children.append(vnodes[rid])
            continue
        n = g.nodes[rid]
        if STUB in n.flags:
            holder = container("external", "External / out of scope", "", None, None)
        else:
            sub = container(f"sub:{n.subscription}", subscription_label(n.subscription, sub_names.get(n.subscription)),
                            "subscription", rules.icons.get("_subscription"), None)
            holder = container(f"rg:{n.subscription}/{n.resource_group}", n.resource_group, "resource group",
                               rules.icons.get("_resourcegroup"), sub)
        holder.children.append(vnodes[rid])

    edges = {
        ViewEdge(keys[e.source], keys[e.target], e.label if e.kind == COLLAPSED else "", _edge_style(e))
        for e in g.edges
        if placement.get(e.source) != e.target and e.source != e.target
    }
    return View(title, roots, sorted(edges))


PRIVATE_ENDPOINT = "microsoft.network/privateendpoints"


def _group_for(n: Node, rules: Rules) -> str:
    for pattern, group in rules.summary_name_overrides:
        if fnmatchcase(n.name.lower(), pattern.lower()):
            return group
    for group, patterns in rules.summary_groups:
        if matches(n.type, patterns):
            return group
    return "Other"


def build_summary(graph: Graph, rules: Rules, title: str = "Architecture overview") -> View:
    g = filter_graph(graph, rules)
    private_targets = {
        e.target for e in g.edges
        if e.kind == REF and g.nodes[e.source].type == PRIVATE_ENDPOINT and "privatelinkserviceid" in e.label.lower()
    }
    keep = {
        rid: n for rid, n in g.nodes.items()
        if STUB not in n.flags and not matches(n.type, rules.summary_hide_types) and n.parent_id not in g.nodes
    }

    rep: dict[str, str] = {}
    members: dict[str, list[str]] = {}
    for rid in sorted(keep):
        r = rid
        for nc in rules.summary_collapse_names:
            if fnmatchcase(keep[rid].name.lower(), nc.pattern.lower()):
                r = f"collapse:{nc.label}"
                break
        rep[rid] = r
        members.setdefault(r, []).append(rid)

    keys = {r: f"n{i}" for i, r in enumerate(sorted(members))}
    vnodes: dict[str, ViewNode] = {}
    for r, ids in members.items():
        first = keep[ids[0]]
        label = f"{r.split(':', 1)[1]} ×{len(ids)}" if r.startswith("collapse:") else first.name
        vnodes[r] = ViewNode(keys[r], label, short_type(first.type), rules.icons.get(first.type), resource_ids=tuple(ids))

    nest: dict[str, str] = {}
    for e in sorted(g.edges):
        if e.source in keep and e.target in keep and matches(keep[e.target].type, rules.summary_nest_into_types):
            s, t = rep[e.source], rep[e.target]
            if s != t:
                nest.setdefault(s, t)

    grouped: dict[str, list[ViewNode]] = {}
    for r in sorted(members):
        if r in nest:
            vnodes[nest[r]].children.append(vnodes[r])
        else:
            grouped.setdefault(_group_for(keep[members[r][0]], rules), []).append(vnodes[r])

    order = [name for name, _ in rules.summary_groups] + ["Other"]
    roots = [
        ViewNode(f"g{i}", name, children=grouped[name], kind="group")
        for i, name in enumerate(n for n in order if n in grouped)
    ]

    styles: dict[tuple[str, str], tuple[str, str]] = {}
    for e in sorted(g.edges):
        if e.source not in keep or e.target not in keep:
            continue
        # Edges from nodes drawn inside a container (apps in their environment) start at the container,
        # so N apps using the same service give one arrow, not N.
        s, t = nest.get(rep[e.source], rep[e.source]), rep[e.target]
        if s == t:
            continue
        style = "private" if e.target in private_targets else _edge_style(e)
        label = e.label if e.kind == COLLAPSED else ""
        if (s, t) not in styles or style == "private":
            styles[(s, t)] = (label, style)
    edges = sorted(ViewEdge(keys[s], keys[t], label, style) for (s, t), (label, style) in styles.items())
    return View(title, roots, edges)
