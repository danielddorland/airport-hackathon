/*
 * autotrack_tag.ino - the AutoTrack bag tag (ESP32-S3)
 * ====================================================
 *
 * WHAT IT DOES
 *   1. Broadcasts a Bluetooth advertisement every ~100 ms so the laptop can
 *      measure how strong the signal is (RSSI). The advertisement contains a
 *      COUNTER that goes up once per second and an 8-byte signature (HMAC) made
 *      with this tag's secret KEY. The tag's serial number ("TAG-0001") is never
 *      broadcast - the server works out which tag it is by checking the
 *      signature against every key it knows.
 *   2. Accepts SIGNED commands over Bluetooth (GATT write) to show an alert:
 *        mode 0 = normal   : faint green blink every 3 s ("heartbeat")
 *        mode 1 = WRONG AIRCRAFT : fast red flashing + beeping, until mode 0
 *        mode 2 = OK       : two green blinks ("loading confirmed"), then normal
 *        mode 3 = LOCATE   : blue blinks + short chirps for ~5 s, then back
 *      A command without the right signature, or with a counter more than 60
 *      away from the tag's own counter, is ignored.
 *   3. Prints what it is doing on the Serial Monitor (115200 baud).
 *
 * HARDWARE
 *   ESP32-S3 dev board with the onboard RGB LED on GPIO 48 (some clones: 38).
 *   Optional: active buzzer on GPIO 4, extra LED (+220 ohm resistor) on GPIO 5.
 *   Works with NOTHING attached (onboard LED only). See docs/HARDWARE.md.
 *
 * ARDUINO IDE SETTINGS
 *   Board "ESP32S3 Dev Module", Tools > USB CDC On Boot = Enabled,
 *   Tools > Partition Scheme = "Huge APP (3MB No OTA/1MB SPIFFS)".
 *   Uses only libraries that come with the ESP32 board package (core 2.0.4+ or 3.x).
 *
 * MESSAGE FORMATS (must match autotrack/tagcrypto.py)
 *   Advertisement, manufacturer data, company ID 0xFFFF (test/unassigned):
 *     [0x01][counter: 4 bytes big-endian][first 8 bytes of HMAC-SHA256(key, "AT1" + counter)]
 *   Command written to the characteristic (13 bytes):
 *     [mode][counter: 4 bytes big-endian][first 8 bytes of HMAC-SHA256(key, "CMD" + mode + counter)]
 *   Reading the characteristic returns a 6-byte status:
 *     [mode][result of last command][current counter: 4 bytes big-endian]
 *
 * THE COUNTER (why it never repeats, even after unplugging)
 *   counter = boot_count * 100000 + seconds_since_boot
 *   boot_count is stored in flash (Preferences/NVS) and goes up by 1 on every
 *   power-on. So after a reboot the counter always jumps above any value used
 *   before. If the tag runs longer than 99000 s (~27.5 h) we bump boot_count
 *   again, so the counter still only ever goes up.
 *   (If you "Erase All Flash" when uploading, boot_count restarts - then just
 *   restart the server too, because it remembers the last counter it saw.)
 *
 * PRIVACY NOTE (honest): this prototype still uses the ESP32's fixed Bluetooth
 *   address, so somebody could follow the tag by its address. A production tag
 *   would use a rotating "resolvable private address" as well.
 */

#include <Arduino.h>
#include <esp_arduino_version.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLEAdvertising.h>
#include <esp_bt.h>
#include <esp_gap_ble_api.h>
#include <Preferences.h>
#include "mbedtls/md.h"

// ============================================================ settings you may change
#define LED_PIN          48   // Onboard RGB LED. If it stays dark, try 38 (some boards).
#define BUZZER_PIN        4   // Optional active buzzer (nothing attached = fine).
#define EXT_LED_PIN       5   // Optional external LED via 220 ohm resistor.
#define BUZZER_ACTIVE_HIGH 1  // 1 = buzzer beeps when pin is HIGH (most modules/transistor).
                              // 0 = "low level trigger" module (beeps when LOW).
