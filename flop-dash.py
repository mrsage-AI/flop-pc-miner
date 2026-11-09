#!/usr/bin/env python3
"""flop-dash.py - live local dashboard for the FLOP devnet miner (native Linux host), part of flop-pc-miner.

Serves one page at http://127.0.0.1:8650 (this PC only). Python standard library only.
Reads: nvidia-smi (GPU), docker compose (containers + worker log), systemctl (tunnel),
the CLI image (`miner status`, on-chain stake/calibration) and the hosted FLOP MCP (settlements).
Never serves secrets: from miner.env it only reads FLOP_CLI, SESSION_PUBLIC_HOST, NET_MTU, WORKER_NAME and TOPLOC_ENABLED.
"""
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

D = os.environ.get("FLOP_DIR", "/opt/flop-miner")
PORT = int(os.environ.get("FLOP_DASH_PORT", "8650"))
RPC = "wss://rpc.devnet.flop.network"
MCP = "https://mcp.flop.network/mcp"
EXPLORER = "https://explorer.devnet.flop.network/account/"
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
LOCK = threading.Lock()

STATE = {
    "started": time.time(),
    "address": None, "worker": "",
    "gpu": None, "gpu_hist": deque(maxlen=900),          # ~30 min at 2 s
    "containers": {}, "tunnel": {"active": False, "host": None},
    "chain": None, "chain_at": None, "chain_err": None,
    "settle": None, "settle_at": None, "settle_err": None,
    "answered_total": 0, "answered_times": deque(maxlen=5000),
    "drop_times": deque(maxlen=5000), "last_answer": None,
    "calib_lines": deque(maxlen=10), "events": deque(maxlen=60),
    "endpoint_ok": None, "log_since": None, "errors": {},
    "sess": {"opened": 0, "settled": 0, "failed": 0, "turns": 0, "gn": 0, "paid_raw": 0,
             "last_error": None, "last_open": None, "last_settle": None, "warnings": 0},
}


