"""The identity overlay's grant evidence as a sortable, filterable table (self-contained HTML plus CSV).

Built from every grant in the evidence, before the overlay collapses edges, so nothing is dropped
because a diagram drew it once per subscription."""
from __future__ import annotations

import csv
import html
import io
import re
from collections import Counter
from dataclasses import dataclass, fields
from pathlib import Path

from .ids import normalise_id
from .model import STUB, Graph
from .rules import Rules, matches
from .security import SecurityEvidence, identity_attachments, scope_contains
from .split import key_env
from .subscriptions import subscription_label
from .views import ViewProjection

MG_PREFIX = '/providers/microsoft.management/managementgroups/'
SUBSCRIPTION = re.compile(r'^/subscriptions/([^/]+)$')
RESOURCE_GROUP = re.compile(r'^/subscriptions/[^/]+/resourcegroups/([^/]+)$')
UAI = 'microsoft.managedidentity/userassignedidentities'
PRINCIPAL_TYPES = {'user': 'user', 'group': 'group', 'serviceprincipal': 'service principal',
                   'foreigngroup': 'group', 'device': 'device'}


@dataclass(frozen=True)
class AccessRow:
    principal_name: str
    principal_type: str
    principal_id: str
    resource: str
    resource_type: str
    role: str
    scope: str
    scope_kind: str
    source: str
    condition: str
    environment: str
    covers: int = 1  # visible diagram elements this assignment reaches in this environment


COLUMNS = [
    ('principal_name', 'Principal'), ('principal_type', 'Principal type'), ('principal_id', 'Principal ID'),
    ('resource', 'Resource'), ('resource_type', 'Resource type'), ('role', 'Role'), ('scope', 'Scope'),
    ('scope_kind', 'Scope kind'), ('source', 'Source'), ('condition', 'Condition'), ('environment', 'Environment'),
    ('covers', 'Elements covered'),
]
assert [c for c, _ in COLUMNS] == [f.name for f in fields(AccessRow)]


def scope_kind(scope: str) -> str:
    if scope.startswith(MG_PREFIX):
        return 'management group'
    if SUBSCRIPTION.match(scope):
        return 'subscription'
    if RESOURCE_GROUP.match(scope):
        return 'resource group'
    if scope in ('', '/'):
        return 'tenant'
    return 'resource'


def _resource_type(scope: str) -> str:
    """ns/type/subtype from a resource ID that is not a node in the graph."""
    parts = scope.split('/providers/', 1)[-1].split('/')
    return '/'.join([parts[0], *parts[1::2]]) if len(parts) > 2 else scope


def build_access_rows(projection: ViewProjection, graph: Graph, evidence: SecurityEvidence, rules: Rules,
                      subscription_names: dict[str, str] | None = None) -> list[AccessRow]:
    names = subscription_names or {}
    env_of_key = key_env(projection.view)
    def env_of(rid: str) -> str:
        key = projection.resources.get(rid)
        return env_of_key.get(key, '') if key else ''
    def visible(rid: str) -> bool:
        n = graph.nodes[rid]
        return (STUB not in n.flags and not matches(n.name, rules.out_of_scope_names) and not (rules.hide_managed and n.managed_by)
                and not n.type.startswith('microsoft.authorization/') and rid in projection.resources)
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
    counts = Counter(n.name.casefold() for n in graph.nodes.values())

    rows: list[AccessRow] = []
    for g in evidence.grants:
        scope = normalise_id(g.scope)
        kind = scope_kind(scope)
        name, ptype = principals.get(g.principal_id) or (
            'unresolved principal', PRINCIPAL_TYPES.get(g.principal_type.casefold(), g.principal_type) or 'unresolved')
        common = dict(principal_name=name, principal_type=ptype, principal_id=g.principal_id, role=g.role_name,
                      scope=scope, scope_kind=kind, source=g.source, condition=g.condition)
        if kind == 'resource':
            node = graph.nodes.get(scope)
            label = node.name if node else scope.rsplit('/', 1)[-1]
            if node and counts[node.name.casefold()] > 1:
                label = f'{node.name} [{node.resource_group}]'
            parent = scope
            while parent and parent not in projection.resources and '/' in parent:
                parent = parent.rsplit('/', 1)[0]
            rows.append(AccessRow(resource=label, resource_type=node.type if node else _resource_type(scope),
                                  environment=env_of(parent) if parent in projection.resources else '', **common))
            continue
        if kind == 'subscription':
            sub = SUBSCRIPTION.match(scope).group(1)
            label = subscription_label(sub, names.get(sub))
        elif kind == 'resource group':
            label = RESOURCE_GROUP.match(scope).group(1)
        elif kind == 'management group':
            label, sub = scope.rsplit('/', 1)[-1], g.subscription.lower()
        else:
            label, sub = 'tenant root', g.subscription.lower()
        covered = Counter()
        for rid in graph.nodes:
            if not visible(rid):
                continue
            n = graph.nodes[rid]
            inside = (n.subscription.lower() == sub if kind in ('subscription', 'management group', 'tenant')
                      else scope_contains(scope, rid))
            if inside:
                covered[env_of(rid)] += 1
        for env, count in sorted(covered.items()) or [('', 0)]:
            rows.append(AccessRow(resource=label, resource_type=kind, environment=env, covers=count, **common))
    return sorted(rows, key=lambda r: (r.environment.casefold(), r.principal_name.casefold(), r.role.casefold(),
                                       r.scope, r.source, r.condition))


