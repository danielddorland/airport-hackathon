"""
run.py - start AutoTrack with one command.

    python run.py                 simulation mode (no hardware needed)
    python run.py --mode real     also use the laptop's Bluetooth + the real ESP32 tag
    python run.py --speed 20      start at 20x speed (1 hour of airport = 3 minutes)
    python run.py --no-smoothing  turn RSSI smoothing off (to show the zone flickering)
    python run.py --mode real --laptop alex --host 0.0.0.0
                                  Room demo main laptop: other laptops' reporters can connect

Then open http://127.0.0.1:8000 (it opens by itself). Press Ctrl+C to stop.
Works offline: everything runs on this laptop.
"""

import argparse
import socket
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def lan_ips():
    """This laptop's Wi-Fi / network addresses (e.g. 192.168.1.23). Works offline: nothing is sent."""
    ips = []
    try:
        # "Connecting" a UDP socket sends no packet; it just asks Windows which
        # network card it would use, which tells us our own address on it.
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            ips.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return [ip for ip in ips if not ip.startswith("127.") and not ip.startswith("169.254.")]


def main():
    p = argparse.ArgumentParser(description="AutoTrack baggage early-warning prototype")
    p.add_argument("--mode", choices=["sim", "real"], default="sim",
                   help="sim = everything simulated; real = also use the ESP32 tag over Bluetooth")
    p.add_argument("--speed", type=float, default=10.0, help="simulated seconds per real second")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--host", default="127.0.0.1",
                   help="network address to listen on (default 127.0.0.1 = this laptop only). "
                        "Room demo: use 0.0.0.0 so the other laptops' reporters can reach this one")
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
    if a.host == "0.0.0.0":
        ips = lan_ips()
        print(" Room demo: other laptops can reach this one at:")
        for ip in ips or ["<this laptop's IP - run ipconfig>"]:
            print(f"     http://{ip}:{a.port}")
        print(" On each OTHER laptop run (change the name to whose laptop it is):")
        ip = ips[0] if ips else "<this laptop's IP>"
        print(f"     .venv\\Scripts\\python tools\\reporter.py --server http://{ip}:{a.port} --laptop joseph")
        print(" If Windows Firewall asks: tick Private networks and press Allow.")
    print(" Press Ctrl+C to stop.")
    print("=" * 60, flush=True)
    if not a.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(server.app, host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
