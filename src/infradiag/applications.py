"""Tag-based application projections with explicit, ordered membership overrides."""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from fnmatch import fnmatchcase

from .model import STUB, Graph, Node
from .rules import DEFAULT_ENVIRONMENT_RULES, Rules
from .views import EnvDecision, View, ViewEdge, ViewNode, ViewProjection, filter_graph, short_type

EXTERNAL = 'External dependencies'


def tags_for(node: Node) -> dict[str, str]:
    return {k.casefold(): str(v).strip() for k, v in node.tags.items()}


def decide_environment(n: Node, tags: dict[str, str], override: dict, override_index: int, config: dict) -> EnvDecision:
    """The environment of one resource and how it was decided, in priority order: an explicit override, the environment
    tag (a real tag is never replaced by a guess), the first environment rule that matches the resource's name, then the
    first that matches its resource group's name; otherwise 'unspecified'."""
    if override.get('environment'):
        return EnvDecision(override['environment'], 'override', f'applications.overrides[{override_index}]')
    tag = config.get('environment_tag', 'environment')
    if value := tags.get(tag.casefold()):
        return EnvDecision(value, 'tag', f'{tag}={value}')
    custom = 'environment_rules' in config
    rules = config['environment_rules'] if custom else DEFAULT_ENVIRONMENT_RULES
    for where, text in (('name', n.name), ('resource_group', n.resource_group)):
        for i, rule in enumerate(rules):
            if where in rule.get('on', ('name', 'resource_group')) and text and (m := re.search(rule['pattern'], text)):
                label = 'resource group' if where == 'resource_group' else 'name'
                rule_name = f'applications.environment_rules[{i}]' if custom else 'default environment rule'
                return EnvDecision(m.expand(rule['environment']), f'{label} pattern',
                                   f"{rule_name} matched '{m.group(0)}' in {text}")
    return EnvDecision('unspecified', 'unspecified', 'no environment tag, override or matching rule')


def _decision_tooltip(decisions: list[EnvDecision]) -> str:
    counts = Counter(d.text() for d in decisions)
    return 'Environment: ' + '\n'.join(t + (f' x{c}' if c > 1 else '') for t, c in sorted(counts.items())[:6]) + (
        f'\n+{len(counts) - 6} more' if len(counts) > 6 else '')


def _annotation(nodes: list[Node], keys: list[str]) -> tuple[str, ...]:
    result = []
    for key in keys:
        values = sorted({tags_for(n).get(key.casefold(), 'unspecified') for n in nodes})
        result.append(f'{key}: ' + (values[0] if len(values) == 1 else 'mixed (' + ', '.join(values) + ')'))
    return tuple(result)


def build_application(graph: Graph, rules: Rules, *, level: str,
                      subscription_names: dict[str, str]) -> ViewProjection:
    if level not in ('application', 'context'):
        raise ValueError(f'unknown application view: {level}')
    g = filter_graph(graph, rules)
    config = rules.applications
    groups = defaultdict(list)
    settings = {}
    decisions: dict[str, EnvDecision] = {}
    for rid, n in sorted(g.nodes.items()):
        tags = tags_for(n)
        index, override = next(((i, r) for i, r in enumerate(config.get('overrides', [])) if all(
            fnmatchcase(str(getattr(n, k)).casefold(), v.casefold()) for k, v in r['match'].items())), (-1, {}))
        app = override.get('application') or tags.get(config.get('application_tag', 'application').casefold()) or 'Unclassified infrastructure'
        decision = decide_environment(n, tags, override, index, config)
        env = decision.environment
        if STUB not in n.flags:
            decisions[rid] = decision
        if STUB in n.flags:
            app = EXTERNAL
        # Explicit group permits merging subscriptions; environments always remain distinct.
        scope = 'group:' + override['group'] if override.get('group') else n.subscription
        key = (scope.casefold(), env.casefold(), app.casefold())
        groups[key].append(n)
        settings[rid] = (app, env, override)
    roots, mapping = [], {}
    for i, (group, members) in enumerate(sorted(groups.items())):
        app, env, override = settings[members[0].id]
        scope_label = group[0][6:] if group[0].startswith('group:') else subscription_names.get(group[0], group[0])
        annotations = _annotation(members, config.get('annotations', []))
        shared = any(settings[n.id][2].get('shared') for n in members)
        boundary = ViewNode(f'a{i}', app, f'{env} | {scope_label}' + (' | shared service' if shared else '') +
                            ''.join('\n' + x for x in annotations), kind='system' if level == 'context' else 'application',
                            resource_ids=tuple(n.id for n in members) if level == 'context' else (),
                            environment='' if all(STUB in n.flags for n in members) else env,
                            tooltip=_decision_tooltip([decisions[n.id] for n in members if n.id in decisions])
                            if level == 'context' and any(n.id in decisions for n in members) else '')
        roots.append(boundary)
        components = defaultdict(list)
        for n in members:
            opts = settings[n.id][2]
            components[opts.get('component') or n.id].append(n)
        for j, component in enumerate(components.values()):
            first = component[0]
            opts = settings[first.id][2]
            role = opts.get('role') or ('Worker' if first.type == 'microsoft.app/jobs' else
                                       'Data store' if first.type.startswith(('microsoft.sql/', 'microsoft.storage/',
                                                                           'microsoft.dbforpostgresql/')) else '')
            key = boundary.key
            if level == 'application':
                key = f'r{i}_{j}'
                label = opts.get('component') or first.name
                technologies = ', '.join(sorted({short_type(n.type) for n in component}))
                boundary.children.append(ViewNode(key, label + (f' ×{len(component)}' if len(component) > 1 else ''),
                                                  (role + ' | ' if role else '') + technologies,
                                                  icon=rules.icons.get(first.type), kind='component',
                                                  resource_ids=tuple(n.id for n in component),
                                                  annotations=_annotation(component, config.get('annotations', [])),
                                                  tooltip=_decision_tooltip([decisions[n.id] for n in component if n.id in decisions])
                                                  if any(n.id in decisions for n in component) else ''))
            mapping.update((n.id, key) for n in component)
    edges = {ViewEdge(mapping[e.source], mapping[e.target], e.label if e.kind == 'collapsed' else 'dependency',
                      'dashed' if e.kind == 'endpoint' else 'solid') for e in g.edges
             if e.source in mapping and e.target in mapping and mapping[e.source] != mapping[e.target]}
    notes = []
    if any(not n.tags for n in g.nodes.values() if STUB not in n.flags):
        notes.append('Some resources have no tags; membership is unclassified unless overridden. Refresh extract for old caches.')
    return ViewProjection(View('Application architecture' if level == 'application' else 'System context', roots,
                               sorted(edges), notes), mapping, decisions)
