"""
tracker.py - the AutoTrack brain.

It only knows what the sensors tell it (the same messages a real airport would send):
  * check-in / transfer input:  bag tag number + BLE tag serial + flight + weight + photo
  * BLE sightings:              which receivers hear which tag, and how strongly
  * RFID reads:                 "bag tag number X was read at reader Y"
  * belt-loader portal:         RFID read + belt-scale weight + camera photo
  * BLE tag removed / docked:   loading confirmation / tag back in the return bin
  * belt jam messages:          from the baggage handling system

From those it keeps one record per bag and gives every bag ONE status with a plain-English
reason. Priority (most serious first):

  IDENTITY FLAG > WRONG AIRCRAFT > IDENTITY REVIEW > MISROUTED > TAG NOT RETURNED > NOT SEEN > LATE > ON TRACK

Every change is published on the shared feed (feed.py). A human always makes the final call:
alerts come with buttons (pull bag, approve, hold for security, tag recovered).
"""

from . import securebag
from .layout import FLIGHTS, KIND_LABEL, zone_kind, zone_name
from .positioning import ZoneEstimator

NOT_SEEN_S = 12          # no BLE signal for this long -> NOT SEEN
MISROUTE_STABLE_S = 10   # bag must sit in the wrong make-up lane this long...
LANE_STRONG_DBM = -68    # ...with a STRONG signal at that lane's receiver (bags 12 m away on a belt read ~-80 dBm)
DETACH_FAR_M = 20        # RFID read this far from where the BLE tag is...
DETACH_STILL_S = 5       # ...while the BLE tag hasn't moved for this long...
DETACH_CONFIRM_S = 6     # ...and it still hasn't caught up this many seconds later -> the tag fell off
TAG_RETURN_S = 60        # tag must come off within this long after the loader clears the bag
WEIGHT_TOLERANCE_KG = 2.0
JAM_PENALTY_S = 30       # extra seconds we expect a jammed belt to cost
LATE_MARGIN_S = 5        # predicted arrival must be at least this far past the cut-off...
LATE_CONFIRM_S = 1       # ...for this many seconds in a row before we say LATE
START_CLOCK = 14 * 3600  # sim second 0 is shown as 14:00:00

PRIORITY = ["IDENTITY FLAG", "WRONG AIRCRAFT", "IDENTITY REVIEW", "MISROUTED",
            "TAG NOT RETURNED", "NOT SEEN", "LATE", "ON TRACK"]


def clock(t):
    s = START_CLOCK + int(t)
    return f"{s // 3600 % 24:02d}:{s // 60 % 60:02d}:{s % 60:02d}"


