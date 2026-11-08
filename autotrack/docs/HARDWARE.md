# AutoTrack hardware guide (the "hero bag" tag)

In the demo there is **one real tag** and **one real receiver**:

- **Tag** = the ESP32-S3 board, taped to the "hero bag". It broadcasts a signed,
  changing ID about 10 times per second and can flash/beep when told to.
- **Receiver** = the **laptop's own Bluetooth**. It plays the role of the
  belt loader zone. Everything else (other bags, other zones) is simulated.

When the hero bag arrives at the wrong plane's belt loader, the server raises
**WRONG AIRCRAFT** and the tag flashes red and beeps until it is cleared.

---

## 1. Parts list

| Part | Needed? | Notes |
|---|---|---|
| ESP32-S3 dev board (DevKitC-1 style, onboard RGB LED) | **Yes** (you have 1) | The onboard LED is on GPIO 48 (some boards: GPIO 38) |
| USB-C cable that carries data | **Yes** | Some cheap cables are charge-only. If no COM port appears, try another cable |
| USB power bank | Nice to have | So the tag can ride on the bag without the laptop cable |
| Active buzzer (3.3 V module with pins VCC / GND / I/O, **or** a bare 3-5 V active buzzer) | Optional | Gives the beep |
| NPN transistor (2N2222, BC547, S8050...) + 1 kΩ resistor | Only for a bare buzzer | The module version doesn't need it |
| LED (any colour) + 220 Ω resistor | Optional | A bigger, easier-to-see light |
| Jumper wires / breadboard | Optional | |

**The sketch works with nothing attached** - then you just get the onboard LED.

**Active or passive buzzer?** An *active* buzzer beeps by itself when you
connect it to 3.3 V (it often has a sealed black bottom). A *passive* one only
clicks. If yours only clicks, set `#define BUZZER_PASSIVE 1` at the top of the sketch.

---

## 2. Wiring

Pins used: **GPIO 4** = buzzer, **GPIO 5** = external LED, **GPIO 48** = onboard LED (no wiring).

### Option A - buzzer *module* (3 pins: VCC, GND, I/O)

```
   ESP32-S3                 Buzzer module
  +---------+              +-------------+
  |     3V3 |--------------| VCC         |
  |     GND |--------------| GND         |
  |  GPIO 4 |--------------| I/O (or S)  |
  +---------+              +-------------+
```
If it beeps all the time and goes quiet during alerts, it's a "low level
trigger" module: set `#define BUZZER_ACTIVE_HIGH 0` in the sketch.

### Option B - bare active buzzer (2 legs, + and -) through an NPN transistor

The ESP32 pin can't safely power a buzzer on its own, so a transistor does the switching.

```
                         + 3V3 (3 V buzzer)  or  5V pin (5 V buzzer)
                         |
                      [BUZZER]   longer leg / "+" goes UP to the supply
                         |
                         | collector (C)
  GPIO 4 ---[ 1 kΩ ]---(B)   NPN  (2N2222 / BC547 / S8050)
                         | emitter (E)
                         |
  GND -------------------+
```
Check your transistor's pinout (E-B-C order differs between models - search
"<model> pinout"). The ESP32 GND and the buzzer's GND must be the same wire.

### External LED (optional)

```
  GPIO 5 ---[ 220 Ω ]---|>|--- GND
                       LED
               long leg (+)   short leg / flat side (-)
```
The external LED copies the onboard LED (on whenever the onboard LED is lit).

---

## 3. Put the program on the ESP32 (Arduino IDE 2.x)

1. Install **Arduino IDE 2** from https://www.arduino.cc/en/software
2. **File > Preferences > Additional boards manager URLs**, paste:
   `https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json`
   then OK.
3. **Tools > Board > Boards Manager**, search **esp32**, install
   **"esp32 by Espressif Systems"** (version 3.x is fine; 2.0.4+ also works).
4. **File > Open** `autotrack\firmware\autotrack_tag\autotrack_tag.ino`
   (the folder and the file must keep the same name).
5. Settings in the **Tools** menu:
   - **Board**: esp32 > **ESP32S3 Dev Module**
   - **USB CDC On Boot**: **Enabled** (otherwise you see nothing in the Serial Monitor)
   - **Partition Scheme**: **Huge APP (3MB No OTA/1MB SPIFFS)** (Bluetooth programs are big)
   - leave everything else as it is
6. Plug the board in using the USB-C port labelled **USB** (not "COM"/"UART").
   If your board has only one port, use that one.
7. **Tools > Port**: pick the new **COMx** port (unplug/replug to see which one appears).
8. Click **Upload** (the arrow button). Wait for "Done uploading".
   - If it says *"Failed to connect"* / *"No serial data received"*: hold the
     **BOOT** button, tap **RST** (or RESET/EN), release **BOOT**, click Upload again.
     After uploading, press **RST** once.
9. **Tools > Serial Monitor**, set the speed (bottom right) to **115200 baud**,
   then press **RST** on the board. You should see something like:

