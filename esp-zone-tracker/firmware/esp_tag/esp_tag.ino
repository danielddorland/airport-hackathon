/*
 * esp_tag.ino - BLE "tag" of the ESP Zone Tracker
 * ------------------------------------------------
 * Hardware : ESP32-S3-N16R8 (DevKitC-1 style) with onboard addressable RGB LED.
 * Arduino  : Board "ESP32S3 Dev Module". Set Tools > "USB CDC On Boot" = Enabled
 *            so Serial (115200) appears on the native USB port.
 *            Uses only the built-in BLE library (core 2.0.x and 3.x).
 *
 * The tag advertises continuously (~50-60 ms, +9 dBm) so laptops can measure its
 * RSSI. A central laptop briefly connects and writes the LED characteristic:
 *
 *   Device name : "ESP-TAG"  (in the primary advertising packet)
 *   Service     : 7b1e0001-3c4d-4e8a-9f21-5a6b7c8d9e01  (advertised)
 *   LED char    : 7b1e0002-3c4d-4e8a-9f21-5a6b7c8d9e01  (READ | WRITE | WRITE_NR)
 *     0x00 = safe     : LED off, brief dim green heartbeat every 3 s
 *     0x01 = alert    : fast red blink (~4 Hz)
 *     0x02 = identify : blue blink for ~5 s, then back to the previous mode
 *   Reading the characteristic returns the current mode byte.
 */

#include <Arduino.h>
#include <esp_arduino_version.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <esp_bt.h>

#define LED_PIN        48   // Onboard RGB LED. Some S3 clones use GPIO 38 instead.
#define LED_BRIGHTNESS 40   // 0-255; kept low so the LED is not blinding.

#define DEVICE_NAME  "ESP-TAG"
#define SERVICE_UUID "7b1e0001-3c4d-4e8a-9f21-5a6b7c8d9e01"
#define LED_UUID     "7b1e0002-3c4d-4e8a-9f21-5a6b7c8d9e01"

enum : uint8_t { MODE_SAFE = 0x00, MODE_ALERT = 0x01, MODE_IDENTIFY = 0x02 };

static const uint32_t HEARTBEAT_PERIOD_MS = 3000;
static const uint32_t HEARTBEAT_ON_MS     = 30;
static const uint32_t ALERT_PERIOD_MS     = 250;   // 4 Hz
static const uint32_t IDENTIFY_PERIOD_MS  = 400;
static const uint32_t IDENTIFY_TIME_MS    = 5000;

static BLECharacteristic *ledChar = nullptr;
static volatile int16_t pendingCmd = -1;  // set from BLE task, consumed in loop()
static uint8_t  mode = MODE_SAFE;
static uint8_t  modeBeforeIdentify = MODE_SAFE;
static uint32_t modeStartMs = 0;
static uint32_t identifyEndMs = 0;

// ---------------------------------------------------------------- LED helpers
static void ledWrite(uint8_t r, uint8_t g, uint8_t b) {
  static int32_t last = -1;  // only push to the LED when the colour changes
  int32_t packed = ((int32_t)r << 16) | ((int32_t)g << 8) | b;
  if (packed == last) return;
  last = packed;
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  rgbLedWrite(LED_PIN, r, g, b);
#else
  neopixelWrite(LED_PIN, r, g, b);
#endif
}

static void publishMode() {  // keep the readable value in sync with the mode
  uint8_t v = mode;
  ledChar->setValue(&v, 1);
}

static void setMode(uint8_t m) {
  mode = m;
  modeStartMs = millis();
  publishMode();
  Serial.printf("[mode] -> 0x%02X (%s)\n", m,
                m == MODE_SAFE ? "safe" : m == MODE_ALERT ? "alert" : "identify");
}

// ------------------------------------------------------------- BLE callbacks
class ServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer *server) override {
    Serial.println("[ble] client connected");
    BLEDevice::startAdvertising();  // keep advertising so RSSI keeps flowing
  }
  void onDisconnect(BLEServer *server) override {
    Serial.println("[ble] client disconnected");
    BLEDevice::startAdvertising();
  }
};

class LedCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic *c) override {
    // getData()/getLength() have the same signature on core 2.x and 3.x.
    if (c->getLength() < 1) return;
    pendingCmd = c->getData()[0];
  }
};

// --------------------------------------------------------------------- setup
void setup() {
  Serial.begin(115200);
  ledWrite(LED_BRIGHTNESS, LED_BRIGHTNESS, LED_BRIGHTNESS);  // boot: white flash
  delay(150);
  ledWrite(0, 0, 0);
  Serial.println("\n[boot] ESP Zone Tracker tag starting");

  BLEDevice::init(DEVICE_NAME);
  esp_ble_tx_power_set(ESP_BLE_PWR_TYPE_DEFAULT, ESP_PWR_LVL_P9);
  esp_ble_tx_power_set(ESP_BLE_PWR_TYPE_ADV, ESP_PWR_LVL_P9);

  BLEServer *server = BLEDevice::createServer();
  server->setCallbacks(new ServerCallbacks());

  BLEService *service = server->createService(SERVICE_UUID);
  ledChar = service->createCharacteristic(
      LED_UUID, BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_WRITE |
                    BLECharacteristic::PROPERTY_WRITE_NR);
  ledChar->setCallbacks(new LedCallbacks());
  publishMode();
  service->start();

  // Custom advertising payload: flags(3) + 128-bit UUID(18) + name(9) = 30 of 31 bytes,
  // so passive scanners also see local_name without a scan response.
  BLEAdvertisementData advData;
  advData.setFlags(ESP_BLE_ADV_FLAG_GEN_DISC | ESP_BLE_ADV_FLAG_BREDR_NOT_SPT);
  advData.setCompleteServices(BLEUUID(SERVICE_UUID));
  advData.setName(DEVICE_NAME);
  BLEAdvertisementData scanResp;  // name again for active scanners
  scanResp.setName(DEVICE_NAME);

  BLEAdvertising *adv = BLEDevice::getAdvertising();
  adv->setAdvertisementData(advData);
  adv->setScanResponseData(scanResp);
  adv->setMinInterval(0x50);  // 0x50 * 0.625 ms = 50 ms
  adv->setMaxInterval(0x60);  // 0x60 * 0.625 ms = 60 ms
  BLEDevice::startAdvertising();
  Serial.println("[ble] advertising as " DEVICE_NAME);
  modeStartMs = millis();
}

// ---------------------------------------------------------------------- loop
void loop() {
  uint32_t now = millis();

  int16_t cmd = pendingCmd;  // apply commands written by the central
  if (cmd >= 0) {
    pendingCmd = -1;
    if (cmd == MODE_IDENTIFY) {
      if (mode != MODE_IDENTIFY) modeBeforeIdentify = mode;
      identifyEndMs = now + IDENTIFY_TIME_MS;
      setMode(MODE_IDENTIFY);
    } else if (cmd == MODE_SAFE || cmd == MODE_ALERT) {
      if (mode == MODE_IDENTIFY) modeBeforeIdentify = (uint8_t)cmd;  // apply after identify
      else if (cmd != mode) setMode((uint8_t)cmd);
      else publishMode();
    } else {
      Serial.printf("[ble] ignored unknown command 0x%02X\n", cmd);
      publishMode();  // restore readable value
    }
  }

  if (mode == MODE_IDENTIFY && (int32_t)(now - identifyEndMs) >= 0) {
    setMode(modeBeforeIdentify);
  }

  uint32_t t = now - modeStartMs;
  switch (mode) {
    case MODE_ALERT:
      ledWrite((t % ALERT_PERIOD_MS) < ALERT_PERIOD_MS / 2 ? LED_BRIGHTNESS : 0, 0, 0);
      break;
    case MODE_IDENTIFY:
      ledWrite(0, 0, (t % IDENTIFY_PERIOD_MS) < IDENTIFY_PERIOD_MS / 2 ? LED_BRIGHTNESS : 0);
      break;
    default:  // safe: dim green heartbeat
      ledWrite(0, (now % HEARTBEAT_PERIOD_MS) < HEARTBEAT_ON_MS ? LED_BRIGHTNESS / 5 : 0, 0);
      break;
  }
  delay(5);
}
