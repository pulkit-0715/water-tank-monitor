// ESP32-C3 SuperMini water tank node (final): power sensor -> measure -> POST to server -> deep sleep.
// Board: ESP32C3 Dev Module, USB CDC On Boot: Enabled.
// TEST_MODE = true  -> never deep sleeps (Serial Monitor stays alive), repeats every SLEEP_SECONDS.
// TEST_MODE = false -> real use: deep sleep between readings. (USB serial disconnects while asleep.)
#include <WiFi.h>
#include <string.h>
#include <time.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>

// ---------- CONFIG ----------
// Wi-Fi name/password and API key live in secrets.h (not committed to Git). Copy secrets.example.h to secrets.h and fill it in.
#include "secrets.h"
const char* SERVER_URL = "https://tank-monitor-kdc9.onrender.com/api/readings";
// TLS certificate check. Paste the PEM of your server's CA certificate between the quotes (see README, "TLS").
// Left empty, the node still encrypts traffic but does NOT verify the server (prints a warning).
const char* ROOT_CA = R"PEM(
-----BEGIN CERTIFICATE-----
MIICCTCCAY6gAwIBAgINAgPlwGjvYxqccpBQUjAKBggqhkjOPQQDAzBHMQswCQYD
VQQGEwJVUzEiMCAGA1UEChMZR29vZ2xlIFRydXN0IFNlcnZpY2VzIExMQzEUMBIG
A1UEAxMLR1RTIFJvb3QgUjQwHhcNMTYwNjIyMDAwMDAwWhcNMzYwNjIyMDAwMDAw
WjBHMQswCQYDVQQGEwJVUzEiMCAGA1UEChMZR29vZ2xlIFRydXN0IFNlcnZpY2Vz
IExMQzEUMBIGA1UEAxMLR1RTIFJvb3QgUjQwdjAQBgcqhkjOPQIBBgUrgQQAIgNi
AATzdHOnaItgrkO4NcWBMHtLSZ37wWHO5t5GvWvVYRg1rkDdc/eJkTBa6zzuhXyi
QHY7qca4R9gq55KRanPpsXI5nymfopjTX15YhmUPoYRlBtHci8nHc8iMai/lxKvR
HYqjQjBAMA4GA1UdDwEB/wQEAwIBhjAPBgNVHRMBAf8EBTADAQH/MB0GA1UdDgQW
BBSATNbrdP9JNqPV2Py1PsVq8JQdjDAKBggqhkjOPQQDAwNpADBmAjEA6ED/g94D
9J+uHXqnLrmvT/aDHQ4thQEd0dlq7A/Cr8deVl5c1RxYIigL9zC2L7F8AjEA8GE8
p/SgguMh1YQdc4acLa/KNJvxn7kjNuK8YAOdgLOaVsjh4rsUecrNIdSUtUlD
-----END CERTIFICATE-----
)PEM";
const int TRIG_PIN = 5;            // sensor RX (Trig) <- GPIO5
const int ECHO_PIN = 4;            // sensor TX (Echo) -> GPIO4
const int SENSOR_POWER_PIN = 6;    // GPIO6 -> 1k -> 2N2222 base; HIGH = sensor ON
const uint32_t SLEEP_SECONDS =3UL * 3600UL ;
const int SAMPLES = 7;
const uint32_t TRIG_PULSE_US = 130;   // trigger pulse width in microseconds (130 gave stable readings)
const uint32_t HTTP_TIMEOUT_MS = 75000;        // free cloud servers can take ~1 min to wake up
const uint32_t SENSOR_WARMUP_MS = 1000;
const bool TEST_MODE = false;                   // true = Serial Monitor stays alive; set false for real use (deep sleep)
// ----------------------------

#define MAX_PENDING 8
// RTC memory survives deep sleep (not a power loss). Unsent readings wait here for the next wake.
RTC_DATA_ATTR uint32_t wakeCount = 0;
RTC_DATA_ATTR int pendingCount = 0;
RTC_DATA_ATTR float pendDist[MAX_PENDING];
RTC_DATA_ATTR uint32_t pendWake[MAX_PENDING];

float readDistanceOnce() {
  digitalWrite(TRIG_PIN, LOW);  delayMicroseconds(5);
  digitalWrite(TRIG_PIN, HIGH); delayMicroseconds(TRIG_PULSE_US);
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
    if (d > 20 && d < 250) v[n++] = d;   // SR04M-2 has ~20 cm dead zone
    delay(120);
  }
  if (n == 0) return -1;
  for (int i = 0; i < n - 1; i++)
    for (int j = i + 1; j < n; j++)
      if (v[j] < v[i]) { float t = v[i]; v[i] = v[j]; v[j] = t; }
  return v[n / 2];
}

void sensorPower(bool on) {
  digitalWrite(SENSOR_POWER_PIN, on ? HIGH : LOW);
}

