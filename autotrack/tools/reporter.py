"""
reporter.py - Room demo: turn THIS laptop into one more "ear" for the main laptop
=================================================================================

In the Room demo 3-4 laptops stand in a square. ONE of them (the "main" laptop)
runs the server:

    .venv\\Scripts\\python run.py --mode real --laptop alex --host 0.0.0.0

Every OTHER laptop only listens for the tag and forwards what it hears:

    .venv\\Scripts\\python tools\\reporter.py --server http://192.168.1.23:8000 --laptop joseph

(use the address that run.py prints on the main laptop).

What it does, in plain English:
  * Scans with the same Bluetooth code as the server (autotrack/real_ble.py), so
    only packets with a correct secret signature (HMAC) are counted.
  * Keeps every heard packet in a small buffer (signal strength + when it was heard).
  * Every 0.25 s sends the buffer to the main laptop:
        POST /api/room/sightings  {"laptop": "joseph", "packets": [{"age_ms": 120, "rssi": -63}, ...]}
    "age_ms" = how long ago the packet was heard, so the two laptops' clocks
    don't need to agree.
  * Prints one status line every ~2 s, e.g. "heard 23 packets, median -61 dBm, server ok".
  * Never sends beep commands - only the main laptop talks to the tag.
  * Never crashes: if the main laptop can't be reached it says so once, keeps
    scanning, and carries on as soon as the server is back. Ctrl+C stops it.

No extra libraries: it uses Python's built-in urllib (and bleak for Bluetooth).
Network test without a tag:  add --fake  (sends made-up packets, NOT for the real demo).
"""

import argparse
import asyncio
import collections
import json
import os
import random
import statistics
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from autotrack.laptops import PROFILES_FILE, slug  # noqa: E402
from autotrack.tagcrypto import load_keys  # noqa: E402

DEFAULT_KEYS = os.path.join(ROOT, "data", "tag_keys.json")
MAX_BUFFER = 500       # the server refuses more than 500 packets per request
MAX_AGE_S = 5.0        # older packets are useless for "where is the tag NOW"
STATUS_EVERY_S = 2.0
HTTP_TIMEOUT_S = 2.0


def stamp():
    return time.strftime("%H:%M:%S")


def normalise_server(url):
    """'192.168.1.23' -> 'http://192.168.1.23:8000' (adds http:// and the default port)."""
    url = url.strip().rstrip("/")
    if "://" not in url:
        url = "http://" + url
    host_part = url.split("://", 1)[1]
    if ":" not in host_part.split("/", 1)[0]:
        url += ":8000"
    return url


