"""
Tests for the Room demo (room.py): trilateration, Kalman filters, touch detection, the bag
state machine, the zone rule, room calibration, the HTTP endpoints and the built-in simulator.

Run either way, from the autotrack folder:
    .venv\\Scripts\\python tests/test_room.py
    .venv\\Scripts\\python -m pytest tests/test_room.py     (if pytest is installed)

No Bluetooth or ESP32 needed. A fake clock is used, so nothing waits in real time
(the HTTP test starts the real server in-process, which takes a few seconds).
All files are written to a temporary folder - data/room.json and the laptop profiles are not touched.
"""

import asyncio
import math
import os
import random
import shutil
import statistics
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from autotrack import laptops as laptops_mod  # noqa: E402
from autotrack import room as room_mod  # noqa: E402
from autotrack.laptops import Laptops, m_to_rssi  # noqa: E402
from autotrack.room import (  # noqa: E402
    PositionKalman, Room, RssiKalman, TouchDetector, calibration_offset, distance_sigma,
    rssi_to_distance, violating_pairs, wls_solve, wls_weights, zone_pairs)

# Keep every file this test writes in a temporary folder.
TMP = Path(tempfile.mkdtemp(prefix="autotrack-room-test-"))
_real_profiles = Path(ROOT) / "data" / "laptop_profiles.json"
if _real_profiles.exists():
    shutil.copy(_real_profiles, TMP / "laptop_profiles.json")
laptops_mod.PROFILES_FILE = TMP / "laptop_profiles.json"
laptops_mod.THIS_FILE = TMP / "this_laptop.json"
room_mod.ROOM_FILE = TMP / "room.json"

SQUARE = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]
quiet = lambda m: None  # noqa: E731


# --------------------------------------------------------------- helpers

class Clock:
    """A fake clock we move by hand."""
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def new_room(name="room"):
    clock = Clock()
    path = TMP / f"{name}-{random.random():.6f}.json"
    room = Room(Laptops(active="alex", log=quiet), log=quiet, path=path, clock=clock, seed=3)
    return room, clock


def rssi_at(room, aid, d):
    """The signal laptop `aid` would hear at distance d (its own curve + room offset), no noise."""
    return m_to_rssi(d, room.profile(aid)) + room.anchors[aid]["offset_db"]


def run(room, clock, seconds, rssi_by_id, rate=8, noise=0.0, rng=None):
    """Feed `rate` packets/s per laptop for `seconds`, ticking every 0.2 s.
    rssi_by_id: {laptop: rssi} or a function(t) -> {laptop: rssi}."""
    rng = rng or random.Random(1)
    steps = int(round(seconds / 0.2))
    for _ in range(steps):
        values = rssi_by_id(clock.t) if callable(rssi_by_id) else rssi_by_id
        per_tick = max(1, int(round(rate * 0.2)))
        for aid, rssi in values.items():
            for k in range(per_tick):
                room.ingest(aid, rssi + (rng.gauss(0, noise) if noise else 0.0),
                            clock.t + 0.2 * (k + 1) / per_tick)
        clock.t += 0.2
        room.tick()


def at_point(room, x, y):
    """{laptop: rssi} for a tag at (x, y)."""
    return {aid: rssi_at(room, aid, math.hypot(x - a["x"], y - a["y"])) for aid, a in room.anchors.items()}


# --------------------------------------------------------------- pure maths

def test_rssi_to_distance_standard_formula():
    # 1 m signal -60, n = 2: -60 -> 1 m, -80 -> 10 m; offset shifts A
    assert abs(rssi_to_distance(-60, -60, 2.0) - 1.0) < 1e-9
    assert abs(rssi_to_distance(-80, -60, 2.0) - 10.0) < 1e-9
    assert abs(rssi_to_distance(-65, -60, 2.0, offset_db=-5) - 1.0) < 1e-9
    assert rssi_to_distance(0, -60, 2.0) == 0.05 and rssi_to_distance(-127, -60, 2.0) == 15.0


def test_wls_recovers_known_point():
    for px, py in [(0.7, 1.3), (1.0, 1.0), (1.9, 0.2), (3.5, 1.0)]:
        d = [math.hypot(px - ax, py - ay) for ax, ay in SQUARE]
        x, y, sigma = wls_solve(SQUARE, d, [1.0] * 4)
        assert abs(x - px) < 1e-3 and abs(y - py) < 1e-3, ((px, py), (x, y))
        assert 0.1 <= sigma <= 3.0
    assert wls_solve(SQUARE[:2], [1, 1], [1, 1]) is None      # needs 3 laptops


