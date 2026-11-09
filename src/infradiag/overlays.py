"""Project evidence as labelled relationships; do not infer exclusive or effective access."""
from __future__ import annotations

import re
from collections import Counter
from copy import deepcopy

from .edges import _walk_strings, get_path
from .ids import find_ids, normalise_id
from .model import Graph
from .normalise import INLINE_CHILDREN, UNEVALUATED
from .rules import Rules, matches
from .security import SecurityEvidence, identity_attachments, scope_contains
from .subscriptions import subscription_label
from .views import View, ViewEdge, ViewNode, ViewProjection

SUBSCRIPTION_SCOPE = re.compile(r'^/subscriptions/([^/]+)$')
RESOURCE_GROUP_SCOPE = re.compile(r'^/subscriptions/[^/]+/resourcegroups/[^/]+$')
MAX_ROLES = 3
USER_ASSIGNED = 'microsoft.managedidentity/userassignedidentities'


def _listed(values, limit: int) -> str:
    values = list(dict.fromkeys(values))
    text = ', '.join(values[:limit])
    return text + (f' +{len(values) - limit} more' if len(values) > limit else '')


def _role_label(name: str) -> str:
    """A role the lookup could not name arrives as a full role-definition ARM path; the label keeps its GUID tail."""
    return name.rsplit('/', 1)[-1] if name.startswith('/') else name


def _only(sources) -> str:
    """Provenance on a label only when one source alone evidences the edge (drift); both sources go on the hover."""
    return f' ({next(iter(sources))} only)' if len(set(sources)) == 1 else ''


def grant_label(principal: str, items: list[tuple]) -> str:
    """The short edge label for every grant between a principal and a target: roles, then sources and scope kind.

    The edge already joins the principal to the target, so neither is repeated. Everything long (principal and target
    names, every role and scope, conditions) is in grant_tooltip, shown on hover."""
    grants = [g for g, _tag, _name in items]
    sources = {g.source for g in grants}
    detail = [*([f'{next(iter(sources))} only'] if len(sources) == 1 else []),
              ' / '.join(dict.fromkeys(t for _g, t, _n in items))]
    if any(g.condition for g in grants):
        detail.append('conditional')
    return _listed([_role_label(g.role_name) for g in grants], MAX_ROLES) + '\n' + ' | '.join(detail)


def grant_tooltip(principal: str, items: list[tuple]) -> str:
    """The hover text for a grant edge: everything the label leaves out, with every role, scope and condition."""
    grants = [g for g, _tag, _name in items]
    targets = list(dict.fromkeys(name for _g, _tag, name in items))
    scopes = list(dict.fromkeys(g.scope for g in grants))
    conditions = list(dict.fromkeys(g.condition for g in grants if g.condition))
    lines = [f'{principal} → {", ".join(targets)}',
             'Roles: ' + ', '.join(dict.fromkeys(g.role_name for g in grants)),
             'Sources: ' + ', '.join(sorted({g.source for g in grants})),
             ('Scope: ' + scopes[0]) if len(scopes) == 1 else 'Scopes:\n' + '\n'.join(scopes)]
    if conditions:
        lines.append('Condition: ' + conditions[0] if len(conditions) == 1 else 'Conditions:\n' + '\n'.join(conditions))
    return '\n'.join(lines)


