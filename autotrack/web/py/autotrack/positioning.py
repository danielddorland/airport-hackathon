"""
positioning.py - "this bag is in this zone, close to these zones".

This is the real AutoTrack positioning code: it runs the same in simulation and with real
receivers. For each BLE tag it keeps a smoothed signal strength per receiver and decides:

  1. PRIMARY ZONE: the receiver that hears the tag best. To stop the zone flickering, a new
     zone must win for SWITCH_SECONDS seconds in a row AND be SWITCH_MARGIN_DB stronger
     than the current zone before we switch.
  2. NEARBY ZONES: the other receivers that hear it, labelled strong / medium / weak.
  3. POSITION ESTIMATE: weighted centroid  x = sum(w_i * x_i) / sum(w_i), where w_i comes
     from RSSI (stronger signal = closer = more weight). Honest accuracy: a few metres.

An exact RFID read beats BLE: when a tunnel or portal reads the bag, the primary zone snaps
to that reader's zone.
"""

import math

SMOOTH_ALPHA = 0.5       # 0..1, how much a new reading moves the smoothed value
STALE_S = 3              # forget a receiver that hasn't heard the tag for this long
SWITCH_SECONDS = 2       # "several consistent sightings before switching zones"
SWITCH_MARGIN_DB = 3.0
NEARBY_WINDOW_DB = 12.0
P0, PATH_N = -55.0, 2.3  # only used to turn RSSI into a rough distance for the centroid weights


def strength_label(rssi):
    if rssi >= -70:
        return "strong"
    if rssi >= -82:
        return "medium"
    return "weak"


class ZoneEstimator:
    def __init__(self, receivers, smoothing=True):
        self.receivers = receivers
        self.smoothing = smoothing
        self.smoothed = {}        # receiver -> [value, last_t]
        self.primary = None       # zone id (e.g. "JA3")
        self.primary_since = None
        self.candidate = None
        self.cand_count = 0
        self.last_heard = None
        self.centroid = None
        self.nearby = []

    def _zone_rssi(self, zone):
        for r, (v, _) in self.smoothed.items():
            if self.receivers[r]["zone"] == zone:
                return v
        return -999.0

    def update(self, t, readings, rfid_zone=None):
        """readings: {receiver_id: rssi} heard during second t (may be empty).
        Returns True if the primary zone changed."""
        if readings:
            self.last_heard = t
        for r, v in readings.items():
            s = self.smoothed.get(r)
            if not self.smoothing or s is None or t - s[1] > STALE_S:
                self.smoothed[r] = [float(v), t]
            else:
                s[0] += SMOOTH_ALPHA * (v - s[0])
                s[1] = t
        keep = 0 if not self.smoothing else STALE_S
        self.smoothed = {r: s for r, s in self.smoothed.items() if t - s[1] <= keep}

        old = self.primary
        if rfid_zone is not None:
            self.primary, self.candidate, self.cand_count = rfid_zone, None, 0
        elif self.smoothed:
            best_r = max(self.smoothed, key=lambda r: self.smoothed[r][0])
            best = self.receivers[best_r]["zone"]
            if self.primary is None or not self.smoothing:
                self.primary = best
            elif best != self.primary:
                self.cand_count = self.cand_count + 1 if best == self.candidate else 1
                self.candidate = best
                if (self.cand_count >= SWITCH_SECONDS and
                        self.smoothed[best_r][0] >= self._zone_rssi(self.primary) + SWITCH_MARGIN_DB):
                    self.primary, self.candidate, self.cand_count = best, None, 0
            else:
                self.candidate, self.cand_count = None, 0
        self._update_nearby_and_centroid()
        if self.primary != old:
            self.primary_since = t
        return self.primary != old

    def zone_rssi(self, zone):
        return self._zone_rssi(zone)

    def _update_nearby_and_centroid(self):
        if not self.smoothed:
            self.nearby = []
            return
        ranked = sorted(self.smoothed.items(), key=lambda kv: -kv[1][0])
        top = ranked[0][1][0]
        self.nearby = [{"zone": self.receivers[r]["zone"], "rssi": round(v), "strength": strength_label(v)}
                       for r, (v, _) in ranked
                       if self.receivers[r]["zone"] != self.primary and v >= top - NEARBY_WINDOW_DB][:3]
        # weighted centroid of the 4 strongest receivers; weight = 1 / estimated distance^2
        wsum = xs = ys = 0.0
        for r, (v, _) in ranked[:4]:
            d = 10 ** ((P0 - v) / (10 * PATH_N))
            w = 1.0 / max(d, 1.0) ** 2
            wsum += w
            xs += w * self.receivers[r]["x"]
            ys += w * self.receivers[r]["y"]
        self.centroid = (xs / wsum, ys / wsum)

    def summary(self):
        return {"zone": self.primary, "nearby": self.nearby,
                "centroid": [round(c, 1) for c in self.centroid] if self.centroid else None}
