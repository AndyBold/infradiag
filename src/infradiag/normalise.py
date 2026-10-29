"""Turn raw what-if and Resource Graph JSON into Graphs with the same shape."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass, field

from .ids import normalise_id, parse_id
from .model import AZURE, BICEP, UNSUPPORTED, Graph, Node

# Child resources Azure returns inline in their parent. Expanded into nodes on both sides so
# what-if (which may list them separately, and omits inline ids) lines up with Resource Graph.
INLINE_CHILDREN: dict[str, tuple[str, ...]] = {
    "microsoft.network/virtualnetworks": ("subnets", "virtualNetworkPeerings"),
}

NODE_CHANGE_TYPES = {"Create", "Modify", "NoChange", "NoEffect", "Deploy"}

# what-if leaves values it can only know at deploy time as template expressions, e.g. "[reference(...)]".
UNEVALUATED = re.compile(r"^\[.*\]$", re.DOTALL)


@dataclass(frozen=True)
class PropertyChange:
    path: str
    change: str


@dataclass
class WhatIfSignals:
    ignored: dict[str, set[str]] = field(default_factory=dict)
    modified: dict[str, list[PropertyChange]] = field(default_factory=dict)
    change_types: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)


def expand_inline_children(graph: Graph, node: Node) -> None:
    for prop in INLINE_CHILDREN.get(node.type, ()):
        for child in node.properties.get(prop) or []:
            name = child.get("name")
            if not name:
                continue
            child_id = child.get("id") or f"{node.id}/{prop}/{name}"
            graph.add_node(Node.from_id(
                child_id,
                name=name,
                properties=child.get("properties") or {},
                sources=set(node.sources),
                metadata={source: deepcopy(child) for source in node.sources},
                entry_points=set(node.entry_points),
            ))


def flatten_delta(items: list | None, prefix: str = "") -> list[PropertyChange]:
    changes: list[PropertyChange] = []
    for item in items or []:
        path = f"{prefix}.{item['path']}" if prefix else item["path"]
        if item.get("children"):
            changes.extend(flatten_delta(item["children"], path))
        elif item["propertyChangeType"] == "NoEffect":
            continue
        elif isinstance(item.get("after"), str) and UNEVALUATED.match(item["after"]):
            continue
        else:
            changes.append(PropertyChange(path, item["propertyChangeType"]))
    return changes


def _error_text(raw: dict) -> str | None:
    error = raw.get("error")
    if not error and raw.get("status") != "Failed":
        return None
    if isinstance(error, dict):
        return error.get("message") or json.dumps(error)
    return str(error or "what-if status Failed")


def graph_from_whatif(results: dict[str, dict]) -> tuple[Graph, WhatIfSignals]:
    graph, signals = Graph(), WhatIfSignals()
    for entry, raw in sorted(results.items()):
        if (error := _error_text(raw)) is not None:
            signals.failed[entry] = error
            continue
        for change in raw.get("changes") or []:
            resource_id, change_type = change.get("resourceId", ""), change.get("changeType")
            if parse_id(resource_id) is None:
                continue
            nid = normalise_id(resource_id)
            if change_type == "Ignore":
                signals.ignored.setdefault(nid, set()).add(entry)
                continue
            if change_type == "Unsupported":
                graph.add_node(Node.from_id(resource_id, sources={BICEP}, entry_points={entry},
                                            flags={UNSUPPORTED}))
                signals.change_types.setdefault(nid, change_type)
                continue
            if change_type not in NODE_CHANGE_TYPES:
                continue
            after = change.get("after") or {}
            node = graph.add_node(Node.from_id(
                resource_id,
                name=after.get("name"),
                properties=after.get("properties") or {},
                sources={BICEP},
                tags=deepcopy(after.get("tags") or {}),
                identity=deepcopy(after.get("identity") or {}),
                metadata={BICEP: deepcopy(after)},
                entry_points={entry},
            ))
            expand_inline_children(graph, node)
            signals.change_types.setdefault(nid, change_type)
            if change_type == "Modify":
                signals.modified.setdefault(nid, []).extend(flatten_delta(change.get("delta")))
    return graph, signals


def graph_from_arg(rows: list[dict]) -> Graph:
    graph = Graph()
    for row in rows:
        resource_id = row.get("id", "")
        if parse_id(resource_id) is None:
            continue
        node = graph.add_node(Node.from_id(
            resource_id,
            name=row.get("name"),
            properties=row.get("properties") or {},
            sources={AZURE},
            tags=deepcopy(row.get("tags") or {}),
            identity=deepcopy(row.get("identity") or {}),
            metadata={AZURE: deepcopy(row)},
            managed_by=row.get("managedBy") or None,
        ))
        expand_inline_children(graph, node)
    return graph