def apply_overlay(projection: ViewProjection, graph: Graph, evidence: SecurityEvidence, *, mode: str,
                  rules: Rules, subscription_names: dict[str, str] | None = None,
                  prune_ungranted: bool = False) -> View:
    subscription_names = subscription_names or {}
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
    def link(s, t, label, kind, tooltip=''):
        if s != t:
            edges.add(ViewEdge(s, t, label, 'dashed' if kind == 'attachment' else 'solid', kind, tooltip))
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
        # One node per identity, whichever source evidences it. Bicep (declared) and Azure (observed) copies of an
        # identity are the same ARM resource; what-if usually lacks the principal ID that Resource Graph returns, so
        # grants are matched to identities through principal IDs from either source. Provenance is kept on the node's
        # sublabel and on every edge, so an identity or attachment present in only one source still shows as drift.
        principals: dict[str, str] = {}
        for a in attachments:
            if a.principal_id:
                principals.setdefault(a.principal_id, a.identity_id)
        for rid, n in sorted(graph.nodes.items()):
            if n.type == USER_ASSIGNED:
                for _source, meta in sorted(n.metadata.items()):
                    if pid := str((meta.get('properties') or {}).get('principalId') or '').lower():
                        principals.setdefault(pid, rid)
        identities: dict[str, str] = {}
        provenance: dict[str, tuple[str, set, set]] = {}

        def identity_key(identity_id, label, kind, source, principal=''):
            key = identities.setdefault(identity_id, f'si{len(identities)}')
            extra(key, label, '', kind)
            _id, sources, pids = provenance.setdefault(key, (identity_id, set(), set()))
            sources.add(source)
            if principal:
                pids.add(principal)
            return key
        uses: dict[tuple[str, str, str, str], tuple[set[str], list[str]]] = {}
        for a in attachments:
            if a.identity_id in graph.nodes and not allowed(a.identity_id):
                continue
            if a.workload not in mapping:
                continue
            key = identity_key(a.identity_id, a.name, 'identity', a.source, a.principal_id)
            workload = graph.nodes[a.workload]
            wname = attribution(workload)
            uses.setdefault((mapping[a.workload], key, wname, 'attachment'), (set(), []))[0].add(a.source)
            # Explicit identity selections in service config are evidence of configured use, not traffic.
            props = workload.metadata.get(a.source, {}).get('properties') or {}
            for path, value in _walk_strings(props, 'properties'):
                if 'identity' in path.lower() and (normalise_id(value) == a.identity_id or
                        (value.casefold() == 'system' and '(system-assigned)' in a.name)):
                    sources, paths = uses.setdefault((mapping[a.workload], key, wname, 'configured'), (set(), []))
                    sources.add(a.source)
                    if path not in paths:
                        paths.append(path)
        for (wkey, key, wname, kind), (sources, paths) in uses.items():
            what = 'attachment' if kind == 'attachment' else 'configured use'
            tip = [f'{wname} → {nodes[key].label}: {what}', 'Sources: ' + ', '.join(sorted(sources))]
            if paths:
                tip.append('Configured at: ' + ', '.join(sorted(paths)))
            link(wkey, key, f'{wname}: {what}{_only(sources)}', kind, '\n'.join(tip))
        grant_edges: dict[tuple[str, str], list[tuple]] = {}
        scope_nodes: dict[tuple[str, str], str] = {}
        for grant in evidence.grants:
            identity_id = principals.get(grant.principal_id)
            if identity_id in graph.nodes and not allowed(identity_id):
                continue
            scope = normalise_id(grant.scope)
            in_sub = SUBSCRIPTION_SCOPE.match(scope)
            in_rg = RESOURCE_GROUP_SCOPE.match(scope)
            ancestor = scope.startswith('/providers/microsoft.management/managementgroups/')
            sub_id = (in_sub.group(1) if in_sub else grant.subscription).lower() if (in_sub or ancestor) else ''

            def find_targets(sub_id=sub_id, grant=grant):
                found = []
                for rid in sorted(mapping):
                    n = graph.nodes[rid]
                    if n.type.startswith('microsoft.authorization/'):
                        continue
                    if sub_id:
                        if n.subscription.lower() == sub_id:
                            found.append(rid)
                    elif scope_contains(grant.scope, rid):
                        found.append(rid)
                return found
            targets = find_targets()
            to_scope_node = bool(sub_id) or (bool(in_rg) and len({mapping[r] for r in targets}) > 1)
            if not to_scope_node:
                # Identities are ARM resources as well as security principals. Keep permissions on the resource
                # distinct from grants made to its principal. Not for grants drawn to a scope node: the identity
                # resource would have no edge.
                for rid, n in sorted(graph.nodes.items()):
                    if (n.type == USER_ASSIGNED and allowed(rid)
                            and scope_contains(grant.scope, rid) and rid not in mapping):
                        mapping[rid] = extra(f'sr{len(nodes)}', n.name, 'Managed identity resource', 'identity_resource')
                targets = find_targets()
            if not targets:
                continue
            if identity_id:
                identity = graph.nodes.get(identity_id)
                name = identity.name if identity else identity_id.rsplit('/', 1)[-1]
                if identity and identity.type != USER_ASSIGNED:
                    name += ' (system-assigned)'
                key = identity_key(identity_id, name, 'identity', grant.source, grant.principal_id)
            else:
                # Not a visible identity: retain the principal ID rather than asserting what kind of principal it is.
                key = identity_key(f'principal:{grant.principal_id}', f'Unresolved principal {grant.principal_id}',
                                   'principal', grant.source)
            # An assignment above the resources (subscription, management group) is drawn once, to a boundary
            # node for the subscription, rather than once per resource it covers. A resource-group assignment
            # gets the same treatment when it covers several visible elements. Resource scope stays on the resource.
            if sub_id:
                skey = scope_nodes.get(('subscription', sub_id))
                if skey is None:
                    skey = scope_nodes[('subscription', sub_id)] = extra(
                        f'ss{len(nodes)}', subscription_label(sub_id, subscription_names.get(sub_id)), 'subscription', 'subscription')
                tag = 'management group scope' if ancestor else 'subscription scope'
                nodes[skey].covers = tuple(sorted({*nodes[skey].covers, *(mapping[r] for r in targets)}))
                grant_edges.setdefault((key, skey), []).append((grant, tag, nodes[skey].label))
            elif to_scope_node:
                rkey = scope_nodes.get(('resource_group', scope))
                if rkey is None:
                    rkey = scope_nodes[('resource_group', scope)] = extra(
                        f'ss{len(nodes)}', graph.nodes[targets[0]].resource_group, 'resource group', 'resource_group')
                nodes[rkey].covers = tuple(sorted({*nodes[rkey].covers, *(mapping[r] for r in targets)}))
                grant_edges.setdefault((key, rkey), []).append((grant, 'resource group scope', nodes[rkey].label))
            else:
                for rid in targets:
                    tag = 'resource scope' if scope == rid else 'inherited'
                    grant_edges.setdefault((key, mapping[rid]), []).append((grant, tag, attribution(graph.nodes[rid])))
        for (pkey, tkey), items in grant_edges.items():
            link(pkey, tkey, grant_label(nodes[pkey].label, items), 'grant', grant_tooltip(nodes[pkey].label, items))
        for key, (identity_id, sources, pids) in provenance.items():
            # Where the identity resource exists, plus where it is attached or granted: Bicep may only reference an
            # identity it does not declare.
            resource = graph.nodes.get(identity_id)
            if resource and resource.type == USER_ASSIGNED:
                sources = sources | resource.sources
            nodes[key].sublabel = ', '.join(sorted(sources))
            if nodes[key].kind == 'identity':
                pids = pids | {pid for pid, iid in principals.items() if iid == identity_id}
                # what-if leaves a principal ID it cannot know yet as an expression; a real one supersedes it
                known = sorted(p for p in pids if not UNEVALUATED.match(p)) or sorted(pids)
                nodes[key].sublabel += f' | principal: {", ".join(known) or "unknown"}'
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
    if prune_ungranted:
        # Grant filters are active: an identity or principal none of whose grants survived is not drawn, nor are the
        # attachment and configured-use edges that only led to it.
        granted = {k for e in edges if e.kind == 'grant' for k in (e.source, e.target)}
        gone = {k for k, n in nodes.items() if n.kind in ('identity', 'principal') and k not in granted}
        edges = {e for e in edges if e.source not in gone and e.target not in gone}
        view.nodes = [n for n in view.nodes if n.key not in gone]
    view.edges = sorted(edges)
    return view
