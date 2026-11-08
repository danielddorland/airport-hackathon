"""
webapi.py - the ops-dashboard API without a server (for the Vercel web demo).

The web demo (autotrack/web/, built by tools/build_web.py) runs the REAL airport simulation
(sim.py + tracker.py + positioning.py ...) inside the visitor's browser with Pyodide, in a Web
Worker. The page's code is unchanged: a small shim turns its fetch("/api/...") calls into
calls to handle() below, which answers exactly like server.py does for the ops dashboard.

Not included (they need a laptop's Bluetooth): Live ESP32, Room demo, laptop calibration.

Also runs in normal Python, so it can be tested without a browser (tests/test_webapi.py).
"""

import copy
import json
import time
from urllib.parse import parse_qs, urlsplit

from .sim import STATE_FIELDS, Simulation

NOT_HERE = "Not available in the web demo (needs the real laptop + ESP32). Run AutoTrack locally for this."


class WebApp:
    def __init__(self, speed=10.0):
        self.sim = Simulation(use_db=False)
        self.playing = True
        self.speed = speed
        self.follow = None
        self._owed = 0.0
        times = [sc["start"] for sc in self.sim.world.scenarios] + list(range(300, self.sim.world.total_ticks, 300))
        self._cp_targets = sorted(set(times))
        self._cp_i = 0
        self._helper = None

    # ------------------------------------------------------------------ clock (called ~20x a second)
    def tick(self, dt):
        """Advance the simulation by dt real seconds x speed (like server.clock_loop)."""
        s = self.sim
        if not self.playing or (s.t >= s.world.total_ticks and not s.live_active()):
            self._owed = 0.0
            return
        self._owed += max(0.0, float(dt)) * self.speed
        n = min(int(self._owed), 200)
        self._owed -= n
        for _ in range(n):
            if s.t >= s.world.total_ticks:
                break
            s.step()
        s.feed._pending.clear()          # no database in the browser: don't let the queue grow

    # ------------------------------------------------------------------ checkpoints, a little at a time
    def build_some(self, budget_s=0.03):
        """Build the jump checkpoints in small slices so the worker stays responsive.
        Same result as Simulation.build_checkpoints. Returns True while there is work left."""
        if self._cp_i >= len(self._cp_targets):
            return False
        s = self.sim
        if self._helper is None:
            self._helper = Simulation(smoothing=s.smoothing, use_db=False, seed=s.seed,
                                      layout=s.layout, world=s.world)
        h = self._helper
        end = time.perf_counter() + budget_s
        while self._cp_i < len(self._cp_targets) and time.perf_counter() < end:
            target = self._cp_targets[self._cp_i]
            if h.t < target:
                h.step()
                h.feed._pending.clear()
                if h.t % 5 == 0:
                    h._auto_staff()
                continue
            h.tracker.feed = s.feed          # the copy must point at the main feed (see build_checkpoints)
            s.checkpoints[target] = copy.deepcopy({k: getattr(h, k) for k in STATE_FIELDS}, s._memo())
            h.tracker.feed = h.feed
            self._cp_i += 1
        if self._cp_i >= len(self._cp_targets):
            self._helper = None
            return False
        return True

    # ------------------------------------------------------------------ requests
    def handle(self, method, url, body=None):
        """(method, '/api/...?...', JSON text or None) -> (status, JSON text)."""
        parts = urlsplit(url)
        path, q = parts.path, {k: v[-1] for k, v in parse_qs(parts.query).items()}
        try:
            data = json.loads(body) if body else {}
        except ValueError:
            return 400, json.dumps({"detail": "invalid JSON"})
        try:
            status, out = self._route(method.upper(), path, q, data)
        except Exception as e:  # never let one request kill the demo
            status, out = 500, {"detail": f"{type(e).__name__}: {e}"}
        return status, json.dumps(out)

    def _route(self, m, path, q, data):
        s = self.sim
        if m == "GET" and path == "/api/layout":
            return 200, {**s.layout.to_json(), "scenarios": s.world.scenarios, "mode": "sim"}
        if m == "GET" and path == "/api/state":
            snap = dict(s.snapshot())
            snap.update(checkpoints_ready=len(s.checkpoints) > 0, playing=self.playing, speed=self.speed,
                        busy=None, mode="sim", smoothing=s.smoothing, follow=self.follow,
                        feed_run=s.feed.run_id)
            return 200, snap
        if m == "GET" and path.startswith("/api/bag/"):
            b = s.tracker.bags.get(path[len("/api/bag/"):])
            if not b:
                return 404, {"detail": "no such bag"}
            d = b.to_json()
            est = s.tracker.estimators.get(b.tag)
            d["signals"] = ([{"zone": s.layout.receivers()[r]["zone"], "rssi": round(v)}
                             for r, (v, _) in sorted(est.smoothed.items(), key=lambda kv: -kv[1][0])] if est else [])
            return 200, d
        if m == "GET" and path == "/api/events":
            return 200, s.feed.since(int(q.get("after", 0)), int(q.get("limit", 200)))
        if m == "GET" and path == "/api/passenger":
            r = s.tracker.passenger(q.get("booking", ""), q.get("plate", ""))
            if not r:
                return 404, {"detail": "No bag found for that booking reference and bag tag number."}
            return 200, r
        if m == "GET" and path == "/api/demo_passengers":
            out = []
            for sc in s.world.scenarios:
                for bid in [sc["bag"], sc.get("other")]:
                    b = s.tracker.bags.get(bid) if bid else None
                    if b:
                        out.append({"scenario": sc["title"], "bag": bid, "booking": b.booking_ref, "plate": b.plate})
            return 200, out
        if m == "POST" and path == "/api/control":
            return self._control(data)
        if m == "POST" and path == "/api/action":
            if not data.get("bag") or not data.get("action"):
                return 400, {"detail": "need 'bag' and 'action'"}
            s.action(data["bag"], data["action"], data.get("who", "ops"))
            return 200, {"ok": True}
        return 404, {"detail": NOT_HERE}

    def _control(self, body):
        s = self.sim
        cmd = body.get("cmd")
        if cmd == "play":
            self.playing = True
        elif cmd == "pause":
            self.playing = False
        elif cmd == "speed":
            self.speed = _speed(body.get("value", 10))
        elif cmd == "seek":
            s.seek(int(body.get("value", 0)))
        elif cmd == "scenario":
            sc = next((x for x in s.world.scenarios if x["id"] == body.get("value")), None)
            if not sc:
                return 404, {"detail": "no such scenario"}
            self.follow = sc.get("other") or sc["bag"]
            s.seek(sc["start"])
            self.speed = _speed(body.get("speed", 2))
            self.playing = True
        elif cmd == "restart":
            s.reset()
        elif cmd == "follow":
            self.follow = body.get("value")
        else:
            return 400, {"detail": "unknown command"}
        self._owed = 0.0
        s.feed._pending.clear()
        return 200, {"ok": True}


def _speed(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        v = 10.0
    return max(0.25, min(120.0, v)) if v == v else 10.0
