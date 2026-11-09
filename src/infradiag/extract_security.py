"""Read-only authorization extraction and independently versioned offline cache."""
from __future__ import annotations

import json
from dataclasses import asdict

from .azcli import AzError, run_az
from .cache import write_json
from .config import Target
from .security import Grant, SecurityEvidence, grants_from_rows

COVERAGE = ('RBAC grants visible to the querying account; not effective-access evaluation. '
            'Group membership, deny assignments, service-specific policies and runtime usage are not evaluated.')


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
