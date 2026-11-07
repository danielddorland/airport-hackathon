# ESP Zone Tracker

Locate an ESP32-S3 inside a room using the Bluetooth of 4 laptops, draw a safe zone on a
website, and get an on-screen alert plus a blinking LED on the ESP when it leaves the zone.

```
  ESP32-S3 "ESP-TAG"  ──BLE adverts──►  Laptop A (central)  ──HTTPS──┐
  (advertises every 50 ms;             Laptop B            ──HTTPS──┤
   LED blinks on alert)                Laptop C            ──HTTPS──┼──► Vercel site ◄── dashboard (browser)
          ▲                            Laptop D            ──HTTPS──┘    (positions, zone, alerts)
          └──── short BLE connect to switch LED ── Laptop A
```

**The laptops never connect to each other.** Each runs the same small Python script that
measures the ESP's signal strength and sends it to the website. The site combines the 4
readings into a position. The `--central` laptop also switches the ESP's LED.

| Folder | What it is |
|---|---|
| `app/`, `components/`, `lib/` | Next.js website + API (deploy to Vercel) |
| `agent/anchor.py` | Script that runs on every laptop |
| `firmware/esp_tag/esp_tag.ino` | Arduino sketch for the ESP32-S3 |
| `tools/simulate.mjs` | Fake laptops + ESP for testing without hardware |

---

## 1. Flash the ESP32-S3

1. Arduino IDE → Boards Manager → install **esp32 by Espressif** (2.0.4+ or 3.x).
2. Board: **ESP32S3 Dev Module**, *USB CDC On Boot*: **Enabled**.
3. Open `firmware/esp_tag/esp_tag.ino`. If the LED doesn't light, change `LED_PIN` from 48 to 38.
4. Upload. You should see a short white flash, then a faint green blink every 3 s.

## 2. Deploy the website to Vercel

1. Push this folder to a GitHub repo, then **vercel.com → Add New → Project** and import it.
2. In the project: **Storage → Create → Upstash for Redis** (free tier) and connect it.
   This adds `KV_REST_API_URL` / `KV_REST_API_TOKEN` automatically.
3. **Settings → Environment Variables**: add `AGENT_KEY` = any secret string.
4. Redeploy. Open `https://<your-app>.vercel.app`.

> Without Redis the site falls back to memory, which works for `npm run dev` but **not** on Vercel.

## 3. Start the 4 laptops

On each laptop (Windows/macOS/Linux, Bluetooth on, Python 3.9+):

```bash
cd agent
pip install -r requirements.txt
```

Then one laptop runs it as central:

```bash
python anchor.py --server https://<your-app>.vercel.app --id laptop-A --central --key <AGENT_KEY>
```

and the others:

```bash
python anchor.py --server https://<your-app>.vercel.app --id laptop-B --key <AGENT_KEY>
```

(`laptop-C`, `laptop-D`, ...). Use the `https://` address. Disable sleep and keep the lids open.
The central laptop briefly blinks the ESP **blue** once at startup to prove the link works.

## 4. Set up the room in the dashboard

1. **Settings**: enter the room's width and depth.
2. **Move laptops**: drag each laptop to its real spot. Corners work best, around the
   height of the ESP. Avoid putting them in a line.
3. **Calibrate**: put the ESP on the crosshair (1 m from the central laptop by default; you can
   move it with *Place calibration spot*), keep it still, and press **Calibrate**. For 12 s the
   site measures how strongly every laptop hears the ESP and works out each laptop's own
   "signal at 1 m" from its known distance to that spot. Laptops' radios differ a lot, so
   this roughly halves the error. Alerts are paused during calibration.
4. **Draw safe zone**: click the corners, then click the first corner (or *Finish*).

Once the ESP is outside the zone for longer than the *Alert delay* (1.5 s), the dashboard flashes red
and beeps (click anywhere on the page once so the browser allows sound). The central laptop then tells
the ESP to blink red. It stops when the ESP comes back in, or when you press *Disarm*.

## Try it without hardware

```bash
npm install
npm run dev
```

In a second terminal:

```bash
node tools/simulate.mjs
```

Open http://localhost:3000, draw a zone in the middle of the room, and watch the simulated ESP
wander in and out. `node tools/simulate.mjs --hold 1.07,0.94` parks it at a spot so you can try
calibration (that's 1 m from the central laptop's default corner, toward the room centre).

## What accuracy to expect

In simulation with realistic noise, 4 calibrated laptops get a median error of about 0.4 m, with 90% of
fixes within 0.8 m. Real rooms are worse because of reflections and people's bodies: plan on **~1 m**.
Draw the safe zone with about 1 m of slack, or raise the alert delay, to avoid false alarms near its edge.
If positions jump too much, try a path-loss *n* between 2.2 and 3.0 and recalibrate.

## Notes and limits

- **Vercel free tier**: 4 laptops × 1 request/s plus the dashboard's polling comes to a few hundred
  thousand requests a day of continuous use. That's fine for demos and sessions of a few hours; stop the
  agents when you're done.
- The dashboard has no login; anyone with the URL can see it and move the zone. Only the laptop
  reports are protected by `AGENT_KEY`.
- The laptop started with `--central` always becomes the central one, even if you pick a different
  radio button on the dashboard.
- If a laptop goes to sleep, restart its agent.
- If the central laptop can't connect to the ESP to switch the LED, make sure no other laptop or
  phone is paired with or connected to it.