```
[boot] AutoTrack tag starting
[boot] boot count 1 -> counter starts at 100000
[selftest] counter 0 adv payload = 0100000000060d2c50a73d7a20  PASS
[selftest] command mode 1 counter 100000 = 01000186a03c1c96a94134cf97  PASS
[selftest] compare with: python tests/test_tagcrypto.py
[ble] advertising as "AT" (no serial number broadcast), counter 100001
[mode] -> 0: normal (heartbeat)
[status] counter 100011, mode 0 (normal (heartbeat))
```
   - Both self-test lines must say **PASS**. The same two hex strings are
     printed by `.venv\Scripts\python tests\test_tagcrypto.py` - that proves
     the tag and the laptop use the same key and the same maths.
   - The boot count goes up by 1 every time you reset or re-plug the board. That's on purpose.
   - The LED gives a short white flash at power-on (and a short beep if a
     buzzer is connected), then a faint green blink every 3 seconds.

What the tag shows:

| Mode | Meaning | LED | Buzzer |
|---|---|---|---|
| 0 | normal | faint green blink every 3 s | quiet |
| 1 | **WRONG AIRCRAFT** | fast bright red flashing, until mode 0 | beep-beep every second, until mode 0 |
| 2 | OK, loading confirmed | two green blinks, then normal | one tiny chirp |
| 3 | locate / identify | blue blinks for ~5 s, then back to what it was doing | short chirp with every blink |

In the Serial Monitor every command shows up as e.g.
`[cmd] mode 1 counter 100230 -> accepted` or `-> REJECTED: bad signature (wrong key?)`.

---

## 4. Check the laptop can hear the tag

From the `autotrack` folder, in PowerShell:

```
.venv\Scripts\python tools\check_ble.py
```
You should see a line every second:
```
16:05:42  TAG-0001  RSSI  -48 dBm  counter 100123  (9 packets)
```
- RSSI is the signal strength: about -40 when the tag is next to the laptop,
  -70 to -90 across the room. It goes up and down a bit; that's normal.
- The counter goes up by one every second.

Test the alert link (the tag should blink blue and chirp for 5 s, then go back to normal):
```
.venv\Scripts\python tools\check_ble.py --beep
```
Stop with **Ctrl+C**. `--seconds 20` stops automatically after 20 s.

Run the tests (no hardware needed):
```
.venv\Scripts\python tests\test_tagcrypto.py
```

---

## 5. Troubleshooting

**The onboard LED never lights up.**
Your board probably has the RGB LED on GPIO 38 instead of 48. In the sketch
change `#define LED_PIN 48` to `#define LED_PIN 38` and upload again.
(Some boards also have a tiny solder jumper marked "RGB" that must be closed.)
Everything else still works without the LED - check the Serial Monitor.

**Serial Monitor shows nothing.**
USB CDC On Boot must be **Enabled**, baud **115200**, and press **RST**.
If you're plugged into the "COM"/"UART" port instead, either switch to the "USB"
port, or set USB CDC On Boot to **Disabled** and upload again.

**"Sketch too big" when uploading.** Tools > Partition Scheme > **Huge APP**.

**check_ble says it can't start scanning / Bluetooth not available.**
Windows Settings > **Bluetooth & devices** > Bluetooth **On**. Turn off airplane
mode. If it still fails, toggle Bluetooth off and on, or restart the laptop.
The server keeps trying every 5 seconds, so you can switch Bluetooth on without restarting it.

**"bleak is not installed".** Run `.venv\Scripts\python -m pip install bleak`
and always start things with `.venv\Scripts\python`, not plain `python`.

**The laptop hears nothing ("no verified tag heard").**
- Is the Serial Monitor showing `[status]` lines every 10 s? If not, the tag isn't running.
- Self-test says FAIL? The key in the sketch doesn't match `data\tag_keys.json`.
- Bring the tag closer (under 2 m) to test.

**The tag is "paired" or connected to a phone or another app.**
Don't pair the tag in Windows Settings - it doesn't need pairing. If it shows
up under Bluetooth & devices, **Remove device**. Close nRF Connect / other
Bluetooth apps on phones nearby; they can hold the connection.

**"Access denied" (or "Unreachable") when sending an alert.**
Usually Windows has old pairing information or another program is using the tag.
1. Windows Settings > Bluetooth & devices: remove any device called "AT".
2. Turn Bluetooth off and on.
3. Press **RST** on the ESP32.
4. Try `check_ble.py --beep` again.
The server retries once by itself; a single failure isn't a problem.

**The alert arrives, but the Serial Monitor says "REJECTED: counter more than 60 away".**
The laptop hadn't heard the tag for over a minute. The software then re-signs
automatically using the tag's own counter. If it keeps happening, restart the server.

**"counter went backwards" in the server log.**
You uploaded with "Erase All Flash" (that resets the boot count), so the tag's
counter is lower than before. Restart the server.

