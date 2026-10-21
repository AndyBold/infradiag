"""Icon caches: the icons bundled with infradiag cover the default views; `infradiag icons` downloads the rest into the
project's cache, so render runs without the network."""

from __future__ import annotations

import re
import urllib.request
from collections.abc import Callable
from pathlib import Path

from .render_d2 import icon_url
from .rules import Rules

BUNDLED_ICONS = Path(__file__).parent / "icons"


def cache_path(icon_dir: Path, path: str) -> Path:
    """Where an icon is cached. d2 can't bundle local files whose path has spaces, so slug each segment."""
    *dirs, name = path.split("/")
    stem, dot, ext = name.rpartition(".")
    slug = lambda s: re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")  # noqa: E731
    return icon_dir.joinpath(*(slug(d) for d in dirs), f"{slug(stem)}{dot}{ext}" if dot else slug(name))


def _download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read()


def update_icons(rules: Rules, icon_dir: Path, fetch: Callable[[str], bytes] | None = None,
                 base_dir: Path | None = None) -> dict[str, str]:
    """Fetch every icon the views file lists into icon_dir, overwriting. Returns icon path -> error for failures."""
    fetch = fetch or _download
    failures: dict[str, str] = {}
    for path in dict.fromkeys(rules.icons.values()):
        if base_dir is not None and (base_dir / path).is_file():
            continue  # a custom icon already on disk, not something to download
        try:
            data = fetch(icon_url(path))
        except Exception as exc:  # noqa: BLE001 - one bad icon shouldn't stop the rest
            failures[path] = str(exc)
            continue
        target = cache_path(icon_dir, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return failures


class IconResolver:
    """Maps a views-file icon value to what D2 loads: a custom file, a cached copy (the project's, then the bundled
    one), or the remote URL."""

    def __init__(self, base_dir: Path, icon_dir: Path, bundled_dir: Path | None = BUNDLED_ICONS):
        self.base_dir = base_dir.resolve()  # d2 resolves relative icon paths from the .d2 file's folder
        self.icon_dirs = [d.resolve() for d in (icon_dir, bundled_dir) if d is not None]
        self.missing: set[str] = set()

    def __call__(self, path: str) -> str:
        for candidate in (self.base_dir / path, *(cache_path(d, path) for d in self.icon_dirs)):
            if candidate.is_file():
                return str(candidate)
        self.missing.add(path)
        return icon_url(path)