def test_wls_stays_close_with_noise():
    rng = random.Random(7)
    errs = []
    for _ in range(200):
        px, py = rng.uniform(0.2, 1.8), rng.uniform(0.2, 1.8)
        d = [max(0.05, math.hypot(px - ax, py - ay) + rng.gauss(0, 0.15)) for ax, ay in SQUARE]
        x, y, _ = wls_solve(SQUARE, d, [1.0] * 4)
        errs.append(math.hypot(x - px, y - py))
    assert statistics.mean(errs) < 0.25, statistics.mean(errs)


def test_closest3_weighting_beats_equal_weights_when_far_anchor_is_wrong():
    px, py = 0.4, 0.5
    d = [math.hypot(px - ax, py - ay) for ax, ay in SQUARE]
    d[2] = 4.5                       # the far laptop (2,2) is badly wrong (true ~2.1 m)
    sig = [distance_sigma(x, 4.0, 2.0) for x in d]
    base = [1 / s ** 2 for s in sig]
    x1, y1, _ = wls_solve(SQUARE, d, base)                                   # equal treatment
    x2, y2, _ = wls_solve(SQUARE, d, wls_weights(d, sig, 1.0, 0.2))          # closest 3 dominate
    e_equal, e_close = math.hypot(x1 - px, y1 - py), math.hypot(x2 - px, y2 - py)
    assert e_close < e_equal, (e_close, e_equal)
    # plain equal weights (no distance weighting at all) are worse still
    x0, y0, _ = wls_solve(SQUARE, d, [1.0] * 4)
    assert e_close < math.hypot(x0 - px, y0 - py)
    w = wls_weights([0.5, 1.0, 3.0, 1.5], [1, 1, 1, 1], 1.0, 0.2)
    assert w == [1.0, 1.0, 0.2, 1.0]


def test_rssi_kalman_reduces_variance():
    rng = random.Random(2)
    kf = RssiKalman()
    raw, out = [], []
    for i in range(800):
        z = -62 + rng.gauss(0, 4.0)
        x = kf.update(i * 0.125, z, 16.0)
        if i > 80:
            raw.append(z)
            out.append(x)
    assert statistics.pstdev(out) < 0.5 * statistics.pstdev(raw), (statistics.pstdev(out), statistics.pstdev(raw))
    assert abs(statistics.mean(out) + 62) < 1.0
    kf.update(1000.0, -40, 16.0)                 # >3 s of silence: starts fresh
    assert kf.x == -40


def test_position_kalman_smooths():
    rng = random.Random(4)
    kf = PositionKalman()
    raw_err, kf_err, vxs = [], [], []
    for i in range(300):
        t = i * 0.2
        tx, ty = 0.1 * t, 1.0                     # walking at 0.1 m/s
        zx, zy = tx + rng.gauss(0, 0.4), ty + rng.gauss(0, 0.4)
        kf.predict(t)
        kf.update(t, zx, zy, 0.4)
        if i > 20:
            x, y = kf.position()
            raw_err.append(math.hypot(zx - tx, zy - ty))
            kf_err.append(math.hypot(x - tx, y - ty))
            vxs.append(kf.velocity()[0])
    assert statistics.mean(kf_err) < 0.7 * statistics.mean(raw_err), (statistics.mean(kf_err), statistics.mean(raw_err))
    assert abs(statistics.mean(vxs) - 0.1) < 0.05, statistics.mean(vxs)     # it learns the walking speed


def test_touch_hysteresis():
    td = TouchDetector()
    level = -45
    assert td.step(-44, level) is None            # 1 tick above: not yet
    assert td.step(-50, level) is None            # dropped: counter resets
    assert td.step(-44, level) is None
    assert td.step(-43, level) == "start" and td.touching
    assert td.step(-47, level) is None            # only 2 dB below: still touching
    assert td.step(-50, level) is None            # 5 dB below: 1
    assert td.step(-50, level) is None            # 2
    assert td.step(-46, level) is None            # back within 4 dB: counter resets
    assert td.step(-50, level) is None and td.step(None, level) is None
    assert td.step(-60, level) == "end" and not td.touching


