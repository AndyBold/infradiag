"""Site-wide navigation for the HTML pages under the output root.

Every render calls `refresh_nav(root)`, which scans the whole output root (so pages from earlier, separate runs are
included and deleted files drop out), writes `<root>/nav.js` and a landing page `<root>/index.html`, and makes sure every
infradiag page links the shared nav file with a relative `<script src>`.

Why a generated classic script: the pages are opened from `file://` (or a `\\\\wsl.localhost\\...` share) with no server, and
browsers block `fetch` of local JSON there, whereas a plain `<script src>` of a local file always loads. nav.js holds both
the scanned entries and the (small, dependency-free) code that draws the sidebar, so a page only needs the one tag; the
page's depth below the root is recovered from the script's own URL at run time.

Pages are discovered, not predicted: any `.html` under the root that looks like one of ours is listed under its folder with
its `<title>` as the label, so new file-name patterns (filter suffixes, new views) appear without changes here.
"""
from __future__ import annotations

import html
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

NAV_FILE = "nav.js"
LANDING = "index.html"
LANDING_MARK = "data-infradiag-landing"
HOME_MARK = "data-infradiag-home"
FRAME_MARK = "data-infradiag-frame"
KIND_ORDER = {"index": 0, "diagram": 1, "table": 2, "other": 3}

_TITLE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_HEAD_BYTES = 4096


@dataclass(frozen=True)
class Page:
    path: str  # posix, relative to the output root
    title: str
    kind: str  # index | diagram | table

    @property
    def folder(self) -> str:
        return self.path.rpartition("/")[0]

    @property
    def stem(self) -> str:
        return self.path.rpartition("/")[2].removesuffix(".html")


def classify(text: str, name: str) -> str | None:
    """What kind of infradiag page `text` is, or None for an HTML file that is not ours."""
    if LANDING_MARK in text[:_HEAD_BYTES]:
        return "landing"
    if 'id="stage"' in text and ("ViewerLogic" in text or "svgPanZoom" in text):  # current and older viewers
        return "diagram"
    if 'id="access"' in text:
        return "table"
    if name.endswith("-index.html") and "<table" in text:
        return "index"
    return None


def scan(root: Path) -> list[Page]:
    """Every infradiag page under `root` (the landing page excluded), sorted for stable output."""
    pages: list[Page] = []
    for path in sorted(root.rglob("*.html")):
        rel = path.relative_to(root).as_posix()
        if rel == LANDING:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        kind = classify(text, path.name)
        if kind is None or kind == "landing":
            continue
        found = _TITLE.search(text)
        title = html.unescape(found.group(1)).strip() if found else ""
        pages.append(Page(rel, title or path.stem, kind))
    return pages


def _item(page: Page) -> dict:
    return {"path": page.path, "title": page.title, "kind": page.kind, "children": []}


def _order(page: Page) -> tuple:
    return (page.stem.removesuffix("-access"), KIND_ORDER[page.kind], page.stem)


def build_tree(pages: list[Page]) -> list[dict]:
    """Folders, each with its pages; pages named `<P>-...` nest under the index page `<P>-index.html` of that folder."""
    folders: dict[str, list[Page]] = {}
    for page in pages:
        folders.setdefault(page.folder, []).append(page)
    result = []
    # Path-wise order, so a folder's subfolders follow it directly (a plain string sort would put `a-b` between `a` and `a/b`).
    for folder in sorted(folders, key=lambda f: (f != "", tuple(part.casefold() for part in f.split("/")))):
        members = sorted(folders[folder], key=_order)
        indexes = {p.stem.removesuffix("-index"): _item(p) for p in members if p.kind == "index"
                   and p.stem.endswith("-index")}
        top: list[dict] = []
        for page in members:
            item = indexes.get(page.stem.removesuffix("-index")) if page.kind == "index" else None
            item = item or _item(page)
            parents = [prefix for prefix in indexes if page.stem.startswith(prefix + "-") and item is not indexes.get(prefix)]
            if parents:
                indexes[max(parents, key=len)]["children"].append(item)
            else:
                top.append(item)
        group = {"name": folder or "(top level)", "items": top}
        parent = folder.rpartition("/")[0]
        if folder and "/" in folder and parent in folders:
            # e.g. `Sub/applications` under `Sub`: drawn indented below it, labelled by its own folder name
            group["label"], group["depth"] = folder.rpartition("/")[2], 1
        result.append(group)
    return result


def nav_js(pages: list[Page]) -> str:
    data = json.dumps({"groups": build_tree(pages)}, ensure_ascii=True, separators=(",", ":"))
    return "/* infradiag navigation data: generated by `infradiag render`; do not edit. */\n" \
           f"window.INFRADIAG_NAV = {data.replace('</', '<\\/')};\n"


