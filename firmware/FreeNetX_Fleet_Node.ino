/*
  FreeNet-X Autonomous Network Intelligence V2.0 - Fleet Node
  Board: DOIT ESP32 DEVKIT V1 / ESP32-WROOM-32
  Purpose: authorized Wi-Fi uplink + private AP + continuous heartbeat +
           provenance-labelled event reporting + central config fetch.

  IMPORTANT:
  - USB on this board is USB-to-UART for power/programming/serial, not USB Ethernet.
  - This MVP sends telemetry to a local trusted lab server over HTTP (or HTTPS/MQTT
    when enabled below). Do not expose to the public internet.
  - This node does not scan for credentials, bypass captive portals, impersonate
    clients, attack networks, or copy itself to other devices.
  - Self-propagation and OTA are intentionally disabled.

  Status LED (default GPIO 2, onboard on most DEVKIT V1 boards):
    - Blue/ON  = uplink connected and last heartbeat accepted
    - OFF      = no uplink or heartbeat failed
  (If your board LED is active-low, flip STATUS_LED_ACTIVE_HIGH.)
*/

#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>

// ---------- Transport selection (compile-time) ----------
// Set to 1 to use HTTPS (WiFiClientSecure). Requires a trusted root cert or
// set ALLOW_INSECURE_TLS=1 for lab testing only.
#define USE_HTTPS 0
#define ALLOW_INSECURE_TLS 1

// Set to 1 to also publish telemetry over MQTT (requires PubSubClient library).
#define USE_MQTT 0

// EXPERIMENTAL: enable NAPT internet sharing on the AP interface.
// The Arduino ESP32 core does not support NAPT natively. For routed internet
// sharing, use the ESP-IDF build in firmware/esp-idf/ (CONFIG_LWIP_NAPT).
// Keep 0 here (telemetry-only node).
#define ENABLE_EXPERIMENTAL_NAPT 0

#if USE_MQTT
#include <PubSubClient.h>
#endif

#if ENABLE_EXPERIMENTAL_NAPT
#include "esp_netif.h"
#endif

// ---------- Node configuration (EDIT PER BOARD) ----------
const char* UPLINK_SSID = "YOUR_AUTHORIZED_WIFI";
const char* UPLINK_PASS = "YOUR_WIFI_PASSWORD";

const char* AP_SSID = "FreeNetX-Node";
const char* AP_PASS = "ChangeThis123";

const char* NODE_ID = "node-001";
const char* NODE_TOKEN = "REPLACE_WITH_UNIQUE_NODE_TOKEN";
const char* API_BASE = "http://192.168.1.10:8000"; // trusted LAN test server only

// ---------- Status LED ----------
#define STATUS_LED 2
#define STATUS_LED_ACTIVE_HIGH 1

// ---------- Timing ----------
const unsigned long SYNC_INTERVAL_MS = 30000;
const unsigned long CONFIG_POLL_EVERY_N_SYNC = 1; // fetch config every sync
const unsigned long WIFI_RETRY_MS = 10000;

unsigned long lastSync = 0;
unsigned long bootAt = 0;
unsigned long lastWifiRetry = 0;
uint32_t syncCounter = 0;
bool lastHeartbeatOk = false;

#if USE_HTTPS
#include <WiFiClientSecure.h>
#endif
#if USE_MQTT
WiFiClient mqttNetClient;
PubSubClient mqttClient(mqttNetClient);
const char* MQTT_BROKER = "192.168.1.10";
const uint16_t MQTT_PORT = 1883;
#endif

void ledWrite(bool on) {
#if STATUS_LED_ACTIVE_HIGH
  digitalWrite(STATUS_LED, on ? HIGH : LOW);
#else
  digitalWrite(STATUS_LED, on ? LOW : HIGH);
#endif
}

void startPrivateAP() {
  WiFi.softAP(AP_SSID, AP_PASS);
  Serial.printf("[AP] SSID=%s IP=%s\n", AP_SSID, WiFi.softAPIP().toString().c_str());
}

void connectAuthorizedWiFi() {
  WiFi.begin(UPLINK_SSID, UPLINK_PASS);
  Serial.printf("[STA] Connecting to authorized SSID: %s\n", UPLINK_SSID);
  unsigned long started = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - started < 30000) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[STA] Connected IP=%s RSSI=%d\n",
      WiFi.localIP().toString().c_str(), WiFi.RSSI());
#if ENABLE_EXPERIMENTAL_NAPT
    esp_netif_t* ap_netif = esp_netif_get_handle_from_ifkey("WIFI_AP_DEF");
    if (ap_netif) {
      if (esp_netif_napt_enable(ap_netif) == ESP_OK) {
        Serial.println("[NAPT] enabled (experimental, untested)");
      } else {
        Serial.println("[NAPT] enable failed");
      }
    }
#endif
  } else {
    Serial.println("[STA] Not connected; will retry in loop.");
  }
}

bool ensureWiFi() {
  if (WiFi.status() == WL_CONNECTED) return true;
  if (millis() - lastWifiRetry < WIFI_RETRY_MS) return false;
  lastWifiRetry = millis();
  Serial.println("[STA] Reconnecting...");
  WiFi.disconnect();
  delay(200);
  WiFi.begin(UPLINK_SSID, UPLINK_PASS);
  unsigned long started = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - started < 15000) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[STA] Reconnected IP=%s RSSI=%d\n",
      WiFi.localIP().toString().c_str(), WiFi.RSSI());
    return true;
  }
  Serial.println("[STA] Reconnect failed.");
  return false;
}

String httpBase() { return String(API_BASE); }