def for_environment(rows: list[AccessRow], environment: str) -> list[AccessRow]:
    return [r for r in rows if r.environment.casefold() == environment.casefold()]


def render_csv(rows: list[AccessRow]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator='\n')
    writer.writerow([label for _, label in COLUMNS])
    for r in rows:
        writer.writerow([getattr(r, c) for c, _ in COLUMNS])
    return out.getvalue()


STYLE = """
:root{color-scheme:light dark;--bg:#fff;--fg:#1d2433;--muted:#5b6472;--line:#d5d9e0;--head:#eef1f6;--accent:#1565c0}
@media (prefers-color-scheme:dark){:root{--bg:#161a22;--fg:#e4e8ef;--muted:#9aa3b2;--line:#303644;--head:#222836;--accent:#7fb0ff}}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,sans-serif}
h1{font-size:20px;margin:0 0 4px}p.note{color:var(--muted);margin:4px 0 12px;max-width:80ch}
#q{width:min(40ch,100%);padding:6px 8px;font:inherit}#count{margin-left:12px;color:var(--muted)}
.wrap{overflow:auto;max-height:80vh;margin-top:12px;border:1px solid var(--line)}
table{border-collapse:collapse;width:100%}th,td{padding:4px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
thead th{position:sticky;background:var(--head);white-space:nowrap}thead tr:first-child th{top:0;cursor:pointer}
thead tr.filters th{top:29px;cursor:default}.filters input{width:100%;box-sizing:border-box;min-width:7ch;font:inherit}
td{word-break:break-word;max-width:48ch}td.num{text-align:right}th[aria-sort=ascending]::after{content:" \\25B2"}th[aria-sort=descending]::after{content:" \\25BC"}
a{color:var(--accent)}
"""

SCRIPT = """
(function () {
  var table = document.getElementById('access'), body = table.tBodies[0];
  var rows = Array.prototype.slice.call(body.rows);
  var q = document.getElementById('q'), count = document.getElementById('count');
  var filters = Array.prototype.slice.call(document.querySelectorAll('.colfilter'));
  function apply() {
    var g = q.value.toLowerCase(), shown = 0;
    rows.forEach(function (r) {
      var ok = !g || r.textContent.toLowerCase().indexOf(g) >= 0;
      filters.forEach(function (f) {
        var v = f.value.toLowerCase();
        if (ok && v && r.cells[f.getAttribute('data-col')].textContent.toLowerCase().indexOf(v) < 0) ok = false;
      });
      r.hidden = !ok;
      if (ok) shown++;
    });
    count.textContent = 'Showing ' + shown + ' of ' + rows.length + ' rows';
  }
  function compare(a, b) {
    var x = parseFloat(a), y = parseFloat(b);
    if (!isNaN(x) && !isNaN(y) && String(x) === a && String(y) === b) return x - y;
    return a.toLowerCase() < b.toLowerCase() ? -1 : a.toLowerCase() > b.toLowerCase() ? 1 : 0;
  }
  Array.prototype.forEach.call(table.tHead.rows[0].cells, function (th, col) {
    th.addEventListener('click', function () {
      var dir = th.getAttribute('aria-sort') === 'ascending' ? -1 : 1;
      Array.prototype.forEach.call(th.parentNode.cells, function (c) { c.removeAttribute('aria-sort'); });
      th.setAttribute('aria-sort', dir === 1 ? 'ascending' : 'descending');
      rows.sort(function (a, b) { return dir * compare(a.cells[col].textContent, b.cells[col].textContent); });
      rows.forEach(function (r) { body.appendChild(r); });
    });
  });
  q.addEventListener('input', apply);
  filters.forEach(function (f) { f.addEventListener('input', apply); });
  apply();
})();
"""


