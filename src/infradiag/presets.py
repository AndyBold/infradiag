"""Named sets of renders for `infradiag build`.

A preset is a tuple of `BuildStep`s, each one `infradiag render` invocation spelled out as data. To add a diagram to the
full set, append a step to `FULL` (or `FOCUSED_VARIANTS` for an identity-overlay filter variant); nothing else needs to
change. To add another preset, add a tuple to `PRESETS`.
"""
from __future__ import annotations

from dataclasses import dataclass

from .filters import GrantFilter


@dataclass(frozen=True)
class BuildStep:
    name: str                          # shown in progress and the summary
    view: str                          # detailed | summary | application | context
    style: str = "azure"               # azure | c4
    overlay: str = "none"              # none | identity | network | all
    split: str | None = None           # "environment": one diagram per environment (application/context only)
    hide_scopes: tuple[str, ...] = ()  # identity-overlay filters, as for `render`
    role_plane: str = ""
    drift_only: bool = False

    @property
    def combined(self) -> bool:
        """Application and context views merge every selected subscription into one set of diagrams."""
        return self.view in ("application", "context")

    @property
    def grant_filter(self) -> GrantFilter:
        return GrantFilter(frozenset(self.hide_scopes), self.role_plane, drift_only=self.drift_only)

    def command(self) -> str:
        """The equivalent `infradiag render` flags, for --dry-run and the docs."""
        parts = [f"--view {self.view}"]
        if self.style != "azure":
            parts.append(f"--style {self.style}")
        if self.overlay != "none":
            parts.append(f"--security-overlay {self.overlay}")
        if self.split:
            parts.append(f"--split {self.split}")
        parts += [f"--hide-scope {s}" for s in self.hide_scopes]
        if self.role_plane:
            parts.append(f"--role-plane {self.role_plane}")
        if self.drift_only:
            parts.append("--drift-only")
        return " ".join(parts)


# Focused variants: the same grants, drawn less. Each lands beside the full diagram under a filter suffix.
FOCUSED_VARIANTS: tuple[BuildStep, ...] = (
    BuildStep("application view, data-plane roles only, no subscription/management-group grants", "application",
              style="c4", overlay="identity", split="environment", hide_scopes=("subscription", "management-group"),
              role_plane="data"),
    BuildStep("application view, drift only (grants one source evidences alone)", "application",
              style="c4", overlay="identity", split="environment", drift_only=True),
)

FULL: tuple[BuildStep, ...] = (
    BuildStep("detailed topology + all security overlays", "detailed", overlay="all"),
    BuildStep("architecture overview + all security overlays", "summary", overlay="all"),
    BuildStep("application view, by environment (C4, identity)", "application", style="c4", overlay="identity",
              split="environment"),
    BuildStep("context view, by environment (C4, identity)", "context", style="c4", overlay="identity",
              split="environment"),
    *FOCUSED_VARIANTS,
)

PRESETS: dict[str, tuple[BuildStep, ...]] = {"full": FULL}
