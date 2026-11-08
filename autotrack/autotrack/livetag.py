"""
livetag.py - "stay near the checkpoint" for the one real ESP32 tag.

The laptop is the checkpoint. Its Bluetooth hears the ESP32 several times a second and
reports the signal strength (RSSI). The tag transmits at -12 dBm (see the sketch's
TX_POWER_LEVEL), so: about -40 dBm = touching, -60 dBm = 1 m, below -65 dBm = more than ~2 m.

  * We smooth the RSSI (median of the last 5 seconds), because single readings jump around.
    Far away, the laptop only catches the few packets that happen to arrive strong, which makes
    the tag look closer than it is. So we remember how many packets per second we normally hear
    when the tag is close, and count every packet we're now missing as a very weak one (-100 dBm):
    silence pulls the estimate towards "far" instead of being ignored.
  * We turn it into a ROUGH distance for the map, using THIS laptop's calibration curve
    (laptops.py). Laptops hear the tag 5-10 dB differently, so each one is calibrated on its
    own; Alex's curve (Nov 7, tag at -12 dBm: touching -39, 30 cm -50, 1 m -60, 3 m -68 dBm)
    is the starting point for laptops that haven't been measured yet.
    Bodies, walls and how the board is held change this a lot, so the metres are approximate.
  * The "too far" limit is set in metres (default 1.9 m = -65 dBm on Alex's laptop) and turned
    into dBm with this laptop's curve. If the smoothed signal is weaker than that for 2 checks in a
    row (1 s), the tag is TOO FAR: we tell the ESP32 to flash red (+ beep if it has a buzzer).
    Quick to alert, slow to clear: it only goes back to normal once the signal has been at least
    6 dB stronger than the threshold for 3 s in a row, so one lucky strong packet can't clear it.
  * If we hear nothing at all for 6 s, the tag is LOST (also counts as too far).
  * When the Room demo (room.py) is on and 2+ laptops hear the tag, the room decides what the
    tag shows, so this monitor keeps measuring but stops sending its own commands
    (`suppress` returns True) - two systems must not fight over the beep.
"""

import asyncio
import statistics
import time

from .laptops import Laptops, m_to_rssi, rssi_to_m as _rssi_to_m

TAG = "TAG-0001"
RETURN_MARGIN_DB = 6        # must come back this much stronger than the threshold to stop alerting
WINDOW_S = 5.0              # smoothing window
CONFIRM_CHECKS = 2          # checks (0.5 s apart) in a row before we raise an alert (1 s)
CLEAR_CHECKS = 6            # checks in a row before we clear one (3 s): quick to alert, slow to clear
LOST_AFTER_S = 6.0
MISSING_DBM = -100.0        # value given to each packet we expected but didn't hear
NEAR_MARGIN_DB = 10         # "close" (for learning the normal packet rate) = this much above the threshold
RATE_LEARN = 0.1            # how fast the normal packet rate adapts

MODE_NORMAL, MODE_ALERT, MODE_LOCATE = 0, 1, 3


