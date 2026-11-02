# Water Tank Monitor

Battery-powered underground water tank level monitor with a self-hosted cloud backend.

**Hardware:** ESP32-C3 SuperMini, SR04M-2 waterproof ultrasonic sensor, TP4056 + 2000 mAh LiPo.
**Backend:** FastAPI, Postgres (SQLite for local dev). **Dashboard:** Chart.js, served by the backend.
**Hosting:** Render (web service, Docker) + Neon (free Postgres).

```
ESP32 (wake every 3 h) -> median of 7 echo samples -> HTTPS POST -> FastAPI -> Postgres -> dashboard
```

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export API_KEY="pick-a-long-random-string"
export EMPTY_DISTANCE_CM=200   # sensor reading with empty tank
export FULL_DISTANCE_CM=25     # sensor reading with full tank
uvicorn app:app --host 0.0.0.0 --port 8000
```

Without `DATABASE_URL`, readings go to a local `tank.db` (SQLite). Open `http://localhost:8000`.

## Deploy (Render + Neon)

1. **Database:** create a free project on Neon and copy its connection string (starts with `postgresql://`).
2. **Code:** push this folder to a GitHub repo.
3. **Server:** on Render choose New > Blueprint (reads `render.yaml`), or New > Web Service > Docker. Set the environment variables:

| Variable | Meaning |
|---|---|
| `API_KEY` | Secret the ESP32 sends in the `X-API-Key` header |
| `DASHBOARD_PASSWORD` | Password for viewing the dashboard (any username) |
| `DATABASE_URL` | Neon connection string |
| `EMPTY_DISTANCE_CM` / `FULL_DISTANCE_CM` | Tank calibration |
| `TZ_NAME` | Time zone for daily usage, default `Asia/Kolkata` |
| `STALE_HOURS` | Show a warning banner after this many hours without data (default 7) |

4. **Firmware:** set `SERVER_URL` to `https://<your-service>.onrender.com/api/readings` and the same `API_KEY`, then flash.

Free-tier notes: a Render free web service sleeps after 15 idle minutes and takes about a minute to wake, so the firmware waits up to 75 s for a response. An uptime pinger hitting `/healthz` every 10 minutes keeps it awake and also saves node battery (no long waits).

## API

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/readings` | `X-API-Key` | Ingest a reading (`distance_cm`, optional `battery_v`, `rssi`, `age_s`) |
| GET | `/api/readings?hours=48` | dashboard password | History with computed level % |
| GET | `/api/latest` | dashboard password | Latest reading |
| GET | `/api/usage?days=7` | dashboard password | Estimated daily consumption |
| GET | `/healthz` | none | Liveness check |

Test without hardware:

```bash
curl -X POST localhost:8000/api/readings -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" -d '{"distance_cm":120,"battery_v":3.9,"rssi":-62}'
```

## Design notes

- Raw distance is stored; level % is computed at read time, so recalibrating fixes old data.
- Median filtering on the device rejects false echoes.
- The node buffers up to 8 unsent readings in RTC memory and sends them with an `age_s` offset, so a server or Wi-Fi outage leaves no gaps in the history.
- The node always returns to deep sleep, even when Wi-Fi fails.
- Device and viewer credentials are separate; the server refuses to start in production with the default API key.
- Dashboard shows a warning banner when no data has arrived for several hours.

## Roadmap

- [ ] Temperature compensation for speed of sound
- [ ] Push alert for low level (Telegram)
- [ ] OTA firmware updates
- [ ] Measured battery life (sleep current) documented here