def sh(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:  # noqa: BLE001
        return 1, "", str(e)


def env_get(key):
    try:
        with open(os.path.join(D, "miner.env")) as f:
            for line in f:
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        return None
    return None


def compose(*args, timeout=30):
    return sh(["docker", "compose", "--env-file", os.path.join(D, "miner.env"),
               "-f", os.path.join(D, "docker-compose.yml"), *args], timeout)


def note(kind, err):
    with LOCK:
        if err:
            STATE["errors"][kind] = str(err)[:200]
        else:
            STATE["errors"].pop(kind, None)


def loop(every, fn):
    def run():
        while True:
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                note(fn.__name__, e)
            time.sleep(every)
    threading.Thread(target=run, daemon=True).start()


def poll_gpu():
    rc, out, err = sh(["nvidia-smi", "--query-gpu=name,power.draw,power.limit,utilization.gpu,memory.used,"
                       "memory.total,temperature.gpu,clocks.sm", "--format=csv,noheader,nounits"], 10)
    if rc != 0 or not out.strip():
        note("gpu", err or "nvidia-smi unavailable"); return
    f = [x.strip() for x in out.strip().splitlines()[0].split(",")]
    num = lambda s: float(s) if re.match(r"^-?[0-9.]+$", s) else None  # noqa: E731
    g = {"name": f[0], "power": num(f[1]), "power_limit": num(f[2]), "util": num(f[3]),
         "mem_used": num(f[4]), "mem_total": num(f[5]), "temp": num(f[6]), "clock": num(f[7]), "t": time.time()}
    with LOCK:
        STATE["gpu"] = g
        STATE["gpu_hist"].append([round(g["t"]), g["power"], g["util"]])
    note("gpu", None)


def poll_containers():
    rc, out, err = compose("ps", "--all", "--format", "json", timeout=20)
    rows = {}
    if rc == 0:
        txt = out.strip()
        items = json.loads(txt) if txt.startswith("[") else [json.loads(l) for l in txt.splitlines() if l.strip()]
        for c in items:
            rows[c.get("Service")] = {"state": c.get("State"), "status": c.get("Status")}
    rc2, out2, _ = sh(["systemctl", "is-active", "flop-tunnel"], 10)
    with LOCK:
        STATE["containers"] = rows
        STATE["tunnel"] = {"active": out2.strip() == "active", "host": env_get("SESSION_PUBLIC_HOST")}
    note("containers", None if rc == 0 else (err or "docker compose ps failed"))


def poll_logs():
    since = STATE["log_since"] or "24h"  # first read: the whole life of the current worker container
    rc, out, err = compose("logs", "--no-color", "--timestamps", "--since", since, "worker", timeout=90)
    if rc != 0:
        note("logs", err or "docker compose logs failed"); return
    now = time.time()
    newest = None
    with LOCK:
        for raw in out.splitlines():
            line = ANSI.sub("", raw)
            m = re.match(r"^\S+\s+\|\s+(\d{4}-\d\d-\d\dT[\d:.]+Z)\s?(.*)$", line)
            if not m:
                continue
            ts, msg = m.group(1), m.group(2).strip()
            if STATE["log_since"] and ts <= STATE["log_since"]:
                continue
            newest = ts
            if "answered task" in msg:
                STATE["answered_total"] += 1; STATE["answered_times"].append(now); STATE["last_answer"] = ts
                n = re.search(r"\(nonce (\d+)\)", msg)
                STATE["events"].append([ts, "job", f"answered test job (nonce {n.group(1) if n else '?'})"])
            elif "[calibration]" in msg:
                text = msg.split("[calibration]", 1)[1].strip()
                STATE["calib_lines"].append([ts, text])
                kind = "bad" if "failed" in text else "good"
                STATE["events"].append([ts, kind, "calibration: " + text])
            elif "disconnected from wss" in msg or "WebSocket is not connected" in msg:
                STATE["drop_times"].append(now)
            elif msg.startswith("Endpoint ad"):
                ok = "published" in msg
                STATE["endpoint_ok"] = ok
                STATE["events"].append([ts, "good" if ok else "bad", "endpoint " + ("published to the mailbox" if ok else "cannot reach the mailbox")])
            elif msg.startswith("[session]"):
                body = msg[len("[session]"):].strip()
                ss = STATE["sess"]
                if body.startswith("open "):
                    ss["opened"] += 1; ss["last_open"] = ts
                    STATE["events"].append([ts, "info", "gateway session opened " + body.split()[1]])
                elif body.startswith("settle cooperative"):
                    ss["settled"] += 1; ss["last_settle"] = ts
                    paid = re.search(r"minerPaid=(\d+)", body)
                    gn = re.search(r"gn=(\d+)", body)
                    tx = re.search(r"tx=(0x[0-9a-fA-F]+)", body)
                    if paid:
                        ss["paid_raw"] += int(paid.group(1))
                    STATE["events"].append([ts, "good", "session SETTLED"
                                            + (f" · {int(paid.group(1)) / 1e18:.4f} test FLOP" if paid else "")
                                            + (f" · gn {gn.group(1)}" if gn else "")
                                            + (f" · tx {tx.group(1)[:12]}…" if tx else "")])
                elif body.startswith("error:"):
                    ss["failed"] += 1
                    err = body[len("error:"):].strip()
                    if err.startswith("Error:"):
                        err = err[len("Error:"):].strip()
                    ss["last_error"] = [ts, err]
                    STATE["events"].append([ts, "bad", "session failed: " + err])
                elif body.startswith("turn ") and "g_n=" in body:
                    ss["turns"] += 1
                    gn = re.search(r"g_n=(\d+)", body)
                    ss["gn"] += int(gn.group(1)) if gn else 0
                elif "omitted (fail-closed)" in body or "rejected" in body or "fence breach" in body:
                    ss["warnings"] += 1
                    STATE["events"].append([ts, "warn", "session: " + body])
            elif msg.startswith("Signer:"):
                STATE["events"].append([ts, "info", "worker started (signer " + msg.split(":", 1)[1].strip()[:10] + "...)"])
        if newest:
            STATE["log_since"] = newest
    note("logs", None)


def poll_chain():
    addr = STATE["address"]
    cli = env_get("FLOP_CLI")
    if not addr or not cli:
        note("chain", "miner key or CLI image not found"); return
    rc, out, err = sh(["docker", "run", "--rm", "--network", "host", cli, "--network", "devnet", "--rpc", RPC,
                       "miner", "status", addr, "--json"], 90)
    try:
        d = json.loads(out)
    except ValueError:
        note("chain", (err or out)[-200:] or "no output"); return
    with LOCK:
        STATE["chain"], STATE["chain_at"] = d, time.time()
    note("chain", None)


def poll_settlements():
    addr = STATE["address"]
    if not addr:
        return
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "flop_list_settlements", "arguments": {"miner": addr, "limit": 50}}}).encode()
    req = urllib.request.Request(MCP, data=body, headers={"content-type": "application/json", "user-agent": "flop-dash/1.0 (+local miner dashboard)",
                                                         "accept": "application/json, text/event-stream"})
    raw = urllib.request.urlopen(req, timeout=30).read().decode()
    lines = [l[6:] for l in raw.splitlines() if l.startswith("data: ")] or [raw]
    msg = json.loads(lines[-1])
    res = msg.get("result", {})
    d = res.get("structuredContent") or json.loads(res.get("content", [{}])[0].get("text", "{}"))
    rows = d.get("data") or []
    paid = sum(int(r.get("minerPaid") or 0) for r in rows) / 1e18
    with LOCK:
        STATE["settle"] = {"count": (d.get("pagination") or {}).get("total", len(rows)), "paid": paid,
                           "last": max((r.get("settledAt") or 0) for r in rows) if rows else None}
        STATE["settle_at"] = time.time()
    note("settlements", None)


