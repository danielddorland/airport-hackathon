# NEXT SESSION: Room demo handoff (branch `room-demo`)

Written Nov 8 from Alex's Claude Code session. Read this top to bottom on Daniel's laptop.

## Where things stand

| Done (committed on `room-demo`) | Not done yet |
|---|---|
| `autotrack/autotrack/room.py`: combines every laptop's signal, per-laptop RSSI Kalman, RSSI→distance with each laptop's curve, **weighted least squares** trilateration (closest 3 weighted highest), position Kalman, touch checkpoints, square zone, room calibration, simulator | Click through the Room demo tab in a browser |
| `/api/room/*` endpoints in `server.py`; old tabs untouched | ESP32 firmware fixed at exactly 100 ms (currently 90–110 ms, fine for now) |
| `tools/reporter.py`: runs on the other laptops, sends their readings to the main laptop (HTTP) | Reporter runs its **own** Kalman filter + sends over **UDP** (planned pipeline below) |
| "Room demo" tab on the dashboard (map, status, timeline, Calibrate room button, Simulate button) | Real-hardware test with 3–4 laptops |
| Joseph's calibration in `data/laptop_profiles.json` (1 m = −67.7 dBm, n = 2.16) | Calibration for the 4th laptop |
| `tests/test_room.py`: **26/26 passing** (+ test_engine 14/14, test_tagcrypto 12/12) | |
| **Review + fix pass DONE**: 22 issues found and fixed, e.g. no false OUT OF ZONE at a corner (zone range now 3.0 m), trilateration no longer snaps to a mirror point inside the square, tag never left stuck beeping when control passes between room and single-laptop monitor, firewall advice | Page JavaScript only syntax-checked, **not clicked through in a real browser yet** |

## 1. Get the code (PowerShell, on Daniel's laptop)

```powershell
cd <wherever airport-hackathon is cloned>
git fetch
git checkout room-demo
git pull
cd autotrack
py -m venv .venv                                   # skip if .venv already exists
.venv\Scripts\python -m pip install -r requirements.txt
```

## 2. Check it works (no hardware needed)

```powershell
.venv\Scripts\python tests\test_room.py            # expect: 26 passed, 0 failed
.venv\Scripts\python tests\test_engine.py
.venv\Scripts\python run.py                        # browser opens at http://127.0.0.1:8000
```
In the browser: open the **Room demo** tab, then press **Simulate**. Over ~60 s you should see:
ON TRACK → **WRONG AIRCRAFT** (at Joseph's laptop) → **CHECKED IN** (at Alex's) → **OUT OF ZONE** → back.
If that works, the backup demo is ready.

## 3. Real demo with 3–4 laptops

All laptops on the **same Wi-Fi or one phone hotspot**. Laptops in a **2 m square**, lids facing in.
Default roles: Alex (0,0) = correct loader "Loader 5 – AC 1234", Joseph (2,0) = WRONG loader "Loader 4 – AC 857",
Daniel (2,2) and the 4th laptop (0,2) = corners. Drag laptops on the map to change positions; change roles in the laptops table.

**Main laptop** (runs the server and is the only one that beeps the tag):
```powershell
.venv\Scripts\python run.py --mode real --laptop alex --host 0.0.0.0
```
It prints its IP address and the exact command for the others. When Windows Firewall asks, choose **Allow (private networks)**.

**Every other laptop** (replace the IP and name):
```powershell
.venv\Scripts\python tools\reporter.py --server http://192.168.x.x:8000 --laptop joseph
```
No ESP32 handy? Add `--fake` to send made-up readings and test the network path.

Then, in the Room demo tab:
1. Check every laptop shows **online** in the laptops table.
2. Put the tag at the centre crosshair, keep it still, and press **Calibrate room (noise floor)** (8 s).
3. Touch the tag to each checkpoint laptop and press **Set touch** for that row.
4. Run the story: touch Joseph's laptop (beeps), then Alex's laptop (stops), then carry it out of the square (beeps).

If anything breaks during judging, press **Simulate**, or restart with `.venv\Scripts\python run.py`.

To add the 4th laptop's calibration: on that laptop run `run.py --mode real --laptop <name>`, use the **Live ESP32** tab's
calibration (Measure at 0.3 / 1 / 2 / 3 m), then commit `data/laptop_profiles.json`.

## 4. Prompt to paste into the next Claude Code session

Open Claude Code in the `airport-hackathon` folder and paste:

```
Read NEXT_SESSION.md. We are on branch room-demo. Do these in order, run tests after each, commit each step, and push to origin room-demo:

1. QUICK CHECK (a full review + fix was already done): run all tests, start run.py, open the Room demo tab, press Simulate, and confirm WRONG AIRCRAFT -> CHECKED IN -> OUT OF ZONE shows on the page with no browser console errors. Fix anything broken.

2. FIRMWARE: in autotrack/firmware/autotrack_tag/autotrack_tag.ino, advertise every 100 ms (setMinInterval/setMaxInterval 0xA0). Don't change the payload or the HMAC.

3. PIPELINE: ESP32 BLE broadcast -> each laptop's tools/reporter.py runs its OWN Kalman filter on RSSI -> sends a UDP datagram every 0.1-0.25 s to the main laptop (port 8765) as JSON {laptop, seq, rssi_filtered, variance, raw_median, packets, t_age_ms} -> aggregator in room.py keeps the newest filtered value per laptop (online = heard within 3 s) -> central processing (distance per laptop curve, touch/zone/bag logic) -> trilateration engine (WLS) -> position Kalman -> (x, y) on the dashboard. The main laptop's own BLE must go through the same per-laptop Kalman. Keep the HTTP /api/room/sightings path as a fallback (reporter --transport http). Stdlib only, no new dependencies. Add tests for the UDP path (send datagrams to a local socket and check room state).

Medium effort is fine. The team has little coding experience: explain what changed in plain English at the end.
```

## Who's who

`alex`, `daniel`, `joseph` have calibration profiles. Any new name passed to `--laptop` joins automatically using Alex's curve until it is calibrated.
