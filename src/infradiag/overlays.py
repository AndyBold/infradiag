"""Project evidence as labelled relationships; do not infer exclusive or effective access."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy

from .edges import _walk_strings, get_path
from .ids import find_ids, normalise_id
from .model import Graph
from .normalise import INLINE_CHILDREN
from .rules import Rules, matches
from .security import SecurityEvidence, identity_attachments, scope_contains
from .views import View, ViewEdge, ViewNode, ViewProjection


def apply_overlay(projection: ViewProjection, graph: Graph, evidence: SecurityEvidence, *, mode: str,
                  rules: Rules) -> View:
    view = deepcopy(projection.view)
    if mode == 'none':
        return view
    if mode not in ('identity', 'network', 'all'):
        raise ValueError(f'unknown security overlay: {mode}')
    mapping = dict(projection.resources)
    nodes = {}
    def index(items):
        for n in items:
            nodes[n.key] = n
            index(n.children)
    index(view.nodes)
    def allowed(rid):
        n = graph.nodes.get(rid)
        return n is not None and not matches(n.name, rules.out_of_scope_names) and not (rules.hide_managed and n.managed_by)
    mapping = {rid: key for rid, key in mapping.items() if allowed(rid)}
    counts = Counter(n.name.casefold() for n in graph.nodes.values())
    def attribution(n):
        return n.name if counts[n.name.casefold()] == 1 else f'{n.name} [{n.resource_group}; {n.subscription}]'
    edges = set(view.edges)
    def link(s, t, label, kind):
        if s != t:
            edges.add(ViewEdge(s, t, label, 'dashed' if kind == 'attachment' else 'solid', kind))
        else:
            note = f'Within {nodes[s].label}: {label}'
            if note not in nodes[s].annotations:
                nodes[s].annotations += (note,)
    def extra(key, label, sublabel, kind):
        if key not in nodes:
            n = ViewNode(key, label, sublabel, kind=kind)
            view.nodes.append(n)
            nodes[key] = n
        return key
    if mode in ('identity', 'all'):
        attachments = list(dict.fromkeys(identity_attachments(graph) + evidence.attachments))
        identities = {}
        excluded_principals = set()
        for a in attachments:
            identity = graph.nodes.get(a.identity_id)
            if identity and not allowed(a.identity_id):
                excluded_principals.add((a.source, a.principal_id))
                continue
            if a.workload not in mapping:
                continue
            ikey = (a.source, a.principal_id or a.identity_id)
            key = identities.setdefault(ikey, f'si{len(identities)}')
            extra(key, a.name, f'{a.source} | principal: {a.principal_id or "unknown"}', 'identity')
            workload = graph.nodes[a.workload]
            link(mapping[a.workload], key, f'{attribution(workload)}: attachment ({a.source})', 'attachment')
            # Explicit identity selections in service config are evidence of configured use, not traffic.
            props = workload.metadata.get(a.source, {}).get('properties') or {}
            for path, value in _walk_strings(props, 'properties'):
                if 'identity' in path.lower() and (normalise_id(value) == a.identity_id or
                        (value.casefold() == 'system' and '(system-assigned)' in a.name)):
                    link(mapping[a.workload], key, f'{attribution(workload)}: configured use at {path} ({a.source})', 'configured')
        for grant in evidence.grants:
            if (grant.source, grant.principal_id) in excluded_principals:
                continue
            # Identities are ARM resources as well as security principals. Keep
            # permissions on the resource distinct from grants made to its principal.
            for rid, n in sorted(graph.nodes.items()):
                ancestor = grant.scope.startswith('/providers/microsoft.management/managementgroups/')
                if (n.type == 'microsoft.managedidentity/userassignedidentities' and allowed(rid)
                        and (scope_contains(grant.scope, rid) or (ancestor and grant.subscription == n.subscription))
                        and rid not in mapping):
                    mapping[rid] = extra(f'sr{len(nodes)}', n.name, 'Managed identity resource', 'identity_resource')
            targets = []
            for rid in sorted(mapping):
                n = graph.nodes[rid]
                if n.type.startswith('microsoft.authorization/'):
                    continue
                ancestor = grant.scope.startswith('/providers/microsoft.management/managementgroups/')
                if scope_contains(grant.scope, rid) or (ancestor and grant.subscription == n.subscription):
                    targets.append(rid)
            if not targets:
                continue
            ikey = (grant.source, grant.principal_id)
            key = identities.setdefault(ikey, f'si{len(identities)}')
            if key not in nodes:
                # Resolve an unattached identity by principal ID when visible, otherwise retain ID.
                identity = next((n for n in graph.nodes.values() if n.type == 'microsoft.managedidentity/userassignedidentities'
                                 and str((n.metadata.get(grant.source, {}).get('properties') or {}).get('principalId', '')).lower()
                                 == grant.principal_id), None)
                if identity and not allowed(identity.id):
                    continue
                extra(key, identity.name if identity else f'Unresolved principal {grant.principal_id}', grant.source, 'identity' if identity else 'principal')
            for rid in targets:
                inherited = 'inherited' if normalise_id(grant.scope) != rid else 'resource scope'
                label = (f'{nodes[key].label} → {attribution(graph.nodes[rid])}: {grant.role_name}\n'
                         f'{grant.source} | {inherited} | scope: {grant.scope}')
                if grant.condition:
                    label += f'\ncondition: {grant.condition}'
                link(key, mapping[rid], label, 'grant')
        view.notes.extend(c for c in evidence.coverage if c not in view.notes)
        view.notes.append('Identity overlay: attachment / configured use / RBAC grant; not effective access or observed traffic. '
                          'Bicep is declared; Azure is observed. Grants do not imply exclusive access.')
    if mode in ('network', 'all'):
        # Network/security objects normally hidden in summary remain available as supporting evidence.
        for rid, n in sorted(graph.nodes.items()):
            if allowed(rid) and n.type in ('microsoft.network/virtualnetworks', 'microsoft.network/virtualnetworks/subnets',
                                         'microsoft.network/networksecuritygroups', 'microsoft.network/privateendpoints'):
                if rid not in mapping:
                    mapping[rid] = extra(f'sn{len(nodes)}', n.name, n.type.rsplit('/', 1)[-1], 'network')
                visible = nodes[mapping[rid]]
                if len(visible.resource_ids) <= 1 and visible.kind not in ('system', 'application', 'group'):
                    visible.kind = 'network'
                else:
                    visible.annotations += (f'Network member: {n.name} ({n.type.rsplit("/", 1)[-1]})',)
        for rid, key in sorted(mapping.items()):
            n = graph.nodes[rid]
            for source, meta in sorted(n.metadata.items()):
                props = meta.get('properties') or {}
                if not n.type.startswith(('microsoft.network/', 'microsoft.managedidentity/', 'microsoft.authorization/')):
                    access_path = 'network.publicNetworkAccess' if n.type == 'microsoft.dbforpostgresql/flexibleservers' else 'publicNetworkAccess'
                    access = get_path(props, access_path)
                    note = f'{attribution(n)} ({source}): public access: {access if access is not None else "unknown"}'
                    nodes[key].annotations += (note,)
                for path, value in _walk_strings(props, 'properties', INLINE_CHILDREN.get(n.type, ())):
                    for ref in find_ids(value):
                        ref = normalise_id(ref)
                        target = graph.nodes.get(ref)
                        if ref not in mapping or target is None:
                            continue
                        label = ''
                        if target.type.endswith('/subnets'):
                            label = 'subnet attachment' if n.type.endswith('/privateendpoints') else 'integration association'
                        elif target.type.endswith('/networksecuritygroups'):
                            label = 'networkSecurityGroup association'
                        elif 'privatelinkserviceid' in path.lower():
                            label = 'private endpoint connection'
                        if label:
                            link(key, mapping[ref], f'{attribution(n)} → {attribution(target)}: {label} ({source})', 'network')
            if n.parent_id in mapping and n.type.endswith('/subnets'):
                link(mapping[n.parent_id], key, f'{n.name}: subnet boundary', 'network')
        # Existing detailed mode nests any subnet-reference consumer. In overlay mode only
        # private endpoint attachments retain that placement; integration consumers are detached.
        detached = []
        subnet_keys = {mapping[rid] for rid in mapping if graph.nodes[rid].type.endswith('/subnets')}
        for parent in list(nodes.values()):
            if parent.key in subnet_keys:
                for child in list(parent.children):
                    members = [graph.nodes[r] for r in child.resource_ids if r in graph.nodes]
                    if members and any(not n.type.endswith('/privateendpoints') for n in members):
                        parent.children.remove(child)
                        detached.append(child)
        view.nodes.extend(detached)
        view.notes.append('Network overlay: zone / association / private endpoint. Public access is shown independently; '
                          'unknown is not disabled. Firewall rules and runtime routing are not evaluated.')
    view.edges = sorted(edges)
    return view
