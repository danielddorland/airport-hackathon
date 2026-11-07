"""
real_ble.py - the laptop's own Bluetooth as ONE real AutoTrack receiver
=======================================================================

What this does, in plain English:
  * Listens (scans) for Bluetooth advertisements using the `bleak` library.
  * For every advertisement that carries AutoTrack manufacturer data
    (company ID 0xFFFF), it checks the secret HMAC against every known tag key
    (see tagcrypto.py). Only if the check passes does it call
    on_sighting(...) - so fake or replayed broadcasts are ignored.
  * set_alert(serial, mode) makes the tag flash/beep: it connects to the tag,
    writes ONE signed command, reads back the tag's answer and disconnects
    again. (On Windows a connection that stays open disturbs the scanning, so
    we never keep it open.)

How the server uses it (inside uvicorn's asyncio event loop):

    ble = RealBLE("belt-loader-1", keys, on_sighting=handle_sighting)
    await ble.start()                 # in a FastAPI startup hook
    ok = await ble.set_alert("TAG-0001", 1)   # WRONG AIRCRAFT
    await ble.stop()                  # in a FastAPI shutdown hook

`bleak` is imported only inside start()/the write code, so the rest of the
app still runs on a computer without bleak (it just reports an error status).
"""

import asyncio
import inspect
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

try:  # normal case: imported as autotrack.real_ble
    from .tagcrypto import (
        COMMAND_CHAR_UUID, COMPANY_ID, MODE_NAMES, RESULT_BAD_MAC, RESULT_NAMES,
        RESULT_OK, RESULT_OLDER_THAN_LAST, RESULT_STALE_COUNTER, make_command,
        parse_adv_payload, parse_status, verify)
except ImportError:  # fallback: real_ble.py imported as a top-level module
    from tagcrypto import (  # type: ignore
        COMMAND_CHAR_UUID, COMPANY_ID, MODE_NAMES, RESULT_BAD_MAC, RESULT_NAMES,
        RESULT_OK, RESULT_OLDER_THAN_LAST, RESULT_STALE_COUNTER, make_command,
        parse_adv_payload, parse_status, verify)


# Timings (seconds)
CONNECT_TIMEOUT_S = 10.0     # how long bleak may try to connect
ATTEMPT_TIMEOUT_S = 15.0     # hard cap for one connect + write + read + disconnect
RETRY_DELAY_S = 1.0          # wait before the one retry
RESTART_DELAY_S = 5.0        # if Bluetooth is off, try starting the scan again this often
DISCONNECT_TIMEOUT_S = 5.0
BACKWARDS_WARN_EVERY_S = 30.0


def _explain(e: BaseException) -> str:
    """Turn a Bluetooth exception into a message a human can act on."""
    text = "{}: {}".format(type(e).__name__, e) if str(e) else type(e).__name__
    low = text.lower()
    if "access" in low and "denied" in low:
        text += (" -> Windows refused the connection. Remove the tag from Windows "
                 "Settings > Bluetooth & devices if it is listed, close other "
                 "Bluetooth apps (nRF Connect on a phone too) and press RESET on the ESP32.")
    elif ("not available" in low or "radio" in low or "turned off" in low
          or "powered off" in low or "no bluetooth" in low):
        text += " -> Is Bluetooth switched on? (Windows Settings > Bluetooth & devices)"
    elif "not found" in low and "device" in low:
        text += " -> the tag is out of range or switched off."
    return text


