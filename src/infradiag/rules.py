"""Load the views file (views.yaml): filtering, grouping, icon and drift rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

import yaml


def matches(value: str, patterns) -> bool:
    value = value.lower()
    return any(fnmatchcase(value, p.lower()) for p in patterns)


@dataclass(frozen=True)
class CollapseRule:
    type: str
    target_path: str
    label: str


@dataclass(frozen=True)
class NameCollapse:
    pattern: str
    label: str


@dataclass(frozen=True)
class Rules:
    hide_types: tuple[str, ...] = ()
    hide_managed: bool = False
    hide_edges_to_types: tuple[str, ...] = ()
    collapse: tuple[CollapseRule, ...] = ()
    icons: dict[str, str] = field(default_factory=dict)
    implicit_types: tuple[str, ...] = ()
    ignore_modify_paths: tuple[str, ...] = ()
    out_of_scope_names: tuple[str, ...] = ()
    summary_groups: tuple[tuple[str, tuple[str, ...]], ...] = ()
    summary_name_overrides: tuple[tuple[str, str], ...] = ()
    summary_hide_types: tuple[str, ...] = ()
    summary_nest_into_types: tuple[str, ...] = ()
    summary_collapse_names: tuple[NameCollapse, ...] = ()


def _lower(items) -> tuple[str, ...]:
    return tuple(str(i).lower() for i in items or [])


def load_rules(path: Path) -> Rules:
    data = yaml.safe_load(Path(path).read_text()) or {}
    summary = data.get("summary") or {}
    return Rules(
        hide_types=_lower(data.get("hide_types")),
        hide_managed=bool(data.get("hide_managed", False)),
        hide_edges_to_types=_lower(data.get("hide_edges_to_types")),
        collapse=tuple(CollapseRule(c["type"].lower(), c["target_path"], c["label"])
                       for c in data.get("collapse") or []),
        icons={k.lower(): v for k, v in (data.get("icons") or {}).items()},
        implicit_types=_lower(data.get("implicit_types")),
        ignore_modify_paths=tuple(data.get("ignore_modify_paths") or []),
        out_of_scope_names=tuple(data.get("out_of_scope_names") or []),
        summary_groups=tuple((name, _lower(types)) for name, types in (summary.get("groups") or {}).items()),
        summary_name_overrides=tuple((pattern, group) for pattern, group in (summary.get("name_overrides") or {}).items()),
        summary_hide_types=_lower(summary.get("hide_types")),
        summary_nest_into_types=_lower(summary.get("nest_into_types")),
        summary_collapse_names=tuple(NameCollapse(c["pattern"], c["label"])
                                     for c in summary.get("collapse_names") or []),
    )
