"""
run.py - start AutoTrack with one command.

    python run.py                 simulation mode (no hardware needed)
    python run.py --mode real     also use the laptop's Bluetooth + the real ESP32 tag
    python run.py --speed 20      start at 20x speed (1 hour of airport = 3 minutes)
    python run.py --no-smoothing  turn RSSI smoothing off (to show the zone flickering)

Then open http://127.0.0.1:8000 (it opens by itself). Press Ctrl+C to stop.
Works offline: everything runs on this laptop.
"""

import argparse
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def main():
    p = argparse.ArgumentParser(description="AutoTrack baggage early-warning prototype")
    p.add_argument("--mode", choices=["sim", "real"], default="sim",
                   help="sim = everything simulated; real = also use the ESP32 tag over Bluetooth")
    p.add_argument("--speed", type=float, default=10.0, help="simulated seconds per real second")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-smoothing", action="store_true", help="turn off RSSI smoothing / zone hysteresis")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--laptop", default=None,
                   help="whose laptop this is (e.g. daniel) - picks that laptop's signal calibration; "
                        "also selectable on the Live ESP32 page, and remembered")
    a = p.parse_args()

    if not (ROOT / "data" / "replay.json").exists():
        print("First run: building the airport replay from the organisers' simulation (takes ~1 min)...")
        from tools.build_replay import build
        build()

    import uvicorn
    from autotrack import server
    server.CONFIG.update(mode=a.mode, speed=a.speed, smoothing=not a.no_smoothing, laptop=a.laptop)

    url = f"http://127.0.0.1:{a.port}"
    print("=" * 60)
    print(f" AutoTrack running in {a.mode.upper()} mode  ->  {url}")
    print(" Press Ctrl+C to stop.")
    print("=" * 60, flush=True)
    if not a.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(server.app, host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
