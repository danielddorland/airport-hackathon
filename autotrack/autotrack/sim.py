"""
sim.py - runs the airport, second by second, and feeds AutoTrack.

Each simulated second:
  1. Belt jams start/stop (from the organisers' simulation)      -> jam messages
  2. New bags are checked in / tagged at transfer input           -> check-in message + RFID read
  3. Bags pass RFID tunnels at sortation                          -> RFID read messages
  4. Bags reach the belt loader after waiting in the make-up lane -> portal read (RFID + scale + camera)
  5. Handlers pull BLE tags off, tags go back to the return bins  -> tag removed / tag returned
  6. Every tag broadcasts; receivers report RSSI                  -> BLE sightings
  7. AutoTrack (tracker.py) updates zones and statuses            -> status changes + alerts on the feed

Everything in steps 1-6 is SIMULATED (we don't have an airport). Step 7 is the real AutoTrack
logic. In real mode, the one real ESP32 tag is a "live bag"; when it reaches lane GA4 its
BLE sightings come from the laptop's Bluetooth instead of the simulator.
"""

import copy
import random
from collections import deque

from .feed import Feed
from .layout import FLIGHTS, Layout, has_rfid
from .radio import SimRadio
from .tracker import Tracker, clock
from .world import BagTruth, World

LIVE_TAG = "TAG-0001"          # the real ESP32
LIVE_RECEIVER = "R-GA4"        # the laptop's Bluetooth = the BLE receiver at belt loader GA4
NEAR_LOADER_DBM = -33          # live tag counts as "at the loader portal" above this smoothed RSSI.
                               # Measured on Alex's laptop + the team's ESP32-S3 (Nov 7):
                               # ~30 cm = -24 dBm, ~3 m = -40 to -49 dBm. Re-measure with
                               # tools/check_ble.py if you use a different laptop.
RECHARGE_S = 300
TAG_POOL = 300
# What a checkpoint saves (everything that changes while the hour runs)
STATE_FIELDS = ["tracker", "radio", "rng", "t", "pool", "recharging", "phys", "hold_map", "fallen_tags", "stats"]


