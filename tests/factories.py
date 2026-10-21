"""Shared builders for tests. Shapes mirror real what-if and Resource Graph output."""

from infradiag.model import Node

SUB = "0a1b2c3d-0000-4000-8000-000000000001"
DEV = "rg-app-dev-weu"
SHARED = "rg-app-shared-weu"


def rid(rg: str, provider_path: str) -> str:
    return f"/subscriptions/{SUB}/resourceGroups/{rg}/providers/{provider_path}"


def node(resource_id: str, sources=("bicep", "azure"), **kwargs) -> Node:
    return Node.from_id(resource_id, sources=set(sources), **kwargs)


def whatif_change(change_type: str, resource_id: str, name: str | None = None,
                  properties: dict | None = None, delta: list | None = None) -> dict:
    after = None
    if change_type not in ("Ignore", "Unsupported", "Delete"):
        after = {"id": resource_id, "name": name or resource_id.rsplit("/", 1)[-1],
                 "properties": properties or {}}
    return {"changeType": change_type, "resourceId": resource_id, "before": None,
            "after": after, "delta": delta}


def whatif_result(*changes: dict) -> dict:
    return {"status": "Succeeded", "error": None, "changes": list(changes)}


def arg_row(resource_id: str, name: str | None = None, properties: dict | None = None,
            managed_by: str = "") -> dict:
    return {"id": resource_id, "name": name or resource_id.rsplit("/", 1)[-1],
            "type": "", "properties": properties or {}, "managedBy": managed_by}
