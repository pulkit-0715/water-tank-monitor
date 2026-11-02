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


DASHBOARD = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<meta name="theme-color" content="#EAF1F4" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#071A22" media="(prefers-color-scheme: dark)">
<title>Water tank</title>
<style>
:root{
  --paper:#EAF1F4; --face:#F8FBFC; --ink:#0F2A36; --muted:#52707F; --rule:#CBD9DF; --track:#D3E1E7;
  --water:#0F7CB8; --warn:#A85F00; --bad:#BE2D3B; --stale:#7D93A0; --batt:#6B4FC0;
}
@media (prefers-color-scheme: dark){
  :root{ --paper:#071A22; --face:#0C2530; --ink:#E4F1F6; --muted:#8DA8B5; --rule:#1C3945; --track:#18343F;
         --water:#3FC1EA; --warn:#F2B544; --bad:#F2777F; --stale:#6C8794; --batt:#B4A1F5; }
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0; background:var(--paper); color:var(--ink);
  font:16px/1.45 ui-rounded,"SF Pro Rounded","Avenir Next","Segoe UI",system-ui,sans-serif;
  padding:max(20px,env(safe-area-inset-top)) 16px max(32px,env(safe-area-inset-bottom));
  font-variant-numeric:tabular-nums;
}
main{max-width:460px;margin:0 auto}
h1{font-size:20px;font-weight:650;margin:0 0 14px;letter-spacing:.2px}

/* the instrument */
.face{
  --c:var(--water);
  background:var(--face); border-radius:28px; padding:22px 18px 20px; text-align:center;
  border:1px solid var(--rule);
}
.face[data-state=warn]{--c:var(--warn)}
.face[data-state=bad]{--c:var(--bad)}
.face[data-state=stale]{--c:var(--stale)}
.gauge{display:block;width:100%;height:auto;max-width:380px;margin:0 auto}
.track{fill:none;stroke:var(--track);stroke-width:16;stroke-linecap:round}
.fill{fill:none;stroke:var(--c);stroke-width:16;stroke-linecap:round;
      transition:stroke-dasharray 1.1s cubic-bezier(.2,.7,.2,1),stroke .3s}
.tick{stroke:var(--muted);stroke-width:1.2;opacity:.45}
.tick.major{stroke-width:2;opacity:.8}
.num{font-size:52px;font-weight:700;fill:var(--ink)}
.pct{font-size:22px;font-weight:600;fill:var(--muted)}
.end{font-size:12px;fill:var(--muted);text-anchor:middle}
.msg{display:flex;align-items:center;justify-content:center;gap:9px;margin:2px 0 0;font-size:19px;font-weight:600}
.dot{width:11px;height:11px;border-radius:50%;background:var(--c);flex:none}
.ago{margin:3px 0 0;color:var(--muted);font-size:14px}
#stale{margin:12px 0 0;padding:10px 12px;border-radius:12px;background:var(--bad);color:#fff;font-size:14px;text-align:left}
@media (prefers-color-scheme: dark){ #stale{color:#1A0B0E} }

/* quiet facts, separated by rules instead of cards */
.facts{margin:22px 4px 0;padding:0}
.facts>div{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:13px 0;border-top:1px solid var(--rule)}
.facts>div:last-child{border-bottom:1px solid var(--rule)}
dt{color:var(--muted);font-size:15px}
dd{margin:0;font-weight:600;text-align:right}
dd small{font-weight:500;color:var(--muted);font-size:13px;margin-left:6px}
.bar{display:inline-block;vertical-align:middle;width:64px;height:8px;border-radius:4px;background:var(--track);margin-left:10px;overflow:hidden}
.bar i{display:block;height:100%;width:0;background:var(--batt);border-radius:4px;transition:width .8s}
.sig{display:inline-block;vertical-align:middle;margin-left:8px}
.sig rect{fill:var(--track)} .sig rect.on{fill:var(--ink)}

/* history, tucked away */
details{margin:22px 4px 0}
summary{cursor:pointer;font-weight:600;padding:8px 0;list-style:none}
summary::-webkit-details-marker{display:none}
summary::after{content:"Show";float:right;color:var(--muted);font-weight:500;font-size:14px}
details[open] summary::after{content:"Hide"}
summary:focus-visible,select:focus-visible{outline:2px solid var(--water);outline-offset:3px;border-radius:6px}
.range{margin:6px 0 10px;color:var(--muted);font-size:14px}
select{font:inherit;font-size:14px;color:var(--ink);background:var(--face);border:1px solid var(--rule);border-radius:8px;padding:5px 8px}
.chart{background:var(--face);border:1px solid var(--rule);border-radius:16px;padding:12px 12px 6px;margin:0 0 12px}
.chart h2{font-size:14px;font-weight:600;color:var(--muted);margin:0 0 6px}
.chart canvas{width:100%!important;height:190px!important}
table{width:100%;border-collapse:collapse;font-size:13.5px;margin-top:4px}
th,td{padding:7px 4px;text-align:right;border-bottom:1px solid var(--rule)}
th:first-child,td:first-child{text-align:left}
th{color:var(--muted);font-weight:500}
#histnote{color:var(--muted);font-size:14px}
@media (prefers-reduced-motion: reduce){ .fill,.bar i{transition:none} }
</style></head>
<body><main>
<h1>Water tank</h1>

<section class="face" id="face" data-state="stale">
  <svg class="gauge" id="gauge" viewBox="0 0 240 196" role="img" aria-label="Tank level: no reading yet">
    <path class="track" d="M 40.33 164 A 92 92 0 1 1 199.67 164" pathLength="100"/>
    <path class="fill" id="fill" d="M 40.33 164 A 92 92 0 1 1 199.67 164" pathLength="100" stroke-dasharray="0 100"/>
    <g id="ticks"></g>
    <text x="120" y="132" text-anchor="middle"><tspan class="num" id="num">&ndash;</tspan><tspan class="pct" dx="3">%</tspan></text>
    <text class="end" x="40" y="188">Empty</text>
    <text class="end" x="200" y="188">Full</text>
  </svg>
  <p class="msg" id="msgrow"><span class="dot"></span><span id="msg">Waiting for the first reading</span></p>
  <p class="ago" id="ago"></p>
  <p id="stale" role="status" hidden></p>
</section>

<dl class="facts">
  <div id="battrow" hidden><dt>Battery</dt><dd><span id="bv">&ndash;</span><small id="bp"></small><span class="bar" aria-hidden="true"><i id="bbar"></i></span></dd></div>
  <div><dt>Wi-Fi signal</dt><dd><span id="sig">&ndash;</span>
    <svg class="sig" id="bars" width="22" height="16" viewBox="0 0 22 16" aria-hidden="true">
      <rect x="0" y="12" width="4" height="4" rx="1"/><rect x="6" y="8" width="4" height="8" rx="1"/>
      <rect x="12" y="4" width="4" height="12" rx="1"/><rect x="18" y="0" width="4" height="16" rx="1"/>
    </svg></dd></div>
  <div><dt>Sensor to water</dt><dd><span id="dist">&ndash;</span></dd></div>
</dl>

<details id="hist">
  <summary>History</summary>
  <p class="range">Show the last <select id="range" aria-label="History range">
    <option value="24">24 hours</option><option value="48" selected>2 days</option>
    <option value="168">7 days</option><option value="720">30 days</option></select></p>
  <p id="histnote" hidden></p>
  <div class="chart"><h2>Tank level (%)</h2><canvas id="cLevel" aria-label="Tank level over time"></canvas></div>
  <div class="chart"><h2>Battery (V)</h2><canvas id="cBatt" aria-label="Battery voltage over time"></canvas></div>
  <table id="tbl"><thead><tr><th>Time</th><th>Level</th><th>Distance</th><th>Battery</th></tr></thead><tbody></tbody></table>
</details>
</main>

<script>
const STALE_H = __STALE_H__, LOW = __LOW_PCT__;
const $ = id => document.getElementById(id);
const CX = 120, CY = 118;
const pt = (deg, r) => { const a = deg * Math.PI / 180; return [CX + r * Math.sin(a), CY - r * Math.cos(a)]; };

// tick marks every 5% along the 240-degree sweep, long ones at 0/25/50/75/100
(function ticks(){
  const g = $('ticks'), NS = 'http://www.w3.org/2000/svg';
  for (let i = 0; i <= 20; i++) {
    const deg = -120 + 240 * i / 20, major = i % 5 === 0;
    const [x1, y1] = pt(deg, 77), [x2, y2] = pt(deg, major ? 63 : 70);
    const l = document.createElementNS(NS, 'line');
    l.setAttribute('x1', x1.toFixed(1)); l.setAttribute('y1', y1.toFixed(1));
    l.setAttribute('x2', x2.toFixed(1)); l.setAttribute('y2', y2.toFixed(1));
    l.setAttribute('class', major ? 'tick major' : 'tick');
    g.appendChild(l);
  }
})();

// resting LiPo voltage -> approximate charge %
const BT = [[3.27,0],[3.61,5],[3.69,10],[3.71,15],[3.73,20],[3.75,25],[3.77,30],[3.79,35],[3.80,40],[3.82,45],
            [3.84,50],[3.85,55],[3.87,60],[3.91,65],[3.95,70],[3.98,75],[4.02,80],[4.08,85],[4.11,90],[4.15,95],[4.20,100]];
function battPct(v){
  if (v <= BT[0][0]) return 0;
  for (let i = 1; i < BT.length; i++) if (v <= BT[i][0]) {
    const [v0,p0] = BT[i-1], [v1,p1] = BT[i];
    return Math.round(p0 + (p1 - p0) * (v - v0) / (v1 - v0));
  }
  return 100;
}
const when = ts => {
  const d = new Date(ts * 1000), today = new Date().toDateString() === d.toDateString();
  const t = d.toLocaleTimeString([], {hour:'numeric', minute:'2-digit'});
  return today ? 'today ' + t : d.toLocaleDateString([], {day:'numeric', month:'short'}) + ', ' + t;
};
function ago(ts){
  const m = Math.round((Date.now()/1000 - ts) / 60);
  if (m < 2) return 'just now';
  if (m < 60) return m + ' min ago';
  const h = Math.floor(m / 60);
  if (h < 24) return h + ' h ' + (m % 60) + ' min ago';
  return Math.floor(h / 24) + ' days ago';
}
function words(p){
  if (p >= 85) return 'Nearly full';
  if (p >= 50) return 'Plenty of water';
  if (p >= LOW) return 'Getting low';
  return 'Running low, refill soon';
}
function showSignal(r){
  const lvl = r >= -55 ? 4 : r >= -67 ? 3 : r >= -78 ? 2 : 1;
  const name = lvl === 4 ? 'Strong' : lvl === 3 ? 'Good' : lvl === 2 ? 'Fair' : 'Weak';
  $('sig').textContent = name + ' (' + r + ' dBm)';
  [...$('bars').children].forEach((b, i) => b.classList.toggle('on', i < lvl));
}

async function refresh(){
  let d = null;
  try { const r = await fetch('/api/latest', {cache:'no-store'}); if (r.ok) d = await r.json(); }
  catch (e) { $('ago').textContent = 'Can’t reach the server right now'; return; }
  const face = $('face');
  if (!d) { face.dataset.state = 'stale'; $('msg').textContent = 'Waiting for the first reading'; return; }

  const ageH = (Date.now()/1000 - d.ts) / 3600, stale = ageH > STALE_H, p = d.level_pct;
  face.dataset.state = stale ? 'stale' : p >= 50 ? 'ok' : p >= LOW ? 'warn' : 'bad';
  $('num').textContent = Math.round(p);
  $('fill').style.strokeDasharray = Math.max(p, 0.01) + ' 100';
  $('msg').textContent = stale ? 'Last known level' : words(p);
  $('ago').textContent = 'Updated ' + ago(d.ts);
  $('gauge').setAttribute('aria-label', 'Tank level ' + Math.round(p) + ' percent. ' + words(p));
  const s = $('stale');
  s.hidden = !stale;
  if (stale) s.textContent = 'No update for ' + ageH.toFixed(1) + ' hours. Check the sensor’s power and Wi-Fi.';

  $('battrow').hidden = !d.battery_v;
  if (d.battery_v) {
    const bp = battPct(d.battery_v);
    $('bv').textContent = d.battery_v.toFixed(2) + ' V';
    $('bp').textContent = bp + '%' + (bp < 20 ? ', charge soon' : '');
    $('bbar').style.width = bp + '%';
  }
  if (d.rssi) showSignal(d.rssi); else $('sig').textContent = '–';
  $('dist').textContent = d.distance_cm.toFixed(1) + ' cm';
}

// ---- history (Chart.js is loaded only when this section is opened) ----
let charts = {}, libPromise = null;
function loadLib(){
  if (window.Chart) return Promise.resolve();
  if (!libPromise) libPromise = new Promise((res, rej) => {
    const s = document.createElement('script');
    s.src = 'https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.js';
    s.onload = res; s.onerror = () => { libPromise = null; rej(); };
    document.head.appendChild(s);
  });
  return libPromise;
}
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
function draw(id, rows, key, color, ymin, ymax){
  if (charts[id]) charts[id].destroy();
  charts[id] = new Chart($(id), {
    type: 'line',
    data: { labels: rows.map(r => when(r.ts)),
            datasets: [{ data: rows.map(r => r[key]), borderColor: color, borderWidth: 2, tension: .3,
                         pointRadius: rows.length > 80 ? 0 : 2.5, pointHoverRadius: 5, spanGaps: true }] },
    options: { maintainAspectRatio: false, animation: false,
      interaction: { mode: 'index', intersect: false },
      plugins: { legend: { display: false } },
      scales: { y: { min: ymin, max: ymax, grid: { color: css('--rule') }, ticks: { color: css('--muted') } },
                x: { grid: { display: false }, ticks: { color: css('--muted'), maxTicksLimit: 5, maxRotation: 0 } } } }
  });
}
const showCharts = on => document.querySelectorAll('.chart').forEach(c => { c.hidden = !on; });
async function history(){
  const note = $('histnote');
  try {
    const rows = await (await fetch('/api/readings?hours=' + $('range').value, {cache:'no-store'})).json();
    note.hidden = rows.length > 0; note.textContent = 'No readings in this period yet.';
    const tb = document.querySelector('#tbl tbody'); tb.innerHTML = '';
    rows.slice(-8).reverse().forEach(r => {
      const tr = document.createElement('tr');
      [when(r.ts), r.level_pct + '%', r.distance_cm.toFixed(1) + ' cm', r.battery_v ? r.battery_v.toFixed(2) + ' V' : '–']
        .forEach(t => { const td = document.createElement('td'); td.textContent = t; tr.appendChild(td); });
      tb.appendChild(tr);
    });
    try {
      await loadLib();
      const hasBatt = rows.some(r => r.battery_v);
      draw('cLevel', rows, 'level_pct', css('--water'), 0, 100);
      if (hasBatt) draw('cBatt', rows.filter(r => r.battery_v), 'battery_v', css('--batt'), 3.0, 4.3);
      showCharts(true);
      $('cBatt').closest('.chart').hidden = !hasBatt;
    } catch (e) { showCharts(false); note.hidden = false; note.textContent = 'Charts couldn’t load. The table below still shows recent readings.'; }
  } catch (e) { note.hidden = false; note.textContent = 'Couldn’t load history.'; }
}
$('hist').addEventListener('toggle', () => { if ($('hist').open) history(); });
$('range').addEventListener('change', history);

refresh();
setInterval(() => { refresh(); if ($('hist').open) history(); }, 60000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_viewer)])
def dashboard():
    return (DASHBOARD.replace("__STALE_H__", str(STALE_HOURS))
                     .replace("__LOW_PCT__", str(LOW_LEVEL_PCT)))
