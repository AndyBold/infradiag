"""Write a self-contained HTML viewer next to a rendered SVG.

Browsers cap zoom (Edge stops at 500%), which is too little for the big application views. The viewer inlines the SVG
and a vendored copy of svg-pan-zoom (see vendor/README.md), so one .html file gives unlimited wheel/pinch zoom and drag
panning. It needs no network and no server, and opens straight from file:// or a \\\\wsl.localhost\\... path.
"""

from __future__ import annotations

import re
from importlib import resources
from pathlib import Path

_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  html, body { margin: 0; height: 100%; overflow: hidden; background: #fff; font-family: sans-serif; }
  #stage { position: fixed; inset: 0; }
  #stage > svg { width: 100%; height: 100%; display: block; }
  #controls { position: fixed; top: 10px; right: 10px; z-index: 10; display: flex; gap: 4px; align-items: center;
              background: rgba(255,255,255,.92); border: 1px solid #bbb; border-radius: 6px; padding: 4px; }
  #controls button { font-size: 16px; min-width: 32px; height: 32px; cursor: pointer; border: 1px solid #999;
                     border-radius: 4px; background: #f4f4f4; }
  #controls button:hover { background: #e4e4e4; }
  #zoom { font-size: 12px; min-width: 52px; text-align: center; color: #333; }
  #hint { position: fixed; left: 10px; bottom: 8px; font-size: 12px; color: #666; z-index: 10; }
</style>
</head>
<body>
<div id="stage">
__SVG__
</div>
<div id="controls">
  <button id="zin" title="Zoom in (+)">+</button>
  <button id="zout" title="Zoom out (-)">&minus;</button>
  <button id="fit" title="Fit to window (F)">Fit</button>
  <button id="reset" title="Actual size, 100% (0)">1:1</button>
  <span id="zoom"></span>
</div>
<div id="hint">Scroll to zoom &middot; drag to pan &middot; double-click to zoom in &middot; keys: + &minus; F 0</div>
<script>
/*! svg-pan-zoom 3.6.2 - https://github.com/bumbu/svg-pan-zoom - BSD-2-Clause licence follows
__LICENSE__
*/
__LIBRARY__
</script>
<script>
(function () {
  var svg = document.querySelector('#stage > svg');
  svg.removeAttribute('width');
  svg.removeAttribute('height');
  // svg-pan-zoom has no pinch of its own, so touch is handled here: one finger pans, two fingers pinch-zoom.
  var touchHandler = {
    haltEventListeners: ['touchstart', 'touchend', 'touchmove', 'touchleave', 'touchcancel'],
    init: function (o) {
      var inst = o.instance, el = o.svgElement, last = null;
      function pts(e) {
        var r = el.getBoundingClientRect(), out = [];
        for (var i = 0; i < e.touches.length; i++) out.push({ x: e.touches[i].clientX - r.left, y: e.touches[i].clientY - r.top });
        return out;
      }
      function dist(a) { return Math.hypot(a[0].x - a[1].x, a[0].y - a[1].y); }
      function mid(a) { return { x: (a[0].x + a[1].x) / 2, y: (a[0].y + a[1].y) / 2 }; }
      function start(e) { e.preventDefault(); last = pts(e); }
      function move(e) {
        e.preventDefault();
        var now = pts(e);
        if (last && now.length === 1 && last.length === 1) {
          inst.panBy({ x: now[0].x - last[0].x, y: now[0].y - last[0].y });
        } else if (last && now.length >= 2 && last.length >= 2 && dist(last) > 0) {
          var m = mid(now), lm = mid(last);
          inst.panBy({ x: m.x - lm.x, y: m.y - lm.y });
          inst.zoomAtPointBy(dist(now) / dist(last), m);
        }
        last = now;
      }
      function end(e) { last = e.touches.length ? pts(e) : null; }
      el.addEventListener('touchstart', start, { passive: false });
      el.addEventListener('touchmove', move, { passive: false });
      el.addEventListener('touchend', end);
      el.addEventListener('touchcancel', end);
      this.destroy = function () {
        el.removeEventListener('touchstart', start);
        el.removeEventListener('touchmove', move);
        el.removeEventListener('touchend', end);
        el.removeEventListener('touchcancel', end);
      };
    },
    destroy: function () {}
  };
  var pz = svgPanZoom(svg, {
    zoomEnabled: true, panEnabled: true, controlIconsEnabled: false, dblClickZoomEnabled: true,
    mouseWheelZoomEnabled: true, preventMouseEventsDefault: true,
    fit: true, center: true, customEventsHandler: touchHandler,
    minZoom: 0.01, maxZoom: 100000, zoomScaleSensitivity: 0.3
  });
  var label = document.getElementById('zoom');
  // Real scale: screen pixels per SVG user unit (getZoom() alone is relative to the fitted size; getSizes().realZoom already includes it).
  function scale() { return pz.getSizes().realZoom; }  // already includes the current zoom
  function show() { label.textContent = Math.round(scale() * 100) + '%'; }
  function actualSize() { pz.zoom(pz.getZoom() / pz.getSizes().realZoom); pz.center(); show(); }
  function fit() { pz.resize(); pz.fit(); pz.center(); show(); }
  pz.setOnZoom(show);
  document.getElementById('zin').onclick = function () { pz.zoomIn(); };
  document.getElementById('zout').onclick = function () { pz.zoomOut(); };
  document.getElementById('fit').onclick = fit;
  document.getElementById('reset').onclick = actualSize;
  window.addEventListener('resize', function () { pz.resize(); });
  window.addEventListener('keydown', function (e) {
    if (e.key === '+' || e.key === '=') pz.zoomIn();
    else if (e.key === '-') pz.zoomOut();
    else if (e.key === 'f' || e.key === 'F') fit();
    else if (e.key === '0') actualSize();
  });
  show();
})();
</script>
</body>
</html>
"""

_PROLOG = re.compile(r"^\s*(<\?xml[^>]*\?>\s*)?(<!DOCTYPE[^>]*>\s*)?", re.IGNORECASE)


def _vendor(name: str) -> str:
    return resources.files("infradiag").joinpath("vendor", name).read_text(encoding="utf-8")


def viewer_html(svg_text: str, title: str) -> str:
    """The viewer page for `svg_text`: the SVG and the pan/zoom library inlined, with no external references."""
    svg = _PROLOG.sub("", svg_text, count=1).strip()
    title = title.replace("&", "&amp;").replace("<", "&lt;")
    license_text = _vendor("svg-pan-zoom.LICENSE").replace("*/", "* /")
    # Replace placeholders in one pass so text inside the SVG or library cannot be re-substituted.
    parts = {"__TITLE__": title, "__SVG__": svg, "__LICENSE__": license_text, "__LIBRARY__": _vendor("svg-pan-zoom.min.js")}
    return re.sub("|".join(parts), lambda m: parts[m.group(0)], _TEMPLATE)


def write_viewer(svg_file: Path, title: str | None = None) -> Path:
    """Write `<stem>.html` beside `svg_file` and return its path. `title` labels the page (default: the file stem)."""
    html_file = svg_file.with_suffix(".html")
    html_file.write_text(viewer_html(svg_file.read_text(encoding="utf-8"), title or svg_file.stem), encoding="utf-8")
    return html_file
