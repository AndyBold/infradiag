"""The graph both sources normalise into: nodes keyed by lowercased ARM ID, plus typed edges."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .ids import normalise_id, parse_id

BICEP = "bicep"
AZURE = "azure"

REF = "ref"              # a resource ID found in another resource's properties
ENDPOINT = "endpoint"    # a service hostname/key found in a container app's config
COLLAPSED = "collapsed"  # a node replaced by an edge in a view (DNS links, peerings)

STUB = "stub"                # referenced but outside the scanned RGs
UNSUPPORTED = "unsupported"  # what-if couldn't evaluate this resource


@dataclass
class Node:
    id: str
    type: str
    name: str
    subscription: str
    resource_group: str
    properties: dict = field(default_factory=dict)
    sources: set[str] = field(default_factory=set)
    entry_points: set[str] = field(default_factory=set)
    flags: set[str] = field(default_factory=set)
    managed_by: str | None = None

    @property
    def parent_id(self) -> str | None:
        parts = parse_id(self.id)
        return parts.parent_id if parts else None

    @classmethod
    def from_id(cls, resource_id: str, *, name: str | None = None, **kwargs) -> Node:
        parts = parse_id(resource_id)
        if parts is None:
            raise ValueError(f"not a resource-group-scoped resource ID: {resource_id}")
        return cls(
            id=normalise_id(resource_id),
            type=parts.type,
            name=(name or parts.name).split("/")[-1],
            subscription=parts.subscription,
            resource_group=parts.resource_group,
            **kwargs,
        )


@dataclass(frozen=True, order=True)
class Edge:
    source: str
    target: str
    kind: str
    label: str


@dataclass
class Graph:
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: set[Edge] = field(default_factory=set)

    def add_node(self, node: Node) -> Node:
        existing = self.nodes.get(node.id)
        if existing is None:
            self.nodes[node.id] = node
            return node
        if not existing.properties and node.properties:
            existing.properties = node.properties
        existing.sources |= node.sources
        existing.entry_points |= node.entry_points
        existing.flags |= node.flags
        existing.managed_by = existing.managed_by or node.managed_by
        if existing.sources:
            existing.flags.discard(STUB)
        return existing


def merge_graphs(*graphs: Graph) -> Graph:
    merged = Graph()
    for graph in graphs:
        for n in graph.nodes.values():
            merged.add_node(
                replace(n, sources=set(n.sources), entry_points=set(n.entry_points), flags=set(n.flags))
            )
        merged.edges |= graph.edges
    return merged
