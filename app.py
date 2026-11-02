"""Water tank monitor backend.

ESP32 posts readings -> stored in Postgres (cloud) or SQLite (local) -> dashboard + JSON API.
Set DATABASE_URL to use Postgres; leave it unset to use a local SQLite file.
"""
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

DATABASE_URL = os.getenv("DATABASE_URL", "")
USE_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
DB_PATH = os.getenv("DB_PATH", "tank.db")
API_KEY = os.getenv("API_KEY", "change-me")                       # device -> server
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")          # viewers; empty = open
# Calibration: measure these once with a tape measure.
EMPTY_DISTANCE_CM = float(os.getenv("EMPTY_DISTANCE_CM", "200"))  # sensor reading when tank is empty
FULL_DISTANCE_CM = float(os.getenv("FULL_DISTANCE_CM", "25"))     # sensor reading when tank is full
LOW_LEVEL_PCT = float(os.getenv("LOW_LEVEL_PCT", "20"))
STALE_HOURS = float(os.getenv("STALE_HOURS", "7"))                # banner if no data for this long (cycle is 3 h)
TZ = ZoneInfo(os.getenv("TZ_NAME", "Asia/Kolkata"))

if (USE_PG or os.getenv("RENDER")) and API_KEY == "change-me":
    raise RuntimeError("Set the API_KEY environment variable before deploying.")

if USE_PG:
    import psycopg
    from psycopg.rows import dict_row

app = FastAPI(title="Water Tank Monitor")
security = HTTPBasic(auto_error=False)


@contextmanager
def db():
    if USE_PG:
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=15)
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def run(conn, sql: str, params=()):
    """Run SQL written with ? placeholders on either SQLite or Postgres."""
    return conn.execute(sql.replace("?", "%s") if USE_PG else sql, params)


def init_db():
    with db() as c:
        if USE_PG:
            run(c, """CREATE TABLE IF NOT EXISTS readings (
                id BIGSERIAL PRIMARY KEY,
                ts BIGINT NOT NULL,
                distance_cm DOUBLE PRECISION NOT NULL,
                battery_v DOUBLE PRECISION,
                rssi INTEGER,
                temp_c DOUBLE PRECISION
            )""")
        else:
            run(c, """CREATE TABLE IF NOT EXISTS readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                distance_cm REAL NOT NULL,
                battery_v REAL,
                rssi INTEGER,
                temp_c REAL
            )""")
        run(c, "CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts)")


init_db()


def level_pct(distance_cm: float) -> float:
    """Convert raw distance to fill %. Computed at read time so recalibrating fixes old data too."""
    span = EMPTY_DISTANCE_CM - FULL_DISTANCE_CM
    pct = (EMPTY_DISTANCE_CM - distance_cm) / span * 100.0
    return round(max(0.0, min(100.0, pct)), 1)


def require_viewer(credentials: Optional[HTTPBasicCredentials] = Depends(security)):
    """Browser login for the dashboard/API. Any username; password = DASHBOARD_PASSWORD."""
    if not DASHBOARD_PASSWORD:
        return
    ok = credentials is not None and secrets.compare_digest(
        credentials.password.encode(), DASHBOARD_PASSWORD.encode()
    )
    if not ok:
        raise HTTPException(status_code=401, detail="login required",
                            headers={"WWW-Authenticate": 'Basic realm="Tank monitor"'})


class Reading(BaseModel):
    distance_cm: float = Field(gt=0, lt=1000)
    battery_v: Optional[float] = Field(default=None, gt=0, lt=10)
    rssi: Optional[int] = None
    temp_c: Optional[float] = None
    # Seconds since this reading was measured (device buffers readings when the server is unreachable).
    age_s: int = Field(default=0, ge=0, le=7 * 86400)


def row_to_dict(r) -> dict:
    return {
        "ts": r["ts"],
        "distance_cm": r["distance_cm"],
        "level_pct": level_pct(r["distance_cm"]),
        "battery_v": r["battery_v"],
        "rssi": r["rssi"],
        "temp_c": r["temp_c"],
    }


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/api/readings", status_code=201)
def add_reading(reading: Reading, x_api_key: str = Header(default="")):
    if not secrets.compare_digest(x_api_key.encode(), API_KEY.encode()):
        raise HTTPException(status_code=401, detail="bad api key")
    ts = int(time.time()) - reading.age_s
    with db() as c:
        run(c, "INSERT INTO readings (ts, distance_cm, battery_v, rssi, temp_c) VALUES (?,?,?,?,?)",
            (ts, reading.distance_cm, reading.battery_v, reading.rssi, reading.temp_c))
    pct = level_pct(reading.distance_cm)
    return {"ok": True, "level_pct": pct, "low": pct < LOW_LEVEL_PCT}


@app.get("/api/readings", dependencies=[Depends(require_viewer)])
def list_readings(hours: int = Query(default=48, ge=1, le=24 * 365)):
    since = int(time.time()) - hours * 3600
    with db() as c:
        rows = run(c, "SELECT * FROM readings WHERE ts >= ? ORDER BY ts", (since,)).fetchall()
    return [row_to_dict(r) for r in rows]


