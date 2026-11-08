# Presenting AutoTrack: what to say, what's real, what each file does

## 1. Every file in plain English

**`run.py`**: The one command you run. It builds the airport replay the first time, starts the web server,
and opens the browser. `--mode real` adds the ESP32 tag; `--speed` sets how fast the hour plays.

**`autotrack/layout.py`**: The map. It turns the organisers' 28 conveyor points into AutoTrack zones
(check-in, transfer input, sortation, transport, early bag storage, make-up + loader), puts one BLE
receiver in each, and says which flight each belt loader serves. It also finds shortest routes between zones.

**`autotrack/world.py`**: The "real world" AutoTrack is watching (simulated). It knows where every bag
*really* is each second, adds bag tag numbers, bookings, weights and make-up lane waiting time, and sets up
the 7 demo scenarios (misrouted bag, tag swap, tag falling off...). AutoTrack never reads it directly.

**`autotrack/radio.py`**: Fake BLE receivers (simulated). It turns "the bag is 9 m from this receiver" into a
messy signal strength like a real baggage hall: signal fading, drifting interference, packet collisions,
receivers that differ.

**`autotrack/sim.py`**: Runs the airport one second at a time. It produces the messages real equipment would
send (check-in, BLE sightings, RFID reads, loader portal reads, belt jams, tags removed) and hands them to
the tracker. It also runs the live ESP32 bag.

**`autotrack/positioning.py`**: The real positioning logic. It smooths each receiver's signal, picks the
primary zone (only switching after 2 consistent seconds and a 3 dB lead, so it doesn't flicker), lists
nearby zones, and computes the weighted-centroid position.

**`autotrack/tracker.py`**: The AutoTrack brain. It keeps one record per bag and gives it one of 8 statuses,
each with a plain-English reason. It runs the wrong-plane check at every belt loader, spots tag swaps,
fallen-off tags, silent tags, forgotten tags and late bags, and raises alerts with buttons for a human.

**`autotrack/securebag.py`**: The photo check. It looks up the REAL results we got by running the organisers'
`bag_compare.verify_bags()` on their sample photos. It never makes up a verdict.

**`autotrack/feed.py`**: The shared message feed. Every event becomes a small JSON message, kept for the
dashboards and saved to `data/events.db`. This is the "message-driven operations" part.

**`autotrack/server.py`**: The local web server (FastAPI). It runs the simulation clock, answers the web page,
and talks to the Bluetooth code in real mode.

**`autotrack/real_ble.py`**: Real Bluetooth. It listens through the laptop's Bluetooth for the ESP32 tag,
checks its signed rotating ID, reports signal strength, and sends it signed "flash/beep" commands.

**`autotrack/tagcrypto.py`**: The tag's security maths (HMAC). The tag's ID changes every second and is
signed with a secret key, so nobody can track or clone it, and only our server can make it beep.

**`static/index.html`**: The whole website: ops dashboard with the live map, loader handheld, passenger
view, and the message feed. Plain HTML/JavaScript, no internet needed.

**`firmware/autotrack_tag/autotrack_tag.ino`**: The ESP32 program (Arduino). It broadcasts the signed rotating
ID, blinks green normally, flashes red and beeps on WRONG AIRCRAFT, and only obeys signed commands.

**`bhs/`**: The organisers' baggage-handling simulation, copied without changes. We run it once to get the
real path of all 718 bags.

**`tools/build_replay.py`**: Runs the organisers' simulation with a fixed random seed (so it gives the same
189 late bags every time) and saves every bag's path to `data/replay.json`.

**`tools/evaluate.py`**: Runs the whole hour without the website and measures how well AutoTrack did
(zone accuracy, early LATE warnings, false alarms). Writes `results/metrics.md`.

**`tools/check_ble.py`**: A quick test for the day: can the laptop hear the ESP32, and can it make it beep?

**`securebag_cache/`**: The script that ran the real SecureBag photo comparison (needs PyTorch, so we ran it
ahead of time) and its saved results.

**`tests/`**: Automatic checks that the logic and the tag security work. Run them before the demo.

---

## 2. 60-second architecture script

> "AutoTrack has three layers. **RFID** is identity: a cheap inlay in the paper bag tag, read in tunnels at
> sortation and at a portal on every belt loader, so we know *exactly* which bag is there. **BLE** is
> position between those reads: the agent clips a reusable tag on at bag drop, it broadcasts a rotating,
> signed ID every second, and a receiver in each zone reports signal strength. Our server smooths that and
> says 'this bag is in make-up lane GA4, close to GA3'. We only claim zone-level accuracy, because that's what
> BLE can honestly do. **SecureBag** is the third layer: when something doesn't add up, like RFID and BLE
> disagreeing or the scale weight changing, it compares the check-in photo with the loader camera photo,
> and a human decides.
>
> Everything is **message-driven**: every zone change, RFID read, photo verdict and alert is a small JSON
> message on one shared feed. Our ops dashboard, the loader's handheld and the passenger view all subscribe
> to that feed, and the same feed could go into Brock's SmartBag and the airlines' baggage messages.
>
> The core check is simple: when the loader's RFID portal reads a bag, we know which flight that loader
> serves. If it doesn't match the booking, the belt pauses, the light goes red, the handheld alerts, and
> the tag on the bag flashes and beeps, *before* the bag leaves Toronto."