class BagRecord:
    def __init__(self, t, bag_id, plate, booking_ref, booked_gate, tag, weight, deadline, transfer, checkin_photo):
        self.id, self.plate, self.booking_ref = bag_id, plate, booking_ref
        self.booked_gate, self.tag, self.weight = booked_gate, tag, weight
        self.flight = FLIGHTS[booked_gate]["flight"]
        self.deadline, self.transfer, self.checkin_photo = deadline, transfer, checkin_photo
        self.checkin_t = t
        self.phase = "in_system"     # in_system -> loaded | pulled
        self.zone, self.zone_since, self.nearby, self.centroid = None, t, [], None
        self.last_seen = t
        self.last_rfid = None        # (zone, t)
        self.status, self.reason, self.status_since = "ON TRACK", "Checked in", t
        self.wrong_aircraft = None   # loader zone where it was stopped
        self.identity = None         # ("IDENTITY REVIEW"/"IDENTITY FLAG", reason)
        self.photo = None
        self.tag_detached = None     # zone where its BLE tag was left behind
        self.reappeared = False
        self.was_not_seen = False
        self.loader_reads = []       # [(zone, t, tag seen at portal)]
        self.cleared_t = None
        self.cleared_tag = None
        self.loaded_t = None
        self.note = None             # staff decision text
        self.held = False
        self.late_first = None       # first second we predicted LATE (for measuring)
        self.lane_strong = None      # (make-up zone, since) while the tag is heard strongly in a make-up lane
        self.cleared_zone = None
        self.portal_tag = None
        self.final = False
        self.detach_pending = None
        self.late_streak = 0
        self.force_alert = False
        self.history = []

    def log(self, t, text):
        self.history.append({"t": t, "clock": clock(t), "text": text})

    def to_json(self, brief=False):
        d = {"id": self.id, "plate": self.plate, "flight": self.flight, "booked_gate": self.booked_gate,
             "tag": self.tag, "status": self.status, "reason": self.reason, "zone": self.zone,
             "zone_name": zone_name(self.zone), "nearby": self.nearby, "phase": self.phase,
             "transfer": self.transfer, "status_since": self.status_since, "centroid": self.centroid,
             "tag_detached": self.tag_detached}
        if not brief:
            d.update({"booking_ref": self.booking_ref, "weight": self.weight, "deadline": self.deadline,
                      "deadline_clock": clock(self.deadline) if self.deadline else None,
                      "centroid": self.centroid, "last_seen": self.last_seen, "photo": self.photo,
                      "history": self.history[-40:], "note": self.note, "checkin_clock": clock(self.checkin_t),
                      "destination": FLIGHTS[self.booked_gate]["to"]})
        return d


