"""
Tests for the AutoTrack engine: positioning, tracker rules, passenger lookup, SecureBag cache,
and one short scenario run.

Run either way, from the autotrack folder:
    .venv\\Scripts\\python tests/test_engine.py
    .venv\\Scripts\\python -m pytest tests/test_engine.py     (if pytest is installed)

Everything except the last test uses tiny hand-built cases, so it runs in about a second.
The last test replays the simulation up to the WRONG AIRCRAFT scenario (about 5 seconds).
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from autotrack import securebag  # noqa: E402
from autotrack.feed import Feed  # noqa: E402
from autotrack.layout import Layout  # noqa: E402
from autotrack.positioning import SWITCH_SECONDS, ZoneEstimator  # noqa: E402
from autotrack.tracker import NOT_SEEN_S, TAG_RETURN_S, Tracker  # noqa: E402

# Two receivers 10 m apart, in zones "A" and "B" (plus a third for the centroid test).
RX = {
    "R-A": {"zone": "A", "x": 0.0, "y": 0.0},
    "R-B": {"zone": "B", "x": 10.0, "y": 0.0},
    "R-C": {"zone": "C", "x": 5.0, "y": 8.0},
}

LAYOUT = Layout()   # the real 28-zone map (loaded once, it's shared by the tracker tests)


# --------------------------------------------------------------- helpers for the tracker tests

def new_tracker():
    """A fresh tracker with an in-memory feed (no SQLite file)."""
    return Tracker(LAYOUT, Feed(use_db=False), smoothing=True)


def checkin(tr, t=0, bag="BTEST1", plate="0141234567", ref="ABC123", gate="GA2", tag="TAG-9001",
            weight=15.0, deadline=None):
    tr.checkin(t, bag, plate, ref, gate, tag, weight, deadline, False, checkin_photo=None, where="IA1")
    return tr.bags[bag]


def hear(tr, t, tag, receiver, rssi=-55):
    """One second where `receiver` hears `tag`, then the tracker re-checks every status."""
    tr.ble_second(t, {tag: {receiver: rssi}})
    tr.tick(t)


def bag_at_loader(tr, gate_booked, loader="GA4", seconds=5):
    """Check in a bag booked on `gate_booked`, let the receiver at `loader` hear its tag for a few
    seconds, then let the loader's RFID portal read it. Returns (bag record, portal answer, time)."""
    b = checkin(tr, gate=gate_booked)
    for t in range(1, seconds + 1):
        hear(tr, t, b.tag, f"R-{loader}")
    t = seconds + 1
    answer = tr.portal(t, loader, b.plate, scale_kg=b.weight)
    tr.ble_second(t, {b.tag: {f"R-{loader}": -55}})
    tr.tick(t)
    return b, answer, t


# --------------------------------------------------------------- positioning

def test_zone_does_not_flicker_on_one_noisy_reading():
    est = ZoneEstimator(RX, smoothing=True)
    est.update(0, {"R-A": -60, "R-B": -80})
    assert est.primary == "A"
    est.update(1, {"R-A": -60, "R-B": -80})
    # one bad second: B suddenly looks much stronger than A
    changed = est.update(2, {"R-A": -78, "R-B": -50})
    assert not changed and est.primary == "A", est.primary
    # ...then everything is back to normal: still A, no switch ever happened
    for t in range(3, 8):
        assert not est.update(t, {"R-A": -60, "R-B": -80})
    assert est.primary == "A"


def test_without_smoothing_the_same_noisy_reading_does_flicker():
    est = ZoneEstimator(RX, smoothing=False)
    est.update(0, {"R-A": -60, "R-B": -80})
    assert est.update(2, {"R-A": -78, "R-B": -50}) and est.primary == "B"


