"""
esp-zone-tracker anchor agent
=============================

Runs on each anchor laptop. Scans BLE advertisements from the ESP32-S3 tag
("ESP-TAG", service 7b1e0001-3c4d-4e8a-9f21-5a6b7c8d9e01), smooths the RSSI
and POSTs one reading per interval to the website ({server}/api/readings).
Laptops never talk to each other; each one only talks to the website.

Exactly one laptop is started with --central. It additionally drives the tag's
LED from the server's "alert" flag, connecting to the tag only on demand.

Usage:
  pip install -r requirements.txt
  python anchor.py --server https://your-app.vercel.app --id laptop-A --central --key SECRET
  python anchor.py --server https://your-app.vercel.app --id laptop-B --key SECRET

Other options:
  --name ESP-TAG     advertised name of the tag to match
  --interval 1.0     seconds between reports
  --verbose          print request/response details and LED debug output
  (the key can also come from the AGENT_KEY environment variable)

Requires Python 3.9+ and bleak>=0.22. Stdlib only otherwise.
"""

import argparse
import asyncio
import http.client
import json
import os
import re
import socket
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

SERVICE_UUID = "7b1e0001-3c4d-4e8a-9f21-5a6b7c8d9e01"
LED_CHAR_UUID = "7b1e0002-3c4d-4e8a-9f21-5a6b7c8d9e01"

LED_OFF = 0x00
LED_ON = 0x01
LED_IDENTIFY = 0x02  # blue blink, used once at startup as a link check

STALE_S = 3.0             # tag considered gone after this many seconds unseen
KALMAN_Q = 2.0            # process noise
KALMAN_R = 6.0            # measurement noise
HTTP_TIMEOUT_S = 5.0
USER_AGENT = "esp-zone-tracker-agent/1.0"

LED_CONNECT_TIMEOUT_S = 10.0
LED_TASK_TIMEOUT_S = 25.0  # hard cap for one connect+write attempt
LED_RETRY_BACKOFF_S = 3.0
LED_REFRESH_S = 60.0       # re-send "on" this often while alert is active
IDENTIFY_MAX_ATTEMPTS = 2

ID_RE = re.compile(r"[\w.\- ]{1,40}")