class Simulation:
    def __init__(self, smoothing=True, use_db=True, seed=1, measure=False, layout=None, world=None):
        self.smoothing, self.use_db, self.seed, self.measure = smoothing, use_db, seed, measure
        self.layout = layout or Layout()
        self.world = world or World(self.layout)
        self.checkpoints = {}       # sim second -> saved state, so jumps are instant (filled by server.py)
        self.feed = Feed(use_db=use_db)
        self.real = None            # RealBLE object in real mode (set by server.py)
        self.real_buffer = []       # live-tag RSSI values from the laptop since last second
        self.on_tag_command = None  # callback(serial, mode) for the real tag
        self.reset()

    # ------------------------------------------------------------------ setup
    def reset(self):
        self.feed.reset()
        self.tracker = Tracker(self.layout, self.feed, smoothing=self.smoothing)
        self.radio = SimRadio(self.layout.receivers(), seed=self.seed)
        self.rng = random.Random(self.seed + 7)
        self.t = 0
        self.pool = deque(f"TAG-{i:04d}" for i in range(2, TAG_POOL + 2))
        self.recharging = []        # (ready_t, serial)
        self.phys = {}              # bag id -> physical state of the bag
        self.hold_map = {}          # record id that was held at a portal -> physical bag id held
        self.fallen_tags = {}       # serial -> (x, y) lying on the floor
        self.jam_starts = {}
        self.jam_ends = {}
        for j in self.world.jams:
            self.jam_starts.setdefault(j["start"], []).append(j)
            self.jam_ends.setdefault(j["end"], []).append(j)
        self._clear_live()
        self.stats = {"n": 0, "ok": 0, "near": 0, "n_makeup": 0, "ok_makeup": 0}

    def seek(self, target, auto_staff=True):
        """Jump to second `target` by replaying from the start (~3 ms per simulated second).
        While fast-forwarding, simulated staff deal with obvious alerts after 60 s (pull misrouted
        bags, recover forgotten tags) so old alerts don't pile up. The live bag is not replayed."""
        best = max((c for c in self.checkpoints if c <= target), default=None)
        if best is not None and (target < self.t or best > self.t):
            self.restore(self.checkpoints[best])
        elif target < self.t:
            self.reset()
        while self.t < min(target, self.world.total_ticks):
            self.step()
            if auto_staff and self.t % 5 == 0:
                self._auto_staff()

    def _auto_staff(self):
        for a in list(self.tracker.alerts.values()):
            if not a["active"] or self.t - a["t"] < 60:
                continue
            act = {"MISROUTED": "pull", "WRONG AIRCRAFT": "pull", "TAG NOT RETURNED": "recover",
                   "IDENTITY REVIEW": "security", "IDENTITY FLAG": "security"}.get(a["status"])
            if act:
                self.action(a["bag"], act, who="staff (simulated)")

    # ------------------------------------------------------------------ checkpoints (instant jumps)
    def _memo(self):
        memo = {id(self.world): self.world, id(self.layout): self.layout, id(self.feed): self.feed}
        for b in self.world.bags:
            memo[id(b)] = b
        return memo

    def checkpoint(self):
        return copy.deepcopy({k: getattr(self, k) for k in STATE_FIELDS}, self._memo())

    def restore(self, cp):
        self.feed.reset()
        for k, v in copy.deepcopy(cp, self._memo()).items():
            setattr(self, k, v)
        self.tracker.feed = self.feed
        self._clear_live()

    def build_checkpoints(self, times, auto_staff=True):
        """Run a private copy of the hour once and save the state at each time in `times`."""
        helper = Simulation(smoothing=self.smoothing, use_db=False, seed=self.seed,
                            layout=self.layout, world=self.world)
        for target in sorted(set(times)):
            helper.seek(target, auto_staff=auto_staff)
            helper.tracker.feed = self.feed  # so the copy points at OUR feed, not the helper's
            cp = copy.deepcopy({k: getattr(helper, k) for k in STATE_FIELDS}, self._memo())
            helper.tracker.feed = helper.feed
            self.checkpoints[target] = cp

    def _clear_live(self):
        self.live = None
        self.live_near = False
        self.live_rssi = None
        self.live_last_real = None
        self.real_buffer = []

    def live_active(self):
        p = self.phys.get(self.live["id"]) if self.live else None
        return bool(p and p["tag_on"])

    # ------------------------------------------------------------------ tag pool
    def _take_tag(self):
        while self.recharging and self.recharging[0][0] <= self.t:
            self.pool.append(self.recharging.pop(0)[1])
        return self.pool.popleft() if self.pool else f"TAG-X{self.rng.randint(1000, 9999)}"

    # ------------------------------------------------------------------ live (real ESP32) bag
    def live_checkin(self, booked_gate="GA2"):
        """Check in the hero bag carrying the real ESP32 tag. It always physically goes to lane GA4
        (where the laptop is). Book it on GA2 (AC 1234) to show WRONG AIRCRAFT, or GA4 (AC 857) to show a clean load."""
        t = self.t
        if self.live_active():  # only one real ESP32: retire the previous live bag first
            old = self.live["id"]
            self.tracker.bags[old].note = "Live demo bag replaced by a new check-in."
            self._remove_tag(old, pulled=True)
        self._clear_live()
        # Clear loader GA4 for the live demo: simulated staff deal with any simulated bag that is
        # holding that belt (e.g. the built-in WRONG AIRCRAFT scenario happens at GA4 around 14:23).
        for a in list(self.tracker.alerts.values()):
            if a["active"] and (a["gate"] == "GA4" or self.tracker.loaders["GA4"]["bag"] == a["bag"]):
                act = {"MISROUTED": "pull", "WRONG AIRCRAFT": "pull", "TAG NOT RETURNED": "recover",
                       "IDENTITY REVIEW": "security", "IDENTITY FLAG": "security"}.get(a["status"])
                if act:
                    self.action(a["bag"], act, who="staff (simulated)")
        path = self.layout.shortest_path("IA1", "GA4")
        hist, tt = [], t
        for i, n in enumerate(path):
            if i:
                tt += self.layout.adj[path[i - 1]][n]
            hist.append([n, tt])
        raw = {"bag_id": f"LIVE-{(t * 7919) % 900 + 100}", "intake": "IA1", "dest": "GA4", "release": t,
               "deadline": None, "arrival": tt, "history": hist}
        truth = BagTruth(raw, self.layout, random.Random(t))
        truth.booked_gate = booked_gate
        World._build_segments(truth, {})
        truth.dwell = 10 ** 9   # waits in the lane until the tag is brought to the loader
        self.world.by_id[truth.bag_id] = truth
        self.live = {"truth": truth, "id": truth.bag_id}
        self._release(truth, tag=LIVE_TAG)
        return truth.bag_id

    def live_at_loader(self):
        """Button fallback: pretend the live tag was brought to the loader portal."""
        if self.live:
            self.live_near = True

    def add_real_sighting(self, s):
        if s.get("tag_serial") == LIVE_TAG and self.live_active():
            self.real_buffer.append(s["rssi"])
            del self.real_buffer[:-50]

    # ------------------------------------------------------------------ one second
    def _release(self, b, tag=None):
        tag = tag or self._take_tag()
        self.phys[b.bag_id] = {"truth": b, "tag": tag, "state": "moving", "removal_t": None, "dock_t": None,
                               "tag_on": True, "loaded_t": None}
        self.tracker.checkin(self.t, b.bag_id, b.plate, b.booking_ref, b.booked_gate, tag, b.weight,
                             b.deadline, b.transfer, checkin_photo=b.checkin_photo, where=b.intake)
        self.tracker.rfid(self.t, b.intake, b.plate)

    def step(self):
        t = self.t
        tr = self.tracker
        for j in self.jam_starts.get(t, []):
            tr.jam(t, j["from"], j["to"], True, j["reason"])
        for j in self.jam_ends.get(t, []):
            tr.jam(t, j["from"], j["to"], False)
        for b in self.world.releases.get(t, []):
            self._release(b)

        live_id = self.live["id"] if self.live else None
        for bid, p in list(self.phys.items()):
            b = p["truth"]
            st = p["state"]
            if st == "moving":
                for node, nt in b.history[1:]:
                    if nt == t and has_rfid(node) and self.rng.random() > 0.002:
                        tr.rfid(t, node, b.plate)
                if t >= b.arrival:
                    p["state"] = "makeup"
            if p["state"] == "makeup":
                ready = (t >= b.loader_tick) if bid != live_id else self.live_near
                ld = tr.loaders[b.dest]
                if ready and ld["paused"] and ld["bag"] not in (None, bid) and bid != live_id:
                    ready = False  # belt is paused for another bag: wait (the live demo bag never waits)
                if ready:
                    plate = self.world.by_id[b.plate_at_loader].plate if b.plate_at_loader else b.plate
                    scale = round(b.weight + self.rng.gauss(0, 0.3), 1)
                    d = tr.portal(t, b.dest, plate, scale_kg=scale, camera_photo=b.camera_photo)
                    if bid == live_id:
                        self.live_near = False
                    if d["clear"]:
                        p["state"] = "cleared"
                        p["removal_t"] = None if b.missed_removal else t + b.unload_delay
                        p["loaded_t"] = t + b.unload_delay
                    else:
                        p["state"] = "held"
                        if d["bag"]:
                            self.hold_map[d["bag"]] = bid
            if p["removal_t"] == t:
                self._remove_tag(bid)
            if p["dock_t"] == t:
                tr.tag_docked(t, p["tag"])
                if p["tag"] != LIVE_TAG:  # the real ESP32 is never handed to a simulated bag
                    self.recharging.append((t + RECHARGE_S, p["tag"]))
                if p["state"] in ("loaded", "pulled") and p["tag"] != LIVE_TAG:
                    del self.phys[bid]

        # BLE: every tag that is switched on broadcasts once this second
        tags = {}
        for bid, p in self.phys.items():
            b = p["truth"]
            if not p["tag_on"]:
                continue
            if b.silent and b.silent[0] <= t < b.silent[1]:
                continue
            if b.tag_falls_off and t >= b.tag_falls_off[0]:
                self.fallen_tags[p["tag"]] = b.tag_falls_off[1]
                continue
            pos = b.position(t)
            if p["state"] == "cleared" and p["loaded_t"] and t >= p["loaded_t"]:
                gx, gy = self.layout.xy_m[b.dest]  # tag forgotten on the bag: it's out on the ramp now
                pos = (gx + 8, gy) if b.dest.startswith("GB") else (gx, gy - 8)
            if pos:
                tags[p["tag"]] = pos
        tags.update(self.fallen_tags)
        heard = {}
        for r, s, v in self.radio.sightings(t, tags):
            heard.setdefault(s, {})[r] = v
        if self.real is not None and self.live:
            self._merge_real(heard)
        tr.ble_second(t, heard)
        tr.tick(t)

        while tr.tag_commands:
            serial, mode = tr.tag_commands.pop(0)
            names = {0: "normal", 1: "WRONG AIRCRAFT: flash red + beep", 2: "OK: green blink", 3: "locate"}
            self.feed.publish(t, "TAG_COMMAND", f"Tag {serial}: {names.get(mode, mode)}", tag=serial, mode=mode,
                              clock=clock(t), real=(serial == LIVE_TAG and self.real is not None))
            if serial == LIVE_TAG and self.on_tag_command:
                self.on_tag_command(serial, mode)

        if self.measure:
            self._measure(t)
        if self.use_db and t % 5 == 0:
            self.feed.flush()
        self.t += 1

    def _merge_real(self, heard):
        p = self.phys.get(self.live["id"])
        vals, self.real_buffer = self.real_buffer, []
        if not p or p["state"] not in ("makeup", "held", "cleared") or not vals:
            return
        # The laptop really heard the ESP32: use the REAL signal instead of the simulated one.
        # (If it hears nothing, the simulated signal stays, so the bag doesn't vanish from lane GA4.)
        v = sorted(vals)[len(vals) // 2]
        heard[LIVE_TAG] = {LIVE_RECEIVER: v}
        self.live_last_real = self.t
        self.live_rssi = v if self.live_rssi is None else self.live_rssi + 0.5 * (v - self.live_rssi)
        if p["state"] == "makeup" and self.live_rssi >= NEAR_LOADER_DBM:
            self.live_near = True

    def _remove_tag(self, bid, pulled=False):
        p = self.phys[bid]
        if not p["tag_on"]:
            return
        p["tag_on"] = False
        p["state"] = "pulled" if pulled else "loaded"
        self.fallen_tags.pop(p["tag"], None)
        self.tracker.tag_removed(self.t, p["tag"], pulled=pulled)
        p["dock_t"] = self.t + self.rng.randint(60, 240)

    # ------------------------------------------------------------------ staff actions
    def action(self, bag_id, action, who="ops"):
        for kind, rec in self.tracker.action(self.t, bag_id, action, who):
            bid = self.hold_map.pop(rec, rec)
            p = self.phys.get(bid)
            if not p:
                continue
            if kind == "pull":
                self._remove_tag(bid, pulled=True)
            elif kind == "release":
                p["state"] = "cleared"
                p["removal_t"] = self.t + p["truth"].unload_delay
                p["loaded_t"] = p["removal_t"]
            elif kind == "recover":
                self._remove_tag(bid)

    # ------------------------------------------------------------------ accuracy measurement
    def _measure(self, t):
        s = self.stats
        for bid, p in self.phys.items():
            b = p["truth"]
            if p["state"] not in ("moving", "makeup") or b.tag_falls_off or b.silent:
                continue
            rec = self.tracker.bags.get(bid)
            truth = b.true_zone(t)
            if not rec or truth is None or t == b.release:
                continue
            s["n"] += 1
            s["ok"] += rec.zone == truth
            s["near"] += rec.zone == truth or truth in [n["zone"] for n in rec.nearby]
            if p["state"] == "makeup":
                s["n_makeup"] += 1
                s["ok_makeup"] += rec.zone == truth

    # ------------------------------------------------------------------ snapshot for the web page
    def _nearest_zone(self, x, y):
        xy = self.layout.xy_m
        return min(xy, key=lambda n: (xy[n][0] - x) ** 2 + (xy[n][1] - y) ** 2)

    def _belt_of(self, bag_id):
        p = self.phys.get(bag_id)
        if not p:
            return None
        if p["state"] in ("makeup", "held", "cleared", "loaded", "pulled"):
            return [p["truth"].dest, p["truth"].dest, 0.0]
        return p["truth"].belt(self.t)

    def snapshot(self):
        tr = self.tracker
        active = tr.active()
        bags = sorted(active, key=lambda b: (PRIO.get(b.status, 9), b.id))
        occupancy = {}
        for b in active:
            if b.zone:
                occupancy[b.zone] = occupancy.get(b.zone, 0) + 1
        jams = [{"from": f, "to": to, "since": clock(s), "reason": r} for (f, to), (s, r) in tr.jams.items()]
        in_use = sum(1 for p in self.phys.values() if p["tag_on"])
        return {
            "t": self.t, "clock": clock(self.t), "total": self.world.total_ticks,
            "phase": next((ph["name"] for ph in PHASES if ph["start_tick"] <= self.t <= ph["end_tick"]), "hour finished"),
            "counts": tr.counts(), "active": len(active),
            # "belt" = where the bag physically is in the simulation (for drawing it on the belts);
            # "zone"/"nearby" = where AutoTrack THINKS it is (from the noisy BLE signals).
            "bags": [{**b.to_json(brief=True), "belt": self._belt_of(b.id)} for b in bags[:400]],
            "alerts": sorted([a for a in tr.alerts.values() if a["active"]], key=lambda a: (PRIO.get(a["status"], 9), -a["t"])),
            "recent_alerts": sorted(tr.alerts.values(), key=lambda a: -a["t"])[:30],
            "loaders": tr.loaders, "flights": FLIGHTS, "occupancy": occupancy, "jams": jams,
            "fallen_tags": [{"tag": s, "x": x, "y": y, "zone": self._nearest_zone(x, y)} for s, (x, y) in self.fallen_tags.items()],
            "tags": {"in_use": in_use, "in_return_bins": len(tr.tags_in_bins), "returned_total": tr.tags_returned,
                     "charging": len(self.recharging), "available": len(self.pool)},
            "live": ({"id": self.live["id"], "tag": LIVE_TAG, "rssi": None if self.live_rssi is None else round(self.live_rssi),
                      "real_recent": self.live_last_real is not None and self.t - self.live_last_real <= 3,
                      "near_dbm": NEAR_LOADER_DBM, "real": self.real is not None,
                      "real_status": getattr(self.real, "status", None)} if self.live else
                     {"id": None, "real": self.real is not None, "real_status": getattr(self.real, "status", None)}),
            "congestion": round(tr.congestion, 2),
        }


PRIO = {"IDENTITY FLAG": 0, "WRONG AIRCRAFT": 1, "IDENTITY REVIEW": 2, "MISROUTED": 3, "TAG NOT RETURNED": 4,
        "NOT SEEN": 5, "LATE": 6, "ON TRACK": 7}
PHASES = [{"name": "warm up", "start_tick": 0, "end_tick": 300}, {"name": "first pressure", "start_tick": 301, "end_tick": 900},
          {"name": "sustained load", "start_tick": 901, "end_tick": 2100}, {"name": "crisis peak", "start_tick": 2101, "end_tick": 3000},
          {"name": "wind down", "start_tick": 3001, "end_tick": 3600}]
