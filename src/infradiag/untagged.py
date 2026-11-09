"""The report of resources whose environment was inferred rather than tagged, and of those still unspecified.

It is the to-do list for tagging the infrastructure properly (e.g. in Bicep): each row says what decided the
environment and, for an inferred one, which tag would make that decision explicit."""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path

from .model import Graph
from .views import EnvDecision

HEADER = ['Resource', 'Resource type', 'Subscription', 'Resource group', 'Environment', 'Decided by', 'Rule / evidence',
          'Suggested tag']


@dataclass(frozen=True)
class UntaggedReport:
    inferred: int
    unspecified: int
    by_source: dict[str, int]

    def summary(self, filename: str) -> str:
        parts = ', '.join(f'{n} by {s}' for s, n in sorted(self.by_source.items()))
        return (f'Environment: {self.inferred} resource(s) have no environment tag and were placed by rule ({parts or "none"}); '
                f'{self.unspecified} stay unspecified. Listed in {filename}.')


def build_untagged_rows(decisions: dict[str, EnvDecision], graph: Graph, tag: str = 'environment') -> list[list[str]]:
    rows = []
    for rid, d in sorted(decisions.items(), key=lambda kv: (kv[1].environment.casefold(), kv[0])):
        if d.source not in ('name pattern', 'resource group pattern', 'unspecified'):
            continue
        n = graph.nodes[rid]
        rows.append([n.name, n.type, n.subscription, n.resource_group, d.environment, d.source, d.detail,
                     f'{tag}={d.environment}' if d.inferred else ''])
    return rows


def write_untagged(decisions: dict[str, EnvDecision], graph: Graph, path: Path, tag: str = 'environment') -> UntaggedReport:
    rows = build_untagged_rows(decisions, graph, tag)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator='\n')
    writer.writerow(HEADER)
    writer.writerows(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(out.getvalue(), encoding='utf-8', newline='')
    sources = [r[5] for r in rows if r[5] != 'unspecified']
    return UntaggedReport(len(sources), sum(r[5] == 'unspecified' for r in rows),
                          {s: sources.count(s) for s in sorted(set(sources))})