def snapshot():
    now = time.time()
    with LOCK:
        c = STATE["chain"] or {}
        calib = c.get("calibration") or {}
        cap = next((v for k, v in calib.items() if "cap" in k.lower() and "empirical" not in k.lower()
                    and isinstance(v, (int, float, str)) and str(v).replace(".", "", 1).isdigit()), None)
        return {
            "now": now, "uptime": now - STATE["started"], "address": STATE["address"], "worker": STATE["worker"],
            "explorer": EXPLORER + (STATE["address"] or ""),
            "gpu": STATE["gpu"], "gpu_hist": list(STATE["gpu_hist"]),
            "containers": STATE["containers"], "tunnel": STATE["tunnel"], "mtu": env_get("NET_MTU"),
            "toploc": env_get("TOPLOC_ENABLED") != "0",
            "chain": {"registered": c.get("registered"), "stake": c.get("stake"), "calibration": calib or None,
                      "cap": cap, "age": (now - STATE["chain_at"]) if STATE["chain_at"] else None},
            "settle": STATE["settle"],
            "jobs": {"total": STATE["answered_total"], "last": STATE["last_answer"],
                     "last_10m": sum(1 for t in STATE["answered_times"] if now - t < 600)},
            "drops_10m": sum(1 for t in STATE["drop_times"] if now - t < 600),
            "endpoint_ok": STATE["endpoint_ok"],
            "sess": {**STATE["sess"], "paid": STATE["sess"]["paid_raw"] / 1e18},
            "calib_lines": list(STATE["calib_lines"]), "events": list(STATE["events"])[-25:][::-1],
            "errors": dict(STATE["errors"]),
        }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("cache-control", "no-store")
        self.send_header("x-content-type-options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api":
            self.send(200, "application/json", json.dumps(snapshot()).encode())
        elif path in ("/", "/index.html"):
            self.send(200, "text/html; charset=utf-8", PAGE.encode())
        else:
            self.send(404, "text/plain", b"not found")


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FLOP Miner</title>
<link rel="icon" id="fav" href="data:,">
<style>
:root{--bg:#0A1128;--panel:#0F1A38;--panel2:#13214A;--line:#1E2D5A;--ink:#F5F8FF;--muted:#8A9AC2;--dim:#5A6B93;
--cyan:#4CC9F0;--amber:#F4B942;--green:#3DDC97;--red:#F25F5C;--mono:ui-monospace,"Cascadia Mono",Consolas,monospace;
--sans:"Segoe UI",Inter,system-ui,sans-serif}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 var(--sans)}
a{color:inherit}
.wrap{max-width:1200px;margin:0 auto;padding:18px 16px 40px}
header{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:flex-end;gap:10px;margin-bottom:14px}
h1{margin:0;font:700 22px var(--mono);display:flex;align-items:center;gap:10px}h1 span{color:var(--cyan)}
.pulse{width:12px;height:12px;border-radius:50%;background:var(--dim);box-shadow:0 0 0 0 transparent;flex:none}
.pulse.ok{background:var(--green);animation:p 2s infinite}.pulse.warn{background:var(--amber)}.pulse.bad{background:var(--red)}
@keyframes p{0%{box-shadow:0 0 0 0 rgba(61,220,151,.55)}70%{box-shadow:0 0 0 9px rgba(61,220,151,0)}100%{box-shadow:0 0 0 0 rgba(61,220,151,0)}}
.sub{color:var(--muted);font:12px var(--mono);word-break:break-all;margin-top:3px}
.tools{display:flex;gap:8px;align-items:center}
button{font:12px var(--mono);padding:6px 10px;border-radius:8px;background:var(--panel2);border:1px solid var(--line);color:var(--muted);cursor:pointer}
button.on{color:var(--green);border-color:var(--green)}button.blocked{color:var(--amber);border-color:var(--amber)}
#toasts{position:fixed;right:12px;bottom:12px;display:flex;flex-direction:column;gap:8px;z-index:9;max-width:min(360px,calc(100vw - 24px))}
.toast{background:var(--panel2);border:1px solid var(--line);border-left:4px solid var(--cyan);border-radius:10px;padding:9px 12px;font-size:13px;box-shadow:0 6px 24px rgba(0,0,0,.45);animation:tin .25s ease-out}
.toast b{display:block;font-size:13px}.toast.good{border-left-color:var(--green)}.toast.bad{border-left-color:var(--red)}.toast.warn{border-left-color:var(--amber)}
@keyframes tin{from{transform:translateY(10px);opacity:0}to{transform:none;opacity:1}}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.chip{font:12px var(--mono);padding:5px 10px;border-radius:999px;background:var(--panel2);border:1px solid var(--line);color:var(--muted)}
.chip::before{content:"● ";color:var(--dim)}.chip.ok::before{color:var(--green)}.chip.bad::before{color:var(--red)}.chip.warn::before{color:var(--amber)}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(min(100%,270px),1fr));margin-bottom:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 14px;min-width:0}
.k{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--dim)}
.v{font:700 28px var(--mono);margin-top:2px;font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.v small{font-size:13px;color:var(--muted);font-weight:400}
.s{font-size:12.5px;color:var(--muted);margin-top:2px;overflow-wrap:anywhere}
.v.good{color:var(--green)}.v.warn{color:var(--amber)}.v.bad{color:var(--red)}.v.cyan{color:var(--cyan)}
.bar{height:6px;background:var(--line);border-radius:3px;margin-top:8px;overflow:hidden;display:flex}.bar i{display:block;height:100%;background:var(--cyan)}
.bar i.g{background:var(--green)}.bar i.r{background:var(--red)}.bar i.a{background:var(--amber)}
.two{display:grid;gap:12px;grid-template-columns:1fr}@media(min-width:900px){.two{grid-template-columns:1.3fr 1fr}}
section{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px;min-width:0}
section h2{margin:0 0 8px;font-size:14px}
svg{width:100%;height:auto;display:block}svg text{font:10px var(--mono);fill:var(--dim)}
.feed{list-style:none;margin:0;padding:0;font:12px var(--mono);max-height:360px;overflow:auto}
.feed li{display:grid;grid-template-columns:64px 1fr;gap:8px;padding:4px 0;border-bottom:1px solid var(--line)}
.feed .t{color:var(--dim)}.feed .good{color:var(--green)}.feed .bad{color:var(--red)}.feed .warn{color:var(--amber)}.feed .job{color:var(--muted)}.feed .info{color:var(--cyan)}
.err{font:12px var(--mono);color:var(--amber);margin:6px 0 0}
.note{background:var(--panel2);border-left:3px solid var(--amber);border-radius:8px;padding:9px 12px;font-size:13px;margin-bottom:12px;display:none}
footer{margin-top:14px;color:var(--dim);font-size:12px;text-align:center}
/* ---------- widget mode (?mini=1) ---------- */
body.mini .wrap{padding:10px 10px 12px}
body.mini header{margin-bottom:8px}body.mini h1{font-size:15px}body.mini .sub{display:none}
body.mini .chips{gap:5px;margin-bottom:8px}body.mini .chip{font-size:10.5px;padding:3px 7px}
body.mini .grid{grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px}
body.mini .card{padding:8px 10px;border-radius:10px}body.mini .v{font-size:20px}body.mini .s{font-size:11px}
body.mini .card.wide{grid-column:1/-1}
body.mini .two{grid-template-columns:1fr;gap:8px}body.mini section{padding:10px}
body.mini #chartbox h2,body.mini #gpuline,body.mini .legend{display:none}
body.mini .feed{max-height:150px;font-size:11px}body.mini .feed li{grid-template-columns:58px 1fr}
body.mini footer,body.mini #errs,body.mini .full-only{display:none}
body:not(.mini) .mini-only{display:none}
</style></head><body><div class="wrap">
<header><div><h1><span class="pulse" id="pulse"></span>FLOP miner <span id="gpuname"></span></h1><div class="sub" id="addr">loading…</div></div>
<div class="tools"><button id="alerts" type="button" title="Desktop alerts for settlements, failures and outages">🔔 alerts off</button>
<button id="widget" class="full-only" type="button" title="Open as a small always-available window">▣ widget</button></div></header>
<div class="note" id="note"></div><div id="toasts"></div>
<div class="chips" id="chips"></div>
<div class="grid" id="cards"></div>
<div class="two">
 <section id="chartbox"><h2>GPU power and load, last 30 min</h2>
  <div class="s legend" style="margin-bottom:6px"><span style="color:var(--cyan)">━</span> power (W) &nbsp; <span style="color:var(--amber)">━</span> GPU load (%)</div>
  <div id="chart"></div><div id="gpuline" class="s" style="margin-top:6px"></div></section>
 <section><h2>Activity</h2><ul class="feed" id="feed"></ul></section>
</div>
<div id="errs"></div>
<footer>Local only (127.0.0.1) · refreshes every 3 s · chain and indexer every 60 s · devnet FLOP has no value</footer>
</div>
<script>
const MINI=new URLSearchParams(location.search).has('mini');if(MINI)document.body.classList.add('mini');
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const dur=s=>{s=Math.max(0,s|0);const h=s/3600|0,m=(s%3600)/60|0;return h?`${h}h ${m}m`:`${m}m ${s%60}s`};
const ago=iso=>{if(!iso)return'—';const s=(Date.now()-Date.parse(iso))/1000;return s<90?Math.round(s)+'s ago':s<5400?Math.round(s/60)+'m ago':Math.round(s/3600)+'h ago'};
const hhmm=iso=>new Date(iso).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
const chip=(l,st)=>`<span class="chip ${st}">${esc(l)}</span>`;
const card=(k,v,s,cls='',bar='',wide=false)=>`<div class="card${wide?' wide':''}"><div class="k">${k}</div><div class="v ${cls}">${v}</div><div class="s">${s}</div>${bar}</div>`;
const pbar=pct=>`<div class="bar"><i style="width:${Math.max(0,Math.min(100,pct))}%"></i></div>`;
const shortErr=e=>/TOPLOC activation commitment/.test(e)?'V2 leaf can’t bind TOPLOC proof':e.length>70?e.slice(0,70)+'…':e;

// favicon dot = overall health
function favicon(color){try{const c=document.createElement('canvas');c.width=c.height=32;const x=c.getContext('2d');x.fillStyle='#0A1128';x.fillRect(0,0,32,32);x.fillStyle=color;x.beginPath();x.arc(16,16,11,0,7);x.fill();$('fav').href=c.toDataURL()}catch(e){}}

// alerts: in-page toast + sound + tab-title flash always work; a browser notification is added when the browser allows it
let alertsOn=false;try{alertsOn=localStorage.getItem('flopAlerts')==='1'}catch(e){}
const canDesk=()=>{try{return 'Notification' in window&&Notification.permission==='granted'}catch(e){return false}};
const deskState=()=>{try{return 'Notification' in window?Notification.permission:'unsupported'}catch(e){return 'unsupported'}};
function paintAlerts(){const b=$('alerts'),ds=deskState();
 b.textContent=!alertsOn?'🔕 alerts off':canDesk()?'🔔 alerts on · desktop + sound':'🔔 alerts on · sound';
 b.classList.toggle('on',alertsOn);b.classList.toggle('blocked',alertsOn&&ds==='denied');
 b.title=!alertsOn?'Click to turn on alerts for settlements, failures and outages'
  :ds==='denied'?'Sound and on-screen alerts are on. Browser pop-ups are blocked for this page: allow them in your browser (padlock › Notifications › Allow)'
  :canDesk()?'Sound, on-screen and browser alerts are on. Click to turn off':'Sound and on-screen alerts are on. Click to turn off'}
let actx=null;
function beep(kind){try{actx=actx||new (window.AudioContext||window.webkitAudioContext)();if(actx.state==='suspended')actx.resume();
 const notes=kind==='good'?[660,880,1320]:kind==='bad'?[440,330]:[700];let t=actx.currentTime+0.02;
 for(const f of notes){const o=actx.createOscillator(),g=actx.createGain();o.type='sine';o.frequency.value=f;
  g.gain.setValueAtTime(0.0001,t);g.gain.exponentialRampToValueAtTime(0.25,t+0.02);g.gain.exponentialRampToValueAtTime(0.0001,t+0.22);
  o.connect(g).connect(actx.destination);o.start(t);o.stop(t+0.25);t+=0.18}}catch(e){}}
let flashTimer=null,flashMsg='';
function flash(msg){flashMsg=msg;clearInterval(flashTimer);let n=0;flashTimer=setInterval(()=>{if(++n>20||!document.hidden&&n>6){clearInterval(flashTimer);flashMsg='';return}document.title=n%2?'🔔 '+msg:'⚡ FLOP miner'},900)}
function toast(t,b,kind){const el=document.createElement('div');el.className='toast '+(kind||'');el.innerHTML=`<b>${esc(t)}</b>${esc(b||'')}`;
 $('toasts').appendChild(el);while($('toasts').children.length>4)$('toasts').firstChild.remove();setTimeout(()=>el.remove(),kind==='bad'?30000:15000)}
function notify(t,b,kind){if(!alertsOn)return;toast(t,b,kind);beep(kind);flash(t.replace(/^FLOP: /,''));
 if(canDesk()){try{new Notification(t,{body:b,tag:t+Date.now()})}catch(e){}}}
$('alerts').onclick=async()=>{
 if(alertsOn){alertsOn=false}else{alertsOn=true;
  if(deskState()==='default'){try{await Notification.requestPermission()}catch(e){}}
  const ds=deskState();
  notify('FLOP: alerts are on',ds==='granted'?'Test alert: you should hear a chime and see a browser pop-up.'
   :ds==='denied'?'Chime + on-screen alerts work. Browser pop-ups are blocked: click the padlock left of the address › Notifications › Allow, then reload.'
   :'Chime + on-screen alerts work. For browser pop-ups, open the dashboard in a normal browser tab and allow notifications.','good')}
 try{localStorage.setItem('flopAlerts',alertsOn?'1':'0')}catch(e){}paintAlerts()};
paintAlerts();
$('widget').onclick=()=>window.open('/?mini=1','flopwidget','popup,width=430,height=640');
let prev=null;
function diffAlerts(d){if(!prev){prev=d;return}
 const ps=prev.sess||{},s=d.sess||{};
 if((s.settled||0)>(ps.settled||0))notify('FLOP: session settled 🎉',`${s.settled} settled · ${(s.paid||0).toFixed(4)} test FLOP paid this session`,'good');
 if((s.failed||0)>(ps.failed||0))notify('FLOP: session failed',shortErr((s.last_error||[])[1]||''),'bad');
 if(d.drops_10m>0&&!(prev.drops_10m>0))notify('FLOP: chain link dropping',`${d.drops_10m} disconnects in the last 10 min`,'warn');
 const wk=(d.containers||{}).worker||{},pw=(prev.containers||{}).worker||{};
 if(pw.state==='running'&&wk.state!=='running')notify('FLOP: worker stopped','The miner worker is no longer running','bad');
 const pc=(prev.chain||{}).cap,c=(d.chain||{}).cap;if(pc!=null&&c!=null&&Number(c)!==Number(pc))notify('FLOP: calibration changed',`${pc} → ${c} GFLOP/s`,'warn');
 prev=d}

function chart(h){
 const W=640,H=180,P={l:30,r:30,t:8,b:20};if(h.length<2){$('chart').innerHTML='<div class="s">collecting…</div>';return}
 const t0=h[0][0],t1=h[h.length-1][0],maxW=Math.max(20,...h.map(x=>x[1]||0))*1.15;
 const X=t=>P.l+(W-P.l-P.r)*(t-t0)/Math.max(1,t1-t0),Yw=v=>P.t+(H-P.t-P.b)*(1-(v||0)/maxW),Yu=v=>P.t+(H-P.t-P.b)*(1-(v||0)/100);
 const path=(f,i)=>h.map((x,j)=>(j?'L':'M')+X(x[0]).toFixed(1)+','+f(x[i]).toFixed(1)).join('');
 let s=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="GPU power and load">`;
 for(let g=0;g<=4;g++){const y=P.t+(H-P.t-P.b)*g/4;s+=`<line x1="${P.l}" x2="${W-P.r}" y1="${y}" y2="${y}" stroke="var(--line)"/><text x="${P.l-4}" y="${y+3}" text-anchor="end">${Math.round(maxW*(1-g/4))}</text><text x="${W-P.r+4}" y="${y+3}">${100-25*g}%</text>`}
 s+=`<path d="${path(Yu,2)}" fill="none" stroke="var(--amber)" stroke-width="1.5" opacity=".8"/><path d="${path(Yw,1)}" fill="none" stroke="var(--cyan)" stroke-width="2"/>`;
 s+=`<text x="${P.l}" y="${H-5}">${Math.round((t1-t0)/60)} min ago</text><text x="${W-P.r}" y="${H-5}" text-anchor="end">now</text></svg>`;$('chart').innerHTML=s}

function render(d){
 const c=d.containers||{},be=c.backend||{},wk=c.worker||{},ch=d.chain||{},cal=ch.calibration||{},g=d.gpu||{},st=d.settle,s=d.sess||{};
 $('gpuname').textContent=g.name?'· '+g.name.replace(/^NVIDIA (GeForce )?/,''):'';
 const running=be.state==='running'&&wk.state==='running';
 const health=!running?'bad':(d.drops_10m||d.endpoint_ok===false||!d.tunnel.active)?'warn':'ok';
 $('pulse').className='pulse '+health;favicon({ok:'#3DDC97',warn:'#F4B942',bad:'#F25F5C'}[health]);
 $('addr').innerHTML=d.address?`${esc(d.worker)} · <a href="${esc(d.explorer)}" target="_blank" rel="noopener">${esc(d.address)}</a> · dashboard up ${dur(d.uptime)}`:'miner key not found';
 $('chips').innerHTML=chip(running?'mining':'stopped',running?'ok':'bad')
  +chip('tunnel '+(d.tunnel.active?'up':'down'),d.tunnel.active?'ok':'bad')
  +chip('chain '+(d.drops_10m?d.drops_10m+' drops/10m':'stable'),d.drops_10m?'bad':(running?'ok':''))
  +chip('mailbox '+(d.endpoint_ok===null?'…':d.endpoint_ok?'ok':'unreachable'),d.endpoint_ok?'ok':d.endpoint_ok===false?'bad':'')
  +chip('TOPLOC '+(d.toploc?'on':'off'),d.toploc?'':'warn')
  +(MINI?'':chip('backend '+(be.state||'stopped'),be.state==='running'?'ok':'bad')+chip('worker '+(wk.state||'stopped'),wk.state==='running'?'ok':'bad')+chip('MTU '+(d.mtu||'?'),''));
 const stake=ch.stake?Number(ch.stake.delegated||0)+Number(ch.stake.self||0):null;
 const total=(s.opened||0),okp=total?100*(s.settled||0)/total:0,badp=total?100*(s.failed||0)/total:0;
 const sessBar=`<div class="bar"><i class="g" style="width:${okp}%"></i><i class="r" style="width:${badp}%"></i></div>`;
 const sessCls=s.settled?'good':s.failed?'bad':'';
 const sessSub=total?`${s.failed||0} failed · ${s.turns||0} turn${s.turns===1?'':'s'}${s.paid?` · ${s.paid.toFixed(4)} FLOP`:''}${s.last_error&&!(s.last_settle&&s.last_settle>s.last_error[0])?`<br><span style="color:var(--red)">last error ${esc(ago(s.last_error[0]))}: ${esc(shortErr(s.last_error[1]))}</span>`:s.last_settle?`<br><span style="color:var(--green)">last settled ${esc(ago(s.last_settle))}${s.last_error?' · no failures since':''}</span>`:''}`:'waiting for the gateway to open one';
 const C={
  cap:card('Calibration cap',ch.cap!=null?`${esc(ch.cap)} <small>GFLOP/s</small>`:'—',ch.cap!=null?`tier ${esc(cal.assurance_tier||cal.assuranceTier||cal.tier||'?')} · checked ${ch.age!=null?Math.round(ch.age)+'s ago':'…'}`:(ch.registered?'waiting for calibration':'reading chain…'),ch.cap!=null?'cyan':'warn'),
  sess:card('Gateway sessions',`${s.settled||0}<small> settled / ${total} opened</small>`,sessSub,sessCls,total?sessBar:'',true),
  paid:card('Paid on chain',st?`${st.count}`:'…',st?`${st.paid.toLocaleString(undefined,{maximumFractionDigits:4})} test FLOP${st.last?' · last '+ago(new Date(st.last).toISOString()):''}`:'reading indexer…',st&&st.count?'good':''),
  jobs:card('Test jobs',`${d.jobs.total}`,`${d.jobs.last_10m} in 10 min · last ${ago(d.jobs.last)}`),
  power:card('GPU power',g.power!=null?`${g.power.toFixed(1)} <small>W</small>`:'—',g.power_limit?`${g.temp??'?'} °C · load ${g.util??'?'}%`:'',g.power>200?'warn':'',g.power_limit?pbar(100*g.power/g.power_limit):''),
  vram:card('VRAM',g.mem_used!=null?`${(g.mem_used/1024).toFixed(1)} <small>/ ${(g.mem_total/1024).toFixed(1)} GB</small>`:'—',`${g.clock??'?'} MHz`,'',g.mem_total?pbar(100*g.mem_used/g.mem_total):''),
  chain:card('On chain',ch.registered===true?'registered':ch.registered===false?'not registered':'…',stake!=null?`${stake.toLocaleString()} test FLOP stake · ${ch.stake.active?'active':'inactive'}`:'',ch.registered?'good':'warn')};
 $('cards').innerHTML=(MINI?['sess','cap','paid','jobs','power']:['cap','sess','paid','jobs','power','vram','chain']).map(k=>C[k]).join('');
 const fails=s.failed||0,toplocFail=s.last_error&&/TOPLOC activation commitment/.test(s.last_error[1]);
 const note=$('note');if(toplocFail&&!s.settled&&d.toploc){note.style.display='block';note.innerHTML=`<b>${fails} gateway session${fails>1?'s':''} failed closed</b>: worker v0.1.286 + backend v0.1.239 can’t attach the TOPLOC proof to a V2 settlement leaf. Reported to @flop_labs; no stake is lost on a fail-closed session.`}else note.style.display='none';
 chart(d.gpu_hist||[]);
 $('gpuline').textContent=g.name?`${g.name}${d.tunnel.host?' · public endpoint https://'+d.tunnel.host:''}`:'';
 $('feed').innerHTML=(d.events||[]).map(e=>`<li><span class="t">${esc(hhmm(e[0]))}</span><span class="${esc(e[1])}">${esc(e[2])}</span></li>`).join('')||'<li><span></span><span class="job">no worker activity yet</span></li>';
 $('errs').innerHTML=Object.entries(d.errors||{}).map(([k,v])=>`<div class="err">${esc(k)}: ${esc(v)}</div>`).join('');
 if(!flashMsg)document.title=`${{ok:'🟢',warn:'🟡',bad:'🔴'}[health]} ${ch.cap!=null?ch.cap+' GFLOP/s':'FLOP'} · ${s.settled||0}/${total} sess · ${g.power!=null?g.power.toFixed(0)+' W':''}`;
 diffAlerts(d)}
async function tick(){try{const r=await fetch('/api',{cache:'no-store'});render(await r.json())}catch(e){$('pulse').className='pulse bad';favicon('#F25F5C');$('chips').innerHTML=chip('dashboard offline: start the miner','bad');document.title='🔴 FLOP miner offline'}}
tick();setInterval(tick,3000);
</script></body></html>"""


def main():
    try:
        with open(os.path.join(D, "address")) as f:
            STATE["address"] = f.read().strip()
    except OSError:
        pass
    STATE["worker"] = env_get("WORKER_NAME") or STATE["worker"]
    loop(2, poll_gpu)
    loop(5, poll_containers)
    loop(5, poll_logs)
    loop(60, poll_chain)
    loop(60, poll_settlements)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"FLOP miner dashboard on http://127.0.0.1:{PORT} (local only)", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