class Reporter:
    """Buffers heard packets and sends them to the main laptop in small batches.

    add(rssi) is called for every verified packet (from the Bluetooth scanner, or
    from the fake source). sender_loop() runs forever and POSTs the buffer.
    """

    def __init__(self, server, laptop, interval=0.25, log=print):
        self.server = normalise_server(server)
        self.laptop = laptop
        self.interval = max(0.05, float(interval))
        self.log = log
        self.buffer = collections.deque()      # (time.monotonic() when heard, rssi)
        self.window = []                       # rssi values heard since the last status line
        self.dropped = 0                       # packets thrown away (buffer full / too old)
        self.sent_total = 0
        self.server_ok = None                  # None = not tried yet, True / False
        self.last_error = ""
        self.refused = None                    # (HTTP code, time logged) of the last refusal we printed

    # ------------------------------------------------------------------ input
    def add(self, rssi, heard_at=None):
        """Remember one packet. Keeps at most MAX_BUFFER (drops the oldest)."""
        self.buffer.append((heard_at if heard_at is not None else time.monotonic(), int(rssi)))
        self.window.append(int(rssi))
        while len(self.buffer) > MAX_BUFFER:
            self.buffer.popleft()
            self.dropped += 1

    def on_sighting(self, s):
        """Callback for RealBLE: s = dict(receiver, tag_serial, rssi, t, counter)."""
        self.add(s["rssi"])

    # ------------------------------------------------------------------ sending
    def take_batch(self):
        """Empty the buffer into a request body. Packets older than MAX_AGE_S are dropped."""
        now = time.monotonic()
        packets = []
        while self.buffer:
            heard_at, rssi = self.buffer.popleft()
            age = now - heard_at
            if age > MAX_AGE_S:
                self.dropped += 1
                continue
            packets.append({"age_ms": int(age * 1000), "rssi": rssi})
        return packets

    def _post(self, path, body):
        """Blocking HTTP POST (runs in a worker thread). Returns the decoded JSON reply."""
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.server + path, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")

    def _get(self, path):
        with urllib.request.urlopen(self.server + path, timeout=HTTP_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")

    def _mark_ok(self):
        if self.server_ok is False:
            self.log(f"{stamp()} main laptop reachable again ({self.server})")
        self.server_ok = True
        self.last_error = ""

    def _mark_down(self, why):
        # Log only once per "unreachable" streak, not 4 times a second.
        if self.server_ok is not False:
            self.log(f"{stamp()} cannot reach the main laptop at {self.server}: {why}")
            self.log("         -> Is run.py running there with --host 0.0.0.0? Same Wi-Fi? "
                     "On the main laptop: set this Wi-Fi/hotspot's network profile to Private "
                     "(Settings > Network & internet > Wi-Fi > the network > Network profile type), "
                     "and in Windows Security > Firewall > Allow an app make sure python.exe is allowed.")
        self.server_ok = False
        self.last_error = why

    async def send_once(self):
        """Send whatever is in the buffer. Returns the number of packets the server accepted."""
        packets = self.take_batch()
        if not packets:
            return 0
        body = {"laptop": self.laptop, "packets": packets}
        try:
            reply = await asyncio.to_thread(self._post, "/api/room/sightings", body)
        except urllib.error.HTTPError as e:
            # The server answered but refused the batch (e.g. 400 bad data, 404 old server).
            # Retrying the same packets won't help, so drop them.
            self.dropped += len(packets)
            self._mark_ok()
            # Say it once (and again every 10 s while it keeps happening), not 4 times a second.
            if self.refused is None or self.refused[0] != e.code or time.monotonic() - self.refused[1] > 10:
                self.refused = (e.code, time.monotonic())
                self.log(f"{stamp()} server refused the packets: HTTP {e.code} {self._detail(e)}")
            return 0
        except (urllib.error.URLError, OSError, ValueError) as e:
            # Server not reachable: put the packets back (they get dropped later if too old).
            for p in reversed(packets):
                self.buffer.appendleft((time.monotonic() - p["age_ms"] / 1000.0, p["rssi"]))
            while len(self.buffer) > MAX_BUFFER:
                self.buffer.popleft()
                self.dropped += 1
            self._mark_down(self._detail(e))
            return 0
        self._mark_ok()
        self.refused = None
        accepted = int(reply.get("accepted", len(packets))) if isinstance(reply, dict) else len(packets)
        self.sent_total += accepted
        return accepted

    @staticmethod
    def _detail(e):
        if isinstance(e, urllib.error.HTTPError):
            try:
                text = e.read().decode("utf-8", "replace").strip()
            except Exception:
                return ""
            # Keep just the first line; skip HTML error pages.
            return "" if text.startswith("<") else text.splitlines()[0][:150] if text else ""
        reason = getattr(e, "reason", None)
        return str(reason if reason is not None else e) or type(e).__name__

    async def check_server(self):
        """One quick GET /api/room at start-up, so problems show up immediately."""
        try:
            await asyncio.to_thread(self._get, "/api/room")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                self.log(f"{stamp()} WARNING: {self.server} answered, but has no Room demo "
                         "(/api/room not found). Is the main laptop running the latest code?")
            self._mark_ok()
            return
        except (urllib.error.URLError, OSError, ValueError) as e:
            self._mark_down(self._detail(e))
            return
        self._mark_ok()
        self.log(f"{stamp()} main laptop reachable at {self.server}")

    async def sender_loop(self):
        """Forever: send the buffer every `interval` seconds, print a status line every ~2 s."""
        next_status = time.monotonic() + STATUS_EVERY_S
        while True:
            await asyncio.sleep(self.interval)
            await self.send_once()
            if time.monotonic() >= next_status:
                next_status = time.monotonic() + STATUS_EVERY_S
                self.log(self.status_line())

    def status_line(self):
        heard, self.window = self.window, []
        if heard:
            text = f"heard {len(heard)} packets, median {statistics.median(heard):.0f} dBm"
        else:
            text = "heard 0 packets - is the tag powered on and in range?"
        if self.server_ok is True and self.refused:
            text += f", server REFUSING packets (HTTP {self.refused[0]})"
        elif self.server_ok is True:
            text += ", server ok"
        elif self.server_ok is False:
            text += f", SERVER UNREACHABLE ({self.last_error})"
        if self.dropped:
            text += f", {self.dropped} dropped"
        return f"{stamp()} {text}"


async def fake_source(rep):
    """--fake: about 8 made-up packets per second around -65 dBm. Only for testing the network."""
    while True:
        await asyncio.sleep(0.125)
        rep.add(round(random.gauss(-65, 4)))


def warn_if_uncalibrated(laptop):
    """Friendly hint if this laptop has no calibration curve in data/laptop_profiles.json."""
    try:
        profiles = json.loads(PROFILES_FILE.read_text(encoding="utf-8")).get("profiles") or {}
    except (OSError, ValueError):
        return
    p = profiles.get(laptop)
    if not p or not p.get("calibrated"):
        print(f"Note: laptop '{laptop}' is not calibrated in this laptop's data/laptop_profiles.json. "
              "The room only uses the curves stored on the MAIN laptop, and uses Alex's curve for "
              "any laptop it doesn't know. To calibrate: on THIS laptop run "
              f"'.venv\\Scripts\\python run.py --mode real --laptop {laptop}', open the Live ESP32 page, "
              "press Measure at 2-3 distances, then copy the '" + laptop + "' entry from this laptop's "
              "data/laptop_profiles.json into the main laptop's data/laptop_profiles.json (or commit "
              "and pull it) BEFORE starting the main laptop.")


async def run(args):
    laptop = slug(args.laptop)
    rep = Reporter(args.server, laptop, args.interval)
    print("=" * 64)
    print(f" AutoTrack reporter: laptop '{laptop}' -> {rep.server}")
    print(" Sends what this laptop hears to the main laptop. Ctrl+C to stop.")
    print("=" * 64, flush=True)
    warn_if_uncalibrated(laptop)
    await rep.check_server()

    ble = None
    if args.fake:
        print("FAKE MODE: sending made-up packets (network test only - don't use in the real demo).")
        source = asyncio.ensure_future(fake_source(rep))
    else:
        try:
            keys = load_keys(args.keys)
        except FileNotFoundError:
            print(f"ERROR: key file not found: {args.keys}")
            return 2
        except (ValueError, OSError) as e:
            print(f"ERROR: could not read {args.keys}: {e}")
            return 2
        from autotrack.real_ble import RealBLE
        ble = RealBLE(laptop, keys, rep.on_sighting, log=lambda m: print(stamp(), m))
        await ble.start()   # never raises; keeps retrying in the background if Bluetooth is off
        if not ble.status["scanning"]:
            print(f"{stamp()} Bluetooth not scanning yet: {ble.status['error']}")
            print("         Check Windows Settings > Bluetooth & devices > Bluetooth = On. Retrying...")
        source = None

    try:
        await rep.sender_loop()
    finally:
        if source is not None:
            source.cancel()
        if ble is not None:
            try:
                await ble.stop()
            except BaseException:
                pass
    return 0


def main():
    p = argparse.ArgumentParser(
        description="Room demo: listen for the AutoTrack tag and forward the signal to the main laptop.")
    p.add_argument("--server", required=True,
                   help="the main laptop's address, as printed by run.py, e.g. http://192.168.1.23:8000")
    p.add_argument("--laptop", required=True,
                   help="whose laptop this is (e.g. joseph) - must match its calibration profile name")
    p.add_argument("--interval", type=float, default=0.25, help="seconds between sends (default 0.25)")
    p.add_argument("--keys", default=DEFAULT_KEYS, help="path to tag_keys.json")
    p.add_argument("--fake", action="store_true",
                   help="no Bluetooth: send made-up packets to test the network connection")
    args = p.parse_args()

    if not args.fake:
        try:
            import bleak  # noqa: F401  (only to give a clear message)
        except ImportError:
            print("ERROR: the 'bleak' Bluetooth library is not installed in this Python.")
            print("Fix:   .venv\\Scripts\\python -m pip install bleak")
            print("(Make sure you run this script with .venv\\Scripts\\python, not plain 'python'.)")
            print("To test only the network connection without Bluetooth, add --fake.")
            return 2

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
