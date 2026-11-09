"""Split an application view into one diagram per environment.

The environment of each top-level application container comes from the application model
(ViewNode.environment, set from the environment tag or override), never from parsing labels.
Whatever an environment's diagram connects to outside itself is drawn as a compact stub."""
from __future__ import annotations

import re
from dataclasses import dataclass, replace

from .views import View, ViewEdge, ViewNode


@dataclass
class EnvironmentView:
    environment: str
    slug: str
    view: View


def slugify(text: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', text.casefold()).strip('-') or 'environment'


def environments(view: View) -> list[str]:
    """Environment names in first-seen order, one per case-insensitive value."""
    seen: dict[str, str] = {}
    for n in view.nodes:
        if n.environment:
            seen.setdefault(n.environment.casefold(), n.environment)
    return sorted(seen.values(), key=str.casefold)


def slugs(names: list[str]) -> dict[str, str]:
    used: set[str] = set()
    result = {}
    for name in names:
        slug, i = slugify(name), 2
        while slug in used:
            slug, i = f'{slugify(name)}-{i}', i + 1
        used.add(slug)
        result[name] = slug
    return result


def key_roots(view: View) -> dict[str, ViewNode]:
    """Every node key in the view mapped to its top-level ancestor."""
    roots: dict[str, ViewNode] = {}
    def walk(node: ViewNode, root: ViewNode) -> None:
        roots[node.key] = root
        for child in node.children:
            walk(child, root)
    for n in view.nodes:
        walk(n, n)
    return roots


def key_env(view: View) -> dict[str, str]:
    """Environment of every node key ('' for nodes outside any environment container)."""
    return {k: root.environment for k, root in key_roots(view).items()}


def _flat(node: ViewNode) -> ViewNode:
    return replace(node, children=[], annotations=())


def _anchor(key: str, roots: dict[str, ViewNode]) -> ViewNode:
    """The compact node standing for `key` in another environment's diagram. An application container
    collapses to one stub; for the external-dependencies root the touched element itself is the stub;
    identities, principals and scope nodes are already small and are copied as they are."""
    root = roots[key]
    node = root if root.environment or key == root.key else _path(key, root)[1]
    if node.children or node.environment:
        where = f'environment: {node.environment}' if node.environment else 'outside this diagram'
        first = node.sublabel.splitlines()[0] if node.sublabel else ''
        return ViewNode(node.key, node.label, ' | '.join(x for x in (first, where) if x), kind='stub', style='stub',
                        resource_ids=node.resource_ids)
    return _flat(node)


def _path(key: str, root: ViewNode) -> list[ViewNode]:
    def find(node: ViewNode, trail: list[ViewNode]) -> list[ViewNode] | None:
        trail = trail + [node]
        if node.key == key:
            return trail
        for child in node.children:
            if found := find(child, trail):
                return found
        return None
    return find(root, []) or [root]


def split_by_environment(view: View) -> list[EnvironmentView]:
    roots = key_roots(view)
    nodes_by_key = {}
    def index(items):
        for n in items:
            nodes_by_key[n.key] = n
            index(n.children)
    index(view.nodes)
    names = environments(view)
    slug_of = slugs(names)
    result = []
    for env in names:
        mine = [n for n in view.nodes if n.environment.casefold() == env.casefold()]
        inside = {k for k, r in roots.items() if r.environment.casefold() == env.casefold()}
        stubs: dict[str, ViewNode] = {}
        redirect: dict[str, str] = {}
        def outside(key: str) -> str:
            if key not in redirect:
                stub = _anchor(key, roots)
                stubs.setdefault(stub.key, stub)
                redirect[key] = stub.key
            return redirect[key]
        edges: set[ViewEdge] = set()
        covered: dict[str, int] = {}
        for e in view.edges:
            s_in, t_in = e.source in inside, e.target in inside
            if not (s_in or t_in):
                # A subscription / resource-group scope node is outside every environment, but the grant
                # reaches this one when the scope covers any of its elements.
                scope = next((nodes_by_key[k] for k in (e.target, e.source) if k in nodes_by_key
                              and nodes_by_key[k].covers and inside.intersection(nodes_by_key[k].covers)), None)
                if scope is None:
                    continue
                covered[scope.key] = len(inside.intersection(scope.covers))
            edges.add(replace(e, source=e.source if s_in else outside(e.source),
                              target=e.target if t_in else outside(e.target)))
        for key, count in covered.items():
            stubs[key] = replace(_flat(nodes_by_key[key]), covers=(),
                                 annotations=(f'covers {count} element(s) in this environment',))
        sub = View(f'{view.title}: {env}', [*mine, *(stubs[k] for k in sorted(stubs))], sorted(edges),
                   [*view.notes, f'Environment view: {env}. Other environments, external dependencies and principals '
                    'are drawn as stubs; only grants touching this environment are shown.'])
        result.append(EnvironmentView(env, slug_of[env], sub))
    return result
