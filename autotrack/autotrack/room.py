"""
room.py - the "Room demo": 3-4 laptops in a square find the one ESP32 tag together.

Every laptop hears the tag and reports the signal strength (RSSI) of each verified packet.
The main laptop (the one running the server) combines them:

  1. Every 0.5 s, take each laptop's LAST 5 PACKETS (the tag sends one every 100 ms, but Windows
     passes on only ~3 per second, so that's roughly the last 1.5 s) and use their MEDIAN as that
     laptop's signal strength. Packets older than 3 s don't count.
  2. Turn it into a distance with the standard log-distance formula
         d = 10 ** ((A - RSSI) / (10 * n))
     where A = the laptop's 1 m signal (its calibration, laptops.py) + a room offset
     (from "Calibrate room"), and n = how fast the signal fades with distance.
  3. Trilaterate: find the point whose distances to the laptops best match (weighted least
     squares). Near laptops are trusted more, and the 3 closest laptops dominate.
  4. Smooth the position with a second Kalman filter (it also gives a speed).
  5. Story rules: touching the WRONG loader's laptop -> WRONG AIRCRAFT (tag beeps + flashes red);
     touching the CORRECT loader's laptop -> CHECKED IN (green + chirp). If two NEIGHBOURING
     laptops (next to each other on the map) both think the tag is further than the zone range
     -> OUT OF ZONE. Never while the tag is touching a laptop (it is at that laptop, so inside).

Bluetooth signal strength is rough (bodies, hands and walls change it by 5-10 dB), so the
location is APPROXIMATE - good to about a metre, not centimetres (that would need UWB).

Everything here is plain Python (no FastAPI, no Bluetooth), so it can be unit-tested.
The server calls Room.ingest() for every packet and Room.tick() every 0.5 s.
Settings and calibration are saved in data/room.json.
"""

import asyncio
import json
import math
import random
import statistics
import threading
import time
from collections import deque

from .laptops import m_to_rssi, new_profile, slug
from .layout import ROOT

ROOM_FILE = ROOT / "data" / "room.json"

TICK_S = 0.5               # position update rate (the page also refreshes every 0.5 s)
MEDIAN_N = 5               # each laptop's RSSI = median of its last 5 packets (none older than ONLINE_S)
ONLINE_S = 3.0              # a laptop is "online" if it heard the tag in the last 3 s
KEEP_S = 10.0               # how much raw packet history we keep per laptop
D_MIN, D_MAX = 0.05, 15.0   # distances are clamped to this range (metres)
SIGMA_DB_DEFAULT = 4.0      # typical RSSI noise before the room is calibrated
RSSI_Q = 0.8                # RSSI Kalman: how fast the true signal may drift (dB^2 per second)
POS_QA = 0.5                # position Kalman: white acceleration noise (m^2/s^3)
POS_RESET_S = 5.0           # forget the position after 5 s without a fix
TOUCH_M = 0.12              # default "touching" distance
TOUCH_ON_TICKS, TOUCH_OFF_TICKS, TOUCH_OFF_DB = 2, 3, 4.0
ZONE_CLEAR_S = 3.0          # OUT OF ZONE clears after 3 s with no violating pair
ZONE_NEAR_M = 0.6           # a laptop hears the tag closer than this = it's at that laptop, so inside
OK_MODE_S = 3.0             # how long we "want" mode 2 (OK) after a check-in
RETRY_S = 5.0               # resend a tag command that failed after this long
TRAIL_LEN = 40
MAX_EVENTS = 50

MODE_NORMAL, MODE_WRONG, MODE_OK, MODE_LOCATE = 0, 1, 2, 3
ROLES = ("correct", "wrong", "corner")

SQUARE_M = 3.0             # the laptops stand at the corners of a 3 m x 3 m square
DEFAULT_ANCHORS = {
    "alex":   {"x": 0.0, "y": 0.0, "role": "correct", "label": "Loader 5 - AC 1234", "flight": "AC 1234", "order": 0},
    "joseph": {"x": SQUARE_M, "y": 0.0, "role": "wrong", "label": "Loader 4 - AC 857", "flight": "AC 857", "order": 1},
    "daniel": {"x": SQUARE_M, "y": SQUARE_M, "role": "corner", "label": "Corner C", "flight": "", "order": 2},
    "prem":   {"x": 0.0, "y": SQUARE_M, "role": "corner", "label": "Corner D", "flight": "", "order": 3},
}
DEFAULT_BAG = {"tag": "TAG-0001", "flight": "AC 1234", "booking": "DSX42K", "plate": "AC123456"}
# Zone range for a 3 m square: anywhere INSIDE, the two laptops of the far edge are at most 3.35 m
# away (tag at the middle of the opposite edge); 1.5 m OUTSIDE an edge they are 4.74 m away.
# 4.0 m sits between the two with ~0.65 m of margin each way. (Rule of thumb: 1.33 x the side.)
DEFAULT_ZONE = {"range_m": 4.0, "hold_s": 2.0}
DEFAULT_WEIGHTS = {"closest3": 1.0, "others": 0.2}


# ============================================================== pure helpers (unit-tested)

def rssi_to_distance(rssi, rssi_1m, n, offset_db=0.0):
    """Standard log-distance formula: d = 10^((A - RSSI) / (10 n)), A = rssi_1m + offset. Clamped."""
    a = rssi_1m + offset_db
    d = 10 ** ((a - rssi) / (10.0 * n))
    return max(D_MIN, min(D_MAX, d))


def distance_sigma(d, sigma_db, n):
    """How uncertain a distance is (metres): a fixed dB error means a bigger error far away."""
    return max(0.05, d * math.log(10) * sigma_db / (10.0 * n))


def wls_weights(dists, sigmas, closest3=1.0, others=0.2):
    """Base weight 1/sigma^2 per laptop; the 3 closest get x closest3, the rest x others."""
    base = [1.0 / (s * s) for s in sigmas]
    closest = set(sorted(range(len(dists)), key=lambda i: dists[i])[:3])
    return [w * (closest3 if i in closest else others) for i, w in enumerate(base)]


