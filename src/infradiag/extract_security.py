"""Read-only authorization extraction and independently versioned offline cache."""
from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict

from .azcli import AzError, run_az
from .cache import write_json
from .config import Target
from .security import DirectoryPrincipal, Grant, SecurityEvidence, grants_from_rows, role_key

COVERAGE = ('RBAC grants visible to the querying account; not effective-access evaluation. '
            'Group membership, deny assignments, service-specific policies and runtime usage are not evaluated.')

GUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


def unresolved_role_guids(rows: list[dict]) -> list[str]:
    """GUIDs of assigned roles with no definition row. An inline role name does not count: the plane needs permissions."""
    defined = {role_key(r['id']) for r in rows
               if str(r.get('type', '')).lower().endswith('/roledefinitions') and r.get('id')}
    missing = set()
    for row in rows:
        if not str(row.get('type', 'microsoft.authorization/roleassignments')).lower().endswith('/roleassignments'):
            continue
        props = row.get('properties') or row
        key = role_key(str(props.get('roleDefinitionId') or ''))
        if key and key not in defined and GUID.match(key):
            missing.add(key)
    return sorted(missing)


def resolve_builtin_roles(cfg: Target, rows: list[dict], run, notes: list[str]) -> None:
    """Resource Graph's authorizationresources holds custom role definitions only; built-in roles
    (Contributor, Reader, ...) are fetched per GUID read-only via the CLI. A handful of distinct
    GUIDs is far cheaper than listing every definition (~1000) in the tenant."""
    for guid in unresolved_role_guids(rows):
        try:
            found = run(['role', 'definition', 'list', '--name', guid, '--subscription', cfg.subscription]) or []
        except AzError as exc:
            notes.append(f'Role definition lookup failed for {guid}; name left as ID: {exc}')
            continue
        for definition in found if isinstance(found, list) else [found]:
            if name := definition.get('roleName'):
                rows.append({'type': 'microsoft.authorization/roledefinitions', 'id': definition.get('id') or guid,
                             'properties': {'roleName': name, 'permissions': definition.get('permissions') or []}})


GRAPH_GET_BY_IDS = 'https://graph.microsoft.com/v1.0/directoryObjects/getByIds'
GRAPH_BATCH = 1000  # getByIds accepts at most 1000 IDs per request
GRAPH_TYPES = ['user', 'group', 'servicePrincipal', 'device']
# RBAC principalType values that fall inside GRAPH_TYPES. Any other (ForeignGroup lives in another tenant) cannot be
# found by this lookup, so not being returned says nothing about whether it exists.
LOOKUP_PRINCIPAL_TYPES = {'', 'user', 'group', 'serviceprincipal', 'device'}
CACHE_VERSION = 2   # 1: grants only. 2: adds `principals`. Both load.


def _directory_principal(obj: dict) -> DirectoryPrincipal | None:
    pid = str(obj.get('id') or '').lower()
    if not pid:
        return None
    kind = str(obj.get('@odata.type') or '').rsplit('.', 1)[-1].casefold()
    arm = next((n for n in obj.get('alternativeNames') or [] if isinstance(n, str) and n.startswith('/')), '')
    return DirectoryPrincipal(pid, str(obj.get('displayName') or ''), kind, str(obj.get('servicePrincipalType') or ''),
                              str(obj.get('appId') or ''), arm.lower(), str(obj.get('userPrincipalName') or ''))


