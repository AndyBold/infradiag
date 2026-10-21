"""Derive edges: ARM ID references (pass 1) and service endpoint references (pass 2)."""

from __future__ import annotations

from collections.abc import Iterator
from fnmatch import fnmatchcase
from urllib.parse import urlparse

from .ids import find_ids, resolve
from .model import ENDPOINT, REF, STUB, Edge, Graph, Node
from .normalise import INLINE_CHILDREN, UNEVALUATED

# Properties Azure fills in at runtime. They point back at dependants (subnet -> NICs, SQL -> PEs)
# or are computed, so they never appear in what-if output and would only add reverse-direction noise.
RUNTIME_REF_PATHS = (
    "properties.ipConfigurations[].id",
    "properties.privateEndpoints[].id",
    "properties.serviceAssociationLinks*",
    "properties.resourceNavigationLinks*",
    "properties.networkInterfaces[].id",
    "properties.privateEndpointConnections*",
    "properties.subnets[].id",
    "properties.remoteGateways*",
    "properties.privateLinkScopedResources*",
    "properties.natGateway.id",                 # public IP -> the NAT gateway using it
    "properties.ipConfiguration.id",            # public IP -> the gateway using it
    "properties.remoteVirtualNetworkPeerings*",
)

# Where each service publishes the hostname/key that consumers put in their config (under properties).
ENDPOINT_PROPERTIES: dict[str, tuple[str, ...]] = {
    "microsoft.keyvault/vaults": ("vaultUri",),
    "microsoft.appconfiguration/configurationstores": ("endpoint",),
    "microsoft.sql/servers": ("fullyQualifiedDomainName",),
    "microsoft.storage/storageaccounts": (
        "primaryEndpoints.blob", "primaryEndpoints.queue", "primaryEndpoints.table", "primaryEndpoints.file",
    ),
    "microsoft.eventhub/namespaces": ("serviceBusEndpoint",),
    "microsoft.servicebus/namespaces": ("serviceBusEndpoint",),
    "microsoft.cognitiveservices/accounts": ("endpoint",),
    "microsoft.containerregistry/registries": ("loginServer",),
    "microsoft.insights/components": ("InstrumentationKey",),
}

CONSUMER_TYPES = ("microsoft.app/containerapps", "microsoft.app/jobs")


def get_path(obj, dotted: str):
    current = obj
    for part in dotted.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _walk_strings(value, path: str, skip_top: tuple[str, ...] = ()) -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        for key, child in value.items():
            if path == "properties" and key in skip_top:
                continue
            yield from _walk_strings(child, f"{path}.{key}", skip_top)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_strings(child, f"{path}[]", skip_top)
    elif isinstance(value, str) and not UNEVALUATED.match(value):
        yield path, value


def add_reference_edges(graph: Graph) -> None:
    known = set(graph.nodes)
    for node in list(graph.nodes.values()):
        for path, text in _walk_strings(node.properties, "properties", INLINE_CHILDREN.get(node.type, ())):
            if any(fnmatchcase(path.lower(), pattern.lower()) for pattern in RUNTIME_REF_PATHS):
                continue
            for found in find_ids(text):
                target = resolve(found, known)
                if target == node.id or target.startswith(node.id + "/") or node.id.startswith(target + "/"):
                    continue
                if target not in graph.nodes:
                    graph.add_node(Node.from_id(target, flags={STUB}))
                    known.add(target)
                graph.edges.add(Edge(node.id, target, REF, path))


def _token(value: str) -> str | None:
    value = value.strip().lower()
    if "://" in value:
        return urlparse(value).hostname
    return value.split(":")[0].rstrip("/") or None


def add_endpoint_edges(graph: Graph) -> None:
    tokens: dict[str, str] = {}
    for node in graph.nodes.values():
        for prop in ENDPOINT_PROPERTIES.get(node.type, ()):
            value = get_path(node.properties, prop)
            if isinstance(value, str) and (token := _token(value)):
                tokens[token] = node.id
    for node in graph.nodes.values():
        if node.type not in CONSUMER_TYPES:
            continue
        for _, text in _walk_strings(node.properties, "properties"):
            lowered = text.lower()
            for token, target in tokens.items():
                if target != node.id and token in lowered:
                    graph.edges.add(Edge(node.id, target, ENDPOINT, "endpoint"))