def test_zone_pairs_and_diagonals():
    assert zone_pairs(["a", "b", "c", "d"]) == [("a", "b"), ("b", "c"), ("c", "d"), ("d", "a")]
    online = dict.fromkeys("abcd", True)
    # a and c are opposite corners (diagonal): never a pair
    assert violating_pairs(zone_pairs(list("abcd")), {"a": 3, "b": 1, "c": 3, "d": 1}, online, 2.5) == []
    assert violating_pairs(zone_pairs(list("abcd")), {"a": 3, "b": 3, "c": 1, "d": 1}, online, 2.5) == [("a", "b")]
    online["b"] = False                            # an offline laptop can't vote
    assert violating_pairs(zone_pairs(list("abcd")), {"a": 3, "b": 3, "c": 1, "d": 1}, online, 2.5) == []


def test_calibration_offset_math():
    # Laptop curve: 1 m = -60, n = 2. Truth is 5 dB stronger: at 2 m we hear -60 - 6.02 + 5
    heard = -60 - 20 * math.log10(2.0) + 5.0
    assert abs(calibration_offset(heard, 2.0, -60, 2.0) - 5.0) < 1e-9
    assert calibration_offset(-20, 2.0, -60, 2.0) == 15.0     # clamped


# --------------------------------------------------------------- the Room

def test_room_defaults_and_trilateration():
    room, clock = new_room()
    assert set(room.anchors) == {"alex", "joseph", "daniel", "ocean"}
    assert room.anchors["joseph"]["role"] == "wrong" and room.anchors["alex"]["role"] == "correct"
    run(room, clock, 4, at_point(room, 0.6, 1.4))
    st = room.state()
    assert st["bag"]["status"] == "ON TRACK", st["bag"]
    assert math.hypot(st["raw_position"]["x"] - 0.6, st["raw_position"]["y"] - 1.4) < 0.05, st["raw_position"]
    assert math.hypot(st["position"]["x"] - 0.6, st["position"]["y"] - 1.4) < 0.1, st["position"]
    assert all(a["online"] and a["distance_m"] for a in st["anchors"])
    assert sum(1 for a in st["anchors"] if a["closest3"]) == 3
    assert abs(sum(a["weight"] for a in st["anchors"]) - 1.0) < 0.01
    keys = {"enabled", "now", "real", "sim", "anchors", "position", "raw_position", "velocity", "pos_sigma_m",
            "trail", "zone", "bag", "alert", "tag_mode", "tag_mode_sent", "last_write", "calibration",
            "weights", "events", "udp"}
    assert set(st) == keys, set(st) ^ keys
    clock.t += 4
    room.tick()
    assert room.state()["bag"]["status"] == "NOT SEEN"


def test_wrong_then_checkin_state_machine():
    room, clock = new_room()
    run(room, clock, 2, at_point(room, 1, 1))
    assert room.status == "ON TRACK"
    # touch Joseph's laptop (the WRONG loader)
    run(room, clock, 4, at_point(room, 2.0 - 0.03, 0.03))   # the RSSI filter needs ~1.5 s to follow
    st = room.state()
    assert st["bag"]["status"] == "WRONG AIRCRAFT", st["bag"]
    assert st["alert"] and st["tag_mode"] == 1 and st["bag"]["checkpoint"] == "joseph"
    assert "AC 1234" in st["bag"]["reason"] and "Loader 4" in st["bag"]["reason"]
    assert st["events"][0]["kind"] == "wrong"
    # walk back to the middle: still latched
    run(room, clock, 3, at_point(room, 1, 1))
    assert room.status == "WRONG AIRCRAFT"
    # touch Alex's laptop (the CORRECT loader)
    run(room, clock, 4, at_point(room, 0.03, 0.03))
    st = room.state()
    assert st["bag"]["status"] == "CHECKED IN" and not st["alert"]
    assert st["events"][0]["kind"] == "checkin"
    assert any(e["kind"] == "wrong" for e in st["events"])
    run(room, clock, 4, at_point(room, 1, 1))
    assert room.status == "CHECKED IN" and room.tag_mode == 0
    room.reset()
    run(room, clock, 1, at_point(room, 1, 1))
    assert room.status == "ON TRACK"


