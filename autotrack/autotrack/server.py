"""
server.py - the web server (FastAPI). Everything runs on this laptop; no internet needed.

  http://127.0.0.1:8000/            the web page (ops dashboard, loader handheld, passenger view)
  /api/state                        live snapshot the page polls twice a second
  /api/events?after=N               the shared message feed (dashboards subscribe to this)
  /api/stream                       the same feed as a live stream (Server-Sent Events) for other systems
  /api/bag/<id>                     one bag's full record and history
  /api/passenger?booking=..&plate=..  passenger view: only their own bag
  POST /api/control                 play / pause / speed / jump to a scenario / restart
  POST /api/action                  staff buttons: pull, approve, security, recover, ack
  GET  /api/livetag                 the real ESP32: signal, rough distance, too far or not
  POST /api/livetag/threshold       change the "too far" signal level (default -50 dBm)
  POST /api/live/beep               Test beep / Stop buttons for the real ESP32

The simulation clock runs in a background task. In real mode, real_ble.py scans the laptop's
Bluetooth for the ESP32 tag and sends it beep/flash commands.
"""

import asyncio
import json
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .layout import ROOT
from .livetag import LiveTagMonitor
from .sim import LIVE_RECEIVER, Simulation

STATIC = ROOT / "static"
CONFIG = {"mode": "sim", "speed": 10.0, "smoothing": True}

app = FastAPI(title="AutoTrack")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

state = {"sim": None, "playing": True, "speed": 10.0, "busy": None, "loop": None, "ble": None,
         "follow": None}
lock = threading.Lock()


def sim():
    return state["sim"]


# ---------------------------------------------------------------------- startup / clock
@app.on_event("startup")
async def startup():
    state["loop"] = asyncio.get_running_loop()
    state["speed"] = CONFIG["speed"]
    state["sim"] = Simulation(smoothing=CONFIG["smoothing"])
    state["live"] = LiveTagMonitor(log=lambda m: print(m, flush=True))
    if CONFIG["mode"] == "real":
        await start_real_ble()
    asyncio.create_task(clock_loop())
    asyncio.create_task(state["live"].run())
    threading.Thread(target=_build_checkpoints, daemon=True).start()


def _build_checkpoints():
    """Runs the hour once in the background (~20 s) and saves the state every 5 minutes and just
    before each scenario, so jumping is instant. Until it's done, jumps replay from the start."""
    s = sim()
    times = [sc["start"] for sc in s.world.scenarios] + list(range(300, s.world.total_ticks, 300))
    try:
        s.build_checkpoints(times)
        print(f"Instant jumps ready ({len(s.checkpoints)} checkpoints).", flush=True)
    except Exception as e:  # jumps still work, just slower
        print(f"Checkpoints not built ({e!r}); jumps will replay from the start.", flush=True)


async def start_real_ble():
    from . import tagcrypto
    from .real_ble import RealBLE
    keys = tagcrypto.load_keys(ROOT / "data" / "tag_keys.json")
    # The real ESP32 feeds the "stay near the checkpoint" monitor (livetag.py) only.
    # The simulated airport keeps running on simulated tags.
    monitor = state["live"]
    ble = RealBLE(LIVE_RECEIVER, keys, on_sighting=monitor.on_sighting,
                  log=lambda m: print(m, flush=True))
    state["ble"] = ble
    monitor.ble = ble
    await ble.start()


async def clock_loop():
    """Advances the simulation `speed` simulated seconds per real second."""
    owed, last = 0.0, time.monotonic()
    while True:
        await asyncio.sleep(0.05)
        now = time.monotonic()
        dt, last = now - last, now
        s = sim()
        if not state["playing"] or state["busy"] or (s.t >= s.world.total_ticks and not s.live_active()):
            owed = 0.0
            continue
        owed += dt * state["speed"]
        n = min(int(owed), 200)
        if n:
            owed -= n
            await asyncio.to_thread(_steps, n)


def _steps(n):
    with lock:
        s = sim()
        for _ in range(n):
            if s.t >= s.world.total_ticks and not s.live_active():
                break
            s.step()


async def _run_busy(label, fn):
    state["busy"] = label
    try:
        await asyncio.to_thread(_locked, fn)
    finally:
        state["busy"] = None


def _locked(fn):
    with lock:
        fn()


# ---------------------------------------------------------------------- pages
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/layout")
def layout():
    s = sim()
    return {**s.layout.to_json(), "scenarios": s.world.scenarios, "mode": CONFIG["mode"]}


@app.get("/api/state")
def get_state():
    s = sim()
    if state["busy"] and state.get("last_snap"):
        snap = dict(state["last_snap"])
    else:
        with lock:
            snap = s.snapshot()
        state["last_snap"] = snap
        snap = dict(snap)
    snap.update(checkpoints_ready=len(s.checkpoints) > 0, playing=state["playing"], speed=state["speed"], busy=state["busy"], mode=CONFIG["mode"],
                smoothing=s.smoothing, follow=state["follow"], feed_run=s.feed.run_id)
    return JSONResponse(snap)


