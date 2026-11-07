"""
laptops.py - per-laptop signal calibration for the live ESP32 tag.

Every laptop hears the same tag differently (Bluetooth chip, antenna, where the antenna
sits in the lid), often 5-10 dB apart. A curve measured on Alex's laptop puts the tag at
the wrong distance on Daniel's. So each laptop gets its own curve

    RSSI = rssi_1m - 10 * n * log10(distance_m)

fitted from a few measurements: hold the ESP still at a known distance, press Measure,
repeat at 2-3 distances. The "too far" limit is stored in METRES, so it means the same
distance on every laptop once each one is calibrated.

  data/laptop_profiles.json   every laptop's calibration (kept in git, so the team shares
                              them - that's what multi-laptop positioning will need)
  data/this_laptop.json       which profile THIS computer is (not in git; set from the
                              page's "This laptop" picker or run.py --laptop NAME)
"""

import json
import math
import re
import threading
import time

from .layout import ROOT

PROFILES_FILE = ROOT / "data" / "laptop_profiles.json"
THIS_FILE = ROOT / "data" / "this_laptop.json"

DEFAULT_ID = "alex"
N_MIN, N_MAX = 1.0, 4.5

# Alex's laptop, measured Nov 7 with the tag at -12 dBm (the original livetag.py curve).
ALEX = {
    "name": "Alex",
    "rssi_1m": -60.0,
    "n": 1.8,
    "threshold_m": 1.9,
    "calibrated": True,
    "points": [
        {"d": 0.3, "rssi": -50.0, "samples": None, "t": None},
        {"d": 1.0, "rssi": -60.0, "samples": None, "t": None},
        {"d": 3.0, "rssi": -68.0, "samples": None, "t": None},
    ],
    "note": "Measured Nov 7 on Alex's laptop (tag at -12 dBm).",
}


def slug(name):
    s = re.sub(r"[^a-z0-9]+", "-", str(name).strip().lower()).strip("-")
    return s[:30] or "laptop"


def rssi_to_m(rssi, p):
    return 10 ** ((p["rssi_1m"] - rssi) / (10 * p["n"]))


def m_to_rssi(m, p):
    return p["rssi_1m"] - 10 * p["n"] * math.log10(max(0.05, m))


def fit(points, n_default):
    """Least-squares fit of rssi_1m and n. With one distance only, keep n and solve rssi_1m."""
    if not points:
        return None
    ds = [p["d"] for p in points]
    xs = [-10 * math.log10(p["d"]) for p in points]
    ys = [p["rssi"] for p in points]
    if max(ds) / min(ds) >= 1.5:  # need distances that actually differ to learn n
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        var = sum((x - mx) ** 2 for x in xs)
        n = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var
        n = max(N_MIN, min(N_MAX, n))
        return my - n * mx, n
    n = n_default
    return sum(y - n * x for x, y in zip(xs, ys)) / len(xs), n


def new_profile(name):
    """A fresh laptop starts from Alex's curve (better than nothing) until it's measured."""
    return {"name": name, "rssi_1m": ALEX["rssi_1m"], "n": ALEX["n"], "threshold_m": ALEX["threshold_m"],
            "calibrated": False, "points": [], "note": "Not calibrated yet - using Alex's numbers."}


class Laptops:
    def __init__(self, active=None, log=print):
        self.log = log
        self.lock = threading.Lock()
        self.profiles = self._load()
        chosen = active or self._load_this()
        if chosen:
            self.active = self._ensure(chosen)
        else:
            self.active = DEFAULT_ID if DEFAULT_ID in self.profiles else next(iter(self.profiles))
        if active:  # run.py --laptop NAME: remember it for next time too
            self._save_this()

    # ------------------------------------------------------------------ files
    def _load(self):
        try:
            data = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
            profiles = data.get("profiles") or {}
        except (FileNotFoundError, ValueError, OSError):
            profiles = {}
        if DEFAULT_ID not in profiles:
            profiles = {DEFAULT_ID: json.loads(json.dumps(ALEX)), **profiles}
        return profiles

    def _save(self):
        PROFILES_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = PROFILES_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"profiles": self.profiles}, indent=2), encoding="utf-8")
        tmp.replace(PROFILES_FILE)

    def _load_this(self):
        try:
            return json.loads(THIS_FILE.read_text(encoding="utf-8")).get("laptop")
        except (FileNotFoundError, ValueError, OSError):
            return None

    def _save_this(self):
        THIS_FILE.parent.mkdir(parents=True, exist_ok=True)
        THIS_FILE.write_text(json.dumps({"laptop": self.active}), encoding="utf-8")

    # ------------------------------------------------------------------ profiles
    def _ensure(self, name_or_id):
        pid = slug(name_or_id)
        if pid not in self.profiles:
            self.profiles[pid] = new_profile(str(name_or_id).strip()[:40] or pid)
            self._save()
            self.log(f"[laptops] added laptop profile '{self.profiles[pid]['name']}'")
        return pid

    def profile(self):
        return self.profiles[self.active]

    def select(self, name_or_id):
        with self.lock:
            self.active = self._ensure(name_or_id)
            self._save_this()
            p = self.profile()
        self.log(f"[laptops] this laptop is now '{p['name']}' "
                 f"({'calibrated' if p['calibrated'] else 'not calibrated'}, 1 m = {p['rssi_1m']:.1f} dBm, n = {p['n']:.2f})")
        return p

    def add_point(self, d, rssi, samples):
        with self.lock:
            p = self.profile()
            # Re-measuring the same distance replaces the old reading.
            p["points"] = [q for q in p["points"] if abs(q["d"] - d) > 1e-6]
            p["points"].append({"d": d, "rssi": round(rssi, 1), "samples": samples, "t": int(time.time())})
            p["points"].sort(key=lambda q: q["d"])
            self._refit(p)
            self._save()
            return p

    def delete_point(self, index):
        with self.lock:
            p = self.profile()
            if 0 <= index < len(p["points"]):
                p["points"].pop(index)
                self._refit(p)
                self._save()
            return p

    def clear_points(self):
        with self.lock:
            p = self.profile()
            p["points"] = []
            self._refit(p)
            self._save()
            return p

    def set_threshold_m(self, m):
        with self.lock:
            p = self.profile()
            p["threshold_m"] = round(max(0.2, min(15.0, float(m))), 2)
            self._save()
            return p

    def _refit(self, p):
        res = fit(p["points"], p["n"] if p["calibrated"] else ALEX["n"])
        if res is None:
            p.update(rssi_1m=ALEX["rssi_1m"], n=ALEX["n"], calibrated=False,
                     note="Not calibrated yet - using Alex's numbers.")
            return
        p["rssi_1m"], p["n"] = round(res[0], 1), round(res[1], 2)
        p["calibrated"] = True
        dists = len({q["d"] for q in p["points"]})
        p["note"] = (f"Fitted from {len(p['points'])} measurement(s)."
                     + ("" if dists >= 2 else " Measure at a second distance to also learn n."))

    def to_json(self):
        p = self.profile()
        pts = [{**q, "fit_rssi": round(m_to_rssi(q["d"], p), 1)} for q in p["points"]]
        return {
            "active": self.active,
            "list": [{"id": k, "name": v["name"], "calibrated": v["calibrated"]} for k, v in self.profiles.items()],
            "profile": {**p, "id": self.active, "points": pts,
                        "threshold_dbm": round(m_to_rssi(p["threshold_m"], p), 1)},
        }