def log(msg: str) -> None:
    print("[{}] {}".format(datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def fmt_num(v: float) -> str:
    """-69.0 -> '-69', -68.5 -> '-68.5'."""
    return "{:g}".format(v)


# --------------------------------------------------------------------------
# Filtering
# --------------------------------------------------------------------------

class Kalman1D:
    """Scalar Kalman filter with a constant-value model."""

    def __init__(self, q: float, r: float) -> None:
        self.q = q
        self.r = r
        self.x: Optional[float] = None
        self.p: float = 0.0

    def reset(self) -> None:
        self.x = None
        self.p = 0.0

    def update(self, z: float) -> float:
        if self.x is None:
            self.x = z
            self.p = self.r
            return self.x
        self.p += self.q
        k = self.p / (self.p + self.r)
        self.x += k * (z - self.x)
        self.p *= (1.0 - k)
        return self.x


# --------------------------------------------------------------------------
# BLE scanning
# --------------------------------------------------------------------------

class TagScanner:
    """Collects RSSI samples of the tag. The callback runs on the event loop
    thread, so plain attributes/lists need no locking."""

    def __init__(self, tag_name: str, verbose: bool) -> None:
        self.tag_name = tag_name
        self.verbose = verbose
        self.samples: List[int] = []
        self.device: Optional[BLEDevice] = None
        self.last_seen: Optional[float] = None  # time.monotonic()
        self._gap = False  # tag reappeared after > STALE_S without samples

    def on_advertisement(self, device: BLEDevice, adv: AdvertisementData) -> None:
        try:
            if not self._matches(device, adv):
                return
            now = time.monotonic()
            if self.device is None:
                log("tag found: {} name={!r} rssi={}".format(
                    device.address, adv.local_name or device.name, adv.rssi))
            if self.last_seen is not None and now - self.last_seen > STALE_S:
                self._gap = True
            self.samples.append(adv.rssi)
            self.device = device
            self.last_seen = now
        except Exception as e:  # never let a callback error kill scanning
            if self.verbose:
                log("scan callback error: {!r}".format(e))

    def _matches(self, device: BLEDevice, adv: AdvertisementData) -> bool:
        if adv.local_name == self.tag_name or device.name == self.tag_name:
            return True
        for u in adv.service_uuids or ():
            if u.lower() == SERVICE_UUID:
                return True
        return False

    def take(self) -> Tuple[List[int], bool]:
        """Return (samples since last call, gap flag) and clear both."""
        batch, self.samples = self.samples, []
        gap, self._gap = self._gap, False
        return batch, gap

    def age(self, now: float) -> Optional[float]:
        if self.last_seen is None:
            return None
        return now - self.last_seen


# --------------------------------------------------------------------------
# Server communication
# --------------------------------------------------------------------------

class HttpStatusError(Exception):
    def __init__(self, code: int, body: str) -> None:
        super().__init__("HTTP {}".format(code))
        self.code = code
        self.body = body


def http_post_json(url: str, payload: Dict[str, Any], key: str, timeout: float) -> Any:
    """Blocking POST; run in a worker thread."""
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if key:
        headers["x-agent-key"] = key
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        try:
            text = e.read().decode("utf-8", "replace")
        except Exception:
            text = ""
        finally:
            e.close()
        raise HttpStatusError(e.code, text) from None
    if not body:
        return {}
    return json.loads(body.decode("utf-8"))


class ServerClient:
    def __init__(self, base_url: str, key: str, verbose: bool) -> None:
        self.url = base_url + "/api/readings"
        self.key = key
        self.verbose = verbose
        self.offline = False
        self.auth_failed = False
        self.last_http_error: Optional[int] = None

    def _back_online(self) -> None:
        if self.offline:
            self.offline = False
            log("reconnected to server")

    async def post(self, payload: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
        """Returns (response dict or None, short status text for the log line)."""
        if self.verbose:
            log("POST {} {}".format(self.url, json.dumps(payload)))
        try:
            resp = await asyncio.to_thread(
                http_post_json, self.url, payload, self.key, HTTP_TIMEOUT_S)
        except HttpStatusError as e:
            self._back_online()  # the server answered, so the network is fine
            if e.code == 401:
                if not self.auth_failed:
                    log("ERROR: server returned HTTP 401 Unauthorized -> wrong --key "
                        "(or AGENT_KEY). Fix the key and restart this agent.")
                self.auth_failed = True
                return None, "HTTP 401 wrong --key"
            if e.code != self.last_http_error or self.verbose:  # log once per streak
                snippet = " ".join(e.body.split())[:120]
                log("server error HTTP {}: {}".format(e.code, snippet or "(empty body)"))
                if 300 <= e.code < 400:
                    log("  (redirect: check --server; use the exact https:// domain)")
            self.last_http_error = e.code
            return None, "HTTP {}".format(e.code)
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            # URLError/socket.timeout/ConnectionError/ssl errors are all OSError;
            # HTTPException covers IncompleteRead, BadStatusLine etc.
            if not self.offline:
                self.offline = True
                reason = getattr(e, "reason", None) or e
                log("server unreachable ({}); retrying every tick...".format(reason))
            return None, "offline"
        except ValueError as e:  # JSON decode error
            self._back_online()
            log("server sent invalid JSON: {}".format(e))
            return None, "bad response"

        self._back_online()
        self.last_http_error = None
        if self.auth_failed:
            self.auth_failed = False
            log("key accepted by server")
        if not isinstance(resp, dict):
            log("server sent unexpected JSON: {!r}".format(resp)[:160])
            return None, "bad response"
        if self.verbose:
            log("response {}".format(json.dumps(resp)))
        return resp, "ok"


def describe_response(resp: Dict[str, Any]) -> str:
    status = resp.get("status", "?")
    pos = resp.get("position")
    pos_txt = "none"
    if isinstance(pos, dict):
        try:
            pos_txt = "({:.2f},{:.2f})".format(float(pos["x"]), float(pos["y"]))
        except (KeyError, TypeError, ValueError):
            pos_txt = "?"
    anchors = resp.get("usedAnchors", "?")
    return "server: {} pos={} anchors={}".format(status, pos_txt, anchors)


# --------------------------------------------------------------------------
# LED control (central only)
# --------------------------------------------------------------------------

class LedController:
    """Drives the tag LED with short on-demand connections.

    A held connection on Windows can disrupt advertisement reception, so each
    write is its own connect -> write -> disconnect, run as a background task
    (at most one at a time) so the report loop never blocks on BLE.
    """

    def __init__(self, scanner: TagScanner, verbose: bool) -> None:
        self.scanner = scanner
        self.verbose = verbose
        self.desired: Optional[bool] = None   # from the latest server response
        self.applied: Optional[bool] = None   # last state confirmed written
        self.applied_at = 0.0
        self.next_try = 0.0
        self.identified = False
        self.identify_attempts = 0
        self.task: Optional["asyncio.Task[None]"] = None
        self._pending: Optional[str] = None

    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    def tick(self, desired: Optional[bool]) -> None:
        """Called once per report. desired=None means 'no fresh server answer':
        keep the previous target."""
        if desired is not None:
            self.desired = desired
        if self.busy():
            return
        device = self.scanner.device
        if device is None:
            return  # tag never seen yet; nothing to connect to
        now = time.monotonic()
        if now < self.next_try:
            return

        if not self.identified:
            self.identify_attempts += 1
            self._spawn(device, LED_IDENTIFY, "identify (blue blink)", None)
            return
        if self.desired is None:
            return
        if self.desired != self.applied:
            self._spawn(device, LED_ON if self.desired else LED_OFF,
                        "on" if self.desired else "off", self.desired)
        elif self.desired and now - self.applied_at >= LED_REFRESH_S:
            self._spawn(device, LED_ON, "on (refresh)", True)

    def _spawn(self, device: BLEDevice, value: int, label: str,
               state: Optional[bool]) -> None:
        self._pending = label
        self.task = asyncio.ensure_future(self._write(device, value, label, state))

    async def _write(self, device: BLEDevice, value: int, label: str,
                     state: Optional[bool]) -> None:
        t0 = time.monotonic()
        try:
            if self.verbose:
                log("LED: connecting to {} to send {}".format(device.address, label))
            await asyncio.wait_for(self._connect_and_write(device, value),
                                   LED_TASK_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.next_try = time.monotonic() + LED_RETRY_BACKOFF_S
            msg = "{}: {}".format(type(e).__name__, e) if str(e) else type(e).__name__
            log("LED: sending {} failed ({}); retrying in {:.0f}s".format(
                label, msg, LED_RETRY_BACKOFF_S))
            if state is None and self.identify_attempts >= IDENTIFY_MAX_ATTEMPTS:
                self.identified = True  # give up on identify, don't block LED control
                log("LED: skipping identify after {} attempts".format(self.identify_attempts))
            return
        finally:
            self._pending = None

        dt = time.monotonic() - t0
        if state is None:
            self.identified = True
            log("LED: identify sent (tag should blink blue) - BLE link OK [{:.1f}s]".format(dt))
        else:
            self.applied = state
            self.applied_at = time.monotonic()
            log("LED: {} written [{:.1f}s]".format(label, dt))

    @staticmethod
    async def _connect_and_write(device: BLEDevice, value: int) -> None:
        async with BleakClient(device, timeout=LED_CONNECT_TIMEOUT_S) as client:
            await client.write_gatt_char(LED_CHAR_UUID, bytes([value]), response=True)

    def describe(self) -> str:
        if self.busy() and self._pending:
            return "LED -> {}...".format(self._pending)
        if self.scanner.device is None:
            return "LED waiting for tag"
        if self.applied is None:
            state = "unknown"
        else:
            state = "ON" if self.applied else "off"
        if self.desired is not None and self.desired != self.applied:
            return "LED {} (want {}, retrying)".format(state, "ON" if self.desired else "off")
        return "LED {}".format(state)

    async def close(self) -> None:
        if self.busy():
            assert self.task is not None
            self.task.cancel()
            try:
                await self.task
            except BaseException:
                pass


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

async def report_once(args: argparse.Namespace, scanner: TagScanner, kf: Kalman1D,
                      server: ServerClient, led: Optional[LedController]) -> None:
    batch, gap = scanner.take()
    now = time.monotonic()
    age = scanner.age(now)

    smoothed: Optional[float]
    raw: Optional[float]
    if batch:
        if gap:
            kf.reset()
        med = float(statistics.median(batch))
        raw = round(med, 1)
        smoothed = round(kf.update(med), 1)
        rssi_txt = "RSSI {} (raw {}, {} pkts)".format(
            fmt_num(smoothed), fmt_num(raw), len(batch))
    elif age is not None and age <= STALE_S and kf.x is not None:
        raw = None
        smoothed = round(kf.x, 1)
        rssi_txt = "RSSI {} (held, 0 pkts)".format(fmt_num(smoothed))
    else:
        kf.reset()
        raw = None
        smoothed = None
        if age is None:
            rssi_txt = "RSSI --  (tag not seen yet)"
        else:
            rssi_txt = "RSSI --  (tag lost {:.0f}s)".format(age)

    payload = {
        "id": args.id,
        "rssi": smoothed,
        "raw": raw,
        "samples": len(batch),
        "central": bool(args.central),
    }
    resp, state = await server.post(payload)
    server_txt = describe_response(resp) if resp is not None else "server: {}".format(state)

    parts = [rssi_txt, server_txt]
    if led is not None:
        desired = bool(resp.get("alert")) if resp is not None else None
        led.tick(desired)
        parts.append(led.describe())
    log(" | ".join(parts))


async def run(args: argparse.Namespace) -> int:
    tag = TagScanner(args.name, args.verbose)
    kf = Kalman1D(KALMAN_Q, KALMAN_R)
    server = ServerClient(args.server, args.key, args.verbose)
    led = LedController(tag, args.verbose) if args.central else None

    scanner = BleakScanner(detection_callback=tag.on_advertisement, scanning_mode="active")
    try:
        await scanner.start()
    except Exception as e:
        log("ERROR: could not start BLE scanning: {}: {}".format(type(e).__name__, e))
        log("Is Bluetooth turned on and is this laptop's adapter BLE-capable?")
        return 2
    log("scanning for '{}' / {} ...".format(args.name, SERVICE_UUID))

    try:
        next_tick = time.monotonic() + args.interval
        while True:
            delay = next_tick - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            next_tick += args.interval
            if next_tick < time.monotonic():  # fell behind (slow HTTP): resync
                next_tick = time.monotonic() + args.interval
            try:
                await report_once(args, tag, kf, server, led)
            except Exception as e:  # never crash the loop
                log("report error: {}: {}".format(type(e).__name__, e))
    finally:
        if led is not None:
            await led.close()
        try:
            await asyncio.wait_for(scanner.stop(), 5.0)
        except BaseException:
            pass
        log("scanner stopped")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="esp-zone-tracker anchor agent: scan BLE RSSI of the tag and report it.")
    p.add_argument("--server", required=True,
                   help="website base URL, e.g. https://xyz.vercel.app or http://localhost:3000")
    p.add_argument("--id", default=socket.gethostname(),
                   help="anchor name as configured on the website (default: hostname)")
    p.add_argument("--key", default=os.environ.get("AGENT_KEY", ""),
                   help="shared agent key, sent as x-agent-key (default: env AGENT_KEY)")
    p.add_argument("--central", action="store_true",
                   help="this laptop also controls the tag's LED")
    p.add_argument("--name", default="ESP-TAG", help="advertised tag name (default ESP-TAG)")
    p.add_argument("--interval", type=float, default=1.0,
                   help="seconds between reports (default 1.0)")
    p.add_argument("--verbose", action="store_true", help="extra debug output")
    args = p.parse_args(argv)

    args.server = args.server.strip().rstrip("/")
    parsed = urllib.parse.urlparse(args.server)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        p.error("--server must be a full http(s) URL, e.g. https://your-app.vercel.app")

    args.id = (args.id or "").strip()
    if not ID_RE.fullmatch(args.id):
        p.error("--id {!r} is invalid: use 1-40 letters, digits, '_', '-', '.' or spaces "
                "(pass --id explicitly, e.g. --id laptop-A)".format(args.id))

    if not args.interval > 0.1:
        p.error("--interval must be greater than 0.1 seconds")
    return args


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    print("=" * 60)
    print("esp-zone-tracker anchor agent")
    print("  server  : {}".format(args.server))
    print("  id      : {}".format(args.id))
    print("  central : {}".format("yes (controls tag LED)" if args.central else "no"))
    print("  tag     : name '{}' or service {}".format(args.name, SERVICE_UUID))
    print("  key     : {}".format("set" if args.key else "not set"))
    print("  interval: {}s".format(fmt_num(args.interval)))
    print("Leave this window open; the laptop must not sleep.")
    print("Press Ctrl+C to stop.")
    print("=" * 60, flush=True)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())
