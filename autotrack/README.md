# AutoTrack - catch the wrong-plane bag before it leaves Toronto Pearson

Daniel Solutions (Alex, Joseph, Daniel, Ocean, Prem) - F26 Airport Innovation Challenge, Mishandled Baggage.

AutoTrack is a hybrid **RFID + BLE** early-warning system with **SecureBag** photo verification.
This prototype runs the **real AutoTrack logic** on top of the organisers' baggage-handling
simulation (718 bags, 1 hour, 28 conveyor points), plus **one real ESP32 tag** that beeps
and flashes when its bag reaches the wrong plane.

It runs **offline on one Windows laptop**. No internet, no cloud, no database server.

---

## 1. Install (once)

Open **PowerShell** in this `autotrack` folder:

```powershell
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

(Python 3.14 or 3.15 both work. Already done on Alex's laptop.)

## 2. Run

```powershell
.venv\Scripts\python run.py
```

The browser opens at **http://127.0.0.1:8000**. If it doesn't, open that address yourself.
Press **Ctrl+C** in PowerShell to stop.

| Command | What it does |
|---|---|
| `.venv\Scripts\python run.py` | Simulation mode. No hardware needed. **Use this if anything goes wrong.** |
| `.venv\Scripts\python run.py --mode real` | Same, plus the laptop's Bluetooth listens for the ESP32 tag (see `docs/HARDWARE.md`) |
| `.venv\Scripts\python run.py --speed 20` | Start at 20x (the hour takes 3 minutes) |
| `.venv\Scripts\python run.py --no-smoothing` | Turn RSSI smoothing off, to show why it matters (zones flicker) |
| `.venv\Scripts\python tools\evaluate.py` | Re-measure the "results in simulation" numbers (writes `results/metrics.md`) |
| `.venv\Scripts\python tests\test_engine.py` | Run the logic tests |
| `.venv\Scripts\python tests\test_tagcrypto.py` | Run the tag-security tests |
| `.venv\Scripts\python tools\check_ble.py` | Day-of check: can the laptop hear the ESP32? (`--beep` makes it beep) |

## 3. What you should see

- **Header:** the airport clock (the simulated hour runs 14:00-15:00), the phase (warm up -> crisis peak -> wind down),
  and SIMULATION or REAL BLE + SIM. Pause/Play, speed, a timeline slider to jump, and Restart.
- **Ops dashboard:**
  - **Map:** the baggage hall with 28 zones (one BLE receiver each). Coloured dots are where AutoTrack *thinks* each bag is
    (the weighted-centroid estimate). Red dashed belts are jammed. Little lamps next to each belt loader turn red or amber when a bag is stopped.
    Click a bag: a red ring shows its **primary zone**, and dashed rings show its **nearby zones**.
  - **Status tiles:** how many bags are in each of the 8 statuses. Click one to filter the table.
  - **Active alerts:** each with a plain-English reason and the buttons a human uses to decide (Pull bag, Approve, Hold for security, Tag recovered).
  - **Demo scenarios:** 7 buttons. Each jumps to just before the event and slows to 2x.
  - **Live ESP32 tag** panel and the **BLE tag fleet** (tags on bags, in return bins, charging, ready).
  - **Bag drawer** (click any bag): status, reason, primary + nearby zones, signal strength per receiver, SecureBag photos and verdict, full history.
- **Loader handheld:** what the crew at one belt loader sees. A big green/amber/red light, BELT PAUSED, and alerts for their belt only.
- **Passenger view:** enter a booking ref + bag tag number to see only your own bag. "Demo passengers" fills them in for you.
- **Message feed:** every event as a JSON message. That's the "message-driven operations" theme: our dashboards subscribe to it, and so could SmartBag or airline BSM/BPM.

## 4. Folder structure

```
autotrack/
  run.py                    one command to start everything
  requirements.txt
  autotrack/                the AutoTrack code
    layout.py               the map: zones, receivers, belts, flights
    world.py                ground truth: where every bag really is + the 7 demo scenarios   [SIMULATED]
    radio.py                fake BLE receivers with realistic noise                          [SIMULATED]
    sim.py                  runs the airport second by second and feeds the tracker          [SIMULATED inputs]
    positioning.py          primary zone + nearby zones + weighted centroid                   [REAL logic]
    tracker.py              statuses, wrong-plane check, anomaly checks, alerts               [REAL logic]
    securebag.py            looks up the cached REAL SecureBag verdicts                        [REAL results]
    feed.py                 the shared JSON message feed (+ SQLite log)                         [REAL]
    server.py               web server (FastAPI)                                                [REAL]
    real_ble.py             laptop Bluetooth: hears the ESP32, sends it beep/flash commands     [REAL]
    tagcrypto.py            rotating signed tag IDs (HMAC) and signed commands                  [REAL]
  static/index.html         the web page (all three views)
  static/bags/              SecureBag sample photos (from the organisers' repo)
  firmware/autotrack_tag/   ESP32 Arduino sketch (the tag)
  bhs/                      the organisers' baggage simulation, copied UNMODIFIED
  data/replay.json          what happened to all 718 bags (built by tools/build_replay.py)
  data/tag_keys.json        secret key of the demo tag (prototype only)
  data/events.db            every feed message (SQLite; created when you run)
  securebag_cache/          real bag_compare.verify_bags() outputs + the script that made them
  tools/                    build_replay.py, evaluate.py, check_ble.py
  tests/                    test_engine.py, test_tagcrypto.py
  results/                  metrics.md / metrics.json ("results in simulation")
  docs/HARDWARE.md          ESP32 wiring + Arduino IDE steps
  docs/PRESENTING.md        file explanations, scripts, simulated vs real, future work
```

## 5. If something breaks during judging

1. Stop with Ctrl+C. Run `.venv\Scripts\python run.py` (simulation mode needs no hardware).
2. If the ESP32 won't connect, use the **"Bring tag to loader GA4"** button. When the laptop can't hear the ESP32, the live bag
   uses a simulated signal, so the wrong-plane alert still fires (only the physical beep is missing). If Bluetooth itself is broken,
   restart in simulation mode.
3. If the page looks stuck, press **Restart** in the header, or refresh the browser.
4. If port 8000 is busy: `.venv\Scripts\python run.py --port 8001`.