def test_zone_switches_after_consistent_stronger_readings():
    est = ZoneEstimator(RX, smoothing=True)
    for t in range(0, 3):
        est.update(t, {"R-A": -60, "R-B": -80})
    switched_at = None
    for t in range(3, 10):   # the bag has moved next to receiver B
        if est.update(t, {"R-A": -80, "R-B": -58}):
            switched_at = t
            break
    assert switched_at is not None, "never switched to B"
    assert est.primary == "B"
    assert switched_at - 3 + 1 >= SWITCH_SECONDS, "switched on the very first reading"
    assert est.primary_since == switched_at


def test_rfid_read_snaps_the_zone_immediately():
    est = ZoneEstimator(RX, smoothing=True)
    for t in range(0, 3):
        est.update(t, {"R-A": -60, "R-B": -80})
    # BLE still says A, but an RFID tunnel in zone B just read the bag: RFID wins at once
    assert est.update(3, {"R-A": -60, "R-B": -80}, rfid_zone="B")
    assert est.primary == "B"


def test_weighted_centroid_lies_between_receivers():
    est = ZoneEstimator(RX, smoothing=True)
    est.update(0, {"R-A": -60, "R-B": -75, "R-C": -78})
    x, y = est.centroid
    assert 0.0 <= x <= 10.0 and 0.0 <= y <= 8.0, (x, y)
    assert x < 5.0, "strongest receiver is A (x=0), so the estimate should be on A's side"
    # equal signals at A and B only -> exactly half-way
    est2 = ZoneEstimator(RX, smoothing=True)
    est2.update(0, {"R-A": -70, "R-B": -70})
    assert abs(est2.centroid[0] - 5.0) < 1e-9 and abs(est2.centroid[1]) < 1e-9


# --------------------------------------------------------------- tracker rules

def test_wrong_aircraft_at_belt_loader():
    tr = new_tracker()
    b, answer, t = bag_at_loader(tr, gate_booked="GA2", loader="GA4")   # booked AC 1234, loader serves AC 857
    assert answer == {"clear": False, "bag": b.id}
    assert b.status == "WRONG AIRCRAFT", b.status
    assert (b.tag, 1) in tr.tag_commands, tr.tag_commands      # mode 1 = flash red + beep
    assert tr.loaders["GA4"]["light"] == "red" and tr.loaders["GA4"]["paused"]
    assert any(a["status"] == "WRONG AIRCRAFT" and a["bag"] == b.id for a in tr.alerts.values())


def test_correct_flight_clears_with_green_blink():
    tr = new_tracker()
    b, answer, t = bag_at_loader(tr, gate_booked="GA4", loader="GA4")
    assert answer == {"clear": True, "bag": b.id}
    assert b.status == "ON TRACK" and b.cleared_t == t
    assert (b.tag, 2) in tr.tag_commands, tr.tag_commands      # mode 2 = OK green blink
    assert (b.tag, 1) not in tr.tag_commands
    assert tr.loaders["GA4"]["light"] == "green"
    assert not tr.alerts


def test_tag_not_returned_after_60_seconds():
    tr = new_tracker()
    b, answer, t_clear = bag_at_loader(tr, gate_booked="GA4", loader="GA4")
    tr.tick(t_clear + TAG_RETURN_S)          # exactly 60 s: still fine
    assert b.status == "ON TRACK", b.status
    tr.tick(t_clear + TAG_RETURN_S + 1)      # 61 s and the tag is still on the bag
    assert b.status == "TAG NOT RETURNED", b.status
    # handler finally removes the tag -> loading confirmed, alert closed
    tr.tag_removed(t_clear + 70, b.tag)
    assert b.phase == "loaded" and b.status == "ON TRACK"
    assert not any(a["active"] for a in tr.alerts.values())


