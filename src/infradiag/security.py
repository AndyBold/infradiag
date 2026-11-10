"""Security evidence, kept separate from topology and effective-access assertions."""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from .ids import normalise_id
from .model import Graph
from .normalise import NODE_CHANGE_TYPES, UNEVALUATED


@dataclass(frozen=True)
class Grant:
    principal_id: str
    role_id: str
    role_name: str
    scope: str
    condition: str = ''
    source: str = 'azure'
    # Ancestor scopes do not share a resource's ARM prefix; extraction supplies membership.
    subscription: str = ''
    principal_type: str = ''
    # 'data' (the role has any dataActions), 'control' (actions only) or '' (not known: no definition was read, or it
    # grants nothing). Older caches have no value; filters report such grants rather than guess.
    role_kind: str = ''


@dataclass(frozen=True)
class Attachment:
    workload: str
    principal_id: str
    name: str
    identity_id: str
    source: str = 'azure'


@dataclass(frozen=True)
class DirectoryPrincipal:
    """What Microsoft Graph records about a principal ID: directory metadata only, nothing secret.

    `deleted` is a principal ID Graph was asked about and did not return (the object no longer exists); it is not set
    when Graph could not be asked at all, in which case no entry is recorded."""
    principal_id: str
    name: str = ''
    object_type: str = ''      # 'user', 'group' or 'serviceprincipal'
    sp_type: str = ''          # servicePrincipalType: ManagedIdentity, Application, Legacy, SocialIdp
    app_id: str = ''
    resource_id: str = ''      # managed identities: the ARM resource ID Graph records in alternativeNames
    upn: str = ''              # users: userPrincipalName
    deleted: bool = False

    @property
    def kind(self) -> str:
        """The principal kind as the access table and --principal-kind see it."""
        if self.deleted:
            return 'deleted'
        if self.object_type == 'serviceprincipal':
            if self.sp_type.casefold() == 'managedidentity':
                rid = self.resource_id.casefold()
                if not rid:
                    return 'managed identity'
                return 'managed identity (user-assigned)' if '/userassignedidentities/' in rid else 'managed identity (system-assigned)'
            return 'service principal'
        return self.object_type or 'unresolved'

    @property
    def display_name(self) -> str:
        return (self.upn or self.name) if self.object_type == 'user' else self.name

    @property
    def kind_label(self) -> str:
        """The kind as drawn in brackets after the name."""
        if self.kind.startswith('managed identity'):
            return 'Managed identity'
        if self.kind == 'service principal':
            return 'Application' if self.sp_type.casefold() == 'application' else 'Service principal'
        return self.kind.capitalize()

    @property
    def label(self) -> str:
        return f'Deleted principal {self.principal_id}' if self.deleted else f'{self.display_name or self.principal_id} [{self.kind_label}]'

    @property
    def tooltip(self) -> str:
        lines = [self.label] if not self.deleted else [self.label, 'Not found in Microsoft Graph: the principal was deleted.']
        if not self.deleted:
            lines.append(f'Type: {self.kind}')
            if self.name and self.name != self.display_name:
                lines.append(f'Display name: {self.name}')
            if self.app_id:
                lines.append(f'Application ID: {self.app_id}')
            if self.resource_id:
                lines.append(f'Resource: {self.resource_id}')
        lines.append(f'Principal ID: {self.principal_id}')
        return '\n'.join(lines)


@dataclass
class SecurityEvidence:
    attachments: list[Attachment] = field(default_factory=list)
    grants: list[Grant] = field(default_factory=list)
    coverage: list[str] = field(default_factory=list)
    # Principal ID (lower case) -> Microsoft Graph metadata. Empty when never looked up or Graph was unavailable.
    principals: dict[str, DirectoryPrincipal] = field(default_factory=dict)

    def extend(self, other: SecurityEvidence) -> None:
        self.attachments.extend(a for a in other.attachments if a not in self.attachments)
        self.grants.extend(g for g in other.grants if g not in self.grants)
        self.coverage.extend(c for c in other.coverage if c not in self.coverage)
        for pid, principal in other.principals.items():
            self.principals.setdefault(pid, principal)


def scope_contains(scope: str, resource_id: str) -> bool:
    scope, resource_id = normalise_id(scope), normalise_id(resource_id)
    return bool(scope) and (resource_id == scope or resource_id.startswith(scope + '/'))


def identity_attachments(graph: Graph) -> list[Attachment]:
    result = []
    for n in graph.nodes.values():
        for source in sorted(n.sources):
            identity = n.metadata.get(source, {}).get('identity') or {}
            if principal := identity.get('principalId'):
                result.append(Attachment(n.id, str(principal).lower(), f'{n.name} (system-assigned)', n.id, source))
            for identity_id, details in (identity.get('userAssignedIdentities') or {}).items():
                iid = normalise_id(identity_id)
                target = graph.nodes.get(iid)
                props = (target.metadata.get(source, {}).get('properties') or {}) if target else {}
                principal = (details or {}).get('principalId') or props.get('principalId') or ''
                result.append(Attachment(n.id, str(principal).lower(), target.name if target else identity_id.rsplit('/', 1)[-1],
                                         iid, source))
    return sorted(set(result), key=lambda a: (a.workload, a.source, a.identity_id))


def role_key(role_id: str) -> str:
    """Role definitions are matched by GUID: Resource Graph returns assignments in tenant form
    (/providers/...) but definitions in subscription form (/subscriptions/<sub>/providers/...)."""
    return normalise_id(role_id).rsplit('/', 1)[-1]