**The laptop goes to sleep / Bluetooth stops after a while.**
Keep the laptop plugged in. Settings > System > Power > Screen and sleep >
"When plugged in, put my device to sleep after" = **Never**. In Device Manager >
Bluetooth > your adapter > Power Management, untick "Allow the computer to turn
off this device to save power" (if that tab exists).

---

## 6. What's real and what's prototype

**Real in the demo:** the tag really broadcasts a changing, signed ID; the
laptop really measures its signal strength and checks the signature; the
WRONG AIRCRAFT alert really travels over Bluetooth to the tag, and the tag
only obeys commands signed with its own key.

**Privacy - honest version:**
- The tag never broadcasts its serial number or anything about the passenger.
  Its ID changes every second, and only a server that has the key can tell which tag it is.
- **But** in this prototype the ESP32 keeps the same Bluetooth *address* all the
  time, so someone with a scanner could still follow "the same gadget" around.
  A production tag would also rotate its Bluetooth address (a "resolvable
  private address") so it can't be followed that way.
- The key is written in the sketch and in `data/tag_keys.json` - fine for a
  hackathon, but a real system would store keys in secure hardware/a key vault.

**The demo shows the electronics; the production tag is a sealed, flat puck:**
- waterproof and dust-proof (**IP67**), no open USB port;
- charged by contact pads or wirelessly between flights;
- a battery inside aviation limits for checked bags: **lithium metal < 0.3 g**
  lithium content, or **Li-ion ≤ 2.7 Wh**;
- tough enough for conveyor belts and baggage handling, and reusable for years.

---

## 7. Room demo: 3-4 laptops in a square

Same single tag, but now **every laptop is a receiver**. Together they estimate
where the tag is in the square and which laptop it is touching.

### Layout (top view, 2 m square)

```
   Prem (0,2) ----------------- Daniel (2,2)
   "Corner D"                   "Corner C"
       |                             |
       |          tag starts         |
       |           here (1,1)        |
       |                             |
   Alex (0,0) ----------------- Joseph (2,0)
   MAIN laptop, runs the server  "Loader 4 - AC 857"
   "Loader 5 - AC 1234"          = WRONG checkpoint
   = CORRECT checkpoint
```

| Laptop | Position | Role | What it runs |
|---|---|---|---|
| **Alex** | (0, 0) | correct checkpoint: Loader 5, serves AC 1234 | `run.py --mode real --laptop alex --host 0.0.0.0` (server + Bluetooth) |
| **Joseph** | (2, 0) | WRONG checkpoint: Loader 4, serves AC 857 | `tools\reporter.py --server http://<alex-ip>:8000 --laptop joseph` |
| **Daniel** | (2, 2) | corner (helps locate the tag) | `tools\reporter.py ... --laptop daniel` |
| **Prem** | (0, 2) | corner (optional 4th laptop) | `tools\reporter.py ... --laptop prem` |

The bag is booked on **AC 1234**. Touching it to Joseph's laptop = WRONG AIRCRAFT
(tag flashes red and beeps). Moving it to Alex's laptop = CHECKED IN (beeping stops,
green blink). Carrying it out of the square = OUT OF ZONE (beeps until it's back).

Positions and roles can be changed on the Room demo page; they are saved in `data/room.json`.
With only 3 laptops leave Prem out: the triangle Alex - Joseph - Daniel still works.

### Physical setup checklist

- **Measure the square**: 2 m between neighbouring laptops (tape on the floor or table corners).
  The distance is what the maths uses, so measure it, don't guess.
- **Same height**: put the laptops at about the height the tag is carried (all on tables,
  or all on the floor). Big height differences add error.
- **Lids facing in**: open each lid and turn the laptop so the screen faces the centre of the
  square. The Bluetooth antenna is usually in the lid, and it hears best through the front.
- **Nothing in between**: no bags, metal or people standing between the tag and the laptops
  while calibrating. Human bodies block 2.4 GHz noticeably (about 5-10 dB).
- **Plugged in, no sleep** (see Troubleshooting above) and Bluetooth **on** for every laptop.
- **Same network**: one Wi-Fi or one phone hotspot for everyone. On the main laptop allow
  Python through **Windows Firewall** on private networks when it asks.
- **Tag**: the ESP32 on the bag, powered from a power bank, antenna side not covered by your hand.

### Order of steps on the day

1. Each laptop calibrated on its own (Live ESP32 page > Measure at 0.3 m, 1 m, 3 m). Joseph's is already
   in `data/laptop_profiles.json`.
2. Start the main laptop, then the reporters. Each reporter should print `server ok` and about
   20-40 packets per 2 s.
3. On the Room demo page every laptop shows **online**.
4. Hold the tag still at the centre (1, 1) and press **Calibrate room** (8 s). This measures each
   laptop's noise and a small correction for "this laptop in this room".
5. Hold the tag against each loader laptop once and press its **touch** button, so "touching" is
   detected from that laptop's real signal.
6. Run the story. Fallback at any time: turn on the simulation on the Room demo page.