@app.get("/api/latest", dependencies=[Depends(require_viewer)])
def latest():
    with db() as c:
        r = run(c, "SELECT * FROM readings ORDER BY ts DESC LIMIT 1").fetchone()
    if r is None:
        raise HTTPException(status_code=404, detail="no readings yet")
    return row_to_dict(r)


@app.get("/api/usage", dependencies=[Depends(require_viewer)])
def usage(days: int = Query(default=7, ge=1, le=90)):
    """Rough daily consumption: sum of level drops between consecutive readings (refills ignored)."""
    since = int(time.time()) - days * 86400
    with db() as c:
        rows = run(c, "SELECT ts, distance_cm FROM readings WHERE ts >= ? ORDER BY ts", (since,)).fetchall()
    per_day: dict[str, float] = {}
    for prev, cur in zip(rows, rows[1:]):
        drop = level_pct(prev["distance_cm"]) - level_pct(cur["distance_cm"])
        if drop > 0:
            day = datetime.fromtimestamp(cur["ts"], TZ).strftime("%Y-%m-%d")
            per_day[day] = round(per_day.get(day, 0) + drop, 1)
    return {"unit": "percent of tank per day", "days": per_day}


DASHBOARD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tank Monitor</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
 body{font-family:system-ui,sans-serif;margin:0;padding:16px;background:#0f172a;color:#e2e8f0}
 .wrap{max-width:900px;margin:auto}
 .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:16px}
 .card{background:#1e293b;border-radius:12px;padding:14px}
 .card h3{margin:0 0 6px;font-size:13px;font-weight:500;color:#94a3b8}
 .card .v{font-size:28px;font-weight:600}
 .low{color:#f87171}
 #banner{display:none;background:#7f1d1d;color:#fecaca;padding:10px 14px;border-radius:10px;margin-bottom:12px}
 canvas{background:#1e293b;border-radius:12px;padding:10px;margin-bottom:16px}
 select{background:#1e293b;color:#e2e8f0;border:1px solid #334155;border-radius:8px;padding:6px}
</style></head><body><div class="wrap">
<h1>Water Tank</h1>
<div id="banner"></div>
<div class="cards">
 <div class="card"><h3>Level</h3><div class="v" id="level">-</div></div>
 <div class="card"><h3>Battery</h3><div class="v" id="batt">-</div></div>
 <div class="card"><h3>Signal</h3><div class="v" id="rssi">-</div></div>
 <div class="card"><h3>Last update</h3><div class="v" id="seen" style="font-size:16px">-</div></div>
</div>
<p>Range: <select id="range"><option value="24">24 h</option><option value="48" selected>48 h</option><option value="168">7 days</option><option value="720">30 days</option></select></p>
<canvas id="levelChart" height="120"></canvas>
<canvas id="battChart" height="80"></canvas>
</div>
<script>
const STALE_H = __STALE_H__;
let lc, bc;
const fmt = ts => new Date(ts*1000).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
function mk(id, label, color, ymin, ymax){
  return new Chart(document.getElementById(id), {type:'line',
    data:{labels:[],datasets:[{label,data:[],borderColor:color,tension:.25,pointRadius:2}]},
    options:{scales:{y:{min:ymin,max:ymax},x:{ticks:{maxTicksLimit:8}}},plugins:{legend:{labels:{color:'#e2e8f0'}}}}});
}
async function load(){
  const hrs = document.getElementById('range').value;
  const rows = await (await fetch('/api/readings?hours='+hrs)).json();
  const labels = rows.map(r=>fmt(r.ts));
  lc.data.labels = labels; lc.data.datasets[0].data = rows.map(r=>r.level_pct); lc.update();
  bc.data.labels = labels; bc.data.datasets[0].data = rows.map(r=>r.battery_v); bc.update();
  let latest = null;
  try { const lr = await fetch('/api/latest'); if (lr.ok) latest = await lr.json(); } catch(e) {}
  const banner = document.getElementById('banner');
  if (!latest) {
    banner.style.display = 'block'; banner.textContent = 'No readings received yet.';
    return;
  }
  const ageH = (Date.now()/1000 - latest.ts) / 3600;
  if (ageH > STALE_H) {
    banner.style.display = 'block';
    banner.textContent = 'No data for ' + ageH.toFixed(1) + ' hours. Check the tank node power and Wi-Fi.';
  } else { banner.style.display = 'none'; }
  const lv = document.getElementById('level');
  lv.textContent = latest.level_pct+'%'; lv.className = 'v'+(latest.level_pct<20?' low':'');
  document.getElementById('batt').textContent = latest.battery_v ? latest.battery_v.toFixed(2)+' V' : '-';
  document.getElementById('rssi').textContent = latest.rssi ? latest.rssi+' dBm' : '-';
  document.getElementById('seen').textContent = fmt(latest.ts);
}
lc = mk('levelChart','Level %','#38bdf8',0,100);
bc = mk('battChart','Battery V','#facc15',3.0,4.3);
document.getElementById('range').onchange = load;
load(); setInterval(load, 60000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_viewer)])
def dashboard():
    return DASHBOARD.replace("__STALE_H__", str(STALE_HOURS))
