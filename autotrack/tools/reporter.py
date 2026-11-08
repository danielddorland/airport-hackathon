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
  * DEFAULT (UDP): runs its own Kalman filter (same as the main laptop) on the RSSI and sends
    ONE small UDP datagram every 0.2 s (a heartbeat even when nothing was heard) to port 8765:
        {"v":1,"laptop":"joseph","seq":7,"rssi_filtered":-61.3,"variance":2.1,"raw_median":-62,
         "packets":2,"t_age_ms":85}
    The old behaviour below is kept with  --transport http.
  * HTTP: every 0.25 s sends the buffer to the main laptop:
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
import socket
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from autotrack.laptops import PROFILES_FILE, slug  # noqa: E402
from autotrack.room import ONLINE_S, SIGMA_DB_DEFAULT, RssiKalman  # noqa: E402
from autotrack.tagcrypto import load_keys  # noqa: E402

DEFAULT_KEYS = os.path.join(ROOT, "data", "tag_keys.json")
MAX_BUFFER = 500       # the server refuses more than 500 packets per request
MAX_AGE_S = 5.0        # older packets are useless for "where is the tag NOW"
STATUS_EVERY_S = 2.0
HTTP_TIMEOUT_S = 2.0
UDP_PORT = 8765
UDP_MIN_S, UDP_MAX_S = 0.1, 0.25    # allowed send interval for UDP


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

    def __init__(self, server, laptop, interval=None, log=print, transport="udp",
                 udp_port=UDP_PORT, clock=time.monotonic, sigma_db=SIGMA_DB_DEFAULT):
        if transport not in ("udp", "http"):
            raise ValueError("transport must be udp or http")
        self.server = normalise_server(server)
        self.laptop = laptop
        self.transport = transport
        self.udp_port = int(udp_port)
        self.clock = clock                     # one clock for packet times, the Kalman filter and datagrams
        if transport == "udp":
            interval = 0.2 if interval is None else interval
            self.interval = max(UDP_MIN_S, min(UDP_MAX_S, float(interval)))
        else:
            self.interval = max(0.05, float(0.25 if interval is None else interval))
        self.log = log
        # Same filter and parameters as the main laptop (room.RssiKalman, r = sigma^2).
        self.kalman = RssiKalman()
        self.r = float(sigma_db) ** 2
        self.seq = 0                           # datagram counter (UDP)
        self.dg_raw = []                       # raw RSSIs heard since the last datagram
        self.last_heard = None                 # clock() time of the last packet
        self.sock = None
        self.udp_host = urllib.parse.urlsplit(self.server).hostname
        self.last_filtered = None              # for the status line
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
        t = heard_at if heard_at is not None else self.clock()
        if self.transport == "udp":
            self.buffer.clear()                # the UDP path doesn't use the packet buffer
        self.buffer.append((t, int(rssi)))
        self.window.append(int(rssi))
        if self.transport == "udp":            # (the http fallback sends raw packets, no filter)
            self.dg_raw.append(int(rssi))
            self.last_heard = t if self.last_heard is None else max(self.last_heard, t)
            self.kalman.update(t, float(rssi), self.r)
        while len(self.buffer) > MAX_BUFFER:
            self.buffer.popleft()
            self.dropped += 1

    def on_sighting(self, s):
        """Callback for RealBLE: s = dict(receiver, tag_serial, rssi, t, counter)."""
        self.add(s["rssi"])

    # ------------------------------------------------------------------ UDP
    def build_datagram(self, now=None):
        """The next datagram as a dict (pure: no network). Always built, also when nothing was heard."""
        now = self.clock() if now is None else now
        heard = self.last_heard is not None and now - self.last_heard <= ONLINE_S
        k = self.kalman
        fresh = heard and k.x is not None
        raw = self.dg_raw
        dg = {
            "v": 1, "laptop": self.laptop, "seq": self.seq,
            "rssi_filtered": round(k.x, 2) if fresh else None,
            "variance": round(k.p, 3) if fresh else None,
            "raw_median": statistics.median(raw) if raw else None,
            "packets": len(raw),
            "t_age_ms": None if self.last_heard is None else max(0, round((now - self.last_heard) * 1000)),
        }
        self.seq += 1
        self.dg_raw = []
        self.last_filtered = dg["rssi_filtered"]
        return dg

    def _udp_send(self, dg):
        """Fire and forget. Errors are logged once per streak."""
        data = json.dumps(dg, separators=(",", ":")).encode("utf-8")
        try:
            if self.sock is None:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.sock.setblocking(False)
            self.sock.sendto(data, (self.udp_host, self.udp_port))
        except (OSError, ValueError) as e:
            if self.server_ok is not False:
                self.log(f"{stamp()} cannot send UDP to {self.udp_host}:{self.udp_port}: {e}")
                self.log("         -> Same Wi-Fi? Right address? (or use --transport http)")
            self.server_ok = False
            self.last_error = str(e) or type(e).__name__
            return False
        if self.server_ok is False:
            self.log(f"{stamp()} UDP sending works again ({self.udp_host}:{self.udp_port})")
        self.server_ok = True
        self.sent_total += 1
        return True

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
                     "Windows Firewall on the main laptop: allow Python on private networks.")
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
        """Forever: send every `interval` seconds, print a status line every ~2 s."""
        next_status = time.monotonic() + STATUS_EVERY_S
        while True:
            await asyncio.sleep(self.interval)
            if self.transport == "udp":
                self._udp_send(self.build_datagram())
            else:
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
        if self.transport == "udp":
            text += f", filtered {self.last_filtered if self.last_filtered is not None else '-'} dBm"
            if self.server_ok is False:
                text += f", UDP SEND FAILING ({self.last_error})"
            else:
                text += f", udp -> {self.udp_host}:{self.udp_port}"
            return f"{stamp()} {text}"
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
        print(f"Note: laptop '{laptop}' is not calibrated in data/laptop_profiles.json, so the main "
              "laptop will use Alex's curve for it. Calibrate it first for better distances "
              "(Live ESP32 page on the main laptop, or measure here and copy the numbers).")


async def run(args):
    laptop = slug(args.laptop)
    rep = Reporter(args.server, laptop, args.interval, transport=args.transport, udp_port=args.udp_port)
    print("=" * 64)
    print(f" AutoTrack reporter: laptop '{laptop}' -> {rep.server}")
    print(" Sends what this laptop hears to the main laptop. Ctrl+C to stop.")
    print("=" * 64, flush=True)
    warn_if_uncalibrated(laptop)
    if rep.transport == "udp":
        print(f" UDP: one datagram every {rep.interval:g} s to {rep.udp_host}:{rep.udp_port}")
    # Quick HTTP check either way: UDP is fire-and-forget, so this is the only early sign that
    # the main laptop (or its firewall) isn't reachable.
    await rep.check_server()
    if rep.transport == "udp":
        rep.server_ok = None      # from here on server_ok tracks UDP sending, not that HTTP check

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
    p.add_argument("--transport", choices=["udp", "http"], default="udp",
                   help="udp (default): send this laptop's own filtered signal; http: old fallback, raw packets")
    p.add_argument("--udp-port", type=int, default=UDP_PORT, help="UDP port on the main laptop (default 8765)")
    p.add_argument("--interval", type=float, default=None,
                   help="seconds between sends (default 0.2 for udp, clamped to 0.1-0.25; 0.25 for http)")
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