def wls_solve(anchors, dists, weights, iters=15):
    """Weighted least squares trilateration (Gauss-Newton).

    anchors: [(x, y)], dists: [d], weights: [w]. Minimises sum w_i * (|p - a_i| - d_i)^2.
    Starts at the weighted centroid of the anchors. Returns (x, y, sigma_m) or None.
    sigma_m = rough position uncertainty from the inverse of J^T W J, clamped to 0.1-3 m.
    """
    if len(anchors) < 3 or sum(weights) <= 0:
        return None
    sw = sum(weights)
    x = sum(w * a[0] for w, a in zip(weights, anchors)) / sw
    y = sum(w * a[1] for w, a in zip(weights, anchors)) / sw

    def cost(px, py):
        return sum(w * (math.hypot(px - ax, py - ay) - d) ** 2 for (ax, ay), d, w in zip(anchors, dists, weights))

    h = (0.0, 0.0, 0.0)
    c = cost(x, y)
    for _ in range(iters):
        # Build the 2x2 normal equations  (J^T W J) step = -J^T W r
        a11 = a12 = a22 = b1 = b2 = 0.0
        for (ax, ay), d, w in zip(anchors, dists, weights):
            dx, dy = x - ax, y - ay
            r = math.hypot(dx, dy)
            if r < 1e-6:          # sitting exactly on an anchor: no direction, nudge a little
                dx, dy, r = 1e-6, 0.0, 1e-6
            jx, jy = dx / r, dy / r
            res = r - d
            a11 += w * jx * jx
            a12 += w * jx * jy
            a22 += w * jy * jy
            b1 -= w * jx * res
            b2 -= w * jy * res
        h = (a11, a12, a22)
        det = a11 * a22 - a12 * a12
        if abs(det) < 1e-12:
            return None
        sx = (a22 * b1 - a12 * b2) / det
        sy = (a11 * b2 - a12 * b1) / det
        step = math.hypot(sx, sy)
        if step > 5.0:            # never jump more than 5 m in one go
            sx, sy = sx * 5.0 / step, sy * 5.0 / step
        # Only take the step if it makes the fit better; otherwise try half a step (up to 8 times).
        f = 1.0
        for _try in range(8):
            nc = cost(x + f * sx, y + f * sy)
            if nc <= c:
                break
            f *= 0.5
        else:
            break                 # no step helps: we're at the best point
        x, y, c = x + f * sx, y + f * sy, nc
        if step * f < 1e-4:
            break
    a11, a12, a22 = h
    det = a11 * a22 - a12 * a12
    if det <= 0:
        sigma = 3.0
    else:
        trace_inv = (a11 + a22) / det          # trace of the inverse of a 2x2 matrix
        sigma = math.sqrt(max(0.0, trace_inv) / 2.0)
    return x, y, max(0.1, min(3.0, sigma))


class RssiKalman:
    """One-number Kalman filter for a laptop's RSSI (random walk: the true signal drifts slowly).
    (Not used by the room any more - it now takes the median of the last 5 packets - kept as a tool.)"""

    def __init__(self, q=RSSI_Q):
        self.q = q
        self.reset()

    def reset(self):
        self.x = None   # smoothed RSSI
        self.p = None   # its variance
        self.t = None   # time of the last packet

    def update(self, t, z, r):
        """Feed one packet (time t, RSSI z) with measurement noise variance r (= sigma_db^2)."""
        if self.x is None or (self.t is not None and t - self.t > ONLINE_S):
            self.x, self.p, self.t = float(z), float(r), t
            return self.x
        dt = max(0.0, t - self.t)                 # out-of-order packet: no extra drift
        self.p += self.q * dt
        k = self.p / (self.p + r)
        self.x += k * (z - self.x)
        self.p *= (1.0 - k)
        self.t = max(self.t, t)
        return self.x


class _Axis:
    """Constant-velocity Kalman filter along one axis: state [position, speed]."""

    def __init__(self, pos, r):
        self.x, self.v = pos, 0.0
        self.p = [[r, 0.0], [0.0, 1.0]]

    def predict(self, dt, qa):
        if dt <= 0:
            return
        self.x += self.v * dt
        p = self.p
        p00 = p[0][0] + dt * (p[1][0] + p[0][1]) + dt * dt * p[1][1] + qa * dt ** 3 / 3.0
        p01 = p[0][1] + dt * p[1][1] + qa * dt * dt / 2.0
        p11 = p[1][1] + qa * dt
        self.p = [[p00, p01], [p01, p11]]

    def update(self, z, r):
        p = self.p
        s = p[0][0] + r
        k0, k1 = p[0][0] / s, p[1][0] / s
        y = z - self.x
        self.x += k0 * y
        self.v += k1 * y
        self.p = [[(1 - k0) * p[0][0], (1 - k0) * p[0][1]],
                  [p[1][0] - k1 * p[0][0], p[1][1] - k1 * p[0][1]]]


class PositionKalman:
    """2-D constant-velocity Kalman filter, state [x, y, vx, vy].

    With equal noise on x and y (R = sigma^2 * I) the 4x4 filter splits exactly into two
    independent 2x2 filters, one per axis - that's what we do, it's easier to read.
    """

    def __init__(self, qa=POS_QA):
        self.qa = qa
        self.reset()

    def reset(self):
        self.ax = self.ay = None
        self.t = None        # time of the last predict
        self.last_fix = None

    @property
    def ready(self):
        return self.ax is not None

    def predict(self, t):
        if self.ready and self.t is not None:
            dt = t - self.t
            self.ax.predict(dt, self.qa)
            self.ay.predict(dt, self.qa)
        self.t = t

    def update(self, t, x, y, sigma):
        r = sigma * sigma
        if not self.ready:
            self.ax, self.ay = _Axis(x, r), _Axis(y, r)
        else:
            self.ax.update(x, r)
            self.ay.update(y, r)
        self.t = t
        self.last_fix = t

    def position(self):
        return None if not self.ready else (self.ax.x, self.ay.x)

    def velocity(self):
        return None if not self.ready else (self.ax.v, self.ay.v)


class TouchDetector:
    """'Is the tag touching this laptop?' with hysteresis, so it doesn't flicker.

    Starts after 2 ticks in a row at or above the touch level; ends after 3 ticks in a row
    more than 4 dB below it (or with no signal at all).
    """

    def __init__(self):
        self.touching = False
        self.on_count = self.off_count = 0

    def reset(self):
        self.__init__()

    def step(self, rssi, touch_dbm):
        """Returns "start", "end" or None."""
        if not self.touching:
            self.on_count = self.on_count + 1 if (rssi is not None and rssi >= touch_dbm) else 0
            if self.on_count >= TOUCH_ON_TICKS:
                self.touching, self.on_count, self.off_count = True, 0, 0
                return "start"
        else:
            self.off_count = self.off_count + 1 if (rssi is None or rssi < touch_dbm - TOUCH_OFF_DB) else 0
            if self.off_count >= TOUCH_OFF_TICKS:
                self.touching, self.on_count, self.off_count = False, 0, 0
                return "end"
        return None


