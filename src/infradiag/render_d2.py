"""Render a View as D2 text, then to SVG with the d2 CLI (ELK layout)."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from textwrap import fill
from urllib.parse import quote

from .views import View, ViewNode

ICON_BASE = "https://icons.terrastruct.com/"

HEADER = """direction: right
classes: {
  bicep_only: {style.stroke-dash: 4}
  azure_only: {style: {stroke: "#d32f2f"; font-color: "#d32f2f"; stroke-width: 3}}
  unverified: {style: {stroke: "#f9a825"; stroke-dash: 2; stroke-width: 2}}
  stub: {style: {stroke: "#9e9e9e"; font-color: "#9e9e9e"; fill: "#f5f5f5"}}
  dashed: {style.stroke-dash: 3}
  private: {style: {stroke: "#1565c0"; stroke-dash: 3}}
}
"""


class D2NotFound(Exception):
    pass


def _q(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def icon_url(path: str) -> str:
    return ICON_BASE + quote(path, safe="")


def _emit(node: ViewNode, parent_path: str, depth: int, lines: list[str], paths: dict[str, str],
          icon_href: Callable[[str], str], style: str = "azure") -> None:
    path = f"{parent_path}.{node.key}" if parent_path else node.key
    paths[node.key] = path
    pad = "  " * depth
    label = node.label + (f"\n{node.sublabel}" if node.sublabel else "")
    if style == "c4":
        stereotype = {"application": "Application", "system": "Software system", "deployment": "Deployment node",
                      "group": "Group", "identity": "Managed identity", "principal": "Security principal",
                      "identity_resource": "Managed identity resource", "network": "Network boundary / association",
                      "component": "Application element"}.get(node.kind, "Infrastructure node")
        label = node.label + f"\n[{stereotype}]" + (f"\n{node.sublabel}" if node.sublabel else "")
    if node.annotations:
        label += "\n" + "\n".join(fill(a, 90) for a in node.annotations)
    if style == "c4":
        label = "\n".join(fill(line, 48) for line in label.splitlines())
    body = []
    if style == "c4":
        boundary = bool(node.children) or node.kind in ("application", "deployment", "group")
        body += [f'{pad}  style.fill: "{"#f2f6fc" if boundary else "#e3efff"}"',
                 f'{pad}  style.border-radius: 6']
        body.append(f'{pad}  style.font-size: 18')
        body.append(f'{pad}  width: {max(360, max(map(len, label.splitlines()), default=0) * 12 + 40)}')
        if boundary:
            body.append(f'{pad}  style.stroke-dash: 4')
    if node.kind in ("identity", "network"):
        body.append(f'{pad}  style.fill: "{"#f3e5f5" if node.kind == "identity" else "#e0f2f1"}"')
    if node.icon and style != "c4":
        body.append(f"{pad}  icon: {_q(icon_href(node.icon))}")
    if node.style != "normal":
        body.append(f"{pad}  class: {node.style}")
    if not body and not node.children:
        lines.append(f"{pad}{node.key}: {_q(label)}")
        return
    lines.append(f"{pad}{node.key}: {_q(label)} {{")
    lines.extend(body)
    for child in node.children:
        _emit(child, path, depth + 1, lines, paths, icon_href, style)
    lines.append(f"{pad}}}")


def render_d2(view: View, icon_href: Callable[[str], str] = icon_url, *, style: str = "azure") -> str:
    if style not in ("azure", "c4"):
        raise ValueError(f"unknown diagram style: {style}")
    lines = [HEADER.rstrip("\n"), f"title: {_q(view.title)} {{near: top-center; shape: text; style.font-size: 28}}"]
    paths: dict[str, str] = {}
    for node in view.nodes:
        _emit(node, "", 0, lines, paths, icon_href, style)
    for edge in view.edges:
        line = f"{paths[edge.source]} -> {paths[edge.target]}"
        label = edge.label or ("dependency" if style == "c4" else "")
        if style == "c4" or edge.kind != "dependency":
            label = "\n".join(fill(line, 72) for line in label.splitlines())
        attrs = []
        if edge.style != "solid":
            attrs.append(f"class: {edge.style}")
        color = {"grant": "#7b1fa2", "attachment": "#7b1fa2", "configured": "#7b1fa2", "network": "#00796b"}.get(edge.kind)
        if color:
            attrs.append(f'style.stroke: "{color}"')
        if label:
            line += f": {_q(label)}"
        if attrs:
            line += (" " if label else ": ") + "{" + "; ".join(attrs) + "}"
        lines.append(line)
    if style == "c4" or view.notes:
        legend = ("Legend\nC4 deployment presentation: labelled elements and nested boundaries.\n"
                  "Purple: identity attachment / configured use / RBAC grant. Teal: network association.\n"
                  "Drift: red = Azure only; dashed = Bicep only; amber = unverified; grey = out of scope.\n"
                  "Dashed dependency = endpoint reference; private endpoint presence does not imply exclusive access.")
        legend += "".join("\n\n" + fill(note, 110) for note in dict.fromkeys(view.notes))
        lines.append(f'legend: {_q(legend)} {{shape: text; style.font-size: 14}}')
    return "\n".join(lines) + "\n"


def render_svg(d2_text: str, d2_file: Path, svg_file: Path) -> None:
    d2_file.parent.mkdir(parents=True, exist_ok=True)
    d2_file.write_text(d2_text)
    exe = shutil.which("d2")
    if exe is None:
        raise D2NotFound(f"d2 not installed — `brew install d2` (see https://d2lang.com). Source written to {d2_file}")
    proc = subprocess.run([exe, "--layout", "elk", str(d2_file), str(svg_file)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"d2 failed for {d2_file}:\n{proc.stderr.strip()}")