def master_html(pages: list[Page]) -> str:
    esc = html.escape
    def li(item: dict) -> str:
        kids = "".join(li(c) for c in item["children"])
        sub = f"<ul>{kids}</ul>" if kids else ""
        return f'<li><a href="{esc(item["path"])}">{esc(item["title"])}</a> <small>{item["kind"]}</small>{sub}</li>'
    plain = "".join(f"<h2>{esc(g['name'])}</h2><ul>{''.join(li(i) for i in g['items'])}</ul>" for g in build_tree(pages))
    return (_MASTER.replace("__MARK__", LANDING_MARK).replace("__NAV__", NAV_FILE)
            .replace("__PLAIN__", plain).replace("__COUNT__", str(len(pages))))


def _home_tag(page_path: Path, root: Path) -> str:
    rel = page_path.relative_to(root).as_posix()
    base = os.path.relpath(root / LANDING, page_path.parent).replace(os.sep, "/")
    target = f"{base}#{quote(rel)}"
    return (f'<a {HOME_MARK} href="{html.escape(target)}" target="_top" title="Browse all diagrams and tables" '
            'style="position:fixed;right:10px;bottom:8px;z-index:20;font:12px system-ui,sans-serif;padding:3px 8px;'
            'border:1px solid #888;border-radius:12px;background:rgba(255,255,255,.92);color:#1565c0;text-decoration:none">'
            '&#9776; all diagrams</a>'
            # Inside the master page's iframe, tell it which page this is: after following a link within the frame the
            # master keeps its hash and highlight in step. postMessage works across file:// origins, which are opaque
            # in Chrome and Edge, so the master cannot read the frame's location itself.
            f'<script {FRAME_MARK}>if(window.parent!==window)try{{parent.postMessage({{infradiag:"nav",path:{json.dumps(rel)}}},"*")}}'
            'catch(e){}</script>')


def _ensure_home_link(path: Path, root: Path) -> bool:
    text = path.read_text(encoding="utf-8", errors="replace")
    tag = _home_tag(path, root)
    existing = re.search(rf"<a {HOME_MARK} .*?</a>(<script {FRAME_MARK}>.*?</script>)?", text, re.DOTALL)
    if existing:
        if existing.group(0) == tag:
            return False
        new = text[:existing.start()] + tag + text[existing.end():]
    else:
        end = text.rfind("</body>")
        new = text[:end] + tag + "\n" + text[end:] if end >= 0 else text + "\n" + tag + "\n"
    path.write_text(new, encoding="utf-8")
    return True


def refresh_nav(root: Path) -> list[Page]:
    """Rescan `root`, rewrite nav.js and the master page, and give every page its link back. Returns the pages found."""
    pages = scan(root)
    (root / NAV_FILE).write_text(nav_js(pages), encoding="utf-8")
    (root / LANDING).write_text(master_html(pages), encoding="utf-8")
    for page in pages:
        _ensure_home_link(root / page.path, root)
    return pages