class Tracker:
    def __init__(self, layout, feed, smoothing=True):
        self.layout, self.feed, self.smoothing = layout, feed, smoothing
        self.receivers = layout.receivers()
        self.bags = {}                 # bag id -> BagRecord
        self.by_plate = {}
        self.bag_of_tag = {}           # BLE serial -> bag id (only while linked)
        self.estimators = {}           # BLE serial -> ZoneEstimator
        self.jams = {}                 # (from, to) -> (since, reason)
        self.alerts = {}               # alert id -> dict
        self.loaders = {g: {"light": "green", "paused": False, "msg": "Ready", "bag": None} for g in FLIGHTS}
        self.tag_commands = []         # (serial, mode) for the tags to beep/flash
        self.congestion = 1.5          # learned: real travel time / belt base time
        self.tags_returned = 0
        self.tags_in_bins = set()
        self.t = 0

    # ------------------------------------------------------------------ helpers
    def _pub(self, t, type_, text, bag=None, **kw):
        return self.feed.publish(t, type_, text, bag=bag.id if bag else None, clock=clock(t), **kw)

    def active(self):
        return [b for b in self.bags.values() if b.phase == "in_system"]

    # ------------------------------------------------------------------ inputs
    def checkin(self, t, bag_id, plate, booking_ref, booked_gate, tag, weight, deadline, transfer,
                checkin_photo=None, where=None):
        b = BagRecord(t, bag_id, plate, booking_ref, booked_gate, tag, weight, deadline, transfer, checkin_photo)
        self.bags[bag_id] = b
        self.by_plate[plate] = bag_id
        self.bag_of_tag[tag] = bag_id
        self.estimators[tag] = ZoneEstimator(self.receivers, self.smoothing)
        b.zone = where
        what = "Transfer bag tagged at" if transfer else "Checked in at"
        b.log(t, f"{what} {zone_name(where)}: bag tag {plate}, BLE tag {tag}, {b.flight}, {weight} kg")
        self._pub(t, "BAG_CHECKED_IN", f"{bag_id} {what.lower()} {where} for {b.flight} (BLE tag {tag})", b,
                  zone=where, tag=tag, flight=b.flight)

    def ble_second(self, t, heard):
        """heard: {serial: {receiver_id: rssi}} for one second. Called every second."""
        for tag, est in self.estimators.items():
            bag = self.bags.get(self.bag_of_tag.get(tag))
            if bag is None or bag.phase != "in_system":
                continue
            readings = heard.get(tag, {})
            rf = bag.last_rfid[0] if bag.last_rfid and bag.last_rfid[1] == t and not bag.tag_detached else None
            changed = est.update(t, readings, rfid_zone=rf)
            if readings:
                if bag.was_not_seen and not bag.tag_detached:
                    gap = t - bag.last_seen
                    bag.was_not_seen, bag.reappeared = False, True
                    bag.log(t, f"BLE tag heard again after {gap} s near {zone_name(est.primary)} - photo check will run at the loader")
                    self._pub(t, "TAG_REAPPEARED", f"{bag.id}'s tag reappeared after {gap} s near {est.primary}", bag, zone=est.primary)
                bag.last_seen = t
            pend = bag.detach_pending
            if pend and not bag.tag_detached and t - pend[2] >= DETACH_CONFIRM_S:
                bag.detach_pending = None
                if est.primary == pend[0]:   # the tag still hasn't moved, but the bag has
                    bag.tag_detached = pend[0]
                    text = (f"RFID read {bag.id} at {zone_name(pend[1])} {t - pend[2]} s ago, but its BLE tag {bag.tag} "
                            f"has not moved from {zone_name(pend[0])}. The tag probably fell off - staff can collect it there. "
                            f"Tracking this bag on RFID reads until the loader photo check.")
                    bag.log(t, text)
                    self._pub(t, "TAG_DETACHED", text, bag, zone=pend[1], tag_zone=pend[0])
                    bag.zone = bag.last_rfid[0] if bag.last_rfid else pend[1]
            if bag.tag_detached:
                continue  # the tag is lying somewhere; the bag's position comes from RFID now
            if bag.cleared_t is not None:
                continue  # cleared at the loader: it stays at that loader until its tag comes off
            if changed and est.primary:
                bag.zone, bag.zone_since = est.primary, t
                self._pub(t, "ZONE_CHANGED", f"{bag.id} -> {zone_name(est.primary)}", bag, zone=est.primary,
                          nearby=[n["zone"] for n in est.nearby])
            bag.nearby, bag.centroid = est.nearby, est.summary()["centroid"]
            z = est.primary
            if z and zone_kind(z) == "makeup" and est.zone_rssi(z) >= LANE_STRONG_DBM:
                if not bag.lane_strong or bag.lane_strong[0] != z:
                    bag.lane_strong = (z, t)
            else:
                bag.lane_strong = None

    def rfid(self, t, zone, plate):
        """An RFID tunnel (check-in, transfer input or sortation) read a bag tag number."""
        bag = self.bags.get(self.by_plate.get(plate))
        if bag is None or bag.phase != "in_system":
            self._pub(t, "RFID_UNKNOWN", f"Unknown bag tag {plate} read at {zone}", zone=zone)
            return
        prev = bag.last_rfid
        bag.last_rfid = (zone, t)
        if prev and prev[0] != zone:
            base = self.layout.costs_from(prev[0]).get(zone)
            if base:
                self.congestion += 0.05 * (min(4.0, max(1.0, (t - prev[1]) / base)) - self.congestion)
        self._pub(t, "RFID_READ", f"RFID {zone} read {plate} ({bag.id})", bag, zone=zone, plate=plate)
        # Does the BLE tag agree with the RFID read?
        est = self.estimators[bag.tag]
        if not bag.tag_detached and t - bag.checkin_t > 3:
            tag_zone = est.primary
            close = tag_zone == zone or zone in [n["zone"] for n in est.nearby]
            heard_recently = est.last_heard is not None and t - est.last_heard <= 3
            still = est.primary_since is not None and t - est.primary_since >= DETACH_STILL_S
            if heard_recently and still and not close and tag_zone and self._far(tag_zone, zone):
                # Don't decide yet: on a fast belt the BLE zone can lag a few seconds behind.
                bag.detach_pending = (tag_zone, zone, t)
            elif close:
                bag.detach_pending = None
        bag.zone, bag.zone_since = zone, t if bag.zone != zone else bag.zone_since

    def _far(self, z1, z2):
        (x1, y1), (x2, y2) = self.layout.xy_m[z1], self.layout.xy_m[z2]
        return (x1 - x2) ** 2 + (y1 - y2) ** 2 > DETACH_FAR_M ** 2

    def portal(self, t, zone, plate, scale_kg=None, camera_photo=None):
        """RFID portal at a belt loader. Runs the wrong-plane check and the identity checks.
        Returns {"clear": bool, "bag": record id or None}."""
        flight_here = FLIGHTS[zone]["flight"]
        bag = self.bags.get(self.by_plate.get(plate))
        if bag is None:
            self._pub(t, "RFID_UNKNOWN", f"Loader {zone}: unknown bag tag {plate} - hold for a human", zone=zone)
            return {"clear": False, "bag": None}
        # Which BLE tags are at this loader right now?
        present = []
        for tag, est in self.estimators.items():
            owner = self.bags.get(self.bag_of_tag.get(tag))
            if owner and owner.phase == "in_system" and est.primary == zone and est.last_heard is not None \
                    and t - est.last_heard <= 3:
                present.append(tag)
        own_here = bag.tag in present and not bag.tag_detached
        tag_seen = bag.tag if own_here else None
        bag.portal_tag = tag_seen
        bag.zone, bag.zone_since = zone, t
        self._pub(t, "PORTAL_READ", f"Loader {zone} ({flight_here}) RFID read {plate} ({bag.id})", bag, zone=zone,
                  plate=plate, scale_kg=scale_kg)

        anomalies, flag = [], None
        if bag.loader_reads:
            z0, t0, tag0 = bag.loader_reads[0]
            first = f"with its own BLE tag {tag0} present" if tag0 else "while its own BLE tag was somewhere else"
            now = f"with its own BLE tag {tag_seen} present" if tag_seen else "without its own BLE tag"
            flag = (f"Duplicate bag tag: {plate} was already read at loader {z0} at {clock(t0)} {first}, and is now "
                    f"read again {now}. Two bags carry the same tag number - stop and call security.")
        bag.loader_reads.append((zone, t, tag_seen))
        if bag.tag_detached:
            anomalies.append(f"its BLE tag fell off at {bag.tag_detached}")
        elif not own_here:
            others = [self.bag_of_tag[x] for x in present if x != bag.tag]
            own_est = self.estimators.get(bag.tag)
            where = zone_name(own_est.primary) if own_est and own_est.primary else "nowhere nearby"
            if others:
                anomalies.append(f"BLE and RFID disagree: RFID says {bag.id}, but {bag.id}'s BLE tag {bag.tag} is in "
                                 f"{where}, not at this loader (possible tag swap)")
            else:
                anomalies.append("RFID read with no BLE tag nearby (tag may have fallen off)")
        if bag.reappeared:
            anomalies.append("its tag was NOT SEEN for a while and then reappeared")
        if scale_kg is not None and abs(scale_kg - bag.weight) > WEIGHT_TOLERANCE_KG:
            anomalies.append(f"belt scale says {scale_kg} kg but it weighed {bag.weight} kg at check-in")

        # Photo check (SecureBag) - every bag at the loader, using the real cached verdicts.
        if bag.checkin_photo and camera_photo == bag.checkin_photo:
            photo = securebag.check(bag.checkin_photo)
        else:
            photo = {"verdict": "not_run", "reason": "no photos in simulation for this bag", "pair": None}
        bag.photo = photo
        if photo["verdict"] != "not_run":
            self._pub(t, "PHOTO_VERDICT", f"SecureBag {photo['verdict'].upper()} for {bag.id} "
                      f"(score {photo['score']}, {photo['reason']}; sample pair {photo['pair']})", bag,
                      zone=zone, verdict=photo["verdict"])

        lights = self.loaders[zone]
        if FLIGHTS[bag.booked_gate]["flight"] != flight_here:
            bag.wrong_aircraft = zone
            bag.held = True
            text = (f"Loader {zone} serves {flight_here} but {bag.id} is booked on {bag.flight} "
                    f"(lane {bag.booked_gate}). Belt paused, BLE tag beeping. Pull the bag.")
            bag.log(t, "WRONG AIRCRAFT: " + text)
            lights.update(light="red", paused=True, msg=f"STOP: {bag.id} is for {bag.flight}", bag=bag.id)
            self.tag_commands.append((bag.tag, 1))
            if tag_seen and tag_seen != bag.tag:
                self.tag_commands.append((tag_seen, 1))
            return {"clear": False, "bag": bag.id}

        if flag:
            bag.identity = ("IDENTITY FLAG", flag)
        elif photo["verdict"] == "flag":
            bag.identity = ("IDENTITY FLAG", f"SecureBag photo check FLAG ({photo['reason']}). Stop - call supervisor/security.")
        elif photo["verdict"] == "review":
            why = "; ".join(anomalies) or "routine loader check"
            bag.identity = ("IDENTITY REVIEW", f"Photo check says REVIEW ({photo['reason']}, score {photo['score']}). "
                                               f"Why we checked: {why}. A human must compare the bag with its check-in photo.")
        elif anomalies and photo["verdict"] != "pass":
            bag.identity = ("IDENTITY REVIEW", "Check needed: " + "; ".join(anomalies) + ". No photo result - check the bag by hand.")
        if bag.identity:
            bag.held = True
            kind = bag.identity[0]
            bag.force_alert = True  # a fresh alert even if the bag already had this status
            bag.log(t, f"{kind} at loader {zone}: {bag.identity[1]}")
            lights.update(light="red" if kind == "IDENTITY FLAG" else "amber", paused=True,
                          msg=f"HOLD: {bag.id} {kind.lower()}", bag=bag.id)
            return {"clear": False, "bag": bag.id}

        note = ""
        if anomalies:
            note = f" ({'; '.join(anomalies)} - photo check PASS confirms it is the same bag)"
        bag.cleared_t, bag.cleared_tag, bag.cleared_zone = t, tag_seen if tag_seen else bag.tag, zone
        bag.log(t, f"Cleared at loader {zone} for {flight_here}{note}. Remove the BLE tag to confirm loading.")
        self._pub(t, "LOADER_CLEARED", f"{bag.id} cleared at {zone} for {flight_here}{note}", bag, zone=zone)
        if not lights["paused"]:  # never un-pause a belt that is stopped for another bag
            lights.update(light="green", paused=False, msg=f"OK: {bag.id} - remove tag", bag=bag.id)
        if not bag.tag_detached:
            self.tag_commands.append((bag.tag, 2))
        return {"clear": True, "bag": bag.id}

    def tag_removed(self, t, serial, pulled=False):
        """Handler took the BLE tag off (loading confirmation), or staff pulled the bag."""
        bag_ids = [b.id for b in self.bags.values() if b.phase == "in_system" and
                   (b.tag == serial or (b.cleared_tag == serial and b.cleared_t is not None))]
        for bid in bag_ids:
            b = self.bags[bid]
            if pulled:
                b.phase = "pulled"
                b.log(t, f"Pulled by staff; BLE tag {serial} removed. {b.note or ''}")
            else:
                b.phase, b.loaded_t = "loaded", t
                b.log(t, f"BLE tag {serial} removed = loading confirmed on {b.flight}")
                self._pub(t, "BAG_LOADED", f"{b.id} loaded on {b.flight} (tag {serial} removed)", b, zone=b.zone)
        self.tags_in_bins.add(serial)
        self.bag_of_tag.pop(serial, None)
        self.estimators.pop(serial, None)
        self._pub(t, "TAG_REMOVED", f"BLE tag {serial} removed{' (bag pulled)' if pulled else ''}", tag=serial)
        self._evaluate(t)

    def tag_docked(self, t, serial):
        self.tags_in_bins.discard(serial)
        self.tags_returned += 1
        self._pub(t, "TAG_RETURNED", f"BLE tag {serial} docked in the return bin (recharging)", tag=serial)

    def jam(self, t, frm, to, on, reason=""):
        if on:
            self.jams[(frm, to)] = (t, reason)
            self._pub(t, "BELT_JAMMED", f"Belt {frm}->{to} jammed ({reason.replace('_', ' ').lower()})", zone=frm)
        else:
            self.jams.pop((frm, to), None)
            self._pub(t, "BELT_CLEARED", f"Belt {frm}->{to} running again", zone=frm)

    # ------------------------------------------------------------------ staff actions
    def action(self, t, bag_id, action, who="ops"):
        """Returns a list of effects for the simulator: ('pull'|'release'|'recover', bag_id)."""
        b = self.bags.get(bag_id)
        if b is None:
            return []
        effects = []
        if action == "pull":
            b.note = f"Pulled by {who} at {clock(t)} for re-routing to lane {b.booked_gate} ({b.flight})."
            b.wrong_aircraft, b.held, b.lane_strong = None, False, None
            b.phase = "pulled"
            effects.append(("pull", bag_id))
        elif action == "approve":
            b.note = f"Approved by {who} at {clock(t)} after a visual check."
            b.identity, b.held = None, False
            if b.portal_tag == b.tag and b.loader_reads:
                b.cleared_t, b.cleared_zone, b.cleared_tag = t, b.loader_reads[-1][0], b.tag
            else:
                b.note += (" The bag at the loader did not carry this record's own BLE tag, so this record"
                           " stays open until its own bag is seen.")
            effects.append(("release", bag_id))
        elif action == "security":
            b.note = f"Held for security by {who} at {clock(t)}."
            b.identity = ("IDENTITY FLAG", b.note + " Bag must not be loaded.")
            for a in self.alerts.values():
                if a["bag"] == bag_id and a["active"]:
                    a["active"], a["resolved_t"] = False, t
                    a["ack"] = f"{who} at {clock(t)}: handed to security"
            b.status = b.identity[0]
            effects.append(("pull", bag_id))  # security takes the bag away; the belt can run again
            for l in self.loaders.values():
                if l["bag"] == bag_id:
                    l.update(light="green", paused=False, msg="Ready", bag=None)
        elif action == "recover":
            b.note = f"Tag {b.tag} recovered by {who} at {clock(t)}."
            effects.append(("recover", bag_id))
        elif action == "ack":
            pass
        for a in self.alerts.values():
            if a["bag"] == bag_id and a["active"]:
                a["ack"] = f"{who} at {clock(t)}: {action}"
        b.log(t, f"Staff action by {who}: {action}")
        self._pub(t, "STAFF_ACTION", f"{who}: {action} on {bag_id}", b, action=action)
        self._resolve_loader(b)
        self._evaluate(t)
        return effects

    def _resolve_loader(self, b):
        for g, l in self.loaders.items():
            if l["bag"] == b.id and not b.held:
                l.update(light="green", paused=False, msg="Ready", bag=None)
                if b.tag:
                    self.tag_commands.append((b.tag, 0))

    # ------------------------------------------------------------------ status rules
    def tick(self, t):
        self.t = t
        self._evaluate(t)

    def _predict_arrival(self, b, t):
        if not b.zone:
            return None
        start = t
        if b.tag_detached and b.last_rfid:
            start = max(b.last_rfid[1], t - 60)  # RFID-only: assume it kept moving since its last read
        blocked = {k: JAM_PENALTY_S for k in self.jams}
        cost = self.layout.costs_from(b.zone, blocked).get(b.booked_gate)
        if cost is None:
            return None
        eta = start + cost * self.congestion
        return eta if b.tag_detached else max(t, eta)

    def _late_check(self, b, t):
        """Returns the LATE reason if this bag is predicted to miss its make-up cut-off, else None.
        Runs every second (even while the bag is NOT SEEN) so the warning is never hidden."""
        if not (b.deadline and b.zone != b.booked_gate and b.cleared_t is None and b.phase == "in_system"):
            b.late_active, b.late_streak = False, 0
            return None
        eta = self._predict_arrival(b, t)
        b.late_streak = b.late_streak + 1 if eta is not None and eta >= b.deadline + LATE_MARGIN_S else 0
        was = getattr(b, "late_active", False)
        b.late_active = eta is not None and (b.late_streak >= LATE_CONFIRM_S or (was and eta > b.deadline - 5))
        if not b.late_active:
            return None
        if b.late_first is None:
            b.late_first = t
        return (f"Expected in lane {b.booked_gate} around {clock(eta)}, after its {clock(b.deadline)} cut-off "
                f"for {b.flight}.{self._held_reason(b, t)} Consider expediting.")

    def _could_reach(self, b, lane, t):
        """Sanity check that combines RFID + the belt map + jam messages: could this bag physically
        be in `lane` by now, starting from its last exact RFID read and not using jammed belts?"""
        if not b.last_rfid:
            return True
        start, t0 = b.last_rfid
        cost = self.layout.costs_from(start, {k: 10 ** 6 for k in self.jams}).get(lane)
        return cost is not None and cost < 10 ** 5 and t - t0 >= 0.8 * cost

    def _held_reason(self, b, t):
        for (frm, to), (since, _) in self.jams.items():
            if frm == b.zone and t - b.zone_since > 5:
                return f" Held: belt {frm}->{to} jammed since {clock(since)}."
        return ""

    def _status_for(self, b, t):
        if b.phase == "loaded":
            return "ON TRACK", f"Loaded on {b.flight} at {clock(b.loaded_t)} (tag removed = loading confirmed)."
        if b.phase == "pulled":
            if b.identity and b.identity[0] == "IDENTITY FLAG":
                return b.identity
            return "ON TRACK", b.note or "Pulled by staff for re-routing."
        if b.identity and b.identity[0] == "IDENTITY FLAG":
            return b.identity
        if b.wrong_aircraft:
            z = b.wrong_aircraft
            return "WRONG AIRCRAFT", (f"At loader {z} which serves {FLIGHTS[z]['flight']}, but booked on {b.flight} "
                                      f"(lane {b.booked_gate}). Belt paused - pull the bag.")
        if b.identity:
            return b.identity
        if (b.cleared_t is None and b.lane_strong and b.lane_strong[0] != b.booked_gate
                and t - b.lane_strong[1] >= MISROUTE_STABLE_S and self._could_reach(b, b.lane_strong[0], t)):
            f = FLIGHTS[b.lane_strong[0]]
            return "MISROUTED", (f"In make-up lane {b.lane_strong[0]} ({f['flight']} to {f['to']}) but booked on {b.flight} "
                                 f"-> lane {b.booked_gate}. Pull it before it is loaded.")
        if b.cleared_t is not None and t - b.cleared_t > TAG_RETURN_S:
            closes = FLIGHTS[b.cleared_zone]["closes"] if b.cleared_zone in FLIGHTS else None
            left = f" {b.flight} closes at {clock(closes)} (in {max(0, closes - t) // 60} min) - remove it before the doors close." if closes else ""
            return "TAG NOT RETURNED", (f"BLE tag {b.tag} is still on the bag {t - b.cleared_t} s after loader {b.cleared_zone} cleared it. "
                                        f"Loading is not confirmed.{left}")
        late = self._late_check(b, t)
        if not b.tag_detached and t - b.last_seen >= NOT_SEEN_S and b.cleared_t is None:
            b.was_not_seen = True
            also = f" Also predicted to miss its {clock(b.deadline)} cut-off for {b.flight}." if late else ""
            return "NOT SEEN", (f"No signal from BLE tag {b.tag} for {t - b.last_seen} s. Last seen near "
                                f"{zone_name(b.zone)} at {clock(b.last_seen)}.{also}")
        if late:
            return "LATE", late
        where = zone_name(b.zone)
        if b.tag_detached:
            where += f" (by RFID; its BLE tag was left at {b.tag_detached})"
        if b.cleared_t is not None:
            return "ON TRACK", f"Cleared at loader {b.cleared_zone}; waiting for the handler to remove BLE tag {b.tag}."
        if b.zone == b.booked_gate:
            return "ON TRACK", f"In the right make-up lane {b.booked_gate} for {b.flight}."
        return "ON TRACK", f"In {where}, heading to lane {b.booked_gate} for {b.flight}.{self._held_reason(b, t)}"

    def _evaluate(self, t):
        for b in self.bags.values():
            if b.final:
                continue
            status, reason = self._status_for(b, t)
            if b.phase != "in_system" and status == "ON TRACK":
                b.final = True
            if status != b.status or b.force_alert:
                b.force_alert = False
                old = b.status
                b.status, b.reason, b.status_since = status, reason, t
                b.log(t, f"{status}: {reason}")
                self._pub(t, "STATUS_CHANGED", f"{b.id}: {old} -> {status}", b, status=status, reason=reason)
                for a in self.alerts.values():
                    if a["bag"] == b.id and a["active"]:
                        a["active"], a["resolved_t"] = False, t
                if status != "ON TRACK":
                    aid = f"A{len(self.alerts) + 1}"
                    gate = b.zone if b.zone in FLIGHTS else None
                    self.alerts[aid] = {"id": aid, "t": t, "clock": clock(t), "bag": b.id, "status": status,
                                        "reason": reason, "gate": gate, "active": True, "ack": None}
                    self._pub(t, "ALERT_RAISED", f"{status}: {b.id} - {reason}", b, status=status, alert=aid, gate=gate)
            else:
                b.reason = reason
                for a in self.alerts.values():
                    if a["bag"] == b.id and a["active"]:
                        a["reason"] = reason

    # ------------------------------------------------------------------ views
    def counts(self):
        c = {s: 0 for s in PRIORITY}
        for b in self.active():
            c[b.status] += 1
        return c

    def passenger(self, booking_ref, plate):
        bid = self.by_plate.get(plate.replace(" ", ""))
        b = self.bags.get(bid)
        if not b or b.booking_ref.upper() != booking_ref.strip().upper():
            return None
        friendly = {
            "ON TRACK": "Your bag is on its way to your flight.",
            "LATE": "Your bag is running late. Staff are working to get it on your flight.",
            "MISROUTED": "Your bag took a wrong turn. Staff have been alerted and are fixing it now.",
            "WRONG AIRCRAFT": "Your bag was stopped before being loaded on the wrong plane. Staff are re-sending it to your flight.",
            "NOT SEEN": "We've briefly lost sight of your bag. Staff are checking.",
            "IDENTITY REVIEW": "Your bag is having a routine security check.",
            "IDENTITY FLAG": "Your bag is having a security check. Please speak to an agent.",
            "TAG NOT RETURNED": "Your bag is on board your flight.",
        }
        msg = friendly[b.status]
        if b.phase == "loaded":
            msg = f"Your bag is loaded on {b.flight}."
        if b.phase == "pulled":
            msg = f"Your bag was caught going the wrong way and is being re-sent to {b.flight}."
        return {"plate": b.plate, "flight": b.flight, "destination": FLIGHTS[b.booked_gate]["to"],
                "message": msg, "updated": clock(b.status_since),
                "where": "Loaded" if b.phase == "loaded" else (KIND_LABEL[zone_kind(b.zone)] + " area" if b.zone else "Checked in"),
                "journey": [h for h in b.history if any(k in h["text"] for k in
                            ("Checked in", "Transfer bag", "Cleared", "loading confirmed", "WRONG AIRCRAFT", "Pulled"))]}