def zone_pairs(ordered_ids):
    """Neighbouring laptops around the square (cyclic): [a,b,c,d] -> (a,b),(b,c),(c,d),(d,a)."""
    n = len(ordered_ids)
    if n < 2:
        return []
    if n == 2:
        return [(ordered_ids[0], ordered_ids[1])]
    return [(ordered_ids[i], ordered_ids[(i + 1) % n]) for i in range(n)]


def square_order(points):
    """Laptop ids in order AROUND the square (by angle from the centre), so 'neighbours' are the
    laptops that really sit next to each other on the map - whatever order they joined in.
    points: {id: (x, y)}."""
    if not points:
        return []
    cx = sum(p[0] for p in points.values()) / len(points)
    cy = sum(p[1] for p in points.values()) / len(points)
    return sorted(points, key=lambda k: (math.atan2(points[k][1] - cy, points[k][0] - cx), k))


def violating_pairs(pairs, dist, online, range_m):
    """Neighbour pairs where BOTH laptops are online and BOTH see the tag further than range_m."""
    out = []
    for a, b in pairs:
        if online.get(a) and online.get(b) and dist.get(a) is not None and dist.get(b) is not None \
                and dist[a] > range_m and dist[b] > range_m:
            out.append((a, b))
    return out


def calibration_offset(median_rssi, d, rssi_1m, n, limit=15.0):
    """Room calibration: the tag sat d metres away and we heard median_rssi.
    Expected A = M + 10 n log10(d); offset = A - rssi_1m (clamped to +-15 dB)."""
    a = median_rssi + 10.0 * n * math.log10(max(D_MIN, d))
    return max(-limit, min(limit, a - rssi_1m))


# ============================================================== the room

def _r(v, nd=2):
    return None if v is None else round(v, nd)


class _Live:
    """Per-laptop runtime data (not saved)."""

    def __init__(self):
        self.packets = deque()        # (t, rssi)
        self.touch = TouchDetector()
        self.last_t = None
        self.median = None            # median RSSI of the last MEDIAN_N packets (this laptop's signal)
        self.remote = None            # newest datagram from a reporter (it sends its own last-5 median)
        self.last_seq = None          # sequence number of the last accepted datagram
        self.counts = deque()         # (t, packets) reported by datagrams


