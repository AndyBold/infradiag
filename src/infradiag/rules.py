"""Load the views file (views.yaml): filtering, grouping, icon and drift rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

import yaml

from .config import ConfigError


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
    applications: dict = field(default_factory=dict)
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
        applications=_applications(data.get("applications", {})),
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


def _applications(raw) -> dict:
    allowed = {'application_tag', 'environment_tag', 'annotations', 'overrides'}
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise ConfigError('applications: expected mapping with application_tag, environment_tag, annotations, overrides')
    for key in ('application_tag', 'environment_tag'):
        if key in raw and (not isinstance(raw[key], str) or not raw[key].strip()):
            raise ConfigError(f'applications.{key}: expected nonempty string')
    if not isinstance(raw.get('annotations', []), list) or any(not isinstance(x, str) for x in raw.get('annotations', [])):
        raise ConfigError('applications.annotations: expected list of tag names')
    if not isinstance(raw.get('overrides', []), list):
        raise ConfigError('applications.overrides: expected list')
    for i, rule in enumerate(raw.get('overrides', [])):
        where = f'applications.overrides[{i}]'
        if not isinstance(rule, dict) or set(rule) - {'match', 'application', 'environment', 'group', 'shared', 'component', 'role'}:
            raise ConfigError(f'{where}: unknown key or expected mapping')
        selector = rule.get('match')
        if not isinstance(selector, dict) or not selector or set(selector) - {'name', 'type', 'subscription'}:
            raise ConfigError(f'{where}.match: expected name, type or subscription selectors')
        if any(not isinstance(v, str) or not v.strip() for v in selector.values()):
            raise ConfigError(f'{where}.match: selectors must be nonempty strings')
        if 'shared' in rule and not isinstance(rule['shared'], bool):
            raise ConfigError(f'{where}.shared: expected boolean')
        if any(not isinstance(v, str) or not v.strip() for k, v in rule.items() if k not in ('match', 'shared')):
            raise ConfigError(f'{where}: override values must be nonempty strings')
    return raw
