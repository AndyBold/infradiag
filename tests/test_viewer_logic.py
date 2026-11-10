"""The viewer's camera and settle logic (src/infradiag/viewer_logic.js), run under node."""

import json
import shutil
import subprocess
from importlib import resources

import pytest

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

LOGIC = str(resources.files("infradiag").joinpath("viewer_logic.js"))


def run(expr: str):
    script = f"const L = require({json.dumps(LOGIC)}); process.stdout.write(JSON.stringify({expr}))"
    return json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)


def test_zoom_keeps_the_point_under_the_cursor_fixed():
    cam = run("L.zoomAt({x: 10, y: 20, k: 2}, 300, 100, 4)")
    assert cam["k"] == 8
    # the user-space point under screen (300, 100) is the same before and after
    assert (10 + 300 / 2, 20 + 100 / 2) == (cam["x"] + 300 / cam["k"], cam["y"] + 100 / cam["k"])


def test_scale_is_clamped_but_effectively_unlimited():
    assert run("L.zoomAt({x: 0, y: 0, k: 1}, 0, 0, 1e12).k") == 100000
    assert run("L.zoomAt({x: 0, y: 0, k: 1}, 0, 0, 1e-12).k") == 0.01
    assert run("L.zoomAt({x: 0, y: 0, k: 1}, 0, 0, 5000).k") == 5000


def test_pan_moves_the_camera_the_opposite_way_in_user_units():
    assert run("L.panBy({x: 0, y: 0, k: 4}, 40, -8)") == {"x": -10, "y": 2, "k": 4}


def test_fit_centres_the_diagram_within_the_margin():
    cam = run("L.fitCamera({x: 0, y: 0, w: 1000, h: 500}, 800, 800, 0.1)")
    assert cam["k"] == pytest.approx(0.64)  # width-limited: 800 * 0.8 / 1000
    assert cam["x"] == pytest.approx(500 - 800 / (2 * 0.64))
    assert cam["y"] == pytest.approx(250 - 800 / (2 * 0.64))


def test_actual_size_is_one_pixel_per_unit_about_the_viewport_centre():
    cam = run("L.actualCamera({x: 100, y: 50, k: 0.5}, 800, 600)")
    assert cam["k"] == 1
    assert (cam["x"] + 400, cam["y"] + 300) == (100 + 800, 50 + 600)  # the centre (900, 650) did not move


def test_transform_maps_the_baked_render_onto_the_live_camera():
    base = {"x": 0, "y": 0, "k": 1}
    cam = {"x": 5, "y": -3, "k": 2}
    t = run(f"L.relTransform({json.dumps(base)}, {json.dumps(cam)})")
    assert t == {"tx": -10, "ty": 6, "s": 2}
    # a user point at baked screen position s0 must land where the live camera puts it
    w = (30, 40)
    screen_now = ((w[0] - cam["x"]) * cam["k"], (w[1] - cam["y"]) * cam["k"])
    screen_baked = ((w[0] - base["x"]) * base["k"], (w[1] - base["y"]) * base["k"])
    assert (screen_baked[0] * t["s"] + t["tx"], screen_baked[1] * t["s"] + t["ty"]) == screen_now


def test_identity_transform_when_nothing_moved():
    assert run("L.relTransform({x: 3, y: 4, k: 2}, {x: 3, y: 4, k: 2})") == {"tx": 0, "ty": 0, "s": 1}


def test_viewbox_covers_the_viewport():
    assert run("L.viewBoxOf({x: 1, y: 2, k: 4}, 800, 600)") == [1, 2, 200, 150]


def test_wheel_factor_direction_and_pinch_gain():
    assert run("L.wheelFactor(-100, 0, false)") > 1 > run("L.wheelFactor(100, 0, false)")
    assert run("L.wheelFactor(-5, 0, true)") > run("L.wheelFactor(-5, 0, false)")
    assert run("L.wheelFactor(-3, 1, false)") == run("L.wheelFactor(-48, 0, false)")  # line-mode deltas count as 16px
    assert run("L.wheelFactor(-1e9, 0, false)") == run("L.wheelFactor(-300, 0, false)")  # one event cannot jump the scale


def test_settler_runs_once_after_the_last_poke():
    out = run("""(() => {
      const timers = []; let now = 0, runs = [];
      const fake = { set: (fn, ms) => { timers.push({fn, at: now + ms, live: true}); return timers.length - 1; },
                     clear: (h) => { timers[h].live = false; } };
      const s = L.createSettler(() => runs.push(now), 150, fake);
      const advance = (to) => { now = to; timers.filter(t => t.live && t.at <= now).forEach(t => { t.live = false; t.fn(); }); };
      s.poke(); advance(100); s.poke(); advance(200); const before = runs.slice(); advance(260);
      const pendingAfter = s.pending(); s.poke(); s.flush();
      return { before, runs, pendingAfter, pendingFlushed: s.pending() };
    })()""")
    assert out == {"before": [], "runs": [260, 260], "pendingAfter": False, "pendingFlushed": False}


def test_overscan_draws_the_diagram_beyond_every_edge_of_the_viewport():
    box = run("L.overscanBox({x: 100, y: 50, k: 2}, 800, 600, 0.5)")
    assert (box["left"], box["top"], box["width"], box["height"]) == (-400, -300, 1600, 1200)
    assert (box["originX"], box["originY"]) == (400, 300)
    # viewBox starts half a viewport (in user units) before the camera and spans twice the viewport
    assert box["viewBox"] == [100 - 400 / 2, 50 - 300 / 2, 1600 / 2, 1200 / 2]
    # so the viewport's own top-left, at element offset (originX, originY), is still the camera origin
    assert box["viewBox"][0] + box["originX"] / 2 == 100