#define BUZZER_PASSIVE    0   // 1 if you only have a PASSIVE buzzer (it just clicks
                              // with the normal setting); then we play a 2.7 kHz tone.
#define LED_BRIGHTNESS   40   // 0-255, normal blinks (kept low so it is not blinding)
#define ALERT_BRIGHTNESS 160  // 0-255, WRONG AIRCRAFT flashing (bright on purpose)

// The tag's secret key for "TAG-0001". The SAME value is in data/tag_keys.json
// as 807caa672f37e3f1106012fc70a608d8. Never print or broadcast it.
static const uint8_t TAG_KEY[16] = {
  0x80, 0x7C, 0xAA, 0x67, 0x2F, 0x37, 0xE3, 0xF1,
  0x10, 0x60, 0x12, 0xFC, 0x70, 0xA6, 0x08, 0xD8
};

// ============================================================ fixed protocol values
#define DEVICE_NAME  "AT"   // short and generic: no serial number in the name
#define SERVICE_UUID "6b570001-7d42-4147-a56d-8061e50f3042"
#define CMD_UUID     "6b570002-7d42-4147-a56d-8061e50f3042"

static const uint16_t COMPANY_ID     = 0xFFFF;  // "test / unassigned"
static const uint8_t  PAYLOAD_VER    = 0x01;
static const uint32_t COUNTER_WINDOW = 60;      // accepted command age, in counter steps (s)
static const uint32_t EPOCH_SPAN     = 100000;  // counter = boot_count * EPOCH_SPAN + seconds
static const uint32_t EPOCH_BUMP_S   = 99000;   // start a new "boot" before seconds reach 100000

enum : uint8_t { MODE_OFF = 0, MODE_WRONG = 1, MODE_OK = 2, MODE_LOCATE = 3 };
enum : uint8_t {
  RES_OK = 0, RES_BAD_LENGTH = 1, RES_BAD_MAC = 2, RES_STALE = 3,
  RES_BAD_MODE = 4, RES_OLDER = 5, RES_NONE = 0xFF
};

// Self-test ("known answer") vectors. tests/test_tagcrypto.py has the same values.
static const uint8_t KAT_ADV0[13] = {   // advertisement payload for counter 0
  0x01, 0x00, 0x00, 0x00, 0x00, 0x06, 0x0D, 0x2C, 0x50, 0xA7, 0x3D, 0x7A, 0x20 };
static const uint8_t KAT_CMD[13] = {    // command: mode 1, counter 100000
  0x01, 0x00, 0x01, 0x86, 0xA0, 0x3C, 0x1C, 0x96, 0xA9, 0x41, 0x34, 0xCF, 0x97 };

// Pattern timings (milliseconds)
static const uint32_t HEARTBEAT_PERIOD_MS = 3000;
static const uint32_t HEARTBEAT_ON_MS     = 30;
static const uint32_t WRONG_FLASH_MS      = 100;   // red on 100 ms, off 100 ms (5 Hz)
static const uint32_t LOCATE_TIME_MS      = 5000;
static const uint32_t LOCATE_PERIOD_MS    = 500;

// ============================================================ global state
static BLECharacteristic *cmdChar = nullptr;
static BLEAdvertising    *advertising = nullptr;

static uint32_t bootCount = 0;
static volatile uint32_t epochBase = 0;      // bootCount * EPOCH_SPAN
static volatile uint32_t epochStartMs = 0;   // millis() when this epoch started
static uint32_t lastAdvCounter = 0xFFFFFFFF;

static volatile int16_t  pendingCmd = -1;    // set by the Bluetooth task, used in loop()
static volatile uint8_t  lastResult = RES_NONE;
static uint32_t lastCmdCounter = 0;          // counter of the last accepted command
static volatile uint8_t  mode = MODE_OFF;    // what the tag is showing now
static uint8_t  baseMode = MODE_OFF;         // 0 or 1: where we return after OK/LOCATE
static uint32_t modeStartMs = 0;
static uint32_t lastStatusPrintMs = 0;

