/* Pure camera and timing logic for the diagram viewer. No DOM access, so it is unit-tested under node.
 *
 * A camera is { x, y, k }: the SVG user-space point at the top-left corner of the viewport, and k screen pixels per
 * user unit. The viewer keeps a "baked" camera that the SVG's viewBox currently renders crisply. A gesture only moves
 * the live camera; the page shows that as a CSS transform on the already-rendered SVG layer (relTransform), and
 * re-renders crisply at the live camera once the gesture has been idle (createSettler).
 */
var ViewerLogic = (function () {
  var MIN_SCALE = 0.01, MAX_SCALE = 100000;

  function clampScale(k, min, max) {
    return Math.min(max === undefined ? MAX_SCALE : max, Math.max(min === undefined ? MIN_SCALE : min, k));
  }

  /* Scale by `factor` keeping the user-space point under screen position (sx, sy) fixed. */
  function zoomAt(cam, sx, sy, factor) {
    var k = clampScale(cam.k * factor);
    return { x: cam.x + sx / cam.k - sx / k, y: cam.y + sy / cam.k - sy / k, k: k };
  }

  /* Drag the diagram by (dx, dy) screen pixels. */
  function panBy(cam, dx, dy) {
    return { x: cam.x - dx / cam.k, y: cam.y - dy / cam.k, k: cam.k };
  }

  /* Scale so `bounds` {x, y, w, h} fits a W x H viewport with `margin` (fraction of the viewport) spare, centred. */
  function fitCamera(bounds, W, H, margin) {
    var m = 1 - 2 * (margin || 0);
    var k = clampScale(Math.min((W * m) / bounds.w, (H * m) / bounds.h));
    return { x: bounds.x + bounds.w / 2 - W / (2 * k), y: bounds.y + bounds.h / 2 - H / (2 * k), k: k };
  }

  /* One screen pixel per user unit (100%), keeping the viewport centre where it is. */
  function actualCamera(cam, W, H) {
    return { x: cam.x + W / (2 * cam.k) - W / 2, y: cam.y + H / (2 * cam.k) - H / 2, k: 1 };
  }

  /* The CSS transform (origin 0 0) that makes the SVG rendered at `base` look like it is at `cam`. */
  function relTransform(base, cam) {
    return { tx: (base.x - cam.x) * cam.k, ty: (base.y - cam.y) * cam.k, s: cam.k / base.k };
  }

  function viewBoxOf(cam, W, H) {
    return [cam.x, cam.y, W / cam.k, H / cam.k];
  }

  /* Zoom factor for a wheel event. Pinch gestures arrive as ctrl+wheel with small deltas, so they get more gain. */
  function wheelFactor(deltaY, deltaMode, ctrlKey) {
    var px = deltaMode === 1 ? deltaY * 16 : deltaMode === 2 ? deltaY * 400 : deltaY;
    px = Math.max(-300, Math.min(300, px));
    return Math.exp(-px * (ctrlKey ? 0.01 : 0.0025));
  }

  /* Debounce: `settle` runs `delay` ms after the last poke(). Timer functions are injectable for tests. */
  function createSettler(settle, delay, timers) {
    var t = timers || { set: setTimeout, clear: clearTimeout };
    var handle = null;
    function run() { handle = null; settle(); }
    return {
      poke: function () { if (handle !== null) t.clear(handle); handle = t.set(run, delay); },
      flush: function () { if (handle !== null) { t.clear(handle); run(); } },
      pending: function () { return handle !== null; }
    };
  }

  return {
    MIN_SCALE: MIN_SCALE, MAX_SCALE: MAX_SCALE, clampScale: clampScale, zoomAt: zoomAt, panBy: panBy,
    fitCamera: fitCamera, actualCamera: actualCamera, relTransform: relTransform, viewBoxOf: viewBoxOf,
    wheelFactor: wheelFactor, createSettler: createSettler
  };
})();
if (typeof module !== 'undefined') module.exports = ViewerLogic;
