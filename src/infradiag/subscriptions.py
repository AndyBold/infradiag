"""Which subscriptions to draw: every enabled one visible to `az login`, or the ones named with --subscription."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .azcli import run_az
from .cache import read_json, write_json


class SubscriptionError(Exception):
    pass


@dataclass(frozen=True)
class Subscription:
    id: str
    name: str

    @property
    def label(self) -> str:
        return subscription_label(self.id, self.name)


def subscription_label(sub_id: str, name: str | None) -> str:
    """How a subscription is named in diagrams and messages: "<name> (<id>)", or the bare ID if the name is unknown."""
    return f"{name} ({sub_id})" if name and name.lower() != sub_id.lower() else sub_id


def discover(run=None) -> list[Subscription]:
    run = run or run_az
    subs = {
        row["id"].lower(): Subscription(row["id"].lower(), row.get("name") or row["id"].lower())
        for row in run(["account", "list"]) or []
        if row.get("state") == "Enabled"
    }
    return sorted(subs.values(), key=lambda s: (s.name.lower(), s.id))


def select(subs: list[Subscription], wanted: list[str] | None) -> list[Subscription]:
    """The subscriptions named in `wanted` (ID, or display name ignoring case), in the order given; all if none."""
    if not wanted:
        return list(subs)
    chosen: list[Subscription] = []
    for w in wanted:
        hits = matching(subs, w)
        if not hits:
            raise SubscriptionError(f"no subscription matches '{w}'; available: {_labels(subs) or 'none'}")
        if len(hits) > 1:
            raise SubscriptionError(f"'{w}' matches {len(hits)} subscriptions, use the ID: {_labels(hits)}")
        if hits[0] not in chosen:
            chosen.append(hits[0])
    return chosen


def matching(subs: list[Subscription], key: str) -> list[Subscription]:
    """Subscriptions whose ID is `key`, else whose display name is `key` ignoring case."""
    key = key.strip().lower()
    return [s for s in subs if s.id == key] or [s for s in subs if s.name.lower() == key]


def _labels(subs: list[Subscription]) -> str:
    return ", ".join(s.label for s in subs)


def folder_names(subs: list[Subscription]) -> dict[str, str]:
    """out/ subfolder per subscription ID: the name made path-safe, with the ID added when two names clash."""
    safe = {s.id: re.sub(r"[^A-Za-z0-9._-]+", "-", s.name).strip("-.") or s.id for s in subs}
    counts: dict[str, int] = {}
    for name in safe.values():
        counts[name.lower()] = counts.get(name.lower(), 0) + 1
    return {sid: name if counts[name.lower()] == 1 else f"{name}-{sid}" for sid, name in safe.items()}


def write_subscriptions(cache_dir: Path, subs: list[Subscription]) -> None:
    write_json(cache_dir / "subscriptions.json", [{"id": s.id, "name": s.name} for s in subs])


def load_subscriptions(cache_dir: Path) -> list[Subscription]:
    return [Subscription(row["id"], row["name"])
            for row in read_json(cache_dir / "subscriptions.json", "infradiag extract")]