def test_zone_needs_two_adjacent_laptops_and_hold_time():
    room, clock = new_room()
    run(room, clock, 2, at_point(room, 1, 1))
    # Make alex (0,0) and daniel (2,2) - a DIAGONAL pair - both far: must not trigger
    far = dict(at_point(room, 1, 1))
    far["alex"], far["daniel"] = rssi_at(room, "alex", 3.5), rssi_at(room, "daniel", 3.5)
    run(room, clock, 5, far)
    assert room.status == "ON TRACK", room.status
    # alex (0,0) and ocean (0,2) are neighbours: both far -> OUT OF ZONE, but only after hold_s (2 s)
    far = dict(at_point(room, 1, 1))
    far["alex"], far["ocean"] = rssi_at(room, "alex", 3.5), rssi_at(room, "ocean", 3.5)
    first_bad = went_out = None
    for _ in range(50):
        run(room, clock, 0.2, far)
        d = {a["id"]: a["distance_m"] for a in room.state()["anchors"]}
        if first_bad is None and d["alex"] > 2.5 and d["ocean"] > 2.5:
            first_bad = clock.t
        if room.status == "OUT OF ZONE":
            went_out = clock.t
            break
    assert first_bad is not None and went_out is not None
    assert 2.0 - 0.01 <= went_out - first_bad <= 2.0 + 0.41, went_out - first_bad      # waited the hold time
    st = room.state()
    assert st["bag"]["status"] == "OUT OF ZONE" and st["alert"] and st["tag_mode"] == 1
    assert sorted(st["zone"]["violating"]) == ["alex", "ocean"] and st["zone"]["inside"] is False
    assert st["events"][0]["kind"] == "zone_out"
    # back inside: clears only after 3 s without a violating pair
    run(room, clock, 2.0, at_point(room, 1, 1))
    assert room.status == "OUT OF ZONE"
    run(room, clock, 4.0, at_point(room, 1, 1))
    assert room.status == "ON TRACK" and room.state()["events"][0]["kind"] == "zone_in"


def test_room_calibration_recovers_offset():
    room, clock = new_room()
    true_offset = {"alex": 0.0, "joseph": 5.0, "daniel": -4.0, "ocean": 2.0}
    p = (1.0, 1.0)

    def heard(t):
        return {aid: m_to_rssi(math.hypot(p[0] - a["x"], p[1] - a["y"]), room.profile(aid)) + true_offset[aid]
                for aid, a in room.anchors.items()}

    before = {k: dict(v) for k, v in room.laptops.profiles.items()}
    run(room, clock, 1, heard)
    room.start_calibration(p[0], p[1], 6)
    assert room.state()["calibration"]["phase"] == "running"
    run(room, clock, 7, heard, noise=1.0)
    cal = room.state()["calibration"]
    assert cal["phase"] == "done", cal
    for r in cal["results"]:
        assert abs(r["offset_db"] - true_offset[r["id"]]) < 1.0, r
        assert r["sigma_db"] >= 1.5 and r["packets"] >= 5
    assert abs(room.anchors["joseph"]["offset_db"] - 5.0) < 1.0
    # saved to the room file, and the profile itself was NOT changed
    again = Room(room.laptops, log=quiet, path=room.path)
    assert abs(again.anchors["joseph"]["offset_db"] - 5.0) < 1.0
    for k, v in before.items():
        assert room.laptops.profiles[k]["rssi_1m"] == v["rssi_1m"] and room.laptops.profiles[k]["n"] == v["n"]


def test_touch_capture():
    room, clock = new_room()
    run(room, clock, 2, {"joseph": -38.0})
    room.capture_touch("joseph")
    assert room.anchors["joseph"]["touch_dbm"] == -41.0
    try:
        room.capture_touch("daniel")
        raise AssertionError("should need 5 packets")
    except ValueError:
        pass


def test_tag_commands_sent_without_blocking():
    class FakeBLE:
        def __init__(self):
            self.sent = []
            self.status = {}

        async def set_alert(self, tag, mode):
            self.sent.append((tag, mode))
            await asyncio.sleep(0)
            return True

    async def go():
        room, clock = new_room()
        room.ble = FakeBLE()
        run(room, clock, 2, at_point(room, 1, 1))
        await asyncio.sleep(0.01)
        run(room, clock, 4, at_point(room, 1.97, 0.03))       # touch the wrong loader
        await asyncio.sleep(0.01)
        run(room, clock, 4, at_point(room, 0.03, 0.03))       # then the correct one
        await asyncio.sleep(0.01)
        run(room, clock, 5, at_point(room, 1, 1))
        await asyncio.sleep(0.01)
        return room

    room = asyncio.run(go())
    modes = [m for _, m in room.ble.sent]
    assert modes == [0, 1, 2], modes      # take over (normal), WRONG, OK; no 0 after OK (tag does that)
    assert room.tag_mode_sent == 2 and room.last_write["ok"]