def test_not_seen_after_12_seconds_of_silence():
    tr = new_tracker()
    b = checkin(tr, gate="GA4")
    hear(tr, 1, b.tag, "R-IA1")              # last time anyone hears the tag
    for t in range(2, 1 + NOT_SEEN_S):       # 11 quiet seconds: not yet
        tr.ble_second(t, {})
        tr.tick(t)
        assert b.status == "ON TRACK", (t, b.status)
    t = 1 + NOT_SEEN_S                       # 12 quiet seconds
    tr.ble_second(t, {})
    tr.tick(t)
    assert b.status == "NOT SEEN", b.status
    # the tag is heard again -> back to ON TRACK, and the loader will want a photo check
    hear(tr, t + 1, b.tag, "R-IA1")
    assert b.status == "ON TRACK" and b.reappeared


def test_staff_pull_clears_wrong_aircraft():
    tr = new_tracker()
    b, answer, t = bag_at_loader(tr, gate_booked="GA2", loader="GA4")
    assert b.status == "WRONG AIRCRAFT"
    effects = tr.action(t + 1, b.id, "pull", who="tester")
    assert effects == [("pull", b.id)]
    assert b.wrong_aircraft is None and not b.held
    assert tr.loaders["GA4"]["light"] == "green" and not tr.loaders["GA4"]["paused"]
    assert (b.tag, 0) in tr.tag_commands                         # tag back to normal (stop beeping)
    tr.tag_removed(t + 2, b.tag, pulled=True)                     # handler takes the tag off the pulled bag
    assert b.phase == "pulled" and b.status == "ON TRACK"
    assert "tester" in b.note
    assert not any(a["active"] for a in tr.alerts.values())


def test_passenger_lookup_needs_booking_ref_and_tag_number():
    tr = new_tracker()
    checkin(tr, bag="B1", plate="0141111111", ref="ABC123", tag="TAG-9001", gate="GA4")
    checkin(tr, bag="B2", plate="0142222222", ref="XYZ789", tag="TAG-9002", gate="GA2")
    ok = tr.passenger("ABC123", "0141111111")
    assert ok is not None and ok["plate"] == "0141111111" and ok["flight"] == "AC 857"
    assert tr.passenger(" abc123 ", "014 111 1111") is not None      # spaces / lower case are fine
    assert tr.passenger("ABC123", "0142222222") is None               # right ref, someone else's tag
    assert tr.passenger("XYZ789", "0141111111") is None               # someone else's ref, right tag
    assert tr.passenger("ABC123", "0149999999") is None               # unknown tag number
    assert tr.passenger("", "0141111111") is None


# --------------------------------------------------------------- SecureBag cache

def test_securebag_returns_cached_verdicts():
    r = securebag.check("test3")
    assert r["verdict"] == "pass" and r["pair"] == "test3" and r["score"] is not None
    assert securebag.check("test4")["verdict"] in ("pass", "review", "flag")


def test_securebag_unknown_pair_is_not_run():
    for pair in ("no_such_pair", None, ""):
        r = securebag.check(pair)
        assert r["verdict"] == "not_run" and r["score"] is None, r


# --------------------------------------------------------------- one short scenario run

def test_wrong_aircraft_scenario_in_simulation():
    from autotrack.sim import Simulation
    sim = Simulation(use_db=False, seed=1)
    sc = next(s for s in sim.world.scenarios if s["id"] == "wrong_aircraft")
    sim.seek(sc["key"] + 5, auto_staff=False)    # stop just after the bag reaches the loader
    rec = sim.tracker.bags[sc["bag"]]
    assert rec.status == "WRONG AIRCRAFT", (rec.status, rec.reason)
    assert rec.wrong_aircraft == sim.world.by_id[sc["bag"]].dest
    assert any(a["bag"] == sc["bag"] and a["status"] == "WRONG AIRCRAFT" for a in sim.tracker.alerts.values())
    # the misrouted scenario happened earlier in the same run: its alert must have been raised
    mis = next(s for s in sim.world.scenarios if s["id"] == "misrouted")
    assert any(a["bag"] == mis["bag"] and a["status"] == "MISROUTED" for a in sim.tracker.alerts.values())


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
    sys.exit(1 if failed else 0)
