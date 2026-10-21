"""Render a View as D2 text, then to SVG with the d2 CLI (ELK layout)."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
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
          icon_href: Callable[[str], str]) -> None:
    path = f"{parent_path}.{node.key}" if parent_path else node.key
    paths[node.key] = path
    pad = "  " * depth
    label = node.label + (f"\n{node.sublabel}" if node.sublabel else "")
    body = []
    if node.icon:
        body.append(f"{pad}  icon: {_q(icon_href(node.icon))}")
    if node.style != "normal":
        body.append(f"{pad}  class: {node.style}")
    if not body and not node.children:
        lines.append(f"{pad}{node.key}: {_q(label)}")
        return
    lines.append(f"{pad}{node.key}: {_q(label)} {{")
    lines.extend(body)
    for child in node.children:
        _emit(child, path, depth + 1, lines, paths, icon_href)
    lines.append(f"{pad}}}")


def render_d2(view: View, icon_href: Callable[[str], str] = icon_url) -> str:
    lines = [HEADER.rstrip("\n"), f"title: {_q(view.title)} {{near: top-center; shape: text; style.font-size: 28}}"]
    paths: dict[str, str] = {}
    for node in view.nodes:
        _emit(node, "", 0, lines, paths, icon_href)
    for edge in view.edges:
        line = f"{paths[edge.source]} -> {paths[edge.target]}"
        if edge.label and edge.style != "solid":
            line += f": {_q(edge.label)} {{class: {edge.style}}}"
        elif edge.label:
            line += f": {_q(edge.label)}"
        elif edge.style != "solid":
            line += f": {{class: {edge.style}}}"
        lines.append(line)
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