def test_touching_a_corner_never_means_out_of_zone():
    room, clock = new_room()
    run(room, clock, 2, at_point(room, 1, 1))
    # Tag touching daniel's corner (2,2). Make the two laptops on the far side read "too far"
    # (alex (0,0) is the diagonal; joseph (2,0) + alex form a neighbour pair): noise like this
    # used to raise OUT OF ZONE although the tag is at a laptop, i.e. inside the square.
    touching = dict(at_point(room, 1.98, 1.98))
    touching["alex"], touching["joseph"] = rssi_at(room, "alex", 3.5), rssi_at(room, "joseph", 3.5)
    run(room, clock, 8, touching)
    assert room.live["daniel"].touch.touching
    assert room.status != "OUT OF ZONE" and not room.out_of_zone, room.status
    # Same far-side readings once the tag has left the corner (no laptop near it): now it does count.
    far = dict(at_point(room, 1.0, 1.4))
    far["alex"], far["joseph"] = rssi_at(room, "alex", 3.5), rssi_at(room, "joseph", 3.5)
    run(room, clock, 10, far)
    assert room.status == "OUT OF ZONE", room.status


def test_neighbours_come_from_the_map_not_the_join_order():
    room, clock = new_room()
    # Scramble the 'order' so alex and daniel (DIAGONAL on the map) come next to each other in the list.
    room.configure({"anchors": {"daniel": {"order": 1}, "joseph": {"order": 2}}})
    run(room, clock, 2, at_point(room, 1, 1))
    far = dict(at_point(room, 1, 1))
    far["alex"], far["daniel"] = rssi_at(room, "alex", 3.5), rssi_at(room, "daniel", 3.5)
    run(room, clock, 6, far)
    assert room.status == "ON TRACK", room.status       # a diagonal pair never triggers
    sq = room.state()["zone"]["square"]
    # the drawn square goes around the corners: the two diagonal corners are 2 steps apart
    assert len(sq) == 4 and abs(sq.index([2.0, 2.0]) - sq.index([0.0, 0.0])) == 2, sq


def test_livetag_suppressed_while_room_in_charge():
    from autotrack.livetag import LiveTagMonitor

    class FakeBLE:
        sent = []

        async def set_alert(self, tag, mode):
            self.sent.append(mode)
            return True

    async def go(suppress):
        mon = LiveTagMonitor(ble=FakeBLE(), laptops=Laptops(active="alex", log=quiet), log=quiet,
                             suppress=lambda: suppress)
        mon._send(1)
        await asyncio.sleep(0.01)

    FakeBLE.sent = []
    asyncio.run(go(True))
    assert FakeBLE.sent == []
    asyncio.run(go(False))
    assert FakeBLE.sent == [1]


def test_sim_runs_the_whole_story():
    room, clock = new_room()
    profiles_before = set(room.laptops.profiles)
    room.set_sim(True)
    seen = []
    for _ in range(int(60 / 0.2)):
        clock.t += 0.2
        room.tick()
        if not seen or seen[-1] != room.status:
            seen.append(room.status)
    assert all(a["online"] for a in room.state()["anchors"])
    i = seen.index("WRONG AIRCRAFT")
    j = seen.index("CHECKED IN", i)
    k = seen.index("OUT OF ZONE", j)
    assert i < j < k, seen
    assert room.status in ("CHECKED IN", "ON TRACK"), seen     # back inside at the end of the loop
    kinds = [e["kind"] for e in room.events]
    assert {"wrong", "checkin", "zone_out", "zone_in"} <= set(kinds), kinds
    assert set(room.laptops.profiles) == profiles_before    # fake packets never create laptop profiles
    room.ingest("alex", -50)              # real packets are ignored while simulating
    room.set_sim(False)
    assert not room.sim


# --------------------------------------------------------------- HTTP endpoints

class _Http:
    """A tiny HTTP client (stdlib only) for the real server running in a background thread."""
    def __init__(self, base):
        self.base = base

    def _call(self, method, path, body=None):
        import json
        import urllib.error
        import urllib.request
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return _Resp(r.status, r.read())
        except urllib.error.HTTPError as e:
            return _Resp(e.code, e.read())

    def get(self, path):
        return self._call("GET", path)

    def post(self, path, json=None):
        return self._call("POST", path, json if json is not None else {})


class _Resp:
    def __init__(self, status, raw):
        self.status_code, self.text = status, raw.decode("utf-8", "replace")

    def json(self):
        import json
        return json.loads(self.text)


