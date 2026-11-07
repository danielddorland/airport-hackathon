"""
livetag.py - "stay near the checkpoint" for the one real ESP32 tag.

The laptop is the checkpoint. Its Bluetooth hears the ESP32 several times a second and
reports the signal strength (RSSI). The tag transmits at -12 dBm (see the sketch's
TX_POWER_LEVEL), so: about -40 dBm = touching, -60 dBm = 1 m, below -65 dBm = more than ~2 m.

  * We smooth the RSSI (median of the last 3 seconds), because single readings jump around.
  * We turn it into a ROUGH distance for the map, using a curve fitted to our own
    measurements on Nov 7 (Alex's laptop, tag at -12 dBm): touching -39, 30 cm -50,
    1 m -60, 3 m -68 dBm. The curve below matches those within about 1 dB.
    Bodies, walls and how the board is held change this a lot, so the metres are approximate.
  * If the smoothed signal is weaker than the threshold (default -65 dBm, ~2 m) for 2 checks in a
    row (1 s), the tag is TOO FAR: we tell the ESP32 to flash red (+ beep if it has a buzzer).
    It goes back to normal once the signal is 3 dB stronger than the threshold again
    (that gap stops it flickering on and off right at the edge).
  * If we hear nothing at all for 6 s, the tag is LOST (also counts as too far).
"""

import asyncio
import math
import statistics
import time

TAG = "TAG-0001"
DEFAULT_THRESHOLD_DBM = -65   # about 2 m with the tag at -12 dBm
RETURN_MARGIN_DB = 3        # must come back this much stronger than the threshold to stop alerting
WINDOW_S = 3.0              # smoothing window
CONFIRM_CHECKS = 2          # checks (0.5 s apart) in a row before we change state
LOST_AFTER_S = 6.0
# Distance curve fitted to our measurements: RSSI = RSSI_1M - 10 * N * log10(distance_m)
RSSI_1M = -60.0
PATH_N = 1.8

MODE_NORMAL, MODE_ALERT, MODE_LOCATE = 0, 1, 3


def rssi_to_m(rssi):
    return 10 ** ((RSSI_1M - rssi) / (10 * PATH_N))


class LiveTagMonitor:
    def __init__(self, ble=None, threshold_dbm=DEFAULT_THRESHOLD_DBM, log=print):
        self.ble = ble                    # RealBLE, or None in simulation mode
        self.threshold = threshold_dbm
        self.log = log
        self.samples = []                 # (time, rssi)
        self.state = "no_tag"             # no_tag | ok | too_far | lost
        self.pending_state, self.pending_count = None, 0
        self.smoothed = None
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

    def set_threshold(self, dbm):
        self.threshold = max(-90, min(-20, float(dbm)))
        self._note(f"Threshold set to {self.threshold:.0f} dBm (about {rssi_to_m(self.threshold):.1f} m)")

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
        if self.samples:
            self.smoothed = statistics.median(r for _, r in self.samples)
            self.history.append((now, self.smoothed))
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
        if self.pending_count < CONFIRM_CHECKS and want != "no_tag":
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
        return {
            "real": self.ble is not None,
            "scanning": bool(st.get("scanning")),
            "error": st.get("error"),
            "state": self.state,
            "rssi": None if self.smoothed is None else round(self.smoothed, 1),
            "distance_m": None if self.smoothed is None else round(rssi_to_m(self.smoothed), 2),
            "threshold_dbm": self.threshold,
            "threshold_m": round(rssi_to_m(self.threshold), 2),
            "seen_ago_s": None if self.last_seen is None else round(now - self.last_seen, 1),
            "packets_3s": len(self.samples),
            "history": [[round(t - now, 1), round(r, 1)] for t, r in self.history],
            "events": [{"ago_s": round(now - e["t"]), "text": e["text"]} for e in self.events][::-1],
            "last_write": st.get("last_write"),
            "curve": {"rssi_1m": RSSI_1M, "n": PATH_N},
        }