def render_html(rows: list[AccessRow], title: str, csv_name: str, back: str = '') -> str:
    esc = html.escape
    head = ''.join(f'<th>{esc(label)}</th>' for _, label in COLUMNS)
    filt = ''.join(f'<th><input class="colfilter" data-col="{i}" aria-label="Filter {esc(label)}" placeholder="filter"></th>'
                   for i, (_, label) in enumerate(COLUMNS))
    body = '\n'.join('<tr>' + ''.join(f'<td class="{"num" if c == "covers" else ""}">{esc(str(getattr(r, c)))}</td>'
                                       for c, _ in COLUMNS) + '</tr>' for r in rows)
    links = f'<a href="{esc(csv_name)}">CSV</a>' + (f' · <a href="{esc(back)}">index</a>' if back else '')
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title><style>{STYLE}</style></head>
<body><h1>{esc(title)}</h1>
<p class="note">RBAC role assignments (Bicep declared and Azure observed): not effective access, group membership or observed
traffic. One row per assignment and environment. Subscription, management group and resource group scopes cover many
resources; <em>Elements covered</em> counts those drawn in the environment. {links}</p>
<input id="q" type="search" placeholder="Filter all columns" aria-label="Filter all columns"><span id="count" aria-live="polite"></span>
<div class="wrap"><table id="access"><thead><tr>{head}</tr><tr class="filters">{filt}</tr></thead>
<tbody>
{body}
</tbody></table></div>
<script>{SCRIPT}</script></body></html>
"""


def write_access(rows: list[AccessRow], stem_path: Path, title: str, back: str = '') -> tuple[Path, Path]:
    """Write <stem_path>-access.html and .csv; returns both paths."""
    table, data = stem_path.with_name(stem_path.name + '-access.html'), stem_path.with_name(stem_path.name + '-access.csv')
    table.parent.mkdir(parents=True, exist_ok=True)
    table.write_text(render_html(rows, title, data.name, back), encoding='utf-8')
    data.write_text(render_csv(rows), encoding='utf-8', newline='')
    return table, data


@dataclass
class IndexEntry:
    label: str
    nodes: int
    edges: int
    svg: str
    viewer: str = ''
    table: str = ''
    rows: int = 0


def render_index(title: str, entries: list[IndexEntry], notes: list[str]) -> str:
    esc = html.escape
    def link(href: str, text: str) -> str:
        return f'<a href="{esc(href)}">{esc(text)}</a>' if href else ''
    body = '\n'.join(
        f'<tr><td>{esc(e.label)}</td><td class="num">{e.nodes}</td><td class="num">{e.edges}</td>'
        f'<td>{link(e.svg, "SVG")}</td><td>{link(e.viewer, "viewer")}</td>'
        f'<td>{link(e.table, f"access table ({e.rows})") if e.table else ""}</td></tr>' for e in entries)
    extra = ''.join(f'<p class="note">{esc(n)}</p>' for n in notes)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title><style>{STYLE}</style></head>
<body><h1>{esc(title)}</h1>{extra}
<div class="wrap"><table><thead><tr><th>Diagram</th><th>Nodes</th><th>Edges</th><th>Diagram</th><th>Pan / zoom</th><th>Access</th></tr></thead>
<tbody>
{body}
</tbody></table></div></body></html>
"""