class Room:
    def __init__(self, laptops, log=print, path=None, clock=time.time, seed=None):
        self.laptops = laptops        # laptops.Laptops: each laptop's calibration curve
        self.log = log
        self.path = path or ROOM_FILE
        self.clock = clock
        self.lock = threading.RLock()
        self.ble = None               # RealBLE on the main laptop (set by the server), else None
        self.loop = None              # the server's asyncio loop (set by the server)
        self.rng = random.Random(seed)
        self._load()
        self.live = {}
        self.events = []
        self.sim = False
        self.udp_stats = {"port": None, "datagrams": 0, "rejected": 0}
        self._sim_t0 = None
        self._sim_next = {}
        self.calib = {"phase": "idle", "progress": 0.0, "point": None, "message": "", "results": []}
        self._calib_buf = None
        self._calib_start = self._calib_end = None
        self._reset_runtime()

    # ------------------------------------------------------------------ files
    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            data = {}
        anchors = data.get("anchors")
        if not isinstance(anchors, dict) or not anchors:
            anchors = json.loads(json.dumps(DEFAULT_ANCHORS))
        self.anchors = {}
        for aid, a in anchors.items():
            self.anchors[slug(aid)] = self._clean_anchor(a, len(self.anchors))
        self.zone = {**DEFAULT_ZONE, **(data.get("zone") or {})}
        self.bag = {**DEFAULT_BAG, **(data.get("bag") or {})}
        self.weights = {**DEFAULT_WEIGHTS, **(data.get("weights") or {})}
        self.enabled = bool(data.get("enabled", True))

    @staticmethod
    def _clean_anchor(a, order):
        role = a.get("role") if a.get("role") in ROLES else "corner"
        return {
            "x": float(a.get("x", 0.0)), "y": float(a.get("y", 0.0)), "role": role,
            "label": str(a.get("label") or ""), "flight": str(a.get("flight") or ""),
            "order": int(a.get("order", order)),
            "offset_db": float(a.get("offset_db") or 0.0),
            "sigma_db": float(a.get("sigma_db") or SIGMA_DB_DEFAULT),
            "touch_dbm": None if a.get("touch_dbm") is None else float(a["touch_dbm"]),
        }

    def save(self):
        with self.lock:
            data = {"anchors": self.anchors, "zone": self.zone, "bag": self.bag,
                    "weights": self.weights, "enabled": self.enabled}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(self.path)

    # ------------------------------------------------------------------ helpers
    def profile(self, aid):
        """This laptop's calibration curve (Alex's curve if it was never measured)."""
        p = self.laptops.profiles.get(aid) if self.laptops else None
        return p or new_profile(aid)

    def _note(self, kind, text, now=None):
        self.events.insert(0, {"t": now if now is not None else self.clock(), "kind": kind, "text": text})
        del self.events[MAX_EVENTS:]
        self.log("[room] " + text)

    def _name(self, aid):
        p = self.laptops.profiles.get(aid) if self.laptops else None
        return p["name"] if p else aid.capitalize()

    def touch_dbm(self, aid):
        a = self.anchors[aid]
        if a["touch_dbm"] is not None:
            return a["touch_dbm"]
        return m_to_rssi(TOUCH_M, self.profile(aid)) + a["offset_db"]

    def _ordered(self):
        return sorted(self.anchors, key=lambda k: (self.anchors[k]["order"], k))

    def _around(self):
        """Laptops in order around the square (for the zone rule and the drawn square)."""
        return square_order({k: (a["x"], a["y"]) for k, a in self.anchors.items()})

    def _reset_runtime(self):
        """Clears bag latches, filters and alarms (keeps settings and calibration)."""
        for lv in self.live.values():
            lv.touch.reset()
            lv.median = None
            lv.remote = None
            lv.counts.clear()
        self.pos_kf = PositionKalman()
        self.raw_pos = None
        self.pos_sigma = None
        self.trail = deque(maxlen=TRAIL_LEN)
        self.wrong_latch = None          # id of the wrong laptop the bag touched
        self.checked_in = None           # id of the correct laptop it touched
        self.checkpoint = None
        self.out_of_zone = False
        self.violating = None
        self._viol_since = {}
        self._last_viol_t = None
        self._ok_until = None
        self._last_tick = None
        self.status, self.reason = "NOT SEEN", ""
        self.tag_mode = MODE_NORMAL
        self.tag_mode_sent = None
        self.last_write = None
        self._last_try = None            # (mode, ok, t)
        self._inflight = None
        self._derived = {}               # per-tick numbers per laptop for the page

    def _add_anchor(self, aid):
        """A laptop we've never seen: put it on the next free corner of the square."""
        xs = [a["x"] for a in self.anchors.values()] or [0.0]
        ys = [a["y"] for a in self.anchors.values()] or [0.0]
        x0, y0 = min(xs), min(ys)
        size = max(max(xs) - x0, max(ys) - y0) or 2.0
        spots = [(x0, y0), (x0 + size, y0), (x0 + size, y0 + size), (x0, y0 + size),
                 (x0 + size / 2, y0), (x0 + size, y0 + size / 2), (x0 + size / 2, y0 + size), (x0, y0 + size / 2)]

        def free(p):
            return all(math.hypot(p[0] - a["x"], p[1] - a["y"]) > 0.1 for a in self.anchors.values())

        x, y = next((p for p in spots if free(p)), (x0 + size / 2 + 0.3 * len(self.anchors), y0 + size / 2))
        order = max([a["order"] for a in self.anchors.values()] or [-1]) + 1
        self.anchors[aid] = self._clean_anchor(
            {"x": x, "y": y, "role": "corner", "label": f"{self._name(aid)}'s laptop", "order": order}, order)
        self._note("info", f"New laptop '{aid}' joined the room at ({x:.1f}, {y:.1f}) m")
        self.save()

    def _ensure_laptop(self, aid, add_profile=True):
        """Make sure the laptop has a calibration profile (uncalibrated = Alex's curve) and an anchor.
        (Simulated packets don't create profiles: profile() falls back to Alex's curve anyway.)"""
        if add_profile and self.laptops is not None and aid not in self.laptops.profiles:
            with self.laptops.lock:
                self.laptops._ensure(aid)
        if aid not in self.anchors:
            self._add_anchor(aid)
        if aid not in self.live:
            self.live[aid] = _Live()

    # ------------------------------------------------------------------ packets in
    def ingest(self, laptop, rssi, t=None, _sim=False):
        """One verified packet: laptop id, RSSI (dBm), time (unix s)."""
        aid = slug(laptop)
        t = self.clock() if t is None else float(t)
        rssi = float(rssi)
        with self.lock:
            if self.sim and not _sim:
                return False            # the simulator is driving the room: ignore real packets
            self._ensure_laptop(aid, add_profile=not _sim)
            lv = self.live[aid]
            lv.packets.append((t, rssi))
            while lv.packets and lv.packets[0][0] < t - KEEP_S:
                lv.packets.popleft()
            lv.last_t = t if lv.last_t is None else max(lv.last_t, t)
            if self._calib_buf is not None and self._calib_start <= t <= self._calib_end:
                self._calib_buf.setdefault(aid, []).append(rssi)
            return True

    def ingest_filtered(self, laptop, rssi_filtered, variance, raw_median, packets, t_age_ms, seq, now=None,
                        raw=None):
        """One UDP datagram from a reporter (every 0.5 s): rssi_filtered = the median of its last 5
        packets (null if none in the last 3 s: we then keep its previous value while it's online).
        raw = optional list of the individual RSSIs heard since its previous datagram (used for
        calibration / touch levels).

        Keeps the newest value per laptop. Returns True if accepted, False if ignored
        (simulator on, or an old/duplicate datagram). Raises ValueError on bad input."""
        def num(v, lo, hi, name, allow_none=True):
            if v is None and allow_none:
                return None
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not (lo <= v <= hi):
                raise ValueError(f"{name} must be a number between {lo} and {hi}")
            return float(v)

        rssi_filtered = num(rssi_filtered, -127, 0, "rssi_filtered")
        variance = num(variance, 0, 1e4, "variance")
        raw_median = num(raw_median, -127, 0, "raw_median")
        packets = num(packets, 0, 1000, "packets", allow_none=False)
        t_age_ms = num(t_age_ms, 0, 60000, "t_age_ms")
        seq = num(seq, 0, 2 ** 53, "seq", allow_none=False)
        packets, seq = int(packets), int(seq)
        if raw is not None:
            if not isinstance(raw, (list, tuple)) or len(raw) > 50:
                raise ValueError("raw must be a list of at most 50 numbers")
            raw = [num(r, -127, 0, "raw", allow_none=False) for r in raw]
        aid = slug(laptop)
        if not aid:
            raise ValueError("laptop name is empty")
        now = self.clock() if now is None else float(now)
        with self.lock:
            if self.sim:
                return False
            self._ensure_laptop(aid)
            lv = self.live[aid]
            if lv.last_seq is not None and seq <= lv.last_seq and not (seq < lv.last_seq - 1000 or seq == 0):
                return False                      # old or duplicate datagram (UDP can reorder)
            if lv.last_seq is not None and seq <= lv.last_seq:
                lv.counts.clear()                 # the reporter restarted
                lv.median = None
            lv.last_seq = seq
            heard_at = None if t_age_ms is None else now - t_age_ms / 1000.0
            if heard_at is not None:
                lv.last_t = heard_at if lv.last_t is None else max(lv.last_t, heard_at)
            if rssi_filtered is None and lv.remote is not None and packets == 0:
                rssi_filtered = lv.remote["rssi"]    # nothing heard in this 0.5 s: hold the last median
            lv.remote = {"rssi": rssi_filtered, "var": variance, "t": heard_at, "seq": seq}
            if packets > 0:
                lv.counts.append((now, packets))
                if heard_at is not None:
                    # the individual readings if sent (spread 100 ms apart, the newest at heard_at),
                    # else just the window's median
                    vals = raw if raw else ([raw_median] if raw_median is not None else [])
                    for i, v in enumerate(vals):
                        t = heard_at - (len(vals) - 1 - i) * 0.1
                        lv.packets.append((t, v))
                        if self._calib_buf is not None and self._calib_start <= t <= self._calib_end:
                            self._calib_buf.setdefault(aid, []).append(v)
            while lv.counts and lv.counts[0][0] < now - KEEP_S:
                lv.counts.popleft()
            while lv.packets and lv.packets[0][0] < now - KEEP_S:
                lv.packets.popleft()
            return True

    def ingest_batch(self, laptop, packets, now=None):
        """Packets from a reporter laptop: [{"age_ms", "rssi"}]. Time = now - age (no clock skew)."""
        now = self.clock() if now is None else now
        items = sorted(((now - float(p.get("age_ms", 0)) / 1000.0, float(p["rssi"])) for p in packets),
                       key=lambda tr: tr[0])
        with self.lock:
            if not items:
                self._ensure_laptop(slug(laptop))
            return sum(1 for t, r in items if self.ingest(laptop, r, t))

    def ingest_sighting(self, s, laptop):
        """A sighting dict from real_ble.py on the main laptop."""
        if s.get("tag_serial") != self.bag["tag"]:
            return
        self.ingest(laptop, s["rssi"], s.get("t"))

    # ------------------------------------------------------------------ who's in charge of the beep
    def online_ids(self, now=None):
        now = self.clock() if now is None else now
        return [k for k, lv in self.live.items()
                if k in self.anchors and lv.last_t is not None and now - lv.last_t <= ONLINE_S]

    def controls_tag(self):
        """True when the room (not the single-laptop monitor) should send tag commands."""
        with self.lock:
            return self.enabled and len(self.online_ids()) >= 2

    # ------------------------------------------------------------------ the main loop
    def tick(self, now=None):
        now = self.clock() if now is None else now
        with self.lock:
            if self.sim:
                self._sim_inject(now)
            self._tick(now)
            self._calib_check(now)
            if self.enabled:
                self._maybe_send(now)

    def _tick(self, now):
        dt = 0.0 if self._last_tick is None else max(0.0, now - self._last_tick)
        self._last_tick = now
        online = {}
        derived = {}
        for aid in self._ordered():
            a = self.anchors[aid]
            lv = self.live.get(aid)
            p = self.profile(aid)
            d = {"rssi": None, "raw": None, "distance_m": None, "packets_s": 0.0, "weight": None,
                 "closest3": False, "online": False}
            if lv is not None:
                if lv.last_t is not None and now - lv.last_t > ONLINE_S:
                    lv.median = None      # silent for >3 s: start fresh next time
                recent3 = [r for t, r in lv.packets if now - ONLINE_S <= t <= now + 0.5]
                recent1 = [r for t, r in lv.packets if now - 1.0 <= t <= now + 0.5]
                d["packets_s"] = len(recent3) / ONLINE_S
                if lv.remote is not None:       # reporter mode: packet counts come from the datagrams
                    d["packets_s"] = sum(n for t, n in lv.counts if now - ONLINE_S <= t <= now + 0.5) / ONLINE_S
                d["raw"] = statistics.median(recent1) if recent1 else None
                d["online"] = lv.last_t is not None and now - lv.last_t <= ONLINE_S
                if lv.remote is not None:
                    rssi = lv.remote["rssi"]    # the reporter's own median of its last 5 packets
                else:
                    # This laptop's last MEDIAN_N packets (none older than ONLINE_S): their median.
                    fresh = [r for t, r in lv.packets if now - ONLINE_S <= t <= now + 0.05]
                    lv.median = statistics.median(fresh[-MEDIAN_N:]) if fresh else None
                    rssi = lv.median
                if d["online"] and rssi is not None:
                    d["rssi"] = rssi
                    d["distance_m"] = rssi_to_distance(rssi, p["rssi_1m"], p["n"], a["offset_db"])
                ev = lv.touch.step(d["rssi"], self.touch_dbm(aid))
                if ev == "start":
                    self._on_touch(aid, now)
            online[aid] = d["online"]
            derived[aid] = d

        # ---- trilateration (needs 3 laptops)
        ids = [k for k in derived if derived[k]["distance_m"] is not None]
        fix = None
        if len(ids) >= 3:
            dists = [derived[k]["distance_m"] for k in ids]
            sigmas = [distance_sigma(derived[k]["distance_m"], self.anchors[k]["sigma_db"], self.profile(k)["n"])
                      for k in ids]
            ws = wls_weights(dists, sigmas, self.weights["closest3"], self.weights["others"])
            closest = set(sorted(range(len(ids)), key=lambda i: dists[i])[:3])
            total = sum(ws) or 1.0
            for i, k in enumerate(ids):
                derived[k]["weight"] = ws[i] / total
                derived[k]["closest3"] = i in closest
            fix = wls_solve([(self.anchors[k]["x"], self.anchors[k]["y"]) for k in ids], dists, ws)
        self.raw_pos = None if fix is None else (fix[0], fix[1])

        # ---- position Kalman
        kf = self.pos_kf
        if kf.ready and (kf.last_fix is None or now - kf.last_fix > POS_RESET_S):
            kf.reset()
            self.trail.clear()
        kf.predict(now)
        if fix is not None:
            kf.update(now, fix[0], fix[1], fix[2])
            self.pos_sigma = fix[2]
        elif not kf.ready:
            self.pos_sigma = None
        if kf.ready:
            self.trail.append(kf.position())
        self._derived = derived

        # ---- zone: two NEIGHBOURING laptops both too far
        # While the tag is touching a laptop it is, by definition, at that laptop - inside the square -
        # so noisy "too far" readings from the other side can't raise OUT OF ZONE.
        # Same if any laptop hears it very close (strong signal = the most trustworthy reading we have):
        # the tag is next to a laptop, so inside.
        dist = {k: v["distance_m"] for k, v in derived.items()}
        touching = any(lv.touch.touching for k, lv in self.live.items() if k in self.anchors)
        near = any(v["online"] and v["distance_m"] is not None and v["distance_m"] < ZONE_NEAR_M
                   for v in derived.values())
        bad = [] if (touching or near) else violating_pairs(zone_pairs(self._around()), dist, online,
                                                            self.zone["range_m"])
        for pair in list(self._viol_since):
            if pair not in bad:
                del self._viol_since[pair]
        for pair in bad:
            self._viol_since.setdefault(pair, now)
        held = [pr for pr in bad if now - self._viol_since[pr] >= self.zone["hold_s"]]
        if bad:
            self._last_viol_t = now
        if held and not self.out_of_zone:
            self.out_of_zone = True
            self.violating = list(held[0])
            a, b = (self._name(x) for x in held[0])
            self._note("zone_out", f"OUT OF ZONE: {a} and {b} both hear the tag further than "
                                   f"{self.zone['range_m']:.1f} m", now)
        elif held:
            self.violating = list(held[0])
        elif self.out_of_zone and (self._last_viol_t is None or now - self._last_viol_t >= ZONE_CLEAR_S):
            self.out_of_zone = False
            self.violating = None
            self._note("zone_in", "Back inside the zone", now)

        # ---- bag status (precedence: NOT SEEN > WRONG > OUT OF ZONE > CHECKED IN > ON TRACK)
        n_online = sum(1 for v in online.values() if v)
        bag = self.bag
        if n_online == 0:
            status, reason = "NOT SEEN", f"No laptop has heard tag {bag['tag']} in the last {ONLINE_S:.0f} s."
        elif self.wrong_latch:
            w = self.anchors.get(self.wrong_latch, {})
            status = "WRONG AIRCRAFT"
            reason = (f"Bag is for {bag['flight']} but {_loader(w.get('label', ''))} serves "
                      f"{w.get('flight') or 'another flight'} - take it to the correct loader.")
        elif self.out_of_zone:
            a, b = (self._name(x) for x in self.violating or ("?", "?"))
            status = "OUT OF ZONE"
            reason = (f"The tag is more than {self.zone['range_m']:.1f} m from both {a} and {b}: "
                      "it has left the loading area (approximate, Bluetooth only).")
        elif self.checked_in:
            c = self.anchors.get(self.checked_in, {})
            status, reason = "CHECKED IN", f"Touched {c.get('label') or self.checked_in}: correct aircraft ({bag['flight']})."
        else:
            status = "ON TRACK"
            reason = "Inside the zone. Location is approximate (Bluetooth signal strength, no UWB)."
        self.status, self.reason = status, reason
        alert = status in ("WRONG AIRCRAFT", "OUT OF ZONE")
        if alert:
            self.tag_mode = MODE_WRONG
        elif self._ok_until is not None and now < self._ok_until:
            self.tag_mode = MODE_OK
        else:
            self.tag_mode = MODE_NORMAL

    def _on_touch(self, aid, now):
        a = self.anchors[aid]
        if a["role"] == "wrong":
            self.checkpoint = aid
            self.checked_in = None
            self._ok_until = None
            if self.wrong_latch != aid:
                self.wrong_latch = aid
                self._note("wrong", f"WRONG AIRCRAFT: bag for {self.bag['flight']} touched "
                                    f"{a['label'] or aid} ({a['flight'] or 'another flight'})", now)
        elif a["role"] == "correct":
            self.checkpoint = aid
            self.wrong_latch = None
            if self.checked_in != aid:
                self.checked_in = aid
                self._ok_until = now + OK_MODE_S
                self._note("checkin", f"CHECKED IN at {a['label'] or aid} - correct aircraft", now)

    # ------------------------------------------------------------------ tag commands
    def _maybe_send(self, now):
        """Send tag_mode to the tag when it changes (never blocks: runs as a background task)."""
        if self.ble is None or self.sim or len(self.online_ids(now)) < 2:
            return
        want = self.tag_mode
        if self._inflight == want:
            return
        last = self._last_try
        if last is not None and last[0] == want and (last[1] or now - last[2] < RETRY_S):
            return
        if want == MODE_NORMAL and last is not None and last[0] == MODE_OK and (last[1] or self._inflight == MODE_OK):
            return              # after OK (green + chirp) the tag goes back to normal by itself
                                # (also while the OK command is still on its way: a write can take ~3 s)
        self._inflight = want
        self._last_try = (want, False, now)
        self._spawn(self._send(want, "auto"))

    def _spawn(self, coro):
        try:
            asyncio.get_running_loop()
            asyncio.ensure_future(coro)
        except RuntimeError:     # called from a thread without a loop: hand it to the server loop
            if self.loop is not None:
                asyncio.run_coroutine_threadsafe(coro, self.loop)
            else:
                coro.close()

    async def _send(self, mode, source):
        ok = False
        try:
            ok = bool(await self.ble.set_alert(self.bag["tag"], mode))
        except Exception as e:  # never let a Bluetooth error stop the room
            self.log(f"[room] tag command failed: {e!r}")
        with self.lock:
            now = self.clock()
            if self._inflight == mode:
                self._inflight = None
            if source == "auto":
                self._last_try = (mode, ok, now)
            if ok:
                self.tag_mode_sent = mode
            self.last_write = {"mode": mode, "ok": ok, "t": now, "source": source}
        return ok

    async def manual_beep(self, mode):
        """POST /api/room/beep: send a mode by hand (0 off, 1 wrong, 2 OK, 3 locate)."""
        if self.ble is None:
            return {"ok": False, "error": "Not in real mode (start with: python run.py --mode real)"}
        ok = await self._send(int(mode), "manual")
        with self.lock:
            self._note("info", f"Manual tag command: mode {mode}" + ("" if ok else " (tag did not confirm)"))
            return {"ok": ok, "last_write": self.last_write}

    # ------------------------------------------------------------------ settings
    def configure(self, patch):
        """Partial update from POST /api/room/config. Raises ValueError on bad input."""
        if not isinstance(patch, dict):
            raise ValueError("body must be a JSON object")
        with self.lock:
            if "enabled" in patch:
                self.enabled = bool(patch["enabled"])
            for aid, a in (patch.get("anchors") or {}).items():
                aid = slug(aid)
                if a is None:
                    self.anchors.pop(aid, None)
                    self.live.pop(aid, None)
                    continue
                if not isinstance(a, dict):
                    raise ValueError(f"anchor {aid} must be an object or null")
                cur = self.anchors.get(aid)
                if cur is None:
                    order = max([v["order"] for v in self.anchors.values()] or [-1]) + 1
                    cur = self._clean_anchor({"label": f"{self._name(aid)}'s laptop", "order": order}, order)
                if "role" in a and a["role"] not in ROLES:
                    raise ValueError("role must be correct, wrong or corner")
                for key in ("x", "y", "offset_db", "sigma_db"):
                    if key in a:
                        cur[key] = float(a[key])
                for key in ("role", "label", "flight"):
                    if key in a:
                        cur[key] = str(a[key] or "")
                if "order" in a:
                    cur["order"] = int(a["order"])
                if "touch_dbm" in a:
                    cur["touch_dbm"] = None if a["touch_dbm"] is None else float(a["touch_dbm"])
                cur["sigma_db"] = max(0.5, min(20.0, cur["sigma_db"]))
                self.anchors[aid] = cur
            z = patch.get("zone") or {}
            if "range_m" in z:
                self.zone["range_m"] = max(0.3, min(15.0, float(z["range_m"])))
            if "hold_s" in z:
                self.zone["hold_s"] = max(0.0, min(30.0, float(z["hold_s"])))
            for key in ("tag", "flight", "booking", "plate"):
                if key in (patch.get("bag") or {}):
                    self.bag[key] = str(patch["bag"][key]).strip()[:40]
            w = patch.get("weights") or {}
            for key in ("closest3", "others"):
                if key in w:
                    self.weights[key] = max(0.0, min(10.0, float(w[key])))
            self.save()
            return self.state()

    def reset(self):
        with self.lock:
            self._reset_runtime()
            self.events = []
            self._note("info", "Room reset: bag status, filters and events cleared")

    # ------------------------------------------------------------------ room calibration
    def start_calibration(self, x=None, y=None, seconds=8.0, now=None):
        """Hold the tag still at (x, y) for `seconds`; learns each laptop's offset and noise."""
        now = self.clock() if now is None else now
        with self.lock:
            if self.calib["phase"] == "running":
                raise ValueError("a calibration is already running")
            seconds = max(2.0, min(30.0, float(seconds)))
            if x is None or y is None:
                ids = self.online_ids(now)
                if not ids:
                    self.calib = {"phase": "error", "progress": 0.0, "point": None, "results": [],
                                  "message": "No laptop hears the tag - switch it on and try again."}
                    return
                x = sum(self.anchors[k]["x"] for k in ids) / len(ids)
                y = sum(self.anchors[k]["y"] for k in ids) / len(ids)
            self._calib_buf = {}
            self._calib_start, self._calib_end = now, now + seconds
            self.calib = {"phase": "running", "progress": 0.0, "point": {"x": round(float(x), 3), "y": round(float(y), 3)},
                          "message": f"Hold the tag still at ({float(x):.2f}, {float(y):.2f}) m for {seconds:.0f} s...",
                          "results": []}

    def _calib_check(self, now):
        if self.calib["phase"] != "running":
            return
        total = self._calib_end - self._calib_start
        self.calib["progress"] = round(max(0.0, min(1.0, (now - self._calib_start) / total)), 3)
        if now < self._calib_end:
            return
        px, py = self.calib["point"]["x"], self.calib["point"]["y"]
        results, done = [], []
        for aid in self._ordered():
            got = self._calib_buf.get(aid, [])
            a = self.anchors[aid]
            p = self.profile(aid)
            d = math.hypot(px - a["x"], py - a["y"])
            row = {"id": aid, "median": None, "sigma_db": None, "packets": len(got),
                   "distance_m": round(d, 2), "rssi_1m_profile": p["rssi_1m"], "offset_db": None}
            if len(got) >= 5:
                med = statistics.median(got)
                sig = max(1.5, statistics.pstdev(got))
                off = calibration_offset(med, d, p["rssi_1m"], p["n"])
                a["offset_db"], a["sigma_db"] = round(off, 2), round(sig, 2)
                row.update(median=round(med, 1), sigma_db=round(sig, 2), offset_db=round(off, 2))
                done.append(f"{self._name(aid)} {off:+.1f} dB (noise {sig:.1f} dB)")
            results.append(row)
        self._calib_buf = None
        skipped = [r["id"] for r in results if r["offset_db"] is None]
        if done:
            self.save()
            msg = "Room calibrated: " + ", ".join(done)
            if skipped:
                msg += ". Not enough packets from: " + ", ".join(skipped)
            self.calib.update(phase="done", progress=1.0, message=msg, results=results)
            self._note("calib", msg, now)
        else:
            self.calib.update(phase="error", progress=1.0, results=results,
                              message="Not enough packets from any laptop (need 5 each). Is the tag on?")

    def capture_touch(self, aid, now=None):
        """The tag is touching laptop `aid` right now: touch level = median of the last 2 s - 3 dB."""
        now = self.clock() if now is None else now
        aid = slug(aid)
        with self.lock:
            if aid not in self.anchors:
                raise ValueError(f"unknown laptop '{aid}'")
            lv = self.live.get(aid)
            got = [r for t, r in (lv.packets if lv else []) if now - 2.0 <= t <= now + 0.5]
            if len(got) < 5:
                raise ValueError(f"Only {len(got)} packets from {aid} in the last 2 s (need 5). Hold the tag on it.")
            level = statistics.median(got) - 3.0
            self.anchors[aid]["touch_dbm"] = round(level, 1)
            self.save()
            self._note("info", f"Touch level for {self._name(aid)} set to {level:.1f} dBm", now)
            return self.state(now)

    # ------------------------------------------------------------------ simulator (no hardware)
    def set_sim(self, on, now=None):
        now = self.clock() if now is None else now
        with self.lock:
            on = bool(on)
            if on == self.sim:
                return
            self.sim = on
            for lv in self.live.values():       # start clean so real and fake packets don't mix
                lv.packets.clear()
                lv.median = None
                lv.last_t = None
                lv.remote = None
                lv.counts.clear()
            self._reset_runtime()
            if on:
                self._sim_t0 = now
                self._sim_next = {aid: now + self.rng.uniform(0, 0.125) for aid in self.anchors}
                self._note("info", "Simulator ON: fake packets for every laptop (60 s story loop)", now)
            else:
                self._note("info", "Simulator OFF", now)

    def sim_position(self, t):
        """Where the simulated tag is, t seconds into the 60 s story loop."""
        ids = self._ordered()
        pts = {k: (self.anchors[k]["x"], self.anchors[k]["y"]) for k in ids}
        cx = sum(p[0] for p in pts.values()) / len(pts)
        cy = sum(p[1] for p in pts.values()) / len(pts)
        centre = (cx, cy)

        def at(aid, fallback):
            if aid is None:
                return fallback
            ax, ay = pts[aid]
            dx, dy = cx - ax, cy - ay
            r = math.hypot(dx, dy) or 1.0
            return ax + dx / r * 0.03, ay + dy / r * 0.03    # 3 cm from the laptop = touching

        wrong = next((k for k in ids if self.anchors[k]["role"] == "wrong"), None)
        correct = next((k for k in ids if self.anchors[k]["role"] == "correct"), None)
        w, c = at(wrong, centre), at(correct, centre)
        # 1.5 m outside the edge between the 2nd and 3rd laptop (or any edge if fewer)
        if len(ids) >= 2:
            a, b = pts[ids[1 % len(ids)]], pts[ids[2 % len(ids)]]
            mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
            dx, dy = mx - cx, my - cy
            r = math.hypot(dx, dy) or 1.0
            out = (mx + dx / r * 1.5, my + dy / r * 1.5)
        else:
            out = (cx + 3.0, cy)
        script = [(6, centre, centre), (6, centre, w), (4, w, w), (8, w, c), (4, c, c),
                  (8, c, out), (6, out, out), (8, out, centre), (10, centre, centre)]   # = 60 s
        t = t % 60.0
        for dur, p0, p1 in script:
            if t <= dur:
                f = t / dur
                return p0[0] + (p1[0] - p0[0]) * f, p0[1] + (p1[1] - p0[1]) * f
            t -= dur
        return centre

    def _sim_inject(self, now):
        """One packet every ~100 ms per laptop (like the real tag), with realistic noise."""
        for aid in list(self.anchors):
            nxt = self._sim_next.get(aid, now)
            a = self.anchors[aid]
            p = self.profile(aid)
            while nxt <= now:
                x, y = self.sim_position(nxt - self._sim_t0)
                d = math.hypot(x - a["x"], y - a["y"])
                rssi = m_to_rssi(d, p) + a["offset_db"] + self.rng.gauss(0, 4.0)
                if self.rng.random() < 0.05:
                    rssi -= 8.0                      # someone's body is in the way
                rssi = round(max(-127.0, min(0.0, rssi)))
                if rssi > -100:                      # very weak packets are simply not heard
                    self.ingest(aid, rssi, nxt, _sim=True)
                nxt += self.rng.uniform(0.095, 0.105)  # the tag advertises every 100 ms
            self._sim_next[aid] = nxt

    # ------------------------------------------------------------------ state for the page
    def state(self, now=None):
        now = self.clock() if now is None else now
        with self.lock:
            out = []
            for aid in self._ordered():
                a = self.anchors[aid]
                p = self.profile(aid)
                d = self._derived.get(aid, {})
                lv = self.live.get(aid)
                out.append({
                    "id": aid, "name": self._name(aid), "label": a["label"], "role": a["role"],
                    "flight": a["flight"], "x": a["x"], "y": a["y"], "order": a["order"],
                    "online": bool(d.get("online")), "packets_s": round(d.get("packets_s", 0.0), 2),
                    "rssi": _r(d.get("rssi"), 1), "raw": _r(d.get("raw"), 1),
                    "sigma_db": round(a["sigma_db"], 2), "distance_m": _r(d.get("distance_m")),
                    "weight": _r(d.get("weight"), 3), "closest3": bool(d.get("closest3")),
                    "touching": bool(lv and lv.touch.touching), "touch_dbm": round(self.touch_dbm(aid), 1),
                    "calibrated": bool(p.get("calibrated")), "offset_db": round(a["offset_db"], 2),
                    "rssi_1m": p["rssi_1m"], "n": p["n"],
                })
            pos = self.pos_kf.position()
            vel = self.pos_kf.velocity()
            ordered = self._around()
            n_online = sum(1 for x in out if x["online"])
            return {
                "enabled": self.enabled, "now": now, "real": self.ble is not None, "sim": self.sim,
                "anchors": out,
                "position": None if pos is None else {"x": round(pos[0], 3), "y": round(pos[1], 3)},
                "raw_position": None if self.raw_pos is None else {"x": round(self.raw_pos[0], 3), "y": round(self.raw_pos[1], 3)},
                "velocity": None if vel is None else {"vx": round(vel[0], 3), "vy": round(vel[1], 3)},
                "pos_sigma_m": _r(self.pos_sigma) if pos is not None else None,
                "trail": [[round(x, 3), round(y, 3)] for x, y in self.trail],
                "zone": {"range_m": self.zone["range_m"], "hold_s": self.zone["hold_s"],
                         "inside": None if n_online < 2 else not self.out_of_zone,
                         "violating": self.violating,
                         "square": [[self.anchors[k]["x"], self.anchors[k]["y"]] for k in ordered]},
                "bag": {**self.bag, "status": self.status, "reason": self.reason, "checkpoint": self.checkpoint},
                "alert": self.status in ("WRONG AIRCRAFT", "OUT OF ZONE"),
                "tag_mode": self.tag_mode, "tag_mode_sent": self.tag_mode_sent, "last_write": self.last_write,
                "calibration": json.loads(json.dumps(self.calib)),
                "weights": dict(self.weights),
                "events": list(self.events),
                "udp": dict(self.udp_stats),
            }