_MASTER = r"""<!doctype html>
<html lang="en" __MARK__><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>infradiag: all diagrams and tables</title>
<style>
:root{color-scheme:light dark;--bg:#f4f6fa;--fg:#1d2433;--muted:#5b6472;--line:#cfd5df;--cur:#d9e6fb;--hover:#e6ebf3;--accent:#1565c0;--w:300px}
@media (prefers-color-scheme:dark){:root{--bg:#1b2029;--fg:#e4e8ef;--muted:#9aa3b2;--line:#343b49;--cur:#27406b;--hover:#262d3a;--accent:#7fb0ff}}
html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);font:13px/1.35 system-ui,sans-serif}
#nav{position:fixed;top:0;left:0;bottom:0;width:var(--w);box-sizing:border-box;display:flex;flex-direction:column;border-right:1px solid var(--line)}
body.collapsed #nav{display:none}body.collapsed #pane{left:0}
#head{display:flex;gap:6px;align-items:center;padding:8px;border-bottom:1px solid var(--line)}
#head strong{white-space:nowrap}#q{flex:1;min-width:0;font:inherit;padding:4px 6px;border:1px solid var(--line);border-radius:4px;background:transparent;color:inherit}
#list{flex:1;overflow:auto;padding:4px 0 12px;overscroll-behavior:contain}
details>summary{cursor:pointer;padding:5px 10px;font-weight:600}
ul{list-style:none;margin:0;padding:0}li ul{margin-left:12px;border-left:1px solid var(--line)}
a.item{display:flex;gap:6px;align-items:baseline;padding:3px 10px;color:inherit;text-decoration:none;word-break:break-word}
a.item:hover{background:var(--hover)}a.item[aria-current=page]{background:var(--cur);font-weight:600}
.kind{flex:none;font-size:10px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
#none{padding:8px 10px;color:var(--muted)}
#toggle{position:fixed;top:6px;z-index:5;left:calc(var(--w) - 36px);width:28px;height:28px;border:1px solid var(--line);border-radius:4px;background:var(--bg);color:var(--fg);cursor:pointer}
body.collapsed #toggle{left:8px}
#pane{position:fixed;top:0;right:0;bottom:0;left:var(--w);background:#fff}
#frame{width:100%;height:100%;border:0;display:block;background:#fff}
#welcome{position:absolute;inset:0;padding:24px 32px;background:var(--bg);overflow:auto}
#frame[hidden],#welcome[hidden]{display:none}
</style></head>
<body>
<nav id="nav" aria-label="Diagrams and tables"><div id="head"><strong>infradiag</strong><input id="q" type="search" placeholder="Filter" aria-label="Filter pages"></div>
<div id="list"><div id="none" hidden>No matching pages</div></div></nav>
<button id="toggle" type="button" aria-label="Hide navigation" title="Hide navigation">&laquo;</button>
<main id="pane"><iframe id="frame" title="Selected diagram or table" hidden></iframe>
<div id="welcome"><h1>infradiag output</h1><p>__COUNT__ page(s) found. Choose one on the left; it loads only when selected.</p>
<noscript>__PLAIN__</noscript></div></main>
<script src="__NAV__"></script>
<script>
(function () {
  'use strict';
  var DATA = window.INFRADIAG_NAV || { groups: [] };
  var frame = document.getElementById('frame'), welcome = document.getElementById('welcome');
  var q = document.getElementById('q'), list = document.getElementById('list'), none = document.getElementById('none');
  var toggle = document.getElementById('toggle');
  var links = {}, groups = [];
  function enc(p) { return p.split('/').map(encodeURIComponent).join('/'); }
  function build(item) {
    var li = document.createElement('li'), a = document.createElement('a');
    a.className = 'item'; a.href = '#' + enc(item.path); a.title = item.path;
    var t = document.createElement('span'), k = document.createElement('span');
    t.textContent = item.title; k.className = 'kind'; k.textContent = item.kind;
    a.appendChild(t); a.appendChild(k); li.appendChild(a);
    links[item.path] = a;
    var entry = { li: li, text: (item.title + ' ' + item.path).toLowerCase(), kids: [] };
    if (item.children.length) {
      var ul = document.createElement('ul');
      item.children.forEach(function (c) { var r = build(c); ul.appendChild(r.li); entry.kids.push(r); });
      li.appendChild(ul);
    }
    return entry;
  }
  DATA.groups.forEach(function (g) {
    var d = document.createElement('details'), s = document.createElement('summary'), ul = document.createElement('ul');
    s.textContent = g.label || g.name; d.appendChild(s); if (g.depth) d.style.marginLeft = '14px'; d.title = g.name; d.appendChild(ul); d.open = true;
    var items = g.items.map(build);
    items.forEach(function (r) { ul.appendChild(r.li); });
    list.insertBefore(d, none);
    groups.push({ d: d, items: items });
  });
  function filter(entry, needle) {
    var kid = false;
    entry.kids.forEach(function (k) { if (filter(k, needle)) kid = true; });
    var ok = !needle || entry.text.indexOf(needle) >= 0 || kid;
    entry.li.hidden = !ok;
    return ok;
  }
  q.addEventListener('input', function () {
    var needle = q.value.trim().toLowerCase(), any = false;
    groups.forEach(function (g) {
      var ok = false;
      g.items.forEach(function (r) { if (filter(r, needle)) ok = true; });
      g.d.hidden = !ok;
      if (needle) g.d.open = true;
      if (ok) any = true;
    });
    none.hidden = any;
  });
  var current = '';
  function mark(path) {
    Object.keys(links).forEach(function (p) { links[p].removeAttribute('aria-current'); });
    links[path].setAttribute('aria-current', 'page');
    links[path].scrollIntoView({ block: 'nearest' });
    document.title = links[path].title + ' - infradiag';
  }
  // A page in the frame announces itself after loading, so following a link inside the frame (index -> viewer) updates
  // the hash and highlight. Reading the frame's location instead would fail on file:// (opaque origins).
  window.addEventListener('message', function (e) {
    var d = e.data;
    if (e.source !== frame.contentWindow || !d || d.infradiag !== 'nav' || !links.hasOwnProperty(d.path)) return;
    if (d.path === current) return;
    current = d.path;
    mark(d.path);
    location.hash = '#' + enc(d.path);
  });
  function select() {
    var path = '';
    try { path = decodeURIComponent(location.hash.slice(1)); } catch (e) {}
    if (!links.hasOwnProperty(path)) {
      Object.keys(links).forEach(function (p) { links[p].removeAttribute('aria-current'); });
      current = ''; frame.hidden = true; welcome.hidden = false; document.title = 'infradiag: all diagrams and tables'; return;
    }
    mark(path);
    if (path === current) return;  // already showing it (the frame navigated itself and told us)
    current = path;
    // replace() so that moving between items adds one history entry (the hash), not two.
    frame.contentWindow.location.replace(enc(path));
    frame.hidden = false; welcome.hidden = true;
  }
  window.addEventListener('hashchange', select);
  toggle.addEventListener('click', function () {
    var c = document.body.classList.toggle('collapsed');
    toggle.innerHTML = c ? '&#9776;' : '&laquo;';
    toggle.title = toggle.ariaLabel = c ? 'Show navigation' : 'Hide navigation';
    try { localStorage.setItem('infradiag-nav-collapsed', c ? '1' : '0'); } catch (e) {}
  });
  try { if (localStorage.getItem('infradiag-nav-collapsed') === '1') toggle.click(); } catch (e) {}
  select();
})();
</script>
</body></html>
"""