// ============================================================ small helpers
static uint32_t currentCounter() {
  return epochBase + (millis() - epochStartMs) / 1000;
}

static void putBE32(uint8_t *p, uint32_t v) {
  p[0] = v >> 24; p[1] = v >> 16; p[2] = v >> 8; p[3] = v;
}

static uint32_t getBE32(const uint8_t *p) {
  return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | p[3];
}

// First 8 bytes of HMAC-SHA256(TAG_KEY, msg).
static void hmac8(const uint8_t *msg, size_t len, uint8_t out[8]) {
  uint8_t full[32];
  const mbedtls_md_info_t *info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  mbedtls_md_hmac(info, TAG_KEY, sizeof(TAG_KEY), msg, len, full);
  memcpy(out, full, 8);
}

// Compare without stopping at the first difference (does not leak timing).
static bool sameBytes(const uint8_t *a, const uint8_t *b, size_t n) {
  uint8_t diff = 0;
  for (size_t i = 0; i < n; i++) diff |= a[i] ^ b[i];
  return diff == 0;
}

// payload = [0x01][counter BE][mac 8]
static void makeAdvPayload(uint32_t counter, uint8_t out[13]) {
  uint8_t msg[7] = {'A', 'T', '1'};
  putBE32(msg + 3, counter);
  out[0] = PAYLOAD_VER;
  putBE32(out + 1, counter);
  hmac8(msg, sizeof(msg), out + 5);
}

// command mac = HMAC(key, "CMD" + mode + counter BE)
static void makeCmdMac(uint8_t m, uint32_t counter, uint8_t out[8]) {
  uint8_t msg[8] = {'C', 'M', 'D', m};
  putBE32(msg + 4, counter);
  hmac8(msg, sizeof(msg), out);
}

static void printHex(const uint8_t *p, size_t n) {
  for (size_t i = 0; i < n; i++) Serial.printf("%02x", p[i]);
}

static const char *modeName(uint8_t m) {
  switch (m) {
    case MODE_OFF:    return "normal (heartbeat)";
    case MODE_WRONG:  return "WRONG AIRCRAFT";
    case MODE_OK:     return "OK / loading confirmed";
    case MODE_LOCATE: return "locate";
    default:          return "?";
  }
}

static const char *resultName(uint8_t r) {
  switch (r) {
    case RES_OK:         return "accepted";
    case RES_BAD_LENGTH: return "REJECTED: wrong length (need 13 bytes)";
    case RES_BAD_MAC:    return "REJECTED: bad signature (wrong key?)";
    case RES_STALE:      return "REJECTED: counter more than 60 away from ours";
    case RES_BAD_MODE:   return "REJECTED: unknown mode";
    case RES_OLDER:      return "REJECTED: older than the last accepted command";
    default:             return "?";
  }
}

// ============================================================ outputs (LED + buzzer)
static void ledWrite(uint8_t r, uint8_t g, uint8_t b) {
  static int32_t last = -1;  // only talk to the LED when the colour changes
  int32_t packed = ((int32_t)r << 16) | ((int32_t)g << 8) | b;
  if (packed == last) return;
  last = packed;
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  rgbLedWrite(LED_PIN, r, g, b);
#else
  neopixelWrite(LED_PIN, r, g, b);
#endif
  digitalWrite(EXT_LED_PIN, (r | g | b) ? HIGH : LOW);  // external LED mirrors it
}

static void buzzer(bool on) {
  static int8_t last = -1;
  if ((int8_t)on == last) return;
  last = on;
#if BUZZER_PASSIVE
  if (on) tone(BUZZER_PIN, 2700); else noTone(BUZZER_PIN);
#else
  digitalWrite(BUZZER_PIN, (on == (BUZZER_ACTIVE_HIGH != 0)) ? HIGH : LOW);
#endif
}

