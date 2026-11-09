"""Raw extract results live as JSON under the cache dir so everything after extract runs offline."""

from __future__ import annotations

import json
from pathlib import Path


class CacheMissing(Exception):
    pass


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1))


def read_json(path: Path, hint: str):
    if not path.exists():
        raise CacheMissing(f"{path} not found — run `{hint}`")
    try:
        return json.loads(path.read_text())
    except ValueError as exc:  # an interrupted extract can leave a truncated file
        raise CacheMissing(f"{path} is not valid JSON ({exc}) — run `{hint}`") from exc