---

## 3. 2-minute demo script

Before you start: `.venv\Scripts\python run.py --mode real` (or without `--mode real` if the ESP32 misbehaves).
Ops dashboard open, ESP32 powered on (USB power bank or laptop), taped to a small bag, ~3 m from the laptop.

| Time | Do | Say |
|---|---|---|
| 0:00 | Point at the map at 10x | "This is the organisers' baggage simulation: 718 real bag paths over one hour. The bags ride the belts exactly as the simulation moves them; AutoTrack never sees that - it only gets noisy Bluetooth signals and RFID reads. Click a bag: the red outline is the zone AutoTrack *thinks* it is in." |
| 0:15 | Click scenario **1** and the bag | "A normal bag: checked in, followed zone by zone. Red ring = primary zone, dashed = nearby zones. At the loader the RFID read matches the flight, the handler pulls the tag, and that removal *is* the loading confirmation." |
| 0:35 | Click scenario **2**, wait for MISROUTED, click **Pull bag** | "This bag is booked on AC 857 but the sorter sent it to the Calgary lane. We see it in the wrong lane with a plain-English reason before anyone loads it. One click and it's pulled." |
| 0:55 | **Live tag:** click **Check in live bag on AC 1234**, set speed 1x | "Now a real tag. This ESP32 is our tag; the laptop's Bluetooth is the receiver at belt loader GA4, which serves AC 857. This bag is booked on AC 1234." |
| 1:05 | When the bag reaches lane GA4, walk the ESP32 up to the laptop | "As the bag reaches the loader, the RFID portal reads it..." **The tag flashes red and beeps, the loader light goes red, belt paused.** "...wrong aircraft. Ramp crews wear hearing protection, so it's visual everywhere: the tag, the loader light, the handheld." Show the **Loader handheld** tab. |
| 1:25 | Click scenario **4** (tag swap), open the bag | "Bag-tag switching is a real problem: Pearson ramp workers were arrested over it this year. Here the paper tag says one bag but its BLE tag is somewhere else, and the scale weight is 10 kg off. SecureBag compares the photos. On this sample pair, the real model says REVIEW, not flag, so a human decides. When the real bag shows up later with the same tag number, we flag it for security." |
| 1:50 | **Passenger view**: click a demo passenger | "The passenger sees only their own bag, with booking ref plus bag tag number. And every one of these events is a JSON message on one feed (**Message feed** tab), ready for SmartBag." |

If the ESP32 doesn't respond: press **Bring tag to loader GA4 (button fallback)** and say "the tag would beep
here; it's the same message". Don't debug live.

---

## 4. What is simulated vs what is real

| Real (works as built) | Simulated (stand-in for airport equipment) |
|---|---|
| Positioning logic: smoothing, primary + nearby zones, weighted centroid | 718 bag journeys (organisers' BHS simulation, unmodified) |
| All 8 statuses + reasons, wrong-plane check, tag swap / fell off / NOT SEEN / TAG NOT RETURNED / LATE rules | The BLE signals from 28 receivers (realistic noise model) |
| Shared JSON message feed + SQLite log + live stream | RFID reads at tunnels and loader portals (no UHF reader) |
| Ops dashboard, loader handheld, passenger view | Belt-scale weights, flight-to-gate table (made up) |
| SecureBag verdicts: **real outputs** of `verify_bags()` on the organisers' sample photos (run ahead of time) | Which sample photo stands in for which bag (only 3 real pairs exist) |
| ESP32 tag: signed rotating ID, signed beep/flash commands | Bag weights, booking refs, make-up lane waiting times |
| Laptop Bluetooth as the receiver at loader GA4 (real RSSI) | Every other receiver, and the hero bag's trip *to* lane GA4 |
| | The baggage hall layout is simplified (88 m x 66 m), **not YYZ** |

Honest limits to say out loud:
- **Accuracy:** BLE is zone-level (a few metres), not exact. RFID does exact identity.
- **Simulation speed:** belts in the organisers' simulation move bags faster than real belts, which makes positioning *harder* than in reality.
- **SecureBag:** on the tag-swap sample pair (test4) the real model says "review", not "flag". It's decision support; humans decide.
- **Prototype tag:** an ESP32 dev board is not rugged or flat. "The demo shows the electronics; the production tag is a sealed, flat puck."

---

## 5. Three future-work points

1. **Pilot at the riskiest points first:** transfer input, make-up lanes and belt loaders at one YYZ pier.
   Measure real RSSI in the hall and tune the zone thresholds (simulation only gets you so far).
   Reuse any RFID readers YYZ already has (ask Brock).
2. **Plug into real messages:** publish our feed as IATA BSM/BPM-compatible events into Brock SmartBag, and
   consume real flight/gate changes (e.g. gate-assignment messages) so a gate change instantly re-checks every bag
   already heading to the old loader.
3. **Make the tag loop production-ready:** a sealed IP67 puck with contact charging and resolvable private
   Bluetooth addresses (the prototype's fixed MAC can still be tracked), plus a dock or scan step that
   makes it impossible to confirm loading without removing the tag (our open "missed removal" problem).

---

## 6. Results in simulation

See `results/metrics.md` (made by `tools/evaluate.py`).
