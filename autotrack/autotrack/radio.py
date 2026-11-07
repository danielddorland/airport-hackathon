"""
radio.py - fake BLE receivers (simulation mode only).

Pretends to be the BLE receiver in every zone. Each second, every tag sends one advert and
every receiver close enough reports the signal strength (RSSI) it heard. We make it messy
on purpose, like a real baggage hall:

  * Path loss: RSSI = P0 - 10 * n * log10(distance)      (P0 = RSSI at 1 m, n = 2.3)
  * Shadowing: +-5 dB that drifts slowly (people, carts, steel walk past)
  * Fast fading: +-2 dB random jitter on every packet
  * Each receiver's radio is slightly different: +-2 dB fixed offset
  * Packets get lost: 5% at random, plus collisions when many tags are near one receiver,
    using the formula from our deck: P(lost) = 1 - e^(-2 N tau / T)  (tau = 0.4 ms, T = 1 s)
  * Receivers can't hear below -95 dBm.

Real mode doesn't use this file: real_ble.py delivers real RSSI in the same format.
"""

import math
import random

P0 = -55.0          # dBm at 1 m
PATH_N = 2.3
SHADOW_SD = 5.0     # dB
SHADOW_RHO = 0.9    # how much the shadowing at second t looks like second t-1
FAST_SD = 2.0
RX_SENS = -95.0
BASE_LOSS = 0.05
TAU, PERIOD = 0.0004, 1.0
HEARING_M = 45.0    # beyond ~45 m the signal is below -93 dBm and almost never heard; skip it to save time


class SimRadio:
    def __init__(self, receivers, seed=1):
        self.receivers = receivers  # {rid: {"x","y","zone"}}
        self.rng = random.Random(seed)
        self.offset = {r: self.rng.gauss(0, 2.0) for r in sorted(receivers)}
        self.shadow = {}

    def sightings(self, t, tags):
        """tags: {serial: (x, y)} for every tag that is broadcasting right now.
        Returns a list of (receiver_id, serial, rssi) heard during second t."""
        near = {r: [] for r in self.receivers}
        dist = {}
        for s in sorted(tags):
            x, y = tags[s]
            for r, rx in self.receivers.items():
                d = math.hypot(x - rx["x"], y - rx["y"])
                if d <= HEARING_M:
                    near[r].append(s)
                    dist[(r, s)] = d
        out = []
        for r in sorted(near):
            n_tags = len(near[r])
            p_collide = 1 - math.exp(-2 * n_tags * TAU / PERIOD)
            for s in near[r]:
                key = (r, s)
                sh = self.shadow.get(key, self.rng.gauss(0, SHADOW_SD))
                sh = SHADOW_RHO * sh + math.sqrt(1 - SHADOW_RHO ** 2) * self.rng.gauss(0, SHADOW_SD)
                self.shadow[key] = sh
                if self.rng.random() < BASE_LOSS + p_collide:
                    continue
                d = max(0.5, dist[key])
                rssi = P0 - 10 * PATH_N * math.log10(d) + sh + self.offset[r] + self.rng.gauss(0, FAST_SD)
                if rssi >= RX_SENS:
                    out.append((r, s, round(rssi)))
        # forget shadowing for pairs that are out of range (keeps memory small)
        if len(self.shadow) > 20000:
            self.shadow = {k: v for k, v in self.shadow.items() if k in dist}
        return out
