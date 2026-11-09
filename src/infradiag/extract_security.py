"""Read-only authorization extraction and independently versioned offline cache."""
from __future__ import annotations

import json
import re
from dataclasses import asdict

from .azcli import AzError, run_az
from .cache import write_json
from .config import Target
from .security import Grant, SecurityEvidence, grants_from_rows, role_key

COVERAGE = ('RBAC grants visible to the querying account; not effective-access evaluation. '
            'Group membership, deny assignments, service-specific policies and runtime usage are not evaluated.')

GUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


def unresolved_role_guids(rows: list[dict]) -> list[str]:
    """GUIDs of assigned roles with neither a definition row nor an inline role name."""
    defined = {role_key(r['id']) for r in rows
               if str(r.get('type', '')).lower().endswith('/roledefinitions') and r.get('id')}
    missing = set()
    for row in rows:
        if not str(row.get('type', 'microsoft.authorization/roleassignments')).lower().endswith('/roleassignments'):
            continue
        props = row.get('properties') or row
        key = role_key(str(props.get('roleDefinitionId') or ''))
        if key and key not in defined and not row.get('roleDefinitionName') and GUID.match(key):
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
    evidence.coverage = notes + evidence.coverage
    write_json(cfg.cache_dir / 'security.json', {'version': 1, 'subscription': cfg.subscription,
               'grants': [asdict(g) for g in evidence.grants], 'coverage': evidence.coverage})
    return evidence


def load_security(cfg: Target) -> SecurityEvidence:
    path = cfg.cache_dir / 'security.json'
    if not path.exists():
        return SecurityEvidence(coverage=[f'Security not extracted for {cfg.subscription}; run '
                                f'`infradiag extract --azure --security --subscription {cfg.subscription}`.'])
    try:
        raw = json.loads(path.read_text())
        if raw.get('version') != 1 or raw.get('subscription', '').lower() != cfg.subscription.lower():
            raise ValueError('cache version or subscription mismatch')
        return SecurityEvidence(grants=[Grant(**g) for g in raw['grants']], coverage=list(raw['coverage']))
    except (ValueError, TypeError, KeyError) as exc:
        return SecurityEvidence(coverage=[f'Incomplete security cache: {exc}; re-run extract --azure --security.'])
