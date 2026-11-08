"""
world.py - the "real world" that AutoTrack is watching (ground truth).

AutoTrack never reads this file's data directly. The simulator (sim.py) uses it to decide
where every bag *really* is, then produces the sensor messages a real airport would
produce (BLE sightings, RFID reads, belt-scale weights, camera photos). AutoTrack only
sees those messages. Because we know the truth, we can measure how often AutoTrack is right.

This file also adds what the organisers' simulation doesn't have: flights, bag tag numbers,
booking references, weights, the make-up lane wait before loading, and the 7 demo scenarios.
"""

import json
import random
from pathlib import Path

from .layout import AIRLINE_NUMERIC, FLIGHTS, ROOT

SEED = 857  # change it and you get different (but still repeatable) bookings/weights/noise


class BagTruth:
    def __init__(self, raw, layout, rng):
        self.bag_id = raw["bag_id"]
        self.intake = raw["intake"]
        self.dest = raw["dest"]                  # where the conveyor physically sends it
        self.booked_gate = raw["dest"]           # where its booking says it should go
        self.release = raw["release"]
        self.deadline = raw["deadline"]          # make-up cut-off for this bag (may be None)
        self.arrival = raw["arrival"]
        self.history = [(n, t) for n, t in raw["history"]]
        self.transfer = self.intake.startswith("IB")
        self.weight = round(rng.uniform(8, 28), 1)
        self.booking_ref = "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
        self.serial6 = rng.randint(100000, 999999)
        self.dwell = rng.randint(25, 60)         # seconds waiting in the make-up lane
        self.unload_delay = rng.randint(4, 10)   # loader read -> handler pulls the BLE tag off
        self.layout = layout
        self.segments = []                       # filled by World (needs jam data)
        # Scenario tweaks (None = normal bag)
        self.scenario = None
        self.plate_at_loader = None              # tag swap: the paper tag on this bag belongs to another bag
        self.tag_falls_off = None                # (tick, (x, y)) the BLE tag drops off and stays there
        self.silent = None                       # (t0, t1) the BLE tag can't be heard (e.g. behind metal)
        self.missed_removal = False              # handler forgets to remove the BLE tag
        self.checkin_photo = None                # which real SecureBag sample pair photo was taken at check-in
        self.camera_photo = None                 # ...and which one the belt-loader camera takes of this physical bag

    @property
    def flight(self):
        return FLIGHTS[self.booked_gate]["flight"]

    @property
    def plate(self):
        """10-digit IATA licence plate printed on the paper bag tag (and stored in its RFID inlay)."""
        return "0" + AIRLINE_NUMERIC[self.flight.split()[0]] + str(self.serial6)

    @property
    def loader_tick(self):
        return self.arrival + self.dwell

    def position(self, t):
        """True (x, y) in metres while the bag is on the conveyors / in make-up, else None."""
        if t < self.release:
            return None
        for seg in self.segments:
            if seg["t0"] <= t < seg["t1"]:
                frac = seg["moved"](t)
                (x0, y0), (x1, y1) = self.layout.xy_m[seg["from"]], self.layout.xy_m[seg["to"]]
                return (x0 + (x1 - x0) * frac, y0 + (y1 - y0) * frac)
        return self.layout.xy_m[self.dest]  # in the make-up lane (or beyond: caller decides)

    def belt(self, t):
        """Where the bag physically is, as (from_node, to_node, fraction along that belt).
        Waiting in the make-up lane = (dest, dest, 0). Used to draw bags moving along the belts."""
        if t < self.release:
            return None
        for seg in self.segments:
            if seg["t0"] <= t < seg["t1"]:
                return [seg["from"], seg["to"], round(seg["moved"](t), 3)]
        return [self.dest, self.dest, 0.0]

    def true_zone(self, t):
        """The zone the bag is really closest to (for measuring accuracy)."""
        p = self.position(t)
        if p is None:
            return None
        return min(self.layout.xy_m, key=lambda n: (self.layout.xy_m[n][0] - p[0]) ** 2 + (self.layout.xy_m[n][1] - p[1]) ** 2)


