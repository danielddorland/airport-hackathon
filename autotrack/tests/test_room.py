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
            "weights", "events"}
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
        rng_m = room.zone["range_m"]
        if first_bad is None and d["alex"] > rng_m and d["ocean"] > rng_m:
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


def test_wls_finds_points_outside_the_square():
    # Gauss-Newton alone stops in a "mirror" dip inside the square for these (review finding).
    for px, py in [(-0.5, -0.5), (3.5, 1.0), (2.5, 2.5), (-1.6, 0.0), (-1.5, -1.5), (1.0, 3.5)]:
        d = [math.hypot(px - ax, py - ay) for ax, ay in SQUARE]
        for ws in ([1.0] * 4, wls_weights(d, [distance_sigma(x, 4.0, 2.0) for x in d], 1.0, 0.2)):
            x, y, _ = wls_solve(SQUARE, d, ws)
            assert abs(x - px) < 1e-2 and abs(y - py) < 1e-2, ((px, py), (x, y))


def test_no_out_of_zone_while_resting_on_a_corner():
    # The far laptops read a few dB weak (a body in the way): the bag on a loader is still inside.
    for corner in ("alex", "joseph", "daniel", "ocean"):
        room, clock = new_room()
        a = room.anchors[corner]
        px, py = a["x"] + (0.03 if a["x"] < 1 else -0.03), a["y"] + (0.03 if a["y"] < 1 else -0.03)

        def heard(t):
            out = at_point(room, px, py)
            for k in out:
                if k != corner:
                    out[k] -= 3.0
            return out
        run(room, clock, 12, heard, noise=4.0, rng=random.Random(5))
        assert not any(e["kind"] == "zone_out" for e in room.events), (corner, room.events)


def test_out_of_zone_stays_when_the_far_laptops_lose_the_tag():
    room, clock = new_room()
    run(room, clock, 2, at_point(room, 1, 1))
    run(room, clock, 7, at_point(room, 3.5, 1.0))          # filter lag + 2 s hold
    assert room.status == "OUT OF ZONE"
    # carried further: alex and ocean (the far pair) stop hearing it - it is NOT back inside
    run(room, clock, 6, {k: v for k, v in at_point(room, 5.0, 1.0).items() if k in ("joseph", "daniel")})
    assert room.status == "OUT OF ZONE", room.status
    run(room, clock, 5, at_point(room, 1, 1))
    assert room.status == "ON TRACK"


class _FakeBLE:
    """Records tag commands; each write takes `delay` seconds (real time)."""
    def __init__(self, delay=0.0):
        self.sent, self.delay, self.status = [], delay, {}

    async def set_alert(self, tag, mode):
        self.sent.append(mode)
        await asyncio.sleep(self.delay)
        return True


def test_room_resends_after_taking_control_back():
    async def go():
        room, clock = new_room()
        room.ble = _FakeBLE()
        run(room, clock, 2, at_point(room, 1, 1))
        await asyncio.sleep(0.01)
        run(room, clock, 4, at_point(room, 1.97, 0.03))          # WRONG AIRCRAFT -> mode 1
        await asyncio.sleep(0.01)
        assert room.controls_tag() and room.ble.sent == [0, 1]
        run(room, clock, 5, {"alex": rssi_at(room, "alex", 1.4)})   # the reporters drop out
        assert not room.controls_tag()        # LiveTagMonitor takes over (and re-sends its mode)
        room.ble.sent.append("livetag")       # pretend it changed the tag
        run(room, clock, 3, at_point(room, 1.97, 0.03))          # reporters back: room in charge again
        await asyncio.sleep(0.01)
        assert room.ble.sent[-1] == 1, room.ble.sent   # re-sent what it wants, not "already sent"
        room.set_sim(True)
        assert not room.controls_tag()        # simulating: the real tag is left to LiveTagMonitor
        room.set_sim(False)
        room.configure({"enabled": False})
        assert not room.controls_tag()
    asyncio.run(go())