bool sendHeartbeat() {
  if (!ensureWiFi()) {
    ledWrite(false);
    return false;
  }

  HTTPClient http;
  String url = httpBase() + "/v1/heartbeat";
#if USE_HTTPS
  WiFiClientSecure client;
  if (ALLOW_INSECURE_TLS) client.setInsecure();
  http.begin(client, url);
#else
  http.begin(url);
#endif
  http.addHeader("Content-Type", "application/json");
  http.addHeader("Authorization", String("Bearer ") + NODE_TOKEN);

  StaticJsonDocument<256> doc;
  doc["node_id"] = NODE_ID;
  doc["firmware"] = "2.0.0-mvp";
  doc["rssi"] = WiFi.RSSI();
  doc["ip_address"] = WiFi.localIP().toString();
  doc["uptime_seconds"] = (millis() - bootAt) / 1000;

  String body;
  serializeJson(doc, body);
  int code = http.POST(body);
  Serial.printf("[SYNC] heartbeat HTTP=%d\n", code);
  bool ok = (code >= 200 && code < 300);
  if (code > 0) Serial.println(http.getString());
  http.end();

  lastHeartbeatOk = ok;
  ledWrite(ok);
  return ok;
}

bool fetchConfig() {
  if (!ensureWiFi()) return false;
  HTTPClient http;
  String url = httpBase() + "/v1/config/" + String(NODE_ID);
#if USE_HTTPS
  WiFiClientSecure client;
  if (ALLOW_INSECURE_TLS) client.setInsecure();
  http.begin(client, url);
#else
  http.begin(url);
#endif
  http.addHeader("Authorization", String("Bearer ") + NODE_TOKEN);
  int code = http.GET();
  Serial.printf("[CONFIG] HTTP=%d\n", code);
  if (code == 200) {
    String payload = http.getString();
    StaticJsonDocument<512> doc;
    DeserializationError err = deserializeJson(doc, payload);
    if (!err) {
      JsonObject cfg = doc["config"].as<JsonObject>();
      Serial.print("[CONFIG] ");
      serializeJson(cfg, Serial);
      Serial.println();
      // Apply known keys here as needed, e.g. sync interval.
      if (cfg.containsKey("sync_interval_ms")) {
        // Would require changing const; left as log-only for safety.
        Serial.printf("[CONFIG] requested sync_interval_ms=%ld (restart to apply)\n",
                      (long)cfg["sync_interval_ms"].as<int>());
      }
    }
  }
  http.end();
  return code >= 200 && code < 300;
}

bool sendEvent(const char* sourceType, const char* sourceRef,
               const char* eventType, JsonDocument& payload) {
  if (!ensureWiFi()) return false;
  HTTPClient http;
  String url = httpBase() + "/v1/events";
#if USE_HTTPS
  WiFiClientSecure client;
  if (ALLOW_INSECURE_TLS) client.setInsecure();
  http.begin(client, url);
#else
  http.begin(url);
#endif
  http.addHeader("Content-Type", "application/json");
  http.addHeader("Authorization", String("Bearer ") + NODE_TOKEN);

  StaticJsonDocument<512> doc;
  doc["node_id"] = NODE_ID;
  doc["source_type"] = sourceType;
  doc["source_ref"] = sourceRef;
  doc["event_type"] = eventType;
  doc["payload"] = payload;

  String body;
  serializeJson(doc, body);
  int code = http.POST(body);
  Serial.printf("[EVENT] HTTP=%d\n", code);
  http.end();
  return code >= 200 && code < 300;
}

#if USE_MQTT
void mqttCallback(char* topic, byte* payload, unsigned int length) {
  Serial.printf("[MQTT] msg on %s\n", topic);
}
void ensureMqtt() {
  if (mqttClient.connected()) return;
  mqttClient.setServer(MQTT_BROKER, MQTT_PORT);
  mqttClient.setCallback(mqttCallback);
  if (mqttClient.connect(NODE_ID)) {
    Serial.println("[MQTT] connected");
    mqttClient.subscribe("freenetx/config/*");
  } else {
    Serial.printf("[MQTT] connect failed rc=%d\n", mqttClient.state());
  }
}
#endif

void setup() {
  Serial.begin(115200);
  delay(500);
  bootAt = millis();
  pinMode(STATUS_LED, OUTPUT);
  ledWrite(false);

  WiFi.mode(WIFI_AP_STA);
  WiFi.setSleep(false);
  startPrivateAP();
  connectAuthorizedWiFi();

  // Initial heartbeat + config fetch.
  sendHeartbeat();
  fetchConfig();
  lastSync = millis();

#if USE_MQTT
  ensureMqtt();
#endif
}

void loop() {
  if (millis() - lastSync >= SYNC_INTERVAL_MS) {
    lastSync = millis();
    syncCounter++;
    bool ok = sendHeartbeat();
    if (ok && (syncCounter % CONFIG_POLL_EVERY_N_SYNC == 0)) {
      fetchConfig();
    }
    // Example: report a provenance-labelled RSSI sample every 5 syncs.
    if (ok && (syncCounter % 5 == 0)) {
      StaticJsonDocument<128> p;
      p["rssi"] = WiFi.RSSI();
      p["uptime_seconds"] = (millis() - bootAt) / 1000;
      sendEvent("owned", "esp32-rssi", "signal_sample", p);
    }
  }

#if USE_MQTT
  if (WiFi.status() == WL_CONNECTED) {
    ensureMqtt();
    mqttClient.loop();
  }
#endif

  // Keep LED reflecting last known state if uplink drops.
  if (WiFi.status() != WL_CONNECTED) {
    ledWrite(false);
  }

  delay(50);
}