class RealBLE:
    """One real receiver = this laptop's Bluetooth adapter."""

    def __init__(self, receiver_id: str, keys: Dict[str, bytes],
                 on_sighting: Callable[[Dict[str, Any]], Any],
                 log: Callable[[str], Any] = print) -> None:
        self.receiver_id = receiver_id
        self.keys = dict(keys)              # {"TAG-0001": b"<16 bytes>"}
        self.on_sighting = on_sighting
        self._log = log

        # Public status for the website / debugging.
        self.status: Dict[str, Any] = {
            "receiver": receiver_id,
            "scanning": False,
            "error": None,
            "tags": {},          # serial -> {"last_seen", "rssi", "counter", "address"}
            "last_write": None,  # {"tag", "mode", "mode_name", "ok", "detail", "t", ...}
        }

        self._scanner = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[int] = None
        self._stopping = False
        self._restart_task: Optional["asyncio.Task[None]"] = None

        self._last_counter: Dict[str, int] = {}     # serial -> last accepted counter
        self._devices: Dict[str, Any] = {}          # serial -> latest bleak BLEDevice
        self._addr_cache: Dict[str, Tuple[bytes, str]] = {}  # address -> (payload, serial)
        self._backwards_warned: Dict[str, float] = {}
        self._callback_errors = 0

        # Alert writes: at most one in flight per tag; newest request waits.
        self._pending: Dict[str, Tuple[int, "asyncio.Future[bool]"]] = {}
        self._writers: Dict[str, "asyncio.Task[None]"] = {}

    # ------------------------------------------------------------------ logging
    def log(self, msg: str) -> None:
        try:
            self._log("[ble] " + msg)
        except Exception:
            pass

    # ------------------------------------------------------------------ scanning
    async def start(self) -> None:
        """Start scanning. Never raises: on failure sets status["error"], logs,
        and keeps retrying every few seconds in the background (so you can
        switch Bluetooth on without restarting the server)."""
        self._stopping = False
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        if self.status["scanning"]:
            return
        ok, fatal = await self._try_start_scanner()
        if not ok and not fatal and (self._restart_task is None or self._restart_task.done()):
            self._restart_task = asyncio.ensure_future(self._restart_loop())

    async def _try_start_scanner(self) -> Tuple[bool, bool]:
        """Returns (started, fatal). fatal=True means retrying will not help."""
        try:
            from bleak import BleakScanner  # lazy import on purpose
        except ImportError:
            self._set_error("bleak is not installed. Run:  .venv\\Scripts\\python -m pip install bleak")
            return False, True
        try:
            scanner = BleakScanner(detection_callback=self._on_advertisement,
                                   scanning_mode="active")
            await asyncio.wait_for(scanner.start(), 10.0)
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # noqa: BLE001 - must never crash the server
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            self._set_error("could not start Bluetooth scanning: " + _explain(e))
            return False, False
        self._scanner = scanner
        self.status["scanning"] = True
        self.status["error"] = None
        self.log("scanning as receiver '{}' for {} known tag(s)".format(
            self.receiver_id, len(self.keys)))
        return True, False

    async def _restart_loop(self) -> None:
        while not self._stopping and not self.status["scanning"]:
            await asyncio.sleep(RESTART_DELAY_S)
            if self._stopping:
                return
            ok, fatal = await self._try_start_scanner()
            if ok or fatal:
                return

    def _set_error(self, msg: str) -> None:
        if self.status["error"] != msg:  # do not spam the same message
            self.log("ERROR: " + msg)
        self.status["error"] = msg
        self.status["scanning"] = False

    async def stop(self) -> None:
        """Stop scanning and cancel any alert writes still running."""
        self._stopping = True
        if self._restart_task is not None:
            self._restart_task.cancel()
            await asyncio.gather(self._restart_task, return_exceptions=True)
            self._restart_task = None
        tasks = [t for t in self._writers.values() if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for _mode, fut in self._pending.values():
            if not fut.done():
                fut.set_result(False)
        self._pending.clear()
        if self._scanner is not None:
            try:
                await asyncio.wait_for(self._scanner.stop(), 5.0)
            except BaseException as e:  # noqa: BLE001
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise
            self._scanner = None
        if self.status["scanning"]:
            self.log("scanning stopped")
        self.status["scanning"] = False

    # ------------------------------------------------------- advertisement handling
    def _on_advertisement(self, device: Any, adv: Any) -> None:
        """Called by bleak for EVERY Bluetooth advertisement nearby (many per second)."""
        # bleak already calls us on the event loop thread; this is just a safety net.
        if (self._loop is not None and self._loop_thread is not None
                and threading.get_ident() != self._loop_thread):
            self._loop.call_soon_threadsafe(self._on_advertisement, device, adv)
            return
        try:
            self._handle_advertisement(device, adv)
        except Exception as e:  # never let one bad packet kill scanning
            self._callback_errors += 1
            if self._callback_errors <= 5:
                self.log("error while handling an advertisement: " + _explain(e))

    def _handle_advertisement(self, device: Any, adv: Any) -> None:
        # Only the manufacturer data matters. We do NOT match on the device name:
        # on Windows the name and the manufacturer data can arrive in separate
        # events (advertisement vs. scan response). An event without our
        # manufacturer data cannot be verified, so it is simply ignored.
        mdata = getattr(adv, "manufacturer_data", None) or {}
        payload = mdata.get(COMPANY_ID)
        if payload is None:
            return
        payload = bytes(payload)
        parsed = parse_adv_payload(payload)
        if parsed is None:
            return  # some other gadget using test company ID 0xFFFF
        counter, mac = parsed
        address = getattr(device, "address", "?")

        serial = self._identify(address, payload, counter, mac)
        if serial is None:
            return  # unknown key or forged mac

        # Replay protection: the counter may repeat (it changes once per second
        # while the tag advertises every 100 ms) but must never go DOWN.
        last = self._last_counter.get(serial)
        if last is not None and counter < last:
            self._warn_backwards(serial, counter, last)
            return
        self._last_counter[serial] = counter
        self._devices[serial] = device

        rssi = getattr(adv, "rssi", None)
        if rssi is None:
            rssi = getattr(device, "rssi", None)
        if rssi is None:
            return
        rssi = int(rssi)
        now = time.time()

        if serial not in self.status["tags"]:
            self.log("verified tag {} (address {}, RSSI {}, counter {})".format(
                serial, address, rssi, counter))
        self.status["tags"][serial] = {
            "last_seen": now, "rssi": rssi, "counter": counter, "address": address}

        sighting = dict(receiver=self.receiver_id, tag_serial=serial, rssi=rssi,
                        t=now, counter=counter)
        try:
            result = self.on_sighting(sighting)
            if inspect.isawaitable(result):  # allow an async on_sighting too
                asyncio.ensure_future(result)
        except Exception as e:
            self._callback_errors += 1
            if self._callback_errors <= 5:
                self.log("on_sighting raised: " + _explain(e))

    def _identify(self, address: str, payload: bytes, counter: int, mac: bytes) -> Optional[str]:
        """Which tag sent this? Uses a tiny cache so that the ~10 identical
        packets per second only cost one HMAC check."""
        cached = self._addr_cache.get(address)
        if cached is not None and cached[0] == payload:
            return cached[1]
        # Try the key that matched this address last time first, then all keys.
        order = list(self.keys.items())
        if cached is not None and cached[1] in self.keys:
            order.sort(key=lambda kv: kv[0] != cached[1])
        for serial, key in order:
            if verify(key, counter, mac):
                self._addr_cache[address] = (payload, serial)
                return serial
        return None

    def _warn_backwards(self, serial: str, counter: int, last: int) -> None:
        now = time.monotonic()
        if now - self._backwards_warned.get(serial, -1e9) < BACKWARDS_WARN_EVERY_S:
            return
        self._backwards_warned[serial] = now
        self.log("ignored {}: counter went backwards ({} < {}). Either a replayed "
                 "recording, or the tag's memory was erased/reflashed - in that case "
                 "restart the server.".format(serial, counter, last))

    # ------------------------------------------------------------------ alerts
    async def set_alert(self, tag_serial: str, mode: int) -> bool:
        """Tell a tag to show `mode` (0 off, 1 WRONG AIRCRAFT, 2 OK, 3 locate).

        Returns True once the tag has the command (and, if its status could be
        read, accepted it). Returns False if it failed after one retry, if the
        tag was never seen, or if a NEWER set_alert for the same tag replaced
        this request before it was sent.
        The actual Bluetooth work runs in a separate background task, so even
        if the caller is cancelled the write still completes. Scanning keeps
        running the whole time.
        """
        if tag_serial not in self.keys:
            self.log("set_alert: unknown tag {!r}".format(tag_serial))
            return False
        if mode not in MODE_NAMES:
            self.log("set_alert: invalid mode {!r} (use 0-3)".format(mode))
            return False
        if self._stopping:
            return False
        loop = asyncio.get_running_loop()
        fut: "asyncio.Future[bool]" = loop.create_future()

        old = self._pending.get(tag_serial)
        if old is not None and not old[1].done():
            self.log("{}: mode {} replaced by newer mode {} before it was sent".format(
                tag_serial, old[0], mode))
            old[1].set_result(False)
        self._pending[tag_serial] = (mode, fut)

        task = self._writers.get(tag_serial)
        if task is None or task.done():
            self._writers[tag_serial] = asyncio.ensure_future(self._writer_loop(tag_serial))
        return await asyncio.shield(fut)

    async def _writer_loop(self, serial: str) -> None:
        """Sends queued modes for one tag, one at a time, newest wins."""
        while not self._stopping:
            item = self._pending.pop(serial, None)
            if item is None:
                return
            mode, fut = item
            ok = False
            try:
                ok = await self._write_with_retry(serial, mode)
            except asyncio.CancelledError:
                if not fut.done():
                    fut.set_result(False)
                raise
            except Exception as e:  # should not happen, but never leave a caller hanging
                self.log("{}: unexpected error while writing: {}".format(serial, _explain(e)))
            if not fut.done():
                fut.set_result(ok)

    async def _write_with_retry(self, serial: str, mode: int) -> bool:
        name = MODE_NAMES[mode]
        t0 = time.monotonic()
        detail = "not tried"
        ok = False
        attempts = 0
        for attempt in (1, 2):
            if attempt == 2:
                if serial in self._pending:
                    detail += " (no retry: a newer mode is waiting)"
                    break
                await asyncio.sleep(RETRY_DELAY_S)
            attempts = attempt
            device = self._devices.get(serial)
            if device is None:
                detail = "tag not seen by this receiver yet"
                continue
            self.log("{}: sending mode {} ({}) [attempt {}]".format(serial, mode, name, attempt))
            try:
                ok, detail = await asyncio.wait_for(
                    self._connect_write(serial, device, mode), ATTEMPT_TIMEOUT_S)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                ok, detail = False, "timed out after {:.0f}s".format(ATTEMPT_TIMEOUT_S)
            except Exception as e:
                ok, detail = False, _explain(e)
            if ok:
                break
            if "wrong key" in detail:
                break  # retrying with the same key will not help
            self.log("{}: mode {} failed: {}".format(serial, mode, detail))

        secs = time.monotonic() - t0
        self.status["last_write"] = {
            "tag": serial, "mode": mode, "mode_name": name, "ok": ok,
            "detail": detail, "attempts": attempts, "seconds": round(secs, 1),
            "t": time.time(),
        }
        if ok:
            self.log("{}: mode {} ({}) {} [{:.1f}s]".format(serial, mode, name, detail, secs))
        else:
            self.log("{}: GAVE UP sending mode {} ({}): {}".format(serial, mode, name, detail))
        return ok

    async def _connect_write(self, serial: str, device: Any, mode: int) -> Tuple[bool, str]:
        """One attempt: connect -> write signed command -> read status -> disconnect."""
        from bleak import BleakClient  # lazy import on purpose

        key = self.keys[serial]
        counter = self._last_counter.get(serial, 0)
        client = BleakClient(device, timeout=CONNECT_TIMEOUT_S)
        await client.connect()
        try:
            await client.write_gatt_char(COMMAND_CHAR_UUID, make_command(key, mode, counter),
                                         response=True)
            st = await self._read_status(client)
            # The tag says our counter was too old (e.g. we had not heard it for
            # a minute)? It also told us its current counter, so sign again with that.
            if (st is not None and st["result"] in (RESULT_STALE_COUNTER, RESULT_OLDER_THAN_LAST)
                    and st["counter"]):
                await client.write_gatt_char(
                    COMMAND_CHAR_UUID, make_command(key, mode, st["counter"]), response=True)
                st = await self._read_status(client)
        finally:
            try:  # always hang up; a held connection hurts scanning on Windows
                await asyncio.wait_for(client.disconnect(), DISCONNECT_TIMEOUT_S)
            except BaseException as e:  # noqa: BLE001
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise
                if isinstance(e, asyncio.CancelledError):
                    raise

        if st is None:
            return True, "written (could not read the tag's answer)"
        if st["result"] == RESULT_OK:
            return True, "accepted by tag"
        reason = RESULT_NAMES.get(st["result"], "code {}".format(st["result"]))
        if st["result"] == RESULT_BAD_MAC:
            reason += " - wrong key"
        return False, "rejected by tag: " + reason

    @staticmethod
    async def _read_status(client: Any) -> Optional[Dict[str, int]]:
        try:
            return parse_status(await client.read_gatt_char(COMMAND_CHAR_UUID))
        except asyncio.CancelledError:
            raise
        except Exception:
            return None