bool connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) return true;
  WiFi.disconnect(true);               // cancel any attempt still in progress
  delay(300);
  WiFi.mode(WIFI_STA);
  WiFi.setTxPower(WIFI_POWER_8_5dBm);  // fixes connection failures on many ESP32-C3 SuperMini boards
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) delay(250);
  Serial.printf("WiFi status: %d (3 = connected), RSSI: %d dBm\n", WiFi.status(), WiFi.RSSI());
  return WiFi.status() == WL_CONNECTED;
}

void savePending(float dist) {
  if (pendingCount == MAX_PENDING) {  // buffer full: drop the oldest
    for (int i = 1; i < MAX_PENDING; i++) {
      pendDist[i - 1] = pendDist[i]; pendWake[i - 1] = pendWake[i];
    }
    pendingCount--;
  }
  pendDist[pendingCount] = dist;
  pendWake[pendingCount] = wakeCount;
  pendingCount++;
}

bool haveCA() { return strstr(ROOT_CA, "BEGIN CERTIFICATE") != nullptr; }

// Certificate checks need the real date (the ESP32 forgets it in deep sleep).
bool syncTime() {
  configTime(0, 0, "pool.ntp.org", "time.google.com");
  unsigned long t0 = millis();
  while (time(nullptr) < 1700000000 && millis() - t0 < 15000) delay(200);
  bool ok = time(nullptr) >= 1700000000;
  Serial.printf("Time sync: %s\n", ok ? "ok" : "FAILED");
  return ok;
}

bool postReading(float dist, uint32_t ageSeconds) {
  WiFiClientSecure secure;
  if (haveCA()) secure.setCACert(ROOT_CA);   // verify the server's certificate
  else secure.setInsecure();                 // encrypted but unverified (set ROOT_CA to fix)
  HTTPClient http;
  bool https = String(SERVER_URL).startsWith("https");
  if (https) http.begin(secure, SERVER_URL); else http.begin(SERVER_URL);
  http.setConnectTimeout(30000);
  http.setTimeout(HTTP_TIMEOUT_MS);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-API-Key", API_KEY);
  String body = "{\"distance_cm\":" + String(dist, 1) + ",\"rssi\":" + String(WiFi.RSSI());
  if (ageSeconds > 0) body += ",\"age_s\":" + String(ageSeconds);
  body += "}";
  int code = http.POST(body);
  Serial.printf("POST -> %d\n", code);
  http.end();
  return code == 201;
}

void goToSleep(uint32_t seconds) {
  sensorPower(false);
  WiFi.disconnect(true);
  esp_sleep_enable_timer_wakeup((uint64_t)seconds * 1000000ULL);
  esp_deep_sleep_start();
}

void setup() {
  Serial.begin(115200);
  delay(TEST_MODE ? 2000 : 300);   // lets a Serial Monitor connect in test mode
  pinMode(TRIG_PIN, OUTPUT);
  pinMode(ECHO_PIN, INPUT);
  pinMode(SENSOR_POWER_PIN, OUTPUT);
  sensorPower(false);
  Serial.println("Tank node started");
}

void runCycle() {
  wakeCount++;

  sensorPower(true);                 // switch the sensor ON through the transistor
  delay(SENSOR_WARMUP_MS);
  float dist = readDistanceMedian();
  sensorPower(false);                // and OFF again to save battery
  Serial.printf("distance=%.1f cm\n", dist);
  if (dist > 0) savePending(dist);

  if (pendingCount > 0 && connectWiFi()) {
    if (!haveCA()) Serial.println("WARNING: ROOT_CA empty, server certificate is NOT verified");
    else if (!syncTime()) {   // cannot verify without the date: keep readings for the next wake
      Serial.println("No time, keeping readings for next time");
      if (!TEST_MODE) goToSleep(SLEEP_SECONDS);
      return;
    }
    int sent = 0;
    while (sent < pendingCount) {  // oldest first
      uint32_t age = (wakeCount - pendWake[sent]) * SLEEP_SECONDS;
      bool ok = false;
      for (int attempt = 0; attempt < 2 && !ok; attempt++) {
        ok = postReading(pendDist[sent], age);
        if (!ok) delay(2000);
      }
      if (!ok) break;  // server unreachable: keep the rest for next time
      sent++;
    }
    for (int i = sent; i < pendingCount; i++) {  // drop what was sent, shift the rest down
      pendDist[i - sent] = pendDist[i]; pendWake[i - sent] = pendWake[i];
    }
    pendingCount -= sent;
  } else if (pendingCount > 0) {
    Serial.println("Wi-Fi not connected; keeping readings for next time");
  }

  if (!TEST_MODE) goToSleep(SLEEP_SECONDS);  // real use: always sleep, even on failure
}

void loop() {
  runCycle();
  delay(SLEEP_SECONDS * 1000UL);   // only reached in test mode
}