UDP_PORT = 8765
UDP_MAX_BYTES = 2048


def parse_datagram(data):
    """Bytes from a reporter -> kwargs for Room.ingest_filtered (without `now`). Raises ValueError."""
    if not isinstance(data, (bytes, bytearray)) or len(data) > UDP_MAX_BYTES:
        raise ValueError("datagram too large")
    try:
        msg = json.loads(bytes(data).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ValueError("not valid JSON")
    if not isinstance(msg, dict) or msg.get("v") != 1 or isinstance(msg.get("v"), bool):
        raise ValueError("unsupported datagram version")
    laptop = msg.get("laptop")
    if not isinstance(laptop, str) or not laptop.strip() or len(laptop) > 40:
        raise ValueError("bad laptop name")
    out = {"laptop": laptop}
    for key in ("rssi_filtered", "variance", "raw_median", "packets", "t_age_ms", "seq"):
        if key not in msg:
            raise ValueError(f"missing {key}")
        out[key] = msg[key]
    # range checks happen in ingest_filtered; do them here too so bad datagrams are rejected early
    for key, lo, hi in (("rssi_filtered", -127, 0), ("variance", 0, 1e4), ("raw_median", -127, 0),
                        ("packets", 0, 1000), ("t_age_ms", 0, 60000), ("seq", 0, 2 ** 53)):
        v = out[key]
        if v is None and key not in ("packets", "seq"):
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not (lo <= v <= hi):
            raise ValueError(f"{key} out of range")
    raw = msg.get("raw")
    if raw is not None:      # optional: the window's individual RSSIs
        if not isinstance(raw, list) or len(raw) > 50 or any(
                isinstance(r, bool) or not isinstance(r, (int, float)) or not -127 <= r <= 0 for r in raw):
            raise ValueError("raw must be a list of up to 50 RSSI values")
        out["raw"] = raw
    return out


class RoomUdpProtocol(asyncio.DatagramProtocol):
    """Receives the reporters' datagrams and feeds the room. Never raises."""

    def __init__(self, room):
        self.room = room

    def datagram_received(self, data, addr):
        stats = self.room.udp_stats
        stats["datagrams"] += 1
        try:
            self.room.ingest_filtered(**parse_datagram(data))
        except Exception:       # ValueError for bad data; anything else must not kill the listener
            stats["rejected"] += 1

    def error_received(self, exc):
        pass


async def start_udp(room, host="127.0.0.1", port=UDP_PORT, log=print):
    """Start the UDP listener. Returns (transport, bound_port) or (None, None) if the port is busy."""
    loop = asyncio.get_running_loop()
    try:
        transport, _ = await loop.create_datagram_endpoint(lambda: RoomUdpProtocol(room), local_addr=(host, port))
    except OSError as e:
        log(f"[room] WARNING: could not listen for UDP on {host}:{port} ({e}). "
            "Reporters with --transport http still work.")
        return None, None
    bound = transport.get_extra_info("sockname")[1]
    room.udp_stats["port"] = bound
    return transport, bound


def _loader(label):
    """'Loader 4 - AC 857' -> 'Loader 4'."""
    return (label.split(" - ")[0].strip() or label) if label else "this loader"
