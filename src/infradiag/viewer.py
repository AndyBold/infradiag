"""Write a self-contained HTML viewer next to a rendered SVG.

Browsers cap zoom (Edge stops at 500%), which is too little for the big application views. The viewer inlines the SVG
and a small pan/zoom script, so one .html file gives unlimited wheel/pinch zoom and drag panning. It needs no network
and no server, and opens straight from file:// or a \\\\wsl.localhost\\... path.

Re-laying-out a whole diagram's geometry on every zoom step is slow, so a gesture only moves the already-rendered SVG
as a GPU layer (a CSS transform) and the SVG is re-rendered crisply, by rewriting its viewBox, once the gesture has
been idle for a moment. Hover tooltips and Ctrl+F text search work on that settled SVG. The camera maths is in
viewer_logic.js, which is inlined into the page and unit-tested under node.
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
  #stage { position: fixed; inset: 0; overflow: hidden; touch-action: none; cursor: grab; outline: none; }
  #stage.dragging { cursor: grabbing; }
  #stage > svg { width: 100%; height: 100%; display: block; transform-origin: 0 0; }
  #stage.moving > svg { will-change: transform; }
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
<div id="stage" tabindex="0">
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
__LOGIC__
</script>
<script>
(function () {
  var L = ViewerLogic, SETTLE_MS = 150;
  var stage = document.getElementById('stage');
  var svg = stage.querySelector('svg');
  var label = document.getElementById('zoom');
  svg.removeAttribute('width');
  svg.removeAttribute('height');
  svg.setAttribute('preserveAspectRatio', 'xMinYMin meet');

  var W = 1, H = 1;       // viewport size in CSS pixels
  var bounds = null;      // the whole diagram, in SVG user units
  var base = null;        // camera the SVG is currently rendered (crisp) at
  var cam = null;         // camera the user has asked for; differs from base while a gesture is in flight
  var raf = 0;

  function measure() { W = stage.clientWidth || 1; H = stage.clientHeight || 1; }
  function readBounds() {
    var vb = svg.viewBox && svg.viewBox.baseVal;
    if (vb && vb.width && vb.height) return { x: vb.x, y: vb.y, w: vb.width, h: vb.height };
    var b = svg.getBBox();
    return { x: b.x, y: b.y, w: b.width || 1, h: b.height || 1 };
  }
  function show() { label.textContent = Math.round(cam.k * 100) + '%'; }

  // Crisp render: point the viewBox at the live camera and drop the transform that was standing in for it.
  function bake() {
    base = cam;
    svg.setAttribute('viewBox', L.viewBoxOf(cam, W, H).join(' '));
    svg.style.transform = '';
    stage.classList.remove('moving');
  }
  var settler = L.createSettler(bake, SETTLE_MS);

  function paint() {
    raf = 0;
    var t = L.relTransform(base, cam);
    svg.style.transform = 'translate(' + t.tx + 'px,' + t.ty + 'px) scale(' + t.s + ')';
  }
  // Called for every gesture step: move the cached layer only, and schedule the crisp render for when it goes quiet.
  function move(next) {
    cam = next;
    stage.classList.add('moving');
    if (!raf) raf = requestAnimationFrame(paint);
    settler.poke();
    show();
  }
  // For buttons, keys, double-click: same as a gesture step.
  function zoomBy(factor, sx, sy) { move(L.zoomAt(cam, sx === undefined ? W / 2 : sx, sy === undefined ? H / 2 : sy, factor)); }
  function fit() { move(L.fitCamera(bounds, W, H, 0.03)); }
  function actualSize() { move(L.actualCamera(cam, W, H)); }
  function localPoint(e) { var r = stage.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; }

  stage.addEventListener('wheel', function (e) {
    e.preventDefault();
    var p = localPoint(e);
    zoomBy(L.wheelFactor(e.deltaY, e.deltaMode, e.ctrlKey), p.x, p.y);
  }, { passive: false });

  // One pointer pans, two pinch-zoom (and pan with their midpoint). Pointer events cover mouse, pen and touch.
  var pointers = new Map(), lastPinch = null;
  function pinchState() {
    var a = Array.from(pointers.values());
    return { x: (a[0].x + a[1].x) / 2, y: (a[0].y + a[1].y) / 2, d: Math.hypot(a[0].x - a[1].x, a[0].y - a[1].y) };
  }
  stage.addEventListener('pointerdown', function (e) {
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    window.focus();  // inside an iframe the keys only reach us once the frame has focus
    stage.focus({ preventScroll: true });
    var p = localPoint(e);
    pointers.set(e.pointerId, p);
    try { stage.setPointerCapture(e.pointerId); } catch (err) { /* pointer already gone */ }
    stage.classList.add('dragging');
    lastPinch = pointers.size === 2 ? pinchState() : null;
  });
  stage.addEventListener('pointermove', function (e) {
    var prev = pointers.get(e.pointerId);
    if (!prev) return;
    var p = localPoint(e);
    pointers.set(e.pointerId, p);
    if (pointers.size === 1) {
      move(L.panBy(cam, p.x - prev.x, p.y - prev.y));
    } else if (pointers.size === 2 && lastPinch && lastPinch.d > 0) {
      var now = pinchState();
      var next = L.panBy(cam, now.x - lastPinch.x, now.y - lastPinch.y);
      move(L.zoomAt(next, now.x, now.y, now.d / lastPinch.d));
      lastPinch = now;
    }
  });
  function release(e) {
    if (!pointers.delete(e.pointerId)) return;
    lastPinch = pointers.size === 2 ? pinchState() : null;
    if (!pointers.size) stage.classList.remove('dragging');
  }
  stage.addEventListener('pointerup', release);
  stage.addEventListener('pointercancel', release);
  stage.addEventListener('dblclick', function (e) { var p = localPoint(e); zoomBy(2, p.x, p.y); });

  document.getElementById('zin').onclick = function () { zoomBy(1.5); };
  document.getElementById('zout').onclick = function () { zoomBy(1 / 1.5); };
  document.getElementById('fit').onclick = fit;
  document.getElementById('reset').onclick = actualSize;
  window.addEventListener('keydown', function (e) {
    if (e.ctrlKey || e.metaKey || e.altKey) return;  // leave Ctrl+F and the browser's own shortcuts alone
    if (e.key === '+' || e.key === '=') zoomBy(1.5);
    else if (e.key === '-') zoomBy(1 / 1.5);
    else if (e.key === 'f' || e.key === 'F') fit();
    else if (e.key === '0') actualSize();
  });

  // The window (or the master page's iframe) changed size: keep the camera, re-render at the new size.
  function resized() { measure(); settler.flush(); bake(); }
  if (window.ResizeObserver) new ResizeObserver(resized).observe(stage);
  else window.addEventListener('resize', resized);

  // Ctrl+F selects each match; if it is off-screen, bring it to the middle so searching actually finds it.
  document.addEventListener('selectionchange', function () {
    var sel = document.getSelection();
    if (!sel || !sel.rangeCount || sel.isCollapsed || !svg.contains(sel.anchorNode)) return;
    var r = sel.getRangeAt(0).getBoundingClientRect(), s = stage.getBoundingClientRect();
    if (!r.width && !r.height) return;
    if (r.left >= s.left && r.right <= s.right && r.top >= s.top && r.bottom <= s.bottom) return;
    move(L.panBy(cam, s.left + W / 2 - (r.left + r.right) / 2, s.top + H / 2 - (r.top + r.bottom) / 2));
  });

  measure();
  bounds = readBounds();
  cam = L.fitCamera(bounds, W, H, 0.03);
  bake();
  show();
})();
</script>
</body>
</html>
"""

_PROLOG = re.compile(r"^\s*(<\?xml[^>]*\?>\s*)?(<!DOCTYPE[^>]*>\s*)?", re.IGNORECASE)


def _asset(name: str) -> str:
    return resources.files("infradiag").joinpath(name).read_text(encoding="utf-8")


def viewer_html(svg_text: str, title: str) -> str:
    """The viewer page for `svg_text`: the SVG and the pan/zoom script inlined, with no external references."""
    svg = _PROLOG.sub("", svg_text, count=1).strip()
    title = title.replace("&", "&amp;").replace("<", "&lt;")
    # Replace placeholders in one pass so text inside the SVG or script cannot be re-substituted.
    parts = {"__TITLE__": title, "__SVG__": svg, "__LOGIC__": _asset("viewer_logic.js")}
    return re.sub("|".join(parts), lambda m: parts[m.group(0)], _TEMPLATE)


def write_viewer(svg_file: Path, title: str | None = None) -> Path:
    """Write `<stem>.html` beside `svg_file` and return its path. `title` labels the page (default: the file stem)."""
    html_file = svg_file.with_suffix(".html")
    html_file.write_text(viewer_html(svg_file.read_text(encoding="utf-8"), title or svg_file.stem), encoding="utf-8")
    return html_file