def canonical_role_id(role_id: str) -> str:
    rid = normalise_id(role_id)
    if '/providers/microsoft.authorization/roledefinitions/' not in rid:
        return rid
    return '/providers/microsoft.authorization/roledefinitions/' + role_key(rid)


def classify_role(permissions) -> str:
    """The plane a role definition acts on, from its permissions (notActions / notDataActions are not subtracted)."""
    perms = [p for p in (permissions or []) if isinstance(p, dict)]
    if any(p.get('dataActions') for p in perms):
        return 'data'
    return 'control' if any(p.get('actions') for p in perms) else ''


UAI = 'microsoft.managedidentity/userassignedidentities'


def resolve_principals(graph: Graph, evidence: SecurityEvidence) -> dict[str, tuple[str, str]]:
    """Principal ID -> (name, kind) for every managed identity the graph or the evidence attaches."""
    principals: dict[str, tuple[str, str]] = {}
    for a in list(identity_attachments(graph)) + evidence.attachments:
        if a.principal_id:
            kind = 'managed identity (system-assigned)' if a.name.endswith('(system-assigned)') else 'managed identity (user-assigned)'
            principals.setdefault(a.principal_id, (a.name, kind))
    for n in graph.nodes.values():
        if n.type == UAI:
            for meta in n.metadata.values():
                if pid := str((meta.get('properties') or {}).get('principalId') or '').lower():
                    principals.setdefault(pid, (n.name, 'managed identity (user-assigned)'))
    for pid, p in evidence.principals.items():
        if pid in principals:
            continue
        node = graph.nodes.get(normalise_id(p.resource_id)) if p.resource_id else None
        if node and p.kind.startswith('managed identity'):
            # Graph's ARM resource ID names the identity even when the principal ID is on no attachment.
            suffix = '' if node.type == UAI else ' (system-assigned)'
            principals[pid] = (node.name + suffix, p.kind)
        elif p.deleted:
            principals[pid] = ('deleted principal', 'deleted')
        else:
            principals[pid] = (p.display_name or pid, p.kind)
    return principals


def grants_from_rows(rows: list[dict], source: str = 'azure', subscription: str = '') -> SecurityEvidence:
    evidence = SecurityEvidence()
    definitions = [r for r in rows if str(r.get('type', '')).lower().endswith('/roledefinitions') and r.get('id')]
    roles = {role_key(r['id']): (r.get('properties') or {}).get('roleName', r['id']) for r in definitions}
    kinds = {role_key(r['id']): classify_role((r.get('properties') or {}).get('permissions')) for r in definitions}
    unnamed: set[int] = set()
    for row in rows:
        if not str(row.get('type', 'microsoft.authorization/roleassignments')).lower().endswith('/roleassignments'):
            continue
        props = row.get('properties') or row
        principal = str(props.get('principalId') or '')
        role_id = str(props.get('roleDefinitionId') or '')
        scope = props.get('scope') or str(row.get('id', '')).lower().rsplit('/providers/microsoft.authorization/', 1)[0]
        if not principal or not role_id or not scope:
            evidence.coverage.append(f'{source}: incomplete role assignment payload')
            continue
        if any(UNEVALUATED.match(str(v)) for v in (principal, role_id, scope)):
            evidence.coverage.append(f'{source}: unresolved role assignment expression')
        role_name = roles.get(role_key(role_id), row.get('roleDefinitionName') or role_id)
        if role_name == role_id:
            evidence.coverage.append(f'{source}: role definition name unavailable: {role_id}')
        grant = Grant(principal.lower(), canonical_role_id(role_id), str(role_name), normalise_id(str(scope)),
                      str(props.get('condition') or ''), source, subscription.lower(),
                      str(props.get('principalType') or ''), kinds.get(role_key(role_id), ''))
        # The same assignment may arrive twice with differing names; keep one, preferring the resolved name.
        same = [i for i, g in enumerate(evidence.grants) if replace(g, role_name='', role_kind='') == replace(grant, role_name='', role_kind='')]
        if not same:
            evidence.grants.append(grant)
            if role_name == role_id:
                unnamed.add(len(evidence.grants) - 1)
        elif same[0] in unnamed and role_name != role_id:
            evidence.grants[same[0]] = grant
            unnamed.discard(same[0])
    return evidence


def declared_security(results: dict[str, dict]) -> SecurityEvidence:
    rows, coverage = [], []
    for entry, raw in sorted(results.items()):
        if raw.get('error') or raw.get('status') == 'Failed':
            coverage.append(f'bicep: what-if failed for {entry}; declared grants incomplete')
            continue
        for change in raw.get('changes') or []:
            rid = str(change.get('resourceId', ''))
            if '/providers/microsoft.authorization/' not in rid.lower():
                continue
            if change.get('changeType') not in NODE_CHANGE_TYPES:
                if change.get('changeType') in ('Ignore', 'Unsupported'):
                    coverage.append(f'bicep: unresolved authorization resource: {rid}')
                continue
            after = change.get('after') or {}
            rows.append({**after, 'id': rid, 'type': after.get('type') or 'microsoft.authorization/' +
                         rid.lower().rsplit('/providers/microsoft.authorization/', 1)[-1].split('/')[0]})
    evidence = grants_from_rows(rows, 'bicep')
    evidence.coverage.extend(coverage)
    return evidence