def test_slow_ok_write_is_not_cut_short_by_mode_0():
    async def go():
        room, clock = new_room()
        room.ble = _FakeBLE(delay=0.3)
        run(room, clock, 2, at_point(room, 1, 1))
        await asyncio.sleep(0.4)
        run(room, clock, 4, at_point(room, 1.97, 0.03))
        await asyncio.sleep(0.4)
        run(room, clock, 4, at_point(room, 0.03, 0.03))          # check-in: mode 2 starts...
        run(room, clock, 4, at_point(room, 1, 1))                # ...still writing after OK_MODE_S
        await asyncio.sleep(0.4)
        run(room, clock, 1, at_point(room, 1, 1))
        await asyncio.sleep(0.4)
        return room.ble.sent
    sent = asyncio.run(go())
    assert sent == [0, 1, 2], sent


def test_livetag_resends_its_mode_when_the_room_lets_go():
    from autotrack.livetag import LiveTagMonitor

    async def go():
        flag = {"room": True}
        ble = _FakeBLE()
        mon = LiveTagMonitor(ble=ble, laptops=Laptops(active="alex", log=quiet), log=quiet,
                             suppress=lambda: flag["room"])
        mon._check()
        await asyncio.sleep(0.01)
        assert ble.sent == []
        flag["room"] = False
        mon._check()
        await asyncio.sleep(0.01)
        return ble.sent
    assert asyncio.run(go()) == [0]


def test_calibration_skips_laptop_on_the_spot_and_refuses_in_sim():
    room, clock = new_room()
    run(room, clock, 1, {"joseph": -40.0})               # only joseph online
    room.start_calibration(seconds=3)
    assert room.calib["point"] == {"x": 1.0, "y": 1.0}    # the square's centre, not joseph's spot
    room2, clock2 = new_room()
    run(room2, clock2, 1, at_point(room2, 2.0, 0.0))
    room2.start_calibration(2.0, 0.0, 3)                 # the spot IS joseph's laptop
    run(room2, clock2, 4, at_point(room2, 2.0, 0.0))
    res = {r["id"]: r for r in room2.calib["results"]}
    assert res["joseph"]["offset_db"] is None and room2.anchors["joseph"]["offset_db"] == 0.0
    assert res["alex"]["offset_db"] is not None
    room2.set_sim(True)
    for fn in (lambda: room2.start_calibration(1, 1, 3), lambda: room2.capture_touch("alex")):
        try:
            fn()
            raise AssertionError("should refuse while simulating")
        except ValueError:
            pass


def test_sim_out_of_zone_with_three_laptops():
    room, clock = new_room()
    room.configure({"anchors": {"ocean": None}})
    room.set_sim(True)
    kinds = set()
    for _ in range(int(60 / 0.2)):
        clock.t += 0.2
        room.tick()
        kinds |= {e["kind"] for e in room.events}
    assert {"wrong", "checkin", "zone_out"} <= kinds, kinds


# --------------------------------------------------------------- HTTP endpoints

def test_http_endpoints():
    from fastapi.testclient import TestClient
    from autotrack import server
    server.CONFIG.update(mode="sim", laptop="alex")
    with TestClient(server.app) as c:
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
        # calibrating / setting touch in the simulator would save fake numbers: refused
        assert c.post("/api/room/calibrate", json={"x": 1, "y": 1, "seconds": 3}).status_code == 400
        assert c.post("/api/room/beep", json={"mode": 3}).json()["ok"] is False      # not real mode
        assert c.post("/api/room/sim", json={"on": False}).json()["sim"] is False
        assert c.post("/api/room/sightings", json={"laptop": "joseph", "packets": [{"age_ms": 0, "rssi": -60}]}).json()["accepted"] == 1
        assert c.post("/api/room/calibrate", json={"x": 1, "y": 1, "seconds": 3}).json()["calibration"]["phase"] == "running"
        # a bad config patch is refused WITHOUT changing anything
        x0 = next(a["x"] for a in c.get("/api/room").json()["anchors"] if a["id"] == "alex")
        assert c.post("/api/room/config", json={"anchors": {"alex": {"x": 0.7, "y": "bad"}}}).status_code == 400
        assert next(a["x"] for a in c.get("/api/room").json()["anchors"] if a["id"] == "alex") == x0
        assert c.post("/api/room/config", json={"anchors": [1]}).status_code == 400
        # the old endpoints still work
        assert c.get("/api/livetag").status_code == 200
        s = c.get("/api/state")
        assert s.status_code == 200 and "mode" in s.json()


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