class LiveTagMonitor:
    def __init__(self, ble=None, laptops=None, log=print, suppress=None):
        self.ble = ble                    # RealBLE, or None in simulation mode
        self.suppress = suppress          # optional function: True = don't send tag commands now
        self.laptops = laptops or Laptops(log=log)
        self.log = log
        self._measuring = None            # list collecting raw RSSI during a calibration measurement
        self.measure_until = None
        self.samples = []                 # (time, rssi)
        self.state = "no_tag"             # no_tag | ok | too_far | lost
        self.pending_state, self.pending_count = None, 0
        self.smoothed = None
        self.near_rate = None             # packets/s we normally hear when the tag is close
        self.missing = 0                  # packets missing from the current window (counted as weak)
        self.history = []                 # (time, smoothed rssi) for the last 60 s
        self.last_seen = None
        self.alert_sent = None            # last mode we successfully sent
        self.events = []                  # short log lines for the page

    # Called by real_ble.py for every verified advertisement from the tag.
    def on_sighting(self, s):
        if s.get("tag_serial") != TAG:
            return
        now = time.time()
        self.samples.append((now, s["rssi"]))
        self.last_seen = now
        if self._measuring is not None:
            self._measuring.append(s["rssi"])

    # ------------------------------------------------------------ this laptop's curve
    def rssi_to_m(self, rssi):
        return _rssi_to_m(rssi, self.laptops.profile())

    @property
    def threshold(self):
        """The "too far" limit in dBm for THIS laptop (stored in metres per laptop)."""
        p = self.laptops.profile()
        return m_to_rssi(p["threshold_m"], p)

    def set_threshold(self, dbm):
        """Old API (dBm): converted to metres with this laptop's curve."""
        dbm = max(-90, min(-20, float(dbm)))
        self.set_threshold_m(self.rssi_to_m(dbm))

    def set_threshold_m(self, m):
        p = self.laptops.set_threshold_m(m)
        self._note(f"Too-far limit set to {p['threshold_m']:.1f} m (= {self.threshold:.0f} dBm on {p['name']}'s laptop)")

    def select_laptop(self, name_or_id):
        p = self.laptops.select(name_or_id)
        self._note(f"This laptop is now {p['name']}'s" + ("" if p["calibrated"] else " (not calibrated yet - using Alex's numbers)"))

    async def measure(self, seconds=5.0):
        """Collect every raw RSSI reading for `seconds` (the tag held still at a known distance)."""
        if self._measuring is not None:
            raise RuntimeError("a measurement is already running")
        self._measuring = []
        self.measure_until = time.time() + seconds
        try:
            await asyncio.sleep(seconds)
            return list(self._measuring)
        finally:
            self._measuring, self.measure_until = None, None

    def _note(self, text):
        self.events.append({"t": time.time(), "text": text})
        self.events = self.events[-12:]
        self.log("[livetag] " + text)

    async def run(self):
        while True:
            try:
                self._check()
            except Exception as e:  # never let the monitor die during a demo
                self.log(f"[livetag] error: {e!r}")
            await asyncio.sleep(0.5)

    def _check(self):
        now = time.time()
        self.samples = [(t, r) for t, r in self.samples if now - t <= WINDOW_S]
        heard = [r for _, r in self.samples]
        # Packets we'd expect at the normal close-range rate but didn't hear count as very weak.
        expected = int(round(self.near_rate * WINDOW_S)) if self.near_rate else 0
        self.missing = max(0, expected - len(heard)) if self.last_seen is not None else 0
        if heard or self.missing:
            self.smoothed = statistics.median(heard + [MISSING_DBM] * self.missing)
            self.history.append((now, self.smoothed))
            # Learn the normal rate only from clearly-close windows (and only once the window is full).
            raw_median = statistics.median(heard) if heard else None
            if (raw_median is not None and raw_median >= self.threshold + NEAR_MARGIN_DB
                    and self.last_seen is not None and now - self.samples[0][0] >= WINDOW_S * 0.8):
                rate = len(heard) / WINDOW_S
                self.near_rate = rate if self.near_rate is None else self.near_rate + RATE_LEARN * (rate - self.near_rate)
        self.history = [(t, r) for t, r in self.history if now - t <= 60]

        # What does the signal say right now?
        if self.last_seen is None:
            want = "no_tag"
        elif now - self.last_seen > LOST_AFTER_S:
            want = "lost"
        elif self.state in ("too_far", "lost"):
            want = "ok" if self.smoothed >= self.threshold + RETURN_MARGIN_DB else "too_far"
        else:
            want = "too_far" if self.smoothed < self.threshold else "ok"

        if want == self.state:
            self.pending_state, self.pending_count = None, 0
            return
        if want == self.pending_state:
            self.pending_count += 1
        else:
            self.pending_state, self.pending_count = want, 1
        needed = CLEAR_CHECKS if (want == "ok" and self.state in ("too_far", "lost")) else CONFIRM_CHECKS
        if self.pending_count < needed and want != "no_tag":
            return
        old, self.state = self.state, want
        self.pending_state, self.pending_count = None, 0
        if want == "ok":
            self._note(f"Back in range ({self.smoothed:.0f} dBm) - tag back to normal")
            self._send(MODE_NORMAL)
        elif want == "too_far":
            self._note(f"TOO FAR ({self.smoothed:.0f} dBm, weaker than {self.threshold:.0f}) - tag flashing red")
            self._send(MODE_ALERT)
        elif want == "lost":
            self._note("Lost the tag (no signal for 6 s) - asking it to flash red if it can still hear us")
            self._send(MODE_ALERT)

    def _send(self, mode):
        if not self.ble:
            return
        if self.suppress is not None and self.suppress():
            return                        # the Room demo is in charge of the tag right now

        async def go():
            ok = await self.ble.set_alert(TAG, mode)
            if ok:
                self.alert_sent = mode
            else:
                self._note("Tag did not confirm the command (it may be out of range)")
        asyncio.ensure_future(go())

    async def manual(self, mode):
        """Test beep (mode 3) / Stop (mode 0) buttons."""
        if not self.ble:
            return False
        ok = await self.ble.set_alert(TAG, mode)
        self._note(("Test beep sent" if mode == MODE_LOCATE else "Stop sent - tag back to normal")
                   + ("" if ok else " (tag did not confirm)"))
        return ok

    def status(self):
        now = time.time()
        st = getattr(self.ble, "status", None) or {}
        p = self.laptops.profile()
        return {
            "real": self.ble is not None,
            "scanning": bool(st.get("scanning")),
            "error": st.get("error"),
            "state": self.state,
            "rssi": None if self.smoothed is None else round(self.smoothed, 1),
            "distance_m": None if self.smoothed is None else round(self.rssi_to_m(self.smoothed), 2),
            "threshold_dbm": round(self.threshold, 1),
            "threshold_m": p["threshold_m"],
            "seen_ago_s": None if self.last_seen is None else round(now - self.last_seen, 1),
            "packets_3s": len(self.samples),   # (name kept for the page; the window is now WINDOW_S)
            "packets_window": len(self.samples),
            "window_s": WINDOW_S,
            "missing": self.missing,
            "near_rate": None if self.near_rate is None else round(self.near_rate, 2),
            "history": [[round(t - now, 1), round(r, 1)] for t, r in self.history],
            "events": [{"ago_s": round(now - e["t"]), "text": e["text"]} for e in self.events][::-1],
            "last_write": st.get("last_write"),
            "curve": {"rssi_1m": p["rssi_1m"], "n": p["n"]},
            "laptops": self.laptops.to_json(),
            "measuring": None if self.measure_until is None else {
                "left_s": round(max(0.0, self.measure_until - now), 1),
                "packets": len(self._measuring or []),
            },
        }