def resolve_directory(ids: list[str], run, unsearchable=frozenset()) -> tuple[dict[str, DirectoryPrincipal], str]:
    """Look up principal IDs in Microsoft Graph with read-only getByIds POSTs (a POST used as a bulk read).

    Returns the principals found, plus a deleted marker for every ID of a batch Graph answered but did not return, and a
    reason if any batch could not be answered (nothing is recorded for the IDs of such a batch). An ID in `unsearchable`
    (its type is outside GRAPH_TYPES) is recorded only if returned, never as deleted. Never raises."""
    found: dict[str, DirectoryPrincipal] = {}
    failure = ''
    ids = sorted(set(ids))
    for start in range(0, len(ids), GRAPH_BATCH):
        batch = ids[start:start + GRAPH_BATCH]
        body = json.dumps({'ids': batch, 'types': GRAPH_TYPES})
        try:
            reply = run(['rest', '--method', 'POST', '--url', GRAPH_GET_BY_IDS, '--headers', 'Content-Type=application/json',
                         '--body', body]) or {}
            objects = reply.get('value') if isinstance(reply, dict) else None
            if not isinstance(objects, list):
                raise AzError('unexpected Graph response')
        except AzError as exc:
            failure = failure or (str(exc).strip().splitlines() or ['no detail'])[0][:300]
            continue
        returned = {p.principal_id: p for o in objects if isinstance(o, dict) and (p := _directory_principal(o))}
        for pid in batch:
            if pid in returned:
                found[pid] = returned[pid]
            elif pid not in unsearchable:
                found[pid] = DirectoryPrincipal(pid, deleted=True)
    return found, failure


def extract_security(cfg: Target, run=None) -> SecurityEvidence:
    run = run or run_az
    rows, notes = [], [COVERAGE]
    token = None
    try:
        while True:
            args = ['graph', 'query', '-q', 'authorizationresources | where type in~ '
                    "('microsoft.authorization/roleassignments', 'microsoft.authorization/roledefinitions')"
                    ' | project id, type, properties | order by id asc',
                    '--subscriptions', cfg.subscription, '--first', '1000']
            if token:
                args += ['--skip-token', token]
            page = run(args) or {}
            rows.extend(page.get('data') or [])
            token = page.get('skip_token')
            if not token:
                break
    except AzError as exc:
        notes.append(f'Incomplete subscription RBAC read: {exc}')
    # Azure CLI includes visible management-group assignments inherited by this subscription.
    try:
        inherited = run(['role', 'assignment', 'list', '--subscription', cfg.subscription,
                         '--scope', f'/subscriptions/{cfg.subscription}', '--include-inherited']) or []
        rows.extend(inherited)
        notes.append('Ancestor grants: visible inherited assignments returned by Azure CLI; visibility may be partial.')
    except AzError as exc:
        notes.append(f'Incomplete ancestor grant coverage: {exc}')
    resolve_builtin_roles(cfg, rows, run, notes)
    evidence = grants_from_rows(rows, subscription=cfg.subscription)
    ids = [g.principal_id for g in evidence.grants if GUID.match(g.principal_id)]
    if ids:
        outside = {g.principal_id for g in evidence.grants if g.principal_type.casefold() not in LOOKUP_PRINCIPAL_TYPES}
        evidence.principals, failure = resolve_directory(ids, run, outside)
        if failure:
            message = (f'Microsoft Graph principal lookup failed ({failure}); principals it could not answer for keep '
                       'their IDs and are drawn as unresolved')
            print(f'warning: {message}', file=sys.stderr)
            notes.append(message[0].upper() + message[1:] + '.')
    evidence.coverage = notes + evidence.coverage
    write_json(cfg.cache_dir / 'security.json', {'version': CACHE_VERSION, 'subscription': cfg.subscription,
               'grants': [asdict(g) for g in evidence.grants], 'coverage': evidence.coverage,
               'principals': {pid: asdict(p) for pid, p in sorted(evidence.principals.items())}})
    return evidence


def load_security(cfg: Target) -> SecurityEvidence:
    path = cfg.cache_dir / 'security.json'
    if not path.exists():
        return SecurityEvidence(coverage=[f'Security not extracted for {cfg.subscription}; run '
                                f'`infradiag extract --azure --security --subscription {cfg.subscription}`.'])
    try:
        raw = json.loads(path.read_text())
        if raw.get('version') not in (1, CACHE_VERSION) or raw.get('subscription', '').lower() != cfg.subscription.lower():
            raise ValueError('cache version or subscription mismatch')
        principals = {pid: DirectoryPrincipal(**p) for pid, p in (raw.get('principals') or {}).items()}
        return SecurityEvidence(grants=[Grant(**g) for g in raw['grants']], coverage=list(raw['coverage']),
                                principals=principals)
    except (ValueError, TypeError, KeyError) as exc:
        return SecurityEvidence(coverage=[f'Incomplete security cache: {exc}; re-run extract --azure --security.'])