// ============================================================ advertising
// Builds the advertisement for `counter` and hands it to the Bluetooth stack.
// Packet: flags (3 bytes) + manufacturer data (17) + name "AT" (4) = 24 of 31 bytes.
static void updateAdvertisement(uint32_t counter) {
  uint8_t payload[13];
  makeAdvPayload(counter, payload);

  // Manufacturer data = company ID (little-endian, as Bluetooth requires) + payload.
  uint8_t mfg[15];
  mfg[0] = COMPANY_ID & 0xFF;
  mfg[1] = COMPANY_ID >> 8;
  memcpy(mfg + 2, payload, sizeof(payload));

  BLEAdvertisementData advData;
  advData.setFlags(ESP_BLE_ADV_FLAG_GEN_DISC | ESP_BLE_ADV_FLAG_BREDR_NOT_SPT);
  // The data contains zero bytes, so we must pass the length explicitly.
  // Core 3.x uses Arduino String here, core 2.x uses std::string.
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  advData.setManufacturerData(String((const char *)mfg, sizeof(mfg)));
#else
  advData.setManufacturerData(std::string((const char *)mfg, sizeof(mfg)));
#endif
  advData.setName(DEVICE_NAME);
  advertising->setAdvertisementData(advData);  // takes effect while advertising

  // Safety net: if this library version cut our binary data short at a zero
  // byte, send the exact raw bytes to the Bluetooth stack ourselves.
  if (advData.getPayload().length() != 24) {
    static bool warned = false;
    if (!warned) {
      warned = true;
      Serial.printf("[ble] note: library built %u-byte advertisement, using raw bytes instead\n",
                    (unsigned)advData.getPayload().length());
    }
    uint8_t raw[24] = {
      2, 0x01, 0x06,               // flags: general discoverable, no classic BT
      16, 0xFF                     // manufacturer data: length 16, type 0xFF
    };
    memcpy(raw + 5, mfg, sizeof(mfg));                       // bytes 5..19
    raw[20] = 3; raw[21] = 0x09; raw[22] = 'A'; raw[23] = 'T';  // complete name "AT"
    esp_ble_gap_config_adv_data_raw(raw, sizeof(raw));
  }
}

// ============================================================ command handling
// Runs inside the Bluetooth task when the laptop writes a command.
static uint8_t checkCommand(const uint8_t *d, size_t n) {
  if (n != 13) return RES_BAD_LENGTH;
  uint8_t  m = d[0];
  uint32_t k = getBE32(d + 1);
  uint8_t  expect[8];
  makeCmdMac(m, k, expect);
  if (!sameBytes(expect, d + 5, 8)) return RES_BAD_MAC;
  if (m > MODE_LOCATE) return RES_BAD_MODE;
  uint32_t c = currentCounter();
  uint32_t diff = (c > k) ? (c - k) : (k - c);
  if (diff > COUNTER_WINDOW) return RES_STALE;
  if (k < lastCmdCounter) return RES_OLDER;   // stops replaying an older command
  lastCmdCounter = k;
  pendingCmd = m;
  return RES_OK;
}

// Puts the 6-byte status into the characteristic so the laptop can read it.
static void publishStatus() {
  uint8_t st[6];
  int16_t p = pendingCmd;
  st[0] = (p >= 0) ? (uint8_t)p : mode;
  st[1] = lastResult;
  putBE32(st + 2, currentCounter());
  cmdChar->setValue(st, sizeof(st));
}

class ServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer *server) override {
    Serial.println("[ble] laptop connected");
    BLEDevice::startAdvertising();  // keep advertising so RSSI keeps flowing
  }
  void onDisconnect(BLEServer *server) override {
    Serial.println("[ble] laptop disconnected");
    BLEDevice::startAdvertising();
  }
};

class CmdCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic *c) override {
    // getData()/getLength() have the same signature on core 2.x and 3.x.
    size_t n = c->getLength();
    uint8_t buf[16] = {0};
    if (n <= sizeof(buf)) memcpy(buf, c->getData(), n);
    uint8_t res = (n <= sizeof(buf)) ? checkCommand(buf, n) : RES_BAD_LENGTH;
    lastResult = res;
    if (n == 13) {
      Serial.printf("[cmd] mode %u counter %lu -> %s\n", buf[0],
                    (unsigned long)getBE32(buf + 1), resultName(res));
    } else {
      Serial.printf("[cmd] %u bytes -> %s\n", (unsigned)n, resultName(res));
    }
    publishStatus();  // replace the written bytes with our status
  }
  void onRead(BLECharacteristic *c) override {
    publishStatus();  // fresh counter for every read
  }
};

// ============================================================ modes
static void setMode(uint8_t m) {
  mode = m;
  modeStartMs = millis();
  Serial.printf("[mode] -> %u: %s\n", m, modeName(m));
}

static void applyCommand(uint8_t m) {
  if (m == MODE_OFF || m == MODE_WRONG) baseMode = m;  // these two "stick"
  if (m == MODE_OK) baseMode = MODE_OFF;               // confirmed -> back to normal
  setMode(m);                                          // newest command wins at once
}

// ============================================================ self-test
static void selfTest() {
  uint8_t adv[13];
  makeAdvPayload(0, adv);
  bool advOk = sameBytes(adv, KAT_ADV0, 13);
  Serial.print("[selftest] counter 0 adv payload = ");
  printHex(adv, 13);
  Serial.println(advOk ? "  PASS" : "  FAIL (key differs from tag_keys.json?)");

  uint8_t cmd[13];
  cmd[0] = 1;
  putBE32(cmd + 1, 100000);
  makeCmdMac(1, 100000, cmd + 5);
  bool cmdOk = sameBytes(cmd, KAT_CMD, 13);
  Serial.print("[selftest] command mode 1 counter 100000 = ");
  printHex(cmd, 13);
  Serial.println(cmdOk ? "  PASS" : "  FAIL");
  Serial.println("[selftest] compare with: python tests/test_tagcrypto.py");
}

// ============================================================ setup
void setup() {
  Serial.begin(115200);
  delay(1500);  // give the USB serial port a moment so you see the first lines

  pinMode(EXT_LED_PIN, OUTPUT);
  digitalWrite(EXT_LED_PIN, LOW);
#if !BUZZER_PASSIVE
  pinMode(BUZZER_PIN, OUTPUT);
#endif
  buzzer(false);

  ledWrite(LED_BRIGHTNESS, LED_BRIGHTNESS, LED_BRIGHTNESS);  // boot: short white flash
  buzzer(true);
  delay(80);
  buzzer(false);
  delay(70);
  ledWrite(0, 0, 0);

  Serial.println("\n[boot] AutoTrack tag starting");

  // ---- boot counter in flash, so the counter never repeats after a reboot
  Preferences prefs;
  prefs.begin("autotrack", false);
  bootCount = prefs.getUInt("boots", 0) + 1;
  prefs.putUInt("boots", bootCount);
  prefs.end();
  epochBase = bootCount * EPOCH_SPAN;
  epochStartMs = millis();
  Serial.printf("[boot] boot count %lu -> counter starts at %lu\n",
                (unsigned long)bootCount, (unsigned long)epochBase);
  if (bootCount >= 42000) Serial.println("[boot] WARNING: boot counter nearly full");

  selfTest();

  // ---- Bluetooth
  BLEDevice::init(DEVICE_NAME);
  esp_ble_tx_power_set(ESP_BLE_PWR_TYPE_DEFAULT, ESP_PWR_LVL_P9);
  esp_ble_tx_power_set(ESP_BLE_PWR_TYPE_ADV, ESP_PWR_LVL_P9);

  BLEServer *server = BLEDevice::createServer();
  server->setCallbacks(new ServerCallbacks());
  BLEService *service = server->createService(SERVICE_UUID);
  cmdChar = service->createCharacteristic(
      CMD_UUID, BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_WRITE);
  cmdChar->setCallbacks(new CmdCallbacks());
  publishStatus();
  service->start();

  advertising = BLEDevice::getAdvertising();
  BLEAdvertisementData scanResp;      // answer to "active" scans: just the name
  scanResp.setName(DEVICE_NAME);
  advertising->setScanResponseData(scanResp);
  advertising->setMinInterval(0x90);  // 0x90 * 0.625 ms = 90 ms
  advertising->setMaxInterval(0xB0);  // 0xB0 * 0.625 ms = 110 ms  (about 10 per second)

  lastAdvCounter = currentCounter();
  updateAdvertisement(lastAdvCounter);
  BLEDevice::startAdvertising();
  Serial.printf("[ble] advertising as \"%s\" (no serial number broadcast), counter %lu\n",
                DEVICE_NAME, (unsigned long)lastAdvCounter);
  setMode(MODE_OFF);
}