class World:
    def __init__(self, layout, seed=SEED):
        self.layout = layout
        data = json.loads((ROOT / "data" / "replay.json").read_text())
        self.total_ticks = data["total_ticks"]
        self.jams = data["jams"]
        rng = random.Random(seed)
        self.bags = [BagTruth(b, layout, rng) for b in data["bags"]]
        self.by_id = {b.bag_id: b for b in self.bags}
        jams_by_belt = {}
        for j in self.jams:
            jams_by_belt.setdefault((j["from"], j["to"]), []).append((j["start"], j["end"]))
        for b in self.bags:
            self._build_segments(b, jams_by_belt)
        self.scenarios = self._pick_scenarios()
        self.releases = {}
        for b in self.bags:
            self.releases.setdefault(b.release, []).append(b)

    @staticmethod
    def _build_segments(bag, jams_by_belt):
        """Between two conveyor points the bag moves along the belt, but it freezes while
        that belt is jammed (exactly like the organisers' evaluator)."""
        for (n0, t0), (n1, t1) in zip(bag.history, bag.history[1:]):
            jams = jams_by_belt.get((n0, n1), [])

            def moving_ticks(a, b, jams=jams):
                # number of ticks in (a, b] when this belt was running
                total = max(0, b - a)
                for s, e in jams:
                    total -= max(0, min(b, e - 1) - max(a, s - 1))
                return max(0, total)

            whole = moving_ticks(t0, t1) or 1

            def moved(t, t0=t0, whole=whole, mt=moving_ticks):
                return min(1.0, mt(t0, t) / whole)

            bag.segments.append({"t0": t0, "t1": t1, "from": n0, "to": n1, "moved": moved})

    # ------------------------------------------------------------------ scenarios
    def _pick(self, test, after, used):
        for b in sorted(self.bags, key=lambda b: b.release):
            if b.release >= after and b.bag_id not in used and test(b):
                used.add(b.bag_id)
                return b
        raise RuntimeError("no bag fits scenario")

    def _pick_scenarios(self):
        used = set()
        on_time = lambda b: b.deadline and b.arrival < b.deadline - 10  # noqa: E731
        sc = []

        b = self._pick(lambda b: b.intake.startswith("IA") and b.dest == "GA4" and on_time(b), 600, used)
        b.scenario = "on_track"
        sc.append(dict(id="on_track", bag=b.bag_id, title="Normal bag ON TRACK",
                       story=f"{b.bag_id} is checked in at {b.intake}, followed zone by zone, cleared at loader {b.dest} and its tag comes off.",
                       start=b.release - 5, key=b.release))

        b = self._pick(lambda b: b.dest == "GA3" and on_time(b), 950, used)
        b.scenario, b.booked_gate = "misrouted", "GA4"
        sc.append(dict(id="misrouted", bag=b.bag_id, title="MISROUTED to the wrong make-up lane",
                       story=f"{b.bag_id} is booked on AC 857 (lane GA4) but the sorter sends it to lane GA3. "
                             "AutoTrack sees it in the wrong lane before anyone loads it. Click 'Pull bag' to fix it.",
                       start=b.arrival - 25, key=b.arrival))

        b = self._pick(lambda b: b.dest == "GA4" and on_time(b), 1250, used)
        b.scenario, b.booked_gate = "wrong_aircraft", "GA2"
        sc.append(dict(id="wrong_aircraft", bag=b.bag_id, title="WRONG AIRCRAFT at the belt loader",
                       story=f"{b.bag_id} is booked on AC 1234 but ends up at loader GA4, which serves AC 857. "
                             "Nobody acted on the lane warning, so the loader's RFID portal stops it: tag beeps, loader light goes red.",
                       start=b.loader_tick - 20, key=b.loader_tick))

        # The victim is a bag on the same flight that is still on the belts (its BLE tag is elsewhere)
        # when the swapped bag reaches the loader. Pick the first pair that fits.
        def victim_for(a):
            return [v for v in self.bags if v is not a and v.bag_id not in used and v.dest == a.dest
                    and v.arrival > a.loader_tick + 5 and v.release < a.loader_tick - 5]

        a = self._pick(lambda b: b.dest.startswith("GB") and on_time(b) and victim_for(b), 1650, used)
        victim = victim_for(a)[0]
        used.add(victim.bag_id)
        a.scenario, a.plate_at_loader, a.camera_photo = "tag_swap", victim.bag_id, "test4"
        victim.scenario, victim.checkin_photo = "tag_swap_victim", "test4"
        sc.append(dict(id="tag_swap", bag=a.bag_id, other=victim.bag_id, title="Tag swap: BLE and RFID disagree",
                       story=f"Someone moved {victim.bag_id}'s paper tag onto {a.bag_id} in the make-up lane. At loader {a.dest} the RFID "
                             f"says '{victim.bag_id}' but the BLE tag on the belt belongs to {a.bag_id}. SecureBag compares photos (sample pair test4).",
                       start=a.loader_tick - 20, key=a.loader_tick))

        b = self._pick(lambda b: b.intake.startswith("IA") and len(b.history) >= 4 and b.history[1][0].startswith("J")
                       and b.history[2][0].startswith("J") and on_time(b), 1980, used)
        node, tick = b.history[1]
        b.scenario, b.tag_falls_off = "tag_fell_off", (tick + 1, self.layout.xy_m[node])
        b.checkin_photo = b.camera_photo = "test3"
        sc.append(dict(id="tag_fell_off", bag=b.bag_id, title="Tag fell off: RFID read with no BLE tag",
                       story=f"{b.bag_id}'s BLE tag drops off at {node}. The next RFID tunnel reads the bag without its tag. "
                             "The photo check at the loader confirms it's the right bag (sample pair test3), and staff are told where the tag is lying.",
                       start=tick - 5, key=b.history[2][1]))

        b = self._pick(lambda b: b.transfer and b.deadline and b.arrival > b.deadline and b.arrival - b.release >= 70
                       and len(b.history) >= 4, 2380, used)
        t_silent = b.history[2][1] + 1
        b.scenario, b.silent = "transfer_late", (t_silent, min(t_silent + 30, b.arrival - 5))
        b.checkin_photo = b.camera_photo = "test3"
        sc.append(dict(id="transfer_late", bag=b.bag_id, title="Transfer bag NOT SEEN, then LATE",
                       story=f"Transfer bag {b.bag_id} enters at {b.intake} during the crisis peak. Its tag goes quiet for a while "
                             "(NOT SEEN), and belt jams mean it will miss its make-up cut-off: AutoTrack says LATE before the deadline passes, with the reason.",
                       start=b.release - 5, key=b.release))

        b = self._pick(lambda b: b.dest.startswith("GB") and on_time(b) and b.loader_tick < 3400, 3000, used)
        b.scenario, b.missed_removal = "tag_not_returned", True
        sc.append(dict(id="tag_not_returned", bag=b.bag_id, title="Tag not removed: TAG NOT RETURNED",
                       story=f"{b.bag_id} is cleared at loader {b.dest} but the handler forgets to pull the BLE tag off. "
                             "AutoTrack raises TAG NOT RETURNED before the flight closes. Click 'Tag recovered' to close it.",
                       start=b.loader_tick + 25, key=b.loader_tick + 61))
        return sc
