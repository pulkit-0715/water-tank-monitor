// ESP32-C3 SuperMini water tank node:
// measure -> buffer -> POST to backend (retry, patient with slow cloud wake-ups) -> deep sleep 3 h.
// Edit the CONFIG block, then flash with Arduino IDE (board: ESP32C3 Dev Module, USB CDC On Boot: Enabled).
#include <WiFi.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>

// ---------- CONFIG ----------
const char* WIFI_SSID = "YOUR_WIFI";
const char* WIFI_PASS = "YOUR_PASSWORD";
const char* SERVER_URL = "https://YOUR-APP.onrender.com/api/readings";  // local test: http://192.168.x.x:8000/api/readings
const char* API_KEY = "change-me";                                      // must match server API_KEY
const int TRIG_PIN = 2;
const int ECHO_PIN = 3;
const int BATT_ADC_PIN = -1;      // set to your ADC pin if you wired a voltage divider, else -1
const float BATT_DIVIDER = 2.0;   // (R1+R2)/R2 of your divider
const uint32_t SLEEP_SECONDS = 3UL * 3600UL;   // use 60 while testing
const int SAMPLES = 7;
const uint32_t HTTP_TIMEOUT_MS = 75000;        // free cloud servers can take ~1 min to wake up
// ----------------------------

#define MAX_PENDING 8
// RTC memory survives deep sleep (not a power loss). Unsent readings wait here for the next wake.
RTC_DATA_ATTR uint32_t wakeCount = 0;
RTC_DATA_ATTR int pendingCount = 0;
RTC_DATA_ATTR float pendDist[MAX_PENDING];
RTC_DATA_ATTR float pendBatt[MAX_PENDING];
RTC_DATA_ATTR uint32_t pendWake[MAX_PENDING];

float readDistanceOnce() {
  digitalWrite(TRIG_PIN, LOW);  delayMicroseconds(5);
  digitalWrite(TRIG_PIN, HIGH); delayMicroseconds(15);
  digitalWrite(TRIG_PIN, LOW);
  unsigned long us = pulseIn(ECHO_PIN, HIGH, 40000UL);  // 40 ms timeout
  if (us == 0) return -1;
  return us * 0.0343f / 2.0f;  // cm at ~20 C
}

// Median of N samples rejects the occasional false echo.
float readDistanceMedian() {
  float v[SAMPLES]; int n = 0;
  for (int i = 0; i < SAMPLES; i++) {
    float d = readDistanceOnce();
    if (d > 20 && d < 600) v[n++] = d;   // SR04M-2 has ~20 cm dead zone
    delay(120);
  }
  if (n == 0) return -1;
  for (int i = 0; i < n - 1; i++)
    for (int j = i + 1; j < n; j++)
      if (v[j] < v[i]) { float t = v[i]; v[i] = v[j]; v[j] = t; }
  return v[n / 2];
}

float readBatteryV() {
  if (BATT_ADC_PIN < 0) return -1;
  uint32_t mv = analogReadMilliVolts(BATT_ADC_PIN);
  return mv * BATT_DIVIDER / 1000.0f;
}

void savePending(float dist, float batt) {
  if (pendingCount == MAX_PENDING) {  // buffer full: drop the oldest
    for (int i = 1; i < MAX_PENDING; i++) {
      pendDist[i - 1] = pendDist[i]; pendBatt[i - 1] = pendBatt[i]; pendWake[i - 1] = pendWake[i];
    }
    pendingCount--;
  }
  pendDist[pendingCount] = dist;
  pendBatt[pendingCount] = batt;
  pendWake[pendingCount] = wakeCount;
  pendingCount++;
}

bool postReading(float dist, float batt, uint32_t ageSeconds) {
  WiFiClientSecure secure;
  secure.setInsecure();  // fine for a hobby project; pin the cert later for extra points
  HTTPClient http;
  bool https = String(SERVER_URL).startsWith("https");
  if (https) http.begin(secure, SERVER_URL); else http.begin(SERVER_URL);
  http.setConnectTimeout(15000);
  http.setTimeout(HTTP_TIMEOUT_MS);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-API-Key", API_KEY);
  String body = "{\"distance_cm\":" + String(dist, 1) + ",\"rssi\":" + String(WiFi.RSSI());
  if (batt > 0) body += ",\"battery_v\":" + String(batt, 2);
  if (ageSeconds > 0) body += ",\"age_s\":" + String(ageSeconds);
  body += "}";
  int code = http.POST(body);
  Serial.printf("POST -> %d\n", code);
  http.end();
  return code == 201;
}

void goToSleep(uint32_t seconds) {
  WiFi.disconnect(true);
  esp_sleep_enable_timer_wakeup((uint64_t)seconds * 1000000ULL);
  esp_deep_sleep_start();
}

void setup() {
  Serial.begin(115200);
  wakeCount++;
  pinMode(TRIG_PIN, OUTPUT);
  pinMode(ECHO_PIN, INPUT);

  float dist = readDistanceMedian();
  float batt = readBatteryV();
  Serial.printf("distance=%.1f cm, battery=%.2f V\n", dist, batt);
  if (dist > 0) savePending(dist, batt);

  if (pendingCount > 0) {
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    unsigned long t0 = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - t0 < 15000) delay(200);

    if (WiFi.status() == WL_CONNECTED) {
      int sent = 0;
      while (sent < pendingCount) {  // oldest first
        uint32_t age = (wakeCount - pendWake[sent]) * SLEEP_SECONDS;
        bool ok = false;
        for (int attempt = 0; attempt < 2 && !ok; attempt++) {
          ok = postReading(pendDist[sent], pendBatt[sent], age);
          if (!ok) delay(2000);
        }
        if (!ok) break;  // server unreachable: keep the rest for next wake
        sent++;
      }
      for (int i = sent; i < pendingCount; i++) {  // drop what was sent, shift the rest down
        pendDist[i - sent] = pendDist[i]; pendBatt[i - sent] = pendBatt[i]; pendWake[i - sent] = pendWake[i];
      }
      pendingCount -= sent;
    } else {
      Serial.println("Wi-Fi not connected; keeping readings for next wake");
    }
  }
  goToSleep(SLEEP_SECONDS);  // always sleep, even on failure, to protect the battery
}

void loop() {}