@app.get("/api/bag/{bag_id}")
def get_bag(bag_id: str):
    s = sim()
    with lock:
        b = s.tracker.bags.get(bag_id)
        if not b:
            raise HTTPException(404, "no such bag")
        d = b.to_json()
        est = s.tracker.estimators.get(b.tag)
        d["signals"] = ([{"zone": s.layout.receivers()[r]["zone"], "rssi": round(v)}
                         for r, (v, _) in sorted(est.smoothed.items(), key=lambda kv: -kv[1][0])] if est else [])
    return d


@app.get("/api/events")
def events(after: int = 0, limit: int = 200):
    return sim().feed.since(after, limit)


@app.get("/api/stream")
async def stream(request: Request):
    """Live feed for other systems (e.g. SmartBag): one JSON message per line, Server-Sent Events."""
    q = asyncio.Queue(maxsize=2000)
    loop = state["loop"]

    def push(ev):
        try:
            loop.call_soon_threadsafe(q.put_nowait, ev)
        except Exception:
            pass

    sim().feed.listeners.append(push)

    async def gen():
        try:
            while not await request.is_disconnected():
                try:
                    ev = await asyncio.wait_for(q.get(), 5)
                    yield f"data: {json.dumps(ev)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            if push in sim().feed.listeners:
                sim().feed.listeners.remove(push)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/passenger")
def passenger(booking: str, plate: str):
    with lock:
        r = sim().tracker.passenger(booking, plate)
    if not r:
        raise HTTPException(404, "No bag found for that booking reference and bag tag number.")
    return r


@app.get("/api/demo_passengers")
def demo_passengers():
    """Booking refs + bag tag numbers of the scenario bags, so the demo can show the passenger view."""
    s = sim()
    out = []
    with lock:
        for sc in s.world.scenarios:
            for bid in [sc["bag"], sc.get("other")]:
                b = s.tracker.bags.get(bid) if bid else None
                if b:
                    out.append({"scenario": sc["title"], "bag": bid, "booking": b.booking_ref, "plate": b.plate})
        if s.live:
            b = s.tracker.bags.get(s.live["id"])
            if b:
                out.append({"scenario": "Live ESP32 bag", "bag": b.id, "booking": b.booking_ref, "plate": b.plate})
    return out


# ---------------------------------------------------------------------- controls
@app.post("/api/control")
async def control(body: dict):
    s = sim()
    cmd = body.get("cmd")
    if state["busy"]:
        return {"ok": False, "busy": state["busy"]}
    if cmd == "play":
        state["playing"] = True
    elif cmd == "pause":
        state["playing"] = False
    elif cmd == "speed":
        state["speed"] = _speed(body.get("value", 10))
    elif cmd == "seek":
        target = int(body.get("value", 0))
        await _run_busy(f"Jumping to {target // 60} min...", lambda: s.seek(target))
    elif cmd == "scenario":
        sc = next((x for x in s.world.scenarios if x["id"] == body.get("value")), None)
        if not sc:
            raise HTTPException(404, "no such scenario")
        state["follow"] = sc.get("other") or sc["bag"]
        await _run_busy(f"Jumping to: {sc['title']}...", lambda: s.seek(sc["start"]))
        state["speed"] = _speed(body.get("speed", 2))
        state["playing"] = True
    elif cmd == "restart":
        await _run_busy("Restarting...", s.reset)
    elif cmd == "follow":
        state["follow"] = body.get("value")
    else:
        raise HTTPException(400, "unknown command")
    return {"ok": True}


def _speed(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        v = 10.0
    return max(0.25, min(120.0, v)) if v == v else 10.0  # v == v is False for NaN


@app.post("/api/action")
def action(body: dict):
    if not body.get("bag") or not body.get("action"):
        raise HTTPException(400, "need 'bag' and 'action'")
    with lock:
        sim().action(body["bag"], body["action"], body.get("who", "ops"))
    return {"ok": True}


# ---------------------------------------------------------------------- live ESP32 tag
@app.get("/api/livetag")
def livetag():
    """Signal strength, rough distance and state of the real ESP32 (see livetag.py)."""
    return state["live"].status()


@app.post("/api/livetag/threshold")
def livetag_threshold(body: dict):
    try:
        state["live"].set_threshold(float(body.get("dbm", -50)))
    except (TypeError, ValueError):
        raise HTTPException(400, "dbm must be a number")
    return {"ok": True}


@app.post("/api/live/beep")
async def live_beep(body: dict):
    """Test beep (mode 3 = blue blinks + chirps) and Stop (mode 0 = normal)."""
    if not state["ble"]:
        return {"ok": False, "error": "Not in real mode (start with: python run.py --mode real)"}
    mode = 0 if int(body.get("mode", 3)) == 0 else 3
    ok = await state["live"].manual(mode)
    return {"ok": ok, "status": state["ble"].status.get("last_write")}