def _serve(app):
    """Start the real FastAPI app with uvicorn on a free local port; returns (client, server)."""
    import socket
    import threading
    import time
    import uvicorn
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(200):
        if srv.started:
            break
        time.sleep(0.05)
    assert srv.started, "server did not start"
    return _Http(f"http://127.0.0.1:{port}"), srv


def test_http_endpoints():
    from autotrack import server
    server.CONFIG.update(mode="sim", laptop="alex")
    c, srv = _serve(server.app)
    try:
        _http_checks(c, server)
    finally:
        srv.should_exit = True


def _http_checks(c, server):
    if True:
        st = c.get("/api/room").json()
        assert st["enabled"] and len(st["anchors"]) == 4 and st["bag"]["tag"] == "TAG-0001"
        # an unknown laptop joins the room automatically
        r = c.post("/api/room/sightings", json={"laptop": "Sam", "packets": [{"age_ms": 100 * i, "rssi": -60} for i in range(8)]})
        assert r.status_code == 200 and r.json() == {"ok": True, "accepted": 8}, r.text
        st = c.get("/api/room").json()
        sam = next(a for a in st["anchors"] if a["id"] == "sam")
        assert sam["role"] == "corner" and not sam["calibrated"] and sam["rssi_1m"] == laptops_mod.ALEX["rssi_1m"]
        assert "sam" in server.state["live"].laptops.profiles
        # bad bodies
        assert c.post("/api/room/sightings", json={"laptop": "sam", "packets": [{"age_ms": 0, "rssi": 5}]}).status_code == 400
        assert c.post("/api/room/sightings", json={"laptop": "sam", "packets": [{"age_ms": 0, "rssi": -60}] * 501}).status_code == 400
        # config, sim, reset, touch, calibrate, beep
        st = c.post("/api/room/config", json={"zone": {"range_m": 3.0}, "anchors": {"sam": None}}).json()
        assert st["zone"]["range_m"] == 3.0 and all(a["id"] != "sam" for a in st["anchors"])
        assert c.post("/api/room/sim", json={"on": True}).json()["sim"] is True
        assert c.post("/api/room/reset", json={}).status_code == 200
        assert c.post("/api/room/touch", json={"id": "daniel"}).status_code == 400   # no packets yet
        assert c.post("/api/room/calibrate", json={"x": 1, "y": 1, "seconds": 3}).json()["calibration"]["phase"] == "running"
        assert c.post("/api/room/beep", json={"mode": 3}).json()["ok"] is False      # not real mode
        assert c.post("/api/room/sim", json={"on": False}).json()["sim"] is False
        # the old endpoints still work
        assert c.get("/api/livetag").status_code == 200
        s = c.get("/api/state")
        assert s.status_code == 200 and "mode" in s.json()


# --------------------------------------------------------------- UDP pipeline (filtered RSSI)

def _dg(laptop="joseph", seq=0, rssi=-60.0, var=2.0, raw=-61.0, packets=2, age=50, v=1):
    import json
    return json.dumps({"v": v, "laptop": laptop, "seq": seq, "rssi_filtered": rssi, "variance": var,
                       "raw_median": raw, "packets": packets, "t_age_ms": age}).encode()


def _anchor(room, aid):
    return next(a for a in room.state()["anchors"] if a["id"] == aid)


