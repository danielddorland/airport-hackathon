"""
check_ble.py - day-of check: can THIS laptop hear (and talk to) the AutoTrack tag?
==================================================================================

Run from the autotrack folder (PowerShell or Git Bash):

    .venv\\Scripts\\python tools\\check_ble.py            # just listen
    .venv\\Scripts\\python tools\\check_ble.py --beep     # also: blue blinks + chirps, then normal
    .venv\\Scripts\\python tools\\check_ble.py --seconds 20

What you should see once per second while the tag is powered:
    12:00:01  TAG-0001  RSSI  -52 dBm  counter 300017  (9 packets)
Press Ctrl+C to stop.

It uses exactly the same code as the server (autotrack/real_ble.py), so if this
works, the server's Bluetooth part will work too.
"""

import argparse
import asyncio
import os
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from autotrack.tagcrypto import MODE_LOCATE, MODE_OFF, load_keys  # noqa: E402
from autotrack.real_ble import RealBLE  # noqa: E402

DEFAULT_KEYS = os.path.join(ROOT, "data", "tag_keys.json")


def stamp() -> str:
    return time.strftime("%H:%M:%S")


async def run(args: argparse.Namespace) -> int:
    try:
        keys = load_keys(args.keys)
    except FileNotFoundError:
        print("ERROR: key file not found: {}".format(args.keys))
        return 2
    except (ValueError, OSError) as e:
        print("ERROR: could not read {}: {}".format(args.keys, e))
        return 2
    print("Known tags: {}".format(", ".join(sorted(keys)) or "(none)"))

    # Collect sightings; print a summary once per second.
    batch = {}   # serial -> list of (rssi, counter)

    def on_sighting(s):
        batch.setdefault(s["tag_serial"], []).append((s["rssi"], s["counter"]))

    ble = RealBLE("check_ble", keys, on_sighting, log=lambda m: print(stamp(), m))
    await ble.start()
    if not ble.status["scanning"]:
        print()
        print("Could not start scanning: {}".format(ble.status["error"]))
        print("Checklist:")
        print("  1. Windows Settings > Bluetooth & devices > Bluetooth = On")
        print("  2. Airplane mode off")
        print("  3. bleak installed:  .venv\\Scripts\\python -m pip install bleak")
        await ble.stop()
        return 2

    print("Listening... (Ctrl+C to stop)")
    t_start = time.monotonic()
    last_any = time.monotonic()
    beep_state = "waiting" if args.beep else "off"
    beep_task = None

    async def beep_sequence(serial):
        print(stamp(), "--beep: sending mode 3 (locate) to", serial)
        ok = await ble.set_alert(serial, MODE_LOCATE)
        print(stamp(), "--beep: mode 3", "OK - the tag should blink blue and chirp" if ok
              else "FAILED: {}".format((ble.status["last_write"] or {}).get("detail")))
        await asyncio.sleep(6)
        ok = await ble.set_alert(serial, MODE_OFF)
        print(stamp(), "--beep: mode 0", "OK - back to normal" if ok
              else "FAILED: {}".format((ble.status["last_write"] or {}).get("detail")))

    try:
        while True:
            await asyncio.sleep(1.0)
            now = time.monotonic()
            current, batch = batch, {}
            if current:
                last_any = now
            for serial in sorted(current):
                rows = current[serial]
                rssi = int(statistics.median(r for r, _ in rows))
                counter = max(c for _, c in rows)
                print("{}  {}  RSSI {:4d} dBm  counter {}  ({} packets)".format(
                    stamp(), serial, rssi, counter, len(rows)))
                if beep_state == "waiting":
                    beep_state = "running"
                    beep_task = asyncio.ensure_future(beep_sequence(serial))
            if not current and now - last_any >= 5:
                last_any = now
                print(stamp(), "no verified tag heard for 5 s - is the ESP32 powered and "
                      "running autotrack_tag? (Serial Monitor should show [status] lines)")
            if args.seconds and now - t_start >= args.seconds:
                break
    finally:
        if beep_task is not None and not beep_task.done():
            try:
                await asyncio.wait_for(beep_task, 40)
            except BaseException:
                pass
        await ble.stop()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Check that this laptop hears the AutoTrack tag.")
    p.add_argument("--beep", action="store_true",
                   help="when the tag is heard, send mode 3 (locate) and then mode 0 (normal)")
    p.add_argument("--seconds", type=float, default=0,
                   help="stop after this many seconds (default: run until Ctrl+C)")
    p.add_argument("--keys", default=DEFAULT_KEYS, help="path to tag_keys.json")
    args = p.parse_args()

    try:
        import bleak  # noqa: F401  (only to give a clear message)
    except ImportError:
        print("ERROR: the 'bleak' Bluetooth library is not installed in this Python.")
        print("Fix:   .venv\\Scripts\\python -m pip install bleak")
        print("(Make sure you run this script with .venv\\Scripts\\python, not plain 'python'.)")
        return 2

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
