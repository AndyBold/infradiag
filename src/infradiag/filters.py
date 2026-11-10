"""Filters that hide whole classes of identity-overlay grants, so a diagram can draw less.

A filter works on the grant evidence before the overlay and the access table are built, so the diagram and its table
always agree. Filters combine with AND. Drift is judged on every grant, before any other filter removes one."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace

from .access import scope_kind
from .ids import normalise_id
from .model import Graph
from .security import Grant, SecurityEvidence, resolve_principals, role_key

HIDE_SCOPES = ('subscription', 'management-group', 'resource-group', 'tenant')
ROLE_PLANES = ('data', 'control')
PRINCIPAL_KINDS = ('managed-identity', 'user', 'group', 'service-principal', 'device', 'deleted',
                  'unresolved')
_SCOPE_TOKEN = {'subscription': 'sub', 'management-group': 'mg', 'resource-group': 'rg', 'tenant': 'tenant'}
_KIND_TOKEN = {'managed-identity': 'mi', 'service-principal': 'sp', 'unresolved': 'unres'}
_PRINCIPAL_TYPES = {'user': 'user', 'group': 'group', 'foreigngroup': 'group', 'serviceprincipal': 'service-principal',
                    'device': 'device'}


@dataclass(frozen=True)
class GrantFilter:
    hide_scopes: frozenset[str] = frozenset()   # scope kinds whose grants are dropped
    role_plane: str = ''                        # keep only 'data' or only 'control' roles
    principal_kinds: frozenset[str] = frozenset()  # keep only these kinds of principal
    roles: frozenset[str] = frozenset()         # keep only these role names (case-folded)
    drift_only: bool = False                    # keep only grants evidenced by one source

    @property
    def active(self) -> bool:
        return bool(self.hide_scopes or self.role_plane or self.principal_kinds or self.roles or self.drift_only)

    def suffix(self) -> str:
        """A short, deterministic output-stem suffix, so filtered and unfiltered renders never share file names."""
        parts = [f'no-{_SCOPE_TOKEN[s]}' for s in HIDE_SCOPES if s in self.hide_scopes]
        if self.role_plane:
            parts.append(self.role_plane)
        if self.principal_kinds:
            parts.append('p-' + '-'.join(_KIND_TOKEN.get(k, k) for k in PRINCIPAL_KINDS if k in self.principal_kinds))
        if self.roles:
            parts.append('roles-' + hashlib.sha1('\n'.join(sorted(self.roles)).encode()).hexdigest()[:6])
        if self.drift_only:
            parts.append('drift')
        return ''.join(f'-{p}' for p in parts)

    def phrases(self, short: bool = False) -> list[str]:
        out = []
        if self.hide_scopes:
            out.append('no ' + '/'.join(s for s in HIDE_SCOPES if s in self.hide_scopes) + ' scope')
        if self.role_plane:
            out.append(f'{self.role_plane}-plane roles')
        if self.principal_kinds:
            out.append('principals: ' + ', '.join(k for k in PRINCIPAL_KINDS if k in self.principal_kinds))
        if self.roles:
            out.append('roles: ' + ', '.join(sorted(self.roles)) if not short or len(self.roles) <= 2
                       else f'{len(self.roles)} roles')
        if self.drift_only:
            out.append('drift only')
        return out

    def title_suffix(self) -> str:
        return f' [filtered: {"; ".join(self.phrases(short=True))}]' if self.active else ''


@dataclass
class FilterReport:
    """What a filter did, as lines for the diagram's notes, the index page and the access table header."""
    lines: list[str]

    @property
    def text(self) -> str:
        return ' '.join(self.lines)


def principal_kind(grant: Grant, principals: dict[str, tuple[str, str]]) -> str:
    resolved = principals.get(grant.principal_id)
    if resolved:
        return resolved[1].split(' (')[0].replace(' ', '-')
    return _PRINCIPAL_TYPES.get(grant.principal_type.casefold(), 'unresolved')


def _unnamed(g: Grant) -> bool:
    """A role the lookup could not name carries its definition ID (or the bare GUID) as its name."""
    return not g.role_name or g.role_name.startswith('/') or g.role_name == role_key(g.role_id)


def _enrich(grants: list[Grant]) -> list[Grant]:
    """Fill a grant's role name and plane from another grant of the same role definition (declared Bicep grants
    usually carry only the role GUID)."""
    named: dict[str, str] = {}
    planes: dict[str, str] = {}
    for g in grants:
        key = role_key(g.role_id)
        if not _unnamed(g):
            named.setdefault(key, g.role_name)
        if g.role_kind:
            planes.setdefault(key, g.role_kind)
    return [replace(g, role_name=named.get(role_key(g.role_id), g.role_name) if _unnamed(g) else g.role_name,
                    role_kind=g.role_kind or planes.get(role_key(g.role_id), '')) for g in grants]


def _drift_keys(grants: list[Grant]) -> set[tuple]:
    seen: dict[tuple, set[str]] = {}
    for g in grants:
        seen.setdefault((g.principal_id, role_key(g.role_id), normalise_id(g.scope), g.condition), set()).add(g.source)
    return {k for k, sources in seen.items() if len(sources) == 1}


def filter_evidence(evidence: SecurityEvidence, graph: Graph, flt: GrantFilter) -> tuple[SecurityEvidence, FilterReport]:
    """The evidence with only the grants that pass `flt`, and a report of what was hidden. Inactive: unchanged."""
    if not flt.active:
        return evidence, FilterReport([])
    grants = _enrich(evidence.grants)
    principals = resolve_principals(graph, evidence)
    drift = _drift_keys(grants) if flt.drift_only else set()
    kept: list[Grant] = []
    unclassified: dict[str, int] = {}
    for g in grants:
        if scope_kind(normalise_id(g.scope)).replace(' ', '-') in flt.hide_scopes:
            continue
        if flt.principal_kinds and principal_kind(g, principals) not in flt.principal_kinds:
            continue
        if flt.roles and g.role_name.casefold() not in flt.roles:
            continue
        if flt.drift_only and (g.principal_id, role_key(g.role_id), normalise_id(g.scope), g.condition) not in drift:
            continue
        if flt.role_plane:
            if not g.role_kind:
                unclassified[g.role_name] = unclassified.get(g.role_name, 0) + 1
                continue
            if g.role_kind != flt.role_plane:
                continue
        kept.append(g)
    lines = [f'Filters: {"; ".join(flt.phrases())}. Showing {len(kept)} of {len(grants)} grants; the access table has '
             'the same filters as the diagram.']
    if unclassified:
        shown = ', '.join(sorted(unclassified)[:5]) + (f' +{len(unclassified) - 5} more' if len(unclassified) > 5 else '')
        lines.append(f'{sum(unclassified.values())} grant(s) with roles of unknown plane ({shown}) are hidden by '
                     f'--role-plane {flt.role_plane}: their definitions were not read. Re-run extract --azure --security '
                     'to classify them.')
    result = SecurityEvidence(list(evidence.attachments), kept, list(evidence.coverage), dict(evidence.principals))
    return result, FilterReport(lines)