def test_parse_datagram():
    from autotrack.room import parse_datagram
    d = parse_datagram(_dg())
    assert d["laptop"] == "joseph" and d["seq"] == 0 and d["rssi_filtered"] == -60.0 and d["packets"] == 2
    ok = parse_datagram(_dg(rssi=None, var=None, raw=None, packets=0, age=None))   # heartbeat
    assert ok["rssi_filtered"] is None and ok["t_age_ms"] is None
    for bad in (b"not json", b"[1,2]", _dg(v=2), _dg(rssi=5), _dg(rssi=-200), _dg(var=-1), _dg(packets=5000),
                _dg(age=999999), _dg(laptop=""), _dg(seq=-1), b"x" * 3000, b"\xff\xfe",
                b'{"v":1,"laptop":"a","seq":1}'):
        try:
            parse_datagram(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted bad datagram {bad[:40]!r}")


def test_ingest_filtered_online_distance_offline():
    room, clock = new_room("udp1")
    want = rssi_at(room, "joseph", 1.5)
    assert room.ingest_filtered("joseph", want, 2.0, want, 2, 0, 0) is True
    room.tick()
    a = _anchor(room, "joseph")
    assert a["online"] and a["rssi"] == round(want, 1) and abs(a["distance_m"] - 1.5) < 0.05, a
    assert a["raw"] == round(want, 1) and a["packets_s"] > 0
    clock.t += 3.5                         # silence (even heartbeats say "never heard" -> not alive)
    room.ingest_filtered("joseph", None, None, None, 0, 3500, 1)
    room.tick()
    a = _anchor(room, "joseph")
    assert not a["online"] and a["rssi"] is None and a["distance_m"] is None


def test_ingest_filtered_order_restart_sim_and_age():
    room, clock = new_room("udp2")
    assert room.ingest_filtered("joseph", -60, 2, -60, 1, 0, 5)
    assert not room.ingest_filtered("joseph", -90, 2, -90, 1, 0, 5)        # duplicate
    assert not room.ingest_filtered("joseph", -90, 2, -90, 1, 0, 4)        # out of order
    assert room.live["joseph"].remote["rssi"] == -60
    assert room.ingest_filtered("joseph", -61, 2, -61, 1, 0, 6)
    assert room.ingest_filtered("joseph", -70, 2, -70, 1, 0, 3000)
    assert room.ingest_filtered("joseph", -71, 2, -71, 1, 0, 3)            # far smaller: reporter restarted
    assert room.live["joseph"].remote["rssi"] == -71
    assert room.ingest_filtered("joseph", -72, 2, -72, 1, 0, 0)            # seq 0 = restart too
    # t_age_ms shifts the heard time (and so the online window)
    room.ingest_filtered("ocean", -60, 2, -60, 1, 2900, 1)
    assert abs(room.live["ocean"].last_t - (clock.t - 2.9)) < 1e-6
    clock.t += 0.5
    room.tick()
    assert not _anchor(room, "ocean")["online"]                            # heard 3.4 s ago
    # heartbeat with "never heard" keeps a new laptop offline
    room.ingest_filtered("sam", None, None, None, 0, None, 0)
    assert room.live["sam"].last_t is None
    # bad input
    for args in ((5, 1, None, 1, 0, 1), (-60, -1, None, 1, 0, 1), (-60, 1, None, 5000, 0, 1), (-60, 1, None, 1, -1, 1)):
        try:
            room.ingest_filtered("joseph", args[0], args[1], args[2], args[3], args[4], 50 + args[5])
        except ValueError:
            continue
        raise AssertionError(f"accepted {args}")
    room.set_sim(True)
    assert room.ingest_filtered("daniel", -60, 2, -60, 1, 0, 1) is False
    assert "daniel" not in room.live or room.live["daniel"].remote is None


def test_filtered_mode_feeds_calibration_and_touch_capture():
    room, clock = new_room("udp3")
    room.ingest_filtered("alex", -50, 2, -50, 3, 0, 0)
    room.start_calibration(1.0, 1.0, seconds=2.0)
    for i in range(1, 15):
        clock.t += 0.2
        room.ingest_filtered("alex", -50, 2, -50 - (i % 3), 2, 0, i)
        room.tick()
    assert room.calib["phase"] == "done", room.calib
    room.ingest_filtered("joseph", -40, 2, -40, 2, 0, 0)
    for i in range(1, 8):
        clock.t += 0.2
        room.ingest_filtered("joseph", -40, 2, -40, 2, 0, i)
    st = room.capture_touch("joseph")
    assert abs(next(a for a in st["anchors"] if a["id"] == "joseph")["touch_dbm"] - (-43.0)) < 0.2


def test_udp_end_to_end_position():
    import socket

    async def go():
        room, clock = new_room("udp4")
        transport, port = await room_mod.start_udp(room, "127.0.0.1", 0, log=quiet)
        assert transport is not None and port
        try:
            tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            tag = (0.8, 1.1)
            for seq in range(6):
                for aid, a in room.anchors.items():
                    d = math.hypot(tag[0] - a["x"], tag[1] - a["y"])
                    tx.sendto(_dg(aid, seq, rssi_at(room, aid, d), 2.0, rssi_at(room, aid, d), 2, 40),
                              ("127.0.0.1", port))
                await asyncio.sleep(0.05)
            for junk in (b"garbage", b"{}", _dg(v=9), _dg(rssi=55), b"z" * 3000):
                tx.sendto(junk, ("127.0.0.1", port))
            await asyncio.sleep(0.2)
            tx.close()
            room.tick()
            clock.t += 0.2
            room.tick()
            st = room.state()
            assert st["udp"]["rejected"] == 5 and st["udp"]["datagrams"] >= 29, st["udp"]
            assert all(a["online"] for a in st["anchors"]), st["anchors"]
            pos = st["raw_position"]
            assert pos is not None and math.hypot(pos["x"] - tag[0], pos["y"] - tag[1]) < 0.15, pos
            assert st["position"] is not None
        finally:
            transport.close()

    asyncio.run(go())


def test_udp_port_busy_is_not_fatal():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    msgs = []

    async def go():
        room, _ = new_room("udp5")
        return await room_mod.start_udp(room, "127.0.0.1", s.getsockname()[1], log=msgs.append)

    try:
        res = asyncio.run(go())
    finally:
        s.close()
    # On Windows a second bind of a UDP port fails; elsewhere it fails too unless SO_REUSEPORT is set.
    assert res == (None, None) and msgs and "WARNING" in msgs[0], (res, msgs)


def _reporter(**kw):
    from tools import reporter
    return reporter.Reporter("http://192.168.1.23:8000", "joseph", log=quiet, clock=kw.pop("clock", Clock()), **kw)


def test_reporter_datagram_fields_and_heartbeat():
    import json
    clock = Clock(100.0)
    rep = _reporter(clock=clock)
    assert rep.udp_host == "192.168.1.23" and rep.interval == 0.2
    dg = rep.build_datagram()
    assert dg["seq"] == 0 and dg["packets"] == 0 and dg["rssi_filtered"] is None
    assert dg["raw_median"] is None and dg["t_age_ms"] is None                       # never heard
    for r in (-60, -62, -64):
        rep.add(r)
        clock.t += 0.05
    clock.t += 0.05
    dg = rep.build_datagram()
    assert dg["seq"] == 1 and dg["packets"] == 3 and dg["raw_median"] == -62 and dg["v"] == 1
    assert dg["t_age_ms"] == 100 and dg["laptop"] == "joseph"
    assert -64 <= dg["rssi_filtered"] <= -60 and dg["variance"] > 0
    assert len(json.dumps(dg)) < 512
    clock.t += 0.2
    dg = rep.build_datagram()                              # heartbeat, nothing new
    assert dg["seq"] == 2 and dg["packets"] == 0 and dg["raw_median"] is None
    assert dg["rssi_filtered"] is not None and dg["t_age_ms"] == 300
    clock.t += 3.5
    dg = rep.build_datagram()                              # unheard for > 3 s
    assert dg["rssi_filtered"] is None and dg["variance"] is None and dg["t_age_ms"] >= 3500
    # interval clamp for udp, none for http
    assert _reporter(interval=0.01).interval == 0.1 and _reporter(interval=5).interval == 0.25
    assert _reporter(transport="http").interval == 0.25


def test_reporter_kalman_reduces_noise():
    clock = Clock(0.0)
    rep = _reporter(clock=clock)
    rng = random.Random(7)
    raw, filt = [], []
    for _ in range(200):
        z = round(-65 + rng.gauss(0, 4))
        rep.add(z)
        clock.t += 0.125
        raw.append(z)
        filt.append(rep.kalman.x)
    assert statistics.pvariance(filt[20:]) < statistics.pvariance(raw[20:]) * 0.5


def test_reporter_sends_udp_to_local_receiver():
    import socket
    from tools import reporter

    async def go():
        room, clock = new_room("udp6")
        transport, port = await room_mod.start_udp(room, "127.0.0.1", 0, log=quiet)
        rep = reporter.Reporter("http://127.0.0.1:8000", "joseph", interval=0.1, udp_port=port, log=quiet)
        src = asyncio.ensure_future(reporter.fake_source(rep))
        loop_task = asyncio.ensure_future(rep.sender_loop())
        try:
            await asyncio.sleep(1.0)
        finally:
            src.cancel()
            loop_task.cancel()
            transport.close()
        return room, rep

    room, rep = asyncio.run(go())
    assert rep.sent_total >= 5 and rep.server_ok is True
    lv = room.live["joseph"]
    assert lv.remote is not None and lv.remote["rssi"] is not None and room.udp_stats["rejected"] == 0
    rep.sock.close()


if __name__ == "__main__":
    failed = 0
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]  # file order
    for name, fn in tests:
        try:
            fn()
            print("PASS", name)
        except Exception as e:  # show every failure, not just the first
            failed += 1
            print("FAIL", name, "->", type(e).__name__, e)
    print("\n{} passed, {} failed".format(len(tests) - failed, failed))
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1 if failed else 0)
