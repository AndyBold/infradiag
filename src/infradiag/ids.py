"""ARM resource ID helpers. IDs are always compared lowercased."""

from __future__ import annotations

import re
from dataclasses import dataclass

_ID_RE = re.compile(
    r"/subscriptions/[0-9a-f-]{36}/resourcegroups/[^/\s\"'?#]+/providers/[^\s\"'?#,;]+",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class IdParts:
    subscription: str
    resource_group: str
    type: str
    name: str
    parent_id: str | None


def normalise_id(resource_id: str) -> str:
    return resource_id.strip().rstrip("/").lower()


def parse_id(resource_id: str) -> IdParts | None:
    rid = normalise_id(resource_id)
    parts = rid.split("/")
    if (
        len(parts) < 9
        or parts[0] != ""
        or parts[1] != "subscriptions"
        or parts[3] != "resourcegroups"
        or parts[5] != "providers"
    ):
        return None
    # Extension resources (diagnostic settings, scoped role assignments) nest a second
    # /providers/ segment; the last one names the resource's own type.
    last = max(i for i, p in enumerate(parts) if p == "providers")
    rest = parts[last + 2 :]
    if not rest or len(rest) % 2:
        return None
    rtype = "/".join([parts[last + 1], *rest[0::2]])
    if len(rest) > 2:
        parent = "/".join(parts[:-2])
    elif last > 5:
        parent = "/".join(parts[:last])
    else:
        parent = None
    return IdParts(parts[2], parts[4], rtype, rest[-1], parent)


def find_ids(text: str) -> list[str]:
    found = []
    for match in _ID_RE.finditer(text):
        rid = normalise_id(match.group(0))
        while parse_id(rid) is None and rid.count("/") > 8:
            rid = rid.rsplit("/", 1)[0]
        if parse_id(rid) is not None:
            found.append(rid)
    return found


def top_level_id(resource_id: str) -> str:
    return "/".join(normalise_id(resource_id).split("/")[:9])


def resolve(resource_id: str, known: set[str]) -> str:
    rid = normalise_id(resource_id)
    candidate: str | None = rid
    while candidate:
        if candidate in known:
            return candidate
        parts = parse_id(candidate)
        candidate = parts.parent_id if parts else None
    return top_level_id(rid)