// ============================================================ loop
void loop() {
  uint32_t now = millis();

  // ---- after ~27.5 hours start a new "boot" so seconds never reach 100000
  if ((now - epochStartMs) / 1000 >= EPOCH_BUMP_S) {
    Preferences prefs;
    prefs.begin("autotrack", false);
    bootCount = prefs.getUInt("boots", bootCount) + 1;
    prefs.putUInt("boots", bootCount);
    prefs.end();
    epochStartMs = now;
    epochBase = bootCount * EPOCH_SPAN;
    Serial.printf("[boot] long run: new epoch %lu\n", (unsigned long)bootCount);
  }

  // ---- once per second: new counter -> new signed advertisement
  uint32_t counter = currentCounter();
  if (counter != lastAdvCounter) {
    lastAdvCounter = counter;
    updateAdvertisement(counter);
  }

  // ---- every 10 s: a short status line
  if (now - lastStatusPrintMs >= 10000) {
    lastStatusPrintMs = now;
    Serial.printf("[status] counter %lu, mode %u (%s)\n",
                  (unsigned long)counter, mode, modeName(mode));
  }

  // ---- apply a command the laptop wrote (checked already in the Bluetooth task)
  int16_t cmd = pendingCmd;
  if (cmd >= 0) {
    pendingCmd = -1;
    applyCommand((uint8_t)cmd);
  }

  // ---- show the current mode on LED + buzzer
  uint32_t t = now - modeStartMs;
  switch (mode) {
    case MODE_WRONG: {
      bool on = (t % (2 * WRONG_FLASH_MS)) < WRONG_FLASH_MS;
      ledWrite(on ? ALERT_BRIGHTNESS : 0, 0, 0);
      uint32_t b = t % 1000;  // "beep-beep ... beep-beep" once per second
      buzzer(b < 120 || (b >= 240 && b < 360));
      break;
    }
    case MODE_OK: {
      // green 200 ms, off 200 ms, green 200 ms, then back to normal
      bool on = (t < 200) || (t >= 400 && t < 600);
      ledWrite(0, on ? LED_BRIGHTNESS : 0, 0);
      buzzer(t < 60);  // one tiny confirmation chirp
      if (t >= 800) setMode(baseMode);
      break;
    }
    case MODE_LOCATE: {
      uint32_t p = t % LOCATE_PERIOD_MS;
      ledWrite(0, 0, p < LOCATE_PERIOD_MS / 2 ? LED_BRIGHTNESS : 0);
      buzzer(p < 40);  // short chirp with every blue blink
      if (t >= LOCATE_TIME_MS) setMode(baseMode);
      break;
    }
    default: {  // normal: faint green blink every 3 s
      bool on = (now % HEARTBEAT_PERIOD_MS) < HEARTBEAT_ON_MS;
      ledWrite(0, on ? LED_BRIGHTNESS / 5 : 0, 0);
      buzzer(false);
      break;
    }
  }
  delay(5);
}
