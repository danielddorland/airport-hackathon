"""
Tests for webapi.py - the ops-dashboard API used by the Vercel web demo (runs in the browser
with Pyodide; here we run the same code in normal Python).

    .venv\\Scripts\\python tests\\test_webapi.py
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from autotrack.webapi import WebApp  # noqa: E402

APP = WebApp()


def call(method, url, body=None):
    status, text = APP.handle(method, url, None if body is None else json.dumps(body))
    return status, json.loads(text)


def test_layout_and_state_like_the_server():
    st, lay = call("GET", "/api/layout")
    assert st == 200 and lay["mode"] == "sim" and len(lay["nodes"]) == 28 and len(lay["scenarios"]) == 7
    for _ in range(40):
        APP.tick(0.05)                                   # 2 real seconds at 10x
    st, s = call("GET", "/api/state")
    assert st == 200 and s["t"] >= 19 and s["playing"] and s["mode"] == "sim"
    for key in ("bags", "alerts", "counts", "clock", "checkpoints_ready", "speed", "follow", "feed_run"):
        assert key in s, key


def test_checkpoints_then_instant_scenario_jump_and_bag_drawer():
    while APP.build_some(0.05):
        pass
    assert len(APP.sim.checkpoints) == 18
    sc = call("GET", "/api/layout")[1]["scenarios"][2]
    assert call("POST", "/api/control", {"cmd": "scenario", "value": sc["id"]}) == (200, {"ok": True})
    assert APP.sim.t == sc["start"] and APP.speed == 2 and APP.follow == (sc.get("other") or sc["bag"])
    for _ in range(100):
        APP.tick(0.05)
    st, bag = call("GET", f"/api/bag/{APP.follow}")
    assert st == 200 and bag["status"] and isinstance(bag["signals"], list)
    assert call("GET", "/api/bag/NOPE")[0] == 404


def test_feed_passengers_actions_controls():
    assert call("GET", "/api/events?after=0&limit=5")[0] == 200
    st, ps = call("GET", "/api/demo_passengers")
    assert st == 200 and ps
    assert call("GET", f"/api/passenger?booking={ps[0]['booking']}&plate={ps[0]['plate']}")[0] == 200
    assert call("GET", "/api/passenger?booking=X&plate=Y")[0] == 404
    assert call("POST", "/api/control", {"cmd": "pause"})[0] == 200 and not APP.playing
    t = APP.sim.t
    APP.tick(1.0)
    assert APP.sim.t == t                                # paused: the clock doesn't move
    assert call("POST", "/api/control", {"cmd": "speed", "value": 999})[0] == 200 and APP.speed == 120
    assert call("POST", "/api/control", {"cmd": "nope"})[0] == 400
    assert call("POST", "/api/action", {})[0] == 400
    assert call("POST", "/api/control", {"cmd": "restart"})[0] == 200 and APP.sim.t == 0


def test_hardware_endpoints_say_not_available():
    for url in ("/api/room", "/api/livetag", "/api/laptops"):
        st, out = call("GET", url)
        assert st == 404 and "web demo" in out["detail"]
    assert APP.handle("POST", "/api/control", "{")[0] == 400


if __name__ == "__main__":
    failed = 0
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        try:
            fn()
            print("PASS", name)
        except Exception as e:
            failed += 1
            print("FAIL", name, "->", type(e).__name__, e)
    print("\n{} passed, {} failed".format(len(tests) - failed, failed))
    sys.exit(1 if failed else 0)
