#!/usr/bin/env python3
"""Hailo-10H + Raspberry Pi 5 Stats Dashboard

Run:  python3 server.py [port]
Then: http://<pi-ip>:8765
"""

import json
import os
import re
import subprocess
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path

from flask import Flask, Response, json

# ─── Config ──────────────────────────────────────────────────────────────────

PORT = 8765
REFRESH_INTERVAL = 2.0          # seconds between stat collections
SENTINEL_PERF_URL = "http://localhost:8181/api/perf"
HAILO_DDR_GB = 8                # Hailo-10H onboard LPDDR5X spec
HAILO_PERF_QUERY = str(Path(__file__).parent / "hailo_perf_query")

# ─── Stats collection ────────────────────────────────────────────────────────

class StatsCollector:
    def __init__(self):
        self._stats = {}
        self._lock = threading.Lock()
        self._our_pid = str(os.getpid())
        self._update()

    # ── low-level helpers ────────────────────────────────────────────────────

    def _cmd(self, cmd, timeout=4):
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
            return r.stdout.strip() if r.returncode == 0 else None
        except Exception:
            return None

    def _read(self, path):
        try:
            return Path(path).read_text().strip()
        except Exception:
            return None

    # ── Hailo data ───────────────────────────────────────────────────────────

    def _hailo_cli(self):
        """Parse hailortcli fw-control identify output."""
        out = self._cmd("hailortcli fw-control identify 2>/dev/null", timeout=6)
        if not out:
            return {}
        result = {}
        for key, pat in [
            ("fw_version",        r"Firmware Version:\s+(.+)"),
            ("protocol_version",  r"Control Protocol Version:\s+(\d+)"),
            ("logger_version",    r"Logger Version:\s+(\d+)"),
            ("architecture",      r"Device Architecture:\s+(.+)"),
        ]:
            m = re.search(pat, out)
            if m:
                result[key] = m.group(1).strip()
        return result

    def _hailo(self):
        result = {"present": False, "firmware_ok": False, "ddr_total_gb": HAILO_DDR_GB}

        # PCIe detection (device physically present even if firmware is down)
        lspci_hailo = self._cmd("lspci | grep -i hailo")
        sysfs_path = None
        if lspci_hailo:
            addr = lspci_hailo.split()[0]
            result["device_id"] = addr
            # Strip address prefix and vendor name for a clean description
            desc = lspci_hailo[len(addr):].strip()  # "Co-processor: Hailo Technologies Ltd. ..."
            result["pcie_desc"] = desc.replace("Hailo Technologies Ltd. ", "").lstrip(": ")
            result["present"] = True  # physically detected on PCIe bus
            sysfs_path = f"/sys/bus/pci/devices/{addr}"

        if not Path("/dev/hailo0").exists():
            result["error"] = "/dev/hailo0 not found – reboot may be needed"
            return result

        # PCIe link info from sysfs
        if sysfs_path:
            for attr in ("current_link_speed", "current_link_width",
                         "max_link_speed", "max_link_width"):
                val = self._read(f"{sysfs_path}/{attr}")
                if val:
                    result[f"pcie_{attr}"] = val

        # Basic identity via CLI (requires firmware to be responsive)
        cli = self._hailo_cli()
        if cli:
            result["firmware_ok"] = True
            result.update(cli)

        # active inference: detect processes using /dev/hailo0 via lsof
        lsof_out = self._cmd("lsof -F p /dev/hailo0 2>/dev/null")
        if lsof_out:
            pids = [l[1:] for l in lsof_out.splitlines() if l.startswith("p") and l[1:] != self._our_pid]
            names = [self._read(f"/proc/{p}/comm") or p for p in pids]
            result["loaded_networks"] = len(pids)
            result["network_names"]   = [n for n in names if n]
        else:
            result["loaded_networks"] = 0
            result["network_names"]   = []

        return result

    # ── Pi CPU ───────────────────────────────────────────────────────────────

    def _cpu(self):
        result = {}

        out = self._cmd("vcgencmd measure_temp")
        if out:
            m = re.search(r"temp=([\d.]+)", out)
            if m:
                t = float(m.group(1))
                result["temp_c"] = t
                result["temp_f"] = round(t * 9 / 5 + 32, 1)

        out = self._cmd("vcgencmd measure_clock arm")
        if out:
            m = re.search(r"frequency\(\d+\)=(\d+)", out)
            if m:
                result["freq_mhz"] = round(int(m.group(1)) / 1_000_000, 0)

        out = self._cmd("vcgencmd get_throttled")
        if out:
            m = re.search(r"throttled=0x([0-9a-fA-F]+)", out)
            if m:
                code = int(m.group(1), 16)
                result["throttle_code"] = code
                result["throttle_ok"]   = (code == 0)
                flags = []
                if code & 0x1: flags.append("under-voltage")
                if code & 0x2: flags.append("freq-capped")
                if code & 0x4: flags.append("throttled")
                if code & 0x8: flags.append("soft-temp-limit")
                result["throttle_flags"] = flags

        out = self._cmd("vcgencmd measure_volts core")
        if out:
            m = re.search(r"volt=([\d.]+)V", out)
            if m:
                result["core_v"] = float(m.group(1))

        lo = self._read("/proc/loadavg")
        if lo:
            parts = lo.split()
            result["load_1"]  = float(parts[0])
            result["load_5"]  = float(parts[1])
            result["load_15"] = float(parts[2])

        return result

    # ── Memory ───────────────────────────────────────────────────────────────

    def _memory(self):
        result = {}
        try:
            data = Path("/proc/meminfo").read_text()

            def kb(prefix):
                m = re.search(rf"^{prefix}:\s+(\d+)\s+kB", data, re.MULTILINE)
                return int(m.group(1)) * 1024 if m else None

            total     = kb("MemTotal")
            available = kb("MemAvailable")
            swap_total = kb("SwapTotal")
            swap_free  = kb("SwapFree")

            if total and available:
                used = total - available
                result["total"]    = total
                result["used"]     = used
                result["available"]= available
                result["used_pct"] = round(used / total * 100, 1)

            if swap_total is not None and swap_free is not None:
                swap_used = swap_total - swap_free
                result["swap_total"] = swap_total
                result["swap_used"]  = swap_used
                result["swap_pct"]   = round(swap_used / max(swap_total, 1) * 100, 1)
        except Exception:
            pass
        return result

    # ── Fan ──────────────────────────────────────────────────────────────────

    def _fan(self):
        for i in range(6):
            val = self._read(f"/sys/class/hwmon/hwmon{i}/fan1_input")
            if val is not None:
                try:
                    return {"rpm": int(val), "hwmon": i}
                except ValueError:
                    pass
        return {}

    # ── System ───────────────────────────────────────────────────────────────

    def _system(self):
        result = {}

        val = self._read("/proc/uptime")
        if val:
            s = float(val.split()[0])
            days  = int(s // 86400)
            hours = int((s % 86400) // 3600)
            mins  = int((s % 3600) // 60)
            result["uptime_s"] = s
            result["uptime"]   = (f"{days}d " if days else "") + f"{hours}h {mins}m"

        val = self._read("/proc/device-tree/model")
        if val:
            result["model"] = val.rstrip("\x00").strip()

        try:
            result["cpu_count"] = len(
                [l for l in Path("/proc/cpuinfo").read_text().split("\n") if l.startswith("processor")]
            )
        except Exception:
            pass

        out = self._cmd("df -k /")
        if out:
            lines = out.strip().split("\n")
            if len(lines) >= 2:
                parts = lines[1].split()
                if len(parts) >= 5:
                    result["disk_total"] = int(parts[1]) * 1024
                    result["disk_used"]  = int(parts[2]) * 1024
                    result["disk_pct"]   = int(parts[4].rstrip("%"))

        out = self._cmd("lspci")
        if out:
            result["pcie_devices"] = [
                {"addr": line.split(" ", 1)[0], "desc": line.split(" ", 1)[1]}
                for line in out.strip().split("\n") if " " in line
            ]

        return result

    # ── Hailo perf/health query (C++ binary wrapping query_performance_stats) ─

    def _hailo_perf_query(self):
        """Run hailo_perf_query binary → dict with cpu_utilization, nnc_utilization,
        ram_size_total, ram_size_used, dsp_utilization, on_die_temperature,
        on_die_voltage, bist_failure_mask. Supported on Hailo-10/15 only."""
        try:
            r = subprocess.run(
                [HAILO_PERF_QUERY],
                capture_output=True, text=True, timeout=5
            )
            if r.returncode == 0 and r.stdout.strip():
                return json.loads(r.stdout)
        except Exception:
            pass
        return {}

    # ── Sentinel inference perf ──────────────────────────────────────────────

    def _sentinel(self):
        try:
            with urllib.request.urlopen(SENTINEL_PERF_URL, timeout=2) as resp:
                return json.loads(resp.read())
        except Exception:
            return {}

    # ── Orchestration ────────────────────────────────────────────────────────

    def _update(self):
        try:
            stats = {
                "ts":         time.time(),
                "hailo":      self._hailo(),
                "hailo_perf": self._hailo_perf_query(),
                "cpu":        self._cpu(),
                "memory":     self._memory(),
                "fan":        self._fan(),
                "system":     self._system(),
                "sentinel":   self._sentinel(),
            }
            with self._lock:
                self._stats = stats
        except Exception:
            pass

    def start(self, interval=REFRESH_INTERVAL):
        def loop():
            while True:
                self._update()
                time.sleep(interval)
        threading.Thread(target=loop, daemon=True).start()
        return self

    def get(self):
        with self._lock:
            return dict(self._stats)


# ─── Flask app ───────────────────────────────────────────────────────────────

app = Flask(__name__)
collector = StatsCollector().start()


@app.route("/api/stats")
def api_stats():
    return app.response_class(
        json.dumps(collector.get()),
        mimetype="application/json"
    )


@app.route("/")
def index():
    return Response(DASHBOARD_HTML, mimetype="text/html")


# ─── Dashboard HTML/CSS/JS ───────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Hailo-10H Dashboard</title>
<style>
:root {
  --bg:          #09101f;
  --card:        #0f1829;
  --card-border: #1e2d45;
  --card-hover:  #141e32;
  --text:        #e8eef8;
  --muted:       #5a7090;
  --label:       #8da8c8;
  --green:  #3dd68c;
  --yellow: #f5c542;
  --red:    #f26b6b;
  --blue:   #5ca8ff;
  --purple: #a98cfa;
  --cyan:   #3dd9d9;
  --orange: #f5934e;
}

* { box-sizing: border-box; margin: 0; padding: 0; }

body {
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  font-size: 14px;
  min-height: 100vh;
}

/* ── Header ── */
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 20px;
  background: #0a1220;
  border-bottom: 1px solid #182033;
  position: sticky;
  top: 0;
  z-index: 100;
  backdrop-filter: blur(4px);
}

.logo {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 15px;
  font-weight: 700;
  letter-spacing: 0.04em;
  color: var(--blue);
}

.logo-icon {
  width: 28px;
  height: 28px;
  background: linear-gradient(135deg, #4070e0, #9060d0);
  border-radius: 7px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  flex-shrink: 0;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 14px;
  font-size: 12px;
  color: var(--muted);
}

.badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 11px;
  border-radius: 20px;
  font-weight: 600;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}

.badge-online  { background: rgba(61,214,140,0.12); color: var(--green);  border: 1px solid rgba(61,214,140,0.25); }
.badge-offline { background: rgba(242,107,107,0.12); color: var(--red);   border: 1px solid rgba(242,107,107,0.25); }
.badge-warn    { background: rgba(245,197,66,0.12);  color: var(--yellow); border: 1px solid rgba(245,197,66,0.25); }

.dot {
  width: 7px; height: 7px;
  border-radius: 50%;
  display: inline-block;
  flex-shrink: 0;
}
.dot-green  { background: var(--green);  animation: pgreen 2s ease-in-out infinite; }
.dot-red    { background: var(--red); }
.dot-yellow { background: var(--yellow); animation: pyellow 1.5s ease-in-out infinite; }

@keyframes pgreen  { 0%,100%{box-shadow:0 0 0 0 rgba(61,214,140,0.5)} 50%{box-shadow:0 0 0 5px rgba(61,214,140,0)} }
@keyframes pyellow { 0%,100%{box-shadow:0 0 0 0 rgba(245,197,66,0.5)} 50%{box-shadow:0 0 0 5px rgba(245,197,66,0)} }

/* ── Grid ── */
.grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
  padding: 16px;
}
@media (max-width: 900px) { .grid { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 580px) { .grid { grid-template-columns: 1fr; } }

.span2 { grid-column: span 2; }
.span3 { grid-column: 1 / -1; }

/* ── Cards ── */
.card {
  background: var(--card);
  border: 1px solid var(--card-border);
  border-radius: 12px;
  padding: 16px;
  transition: border-color 0.2s, box-shadow 0.2s;
}
.card:hover {
  border-color: rgba(92,168,255,0.3);
  box-shadow: 0 0 20px rgba(92,168,255,0.05);
}

.card-hailo {
  background: linear-gradient(140deg, #0f1829 0%, #130d24 100%);
  border-color: rgba(169,140,250,0.3);
}
.card-hailo:hover { border-color: rgba(169,140,250,0.5); }

.card-title {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 14px;
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.09em;
  color: var(--label);
}
.card-title .ti { font-size: 13px; opacity: 0.85; }
.card-title .ct-badge {
  margin-left: auto;
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 10px;
  font-weight: 600;
  text-transform: none;
  letter-spacing: 0;
}

/* ── Stat rows ── */
.row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 5px 0;
  border-bottom: 1px solid rgba(255,255,255,0.04);
  gap: 8px;
}
.row:last-child { border-bottom: none; }
.rl { color: var(--label); font-size: 13px; flex-shrink: 0; }
.rv {
  font-family: "SF Mono", "Fira Code", ui-monospace, monospace;
  font-size: 12px;
  text-align: right;
  word-break: break-all;
}

/* ── Colors ── */
.ok   { color: var(--green); }
.warn { color: var(--yellow); }
.err  { color: var(--red); }
.info { color: var(--blue); }
.acc  { color: var(--purple); }
.cy   { color: var(--cyan); }
.or   { color: var(--orange); }
.mu   { color: var(--muted); }

/* ── Big number ── */
.bignum {
  font-size: 44px;
  font-weight: 800;
  font-family: "SF Mono", ui-monospace, monospace;
  line-height: 1;
  letter-spacing: -0.02em;
}
.bignum .unit { font-size: 20px; font-weight: 600; margin-left: 2px; }
.bignumsub {
  font-size: 12px;
  color: var(--muted);
  margin-top: 3px;
}

/* ── Memory bars ── */
.memblock { margin-bottom: 14px; padding-bottom: 14px; border-bottom: 1px solid rgba(255,255,255,0.05); }
.memblock:last-child { margin-bottom: 0; padding-bottom: 0; border-bottom: none; }

.memtop {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 3px;
}
.memname {
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.07em;
  color: var(--label);
  display: flex;
  align-items: center;
  gap: 6px;
}
.chip-tag {
  font-size: 9px;
  padding: 1px 6px;
  border-radius: 4px;
  font-weight: 600;
  letter-spacing: 0.05em;
  text-transform: uppercase;
}
.tag-pi    { background: rgba(92,168,255,0.15); color: var(--blue); }
.tag-hailo { background: rgba(169,140,250,0.15); color: var(--purple); }
.tag-swap  { background: rgba(93,214,214,0.12); color: var(--cyan); }

.memval {
  font-family: monospace;
  font-size: 14px;
  font-weight: 600;
}
.mempct { font-family: monospace; font-size: 12px; color: var(--muted); }

.track {
  height: 8px;
  background: rgba(255,255,255,0.06);
  border-radius: 4px;
  overflow: hidden;
  margin-top: 6px;
}
.fill { height: 100%; border-radius: 4px; transition: width 0.5s ease; }
.fill-blue   { background: linear-gradient(90deg, #3a6ddb, #5ca8ff); }
.fill-purple { background: linear-gradient(90deg, #7040c8, #a98cfa); }
.fill-green  { background: linear-gradient(90deg, #1a9e5a, #3dd68c); }
.fill-yellow { background: linear-gradient(90deg, #c48810, #f5c542); }
.fill-red    { background: linear-gradient(90deg, #c02828, #f26b6b); }
.fill-ghost  { background: rgba(255,255,255,0.07); }

/* ── Fan ── */
.fan-center { text-align: center; padding: 6px 0; }
.fan-ring {
  width: 80px; height: 80px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  margin-bottom: 6px;
  border: 2px solid var(--card-border);
  font-size: 11px;
  font-weight: 600;
  flex-direction: column;
  gap: 2px;
}
.fan-ring.spinning { border-color: var(--green); box-shadow: 0 0 16px rgba(61,214,140,0.2); }
.fan-ring .fan-icon { font-size: 24px; }
.fan-ring .fan-label { color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: 0.05em; }

/* ── Network tags ── */
.net-tag {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 10px;
  background: rgba(92,168,255,0.1);
  border: 1px solid rgba(92,168,255,0.2);
  border-radius: 6px;
  font-family: monospace;
  font-size: 12px;
  margin: 3px 2px;
  color: var(--blue);
}

/* ── PCIe table ── */
.pcietable { width: 100%; font-size: 12px; border-collapse: collapse; }
.pcietable tr { border-bottom: 1px solid rgba(255,255,255,0.04); }
.pcietable tr:last-child { border-bottom: none; }
.pcietable td { padding: 5px 4px; vertical-align: top; }
.pcietable td:first-child { color: var(--yellow); white-space: nowrap; padding-right: 12px; font-family: monospace; }
.pcietable td:last-child  { color: var(--muted); }

/* ── Throttle flags ── */
.flags { display: flex; flex-wrap: wrap; gap: 4px; justify-content: flex-end; }
.flag {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 4px;
  font-weight: 600;
  background: rgba(245,197,66,0.15);
  color: var(--yellow);
  border: 1px solid rgba(245,197,66,0.25);
}

/* ── Footer ── */
.footer {
  text-align: center;
  padding: 20px;
  color: var(--muted);
  font-size: 11px;
}

/* ── Divider ── */
.divider { border: none; border-top: 1px solid rgba(255,255,255,0.05); margin: 10px 0; }
</style>
</head>
<body>

<div class="header">
  <div class="logo">
    <div class="logo-icon">◈</div>
    Hailo-10H Dashboard
  </div>
  <div class="header-right">
    <div id="hbadge" class="badge badge-warn">
      <span class="dot dot-yellow"></span>
      <span id="hbtext">Connecting…</span>
    </div>
    <span id="lupdate">—</span>
  </div>
</div>

<div class="grid" id="grid">
  <div class="card span3" style="text-align:center;padding:40px;color:var(--muted);">Loading stats…</div>
</div>

<div class="footer">
  Auto-refreshes every 2 s &nbsp;·&nbsp; polls: <span id="pcnt">0</span>
</div>

<script>
const GB = 1073741824, MB = 1048576;

function fmtBytes(b) {
  if (b == null) return "N/A";
  if (b >= GB)   return (b / GB).toFixed(2) + " GB";
  if (b >= MB)   return (b / MB).toFixed(0) + " MB";
  return Math.round(b / 1024) + " KB";
}

function tempClass(c) {
  if (c == null) return "mu";
  if (c >= 80) return "err";
  if (c >= 60) return "warn";
  return "ok";
}

function barClass(pct) {
  if (pct >= 90) return "fill-red";
  if (pct >= 70) return "fill-yellow";
  return "fill-blue";
}

function row(label, valHtml, cls) {
  return `<div class="row"><span class="rl">${label}</span><span class="rv ${cls||""}">${valHtml}</span></div>`;
}

// ── Masked reveal helpers ──────────────────────────────────────────────────
const _revealed = new Set();

function maskMac(mac) {
  const p = mac.split(':');
  return p.slice(0, 3).join(':') + ':••:••:••';
}

function maskedRow(label, real, masked) {
  const id = "mask_" + label.replace(/\s+/g, '_');
  return `<div class="row">
    <span class="rl">${label}</span>
    <span class="rv mu" style="display:flex;align-items:center;gap:6px;justify-content:flex-end;">
      <span id="mv-${id}" data-real="${real}" data-masked="${masked}" style="font-family:monospace;font-size:12px;">${masked}</span><button id="mb-${id}" onclick="toggleMask('${id}')" title="Reveal" style="background:none;border:none;cursor:pointer;padding:0;color:var(--muted);font-size:14px;opacity:0.45;line-height:1;flex-shrink:0;">👁</button>
    </span>
  </div>`;
}

function toggleMask(id) {
  const val = document.getElementById('mv-' + id);
  const btn = document.getElementById('mb-' + id);
  if (!val || !btn) return;
  if (_revealed.has(id)) {
    _revealed.delete(id);
    val.textContent = val.dataset.masked;
    btn.style.opacity = '0.45';
    btn.style.color   = 'var(--muted)';
    btn.title = 'Reveal';
  } else {
    _revealed.add(id);
    val.textContent = val.dataset.real;
    btn.style.opacity = '1';
    btn.style.color   = 'var(--cyan)';
    btn.title = 'Hide';
  }
}

function applyRevealState() {
  _revealed.forEach(id => {
    const val = document.getElementById('mv-' + id);
    const btn = document.getElementById('mb-' + id);
    if (val) val.textContent = val.dataset.real;
    if (btn) { btn.style.opacity = '1'; btn.style.color = 'var(--cyan)'; btn.title = 'Hide'; }
  });
}

// ── Hailo device card ──────────────────────────────────────────────────────
function cardHailo(h) {
  let body = "";
  if (h && h.present) {
    if (h.device_id)      body += row("Device ID",     `<span class="info">${h.device_id}</span>`);
    if (h.architecture)   body += row("Architecture",  `<span class="acc">${h.architecture}</span>`);
    if (h.fw_version)     body += row("Firmware",       `<span class="ok">${h.fw_version}</span>`);
    if (h.protocol_version) body += row("Protocol",    "v" + h.protocol_version);
    if (h.board_name)     body += row("Board",          h.board_name);
    if (h.product_name)   body += row("Product",        h.product_name);
    if (h.part_number)    body += row("Part #",         h.part_number, "mu");
    if (h.serial_number)  body += row("Serial",         h.serial_number, "mu");
    if (h.nn_clock_mhz)   body += row("NN Clock",       `<span class="cy">${h.nn_clock_mhz} MHz</span>`);
    if (h.boot_source)    body += row("Boot Source",    h.boot_source);
    if (h.lcs)            body += row("LCS",            h.lcs);
    if (h.mac_address)    body += maskedRow("MAC Address", h.mac_address, maskMac(h.mac_address));
    if (h.soc_id)         body += maskedRow("SoC ID",      h.soc_id,      h.soc_id.slice(0,8) + "••••••••…");
    if (h.pcie_current_link_speed) {
      const w = h.pcie_current_link_width ? ` ×${h.pcie_current_link_width}` : "";
      body += row("PCIe Link", `<span class="info">${h.pcie_current_link_speed}${w}</span>`);
    }
    body += row("Onboard DRAM", `<span class="acc">8 GB LPDDR5X</span>`);
  } else {
    const msg = (h && h.error) ? h.error : "Device not available";
    body = `<div class="row"><span class="err" style="font-size:12px;">⚠ ${msg}</span></div>`;
  }
  const fw_ok = h && h.firmware_ok;
  const detected = h && h.present;
  const statusLabel = fw_ok ? "Online" : detected ? "FW Error" : "Offline";
  const dotCls = fw_ok ? "dot-green" : detected ? "dot-yellow" : "dot-red";
  const statusDot = `<span class="dot ${dotCls}" style="display:inline-block"></span>`;
  return `<div class="card card-hailo">
    <div class="card-title"><span class="ti">◈</span> Hailo-10H Accelerator
      <span class="ct-badge" style="background:rgba(169,140,250,0.12);color:var(--purple);">${statusDot} ${statusLabel}</span>
    </div>${body}</div>`;
}

// ── Temperature card ───────────────────────────────────────────────────────
function cardTemp(h, cpu, hp) {
  let body = "";

  // Pi CPU temperature
  const ct = cpu && cpu.temp_c;
  if (ct != null) {
    const cls = tempClass(ct);
    body += `<div style="margin-bottom:10px;">
      <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--label);margin-bottom:4px;">Pi CPU</div>
      <div style="display:flex;align-items:baseline;gap:10px;">
        <span class="bignum ${cls}">${ct.toFixed(1)}<span class="unit">°C</span></span>
        <span class="bignumsub">${cpu.temp_f != null ? cpu.temp_f.toFixed(1)+"°F" : ""}</span>
      </div>
    </div>`;
  }

  // Hailo chip on-die temperature (from query_health_stats)
  const odt = hp && hp.on_die_temperature != null && hp.on_die_temperature !== -1 ? hp.on_die_temperature : null;
  body += `<hr class="divider">`;
  body += `<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--label);margin-bottom:6px;">Hailo-10H On-Die</div>`;
  if (odt != null) {
    const cls = tempClass(odt);
    body += row("Temperature", `<span class="${cls}">${odt.toFixed(1)} °C</span>`);
  } else if (h && h.present) {
    body += `<div class="row"><span class="mu" style="font-size:12px;">Not available</span></div>`;
  }
  const bist = hp && hp.bist_failure_mask != null ? hp.bist_failure_mask : null;
  if (bist != null) {
    body += row("BIST", bist === 0
      ? `<span class="ok">✓ Pass</span>`
      : `<span class="err">Failures: 0x${bist.toString(16)}</span>`);
  }

  return `<div class="card"><div class="card-title"><span class="ti">🌡</span> Temperature</div>${body}</div>`;
}

// ── Hailo performance card ─────────────────────────────────────────────────
function cardPower(h, hp) {
  let body = "";
  const perfOk = hp && hp.perf_ok;
  const healthOk = hp && hp.health_ok;

  // Hailo SoC CPU utilization (global — reflects inference pipeline activity)
  const cpu_u = hp && hp.cpu_utilization != null && hp.cpu_utilization !== -1 ? hp.cpu_utilization : null;
  if (cpu_u != null) {
    const cls = cpu_u >= 80 ? "err" : cpu_u >= 50 ? "warn" : "ok";
    body += row("Hailo SoC CPU", `<span class="${cls}">${cpu_u.toFixed(1)}%</span>`);
  }

  // NNC utilization — per-session, always 0 from an external monitor process
  body += row("NNC utilization", `<span class="mu">N/A <span style="font-size:10px;opacity:.6">(per-session)</span></span>`);

  // On-die voltage
  const odv = hp && hp.on_die_voltage != null && hp.on_die_voltage !== -1 ? hp.on_die_voltage : null;
  if (odv != null) {
    body += row("On-Die Voltage", `<span class="cy">${(odv / 1000).toFixed(3)} V</span>`);
  }

  // SoC internal working memory (≈6 MB — Hailo SoC OS heap, NOT the 8 GB LPDDR5X)
  const ramt = hp && hp.ram_size_total != null && hp.ram_size_total !== -1 ? hp.ram_size_total : null;
  const ramu = hp && hp.ram_size_used  != null && hp.ram_size_used  !== -1 ? hp.ram_size_used  : null;
  if (ramt != null && ramu != null) {
    body += row("SoC heap", `<span class="acc">${fmtBytes(ramu)}</span> <span class="mu">/ ${fmtBytes(ramt)}</span>`);
  }

  if (h && h.nn_clock_mhz) {
    body += row("NN Core Clock", `<span class="cy">${h.nn_clock_mhz} MHz</span>`);
  }

  if (!perfOk && !healthOk && h && h.present) {
    body += `<div class="row"><span class="mu" style="font-size:12px;">perf query unavailable</span></div>`;
  }

  return `<div class="card"><div class="card-title"><span class="ti">⚡</span> Hailo Performance</div>${body}</div>`;
}

// ── Memory comparison card ─────────────────────────────────────────────────
function cardMemory(mem, hailo) {
  let body = "";

  // Pi RAM
  const total = mem && mem.total, used = mem && mem.used, pct = (mem && mem.used_pct) || 0;
  const usedGb  = used  ? (used / GB).toFixed(2) : "?";
  const totalGb = total ? (total / GB).toFixed(1)  : "?";
  body += `<div class="memblock">
    <div class="memtop">
      <div class="memname">Pi RAM <span class="chip-tag tag-pi">LPDDR4X</span></div>
      <span class="mempct">${pct}%</span>
    </div>
    <div class="memtop" style="margin-bottom:6px;">
      <span class="memval">${usedGb} GB used</span>
      <span class="mu" style="font-size:12px;font-family:monospace;">${usedGb} / ${totalGb} GB</span>
    </div>
    <div class="track"><div class="fill ${barClass(pct)}" style="width:${Math.min(pct,100)}%"></div></div>
  </div>`;

  // Hailo onboard LPDDR5X
  const hNetworks = (hailo && hailo.loaded_networks) || 0;
  const hInferring = hNetworks > 0;
  const hNames = (hailo && hailo.network_names) || [];
  const hNote = hInferring
    ? hNames.join(", ") || `${hNetworks} process${hNetworks > 1 ? "es" : ""}`
    : "idle · no active inference";
  body += `<div class="memblock">
    <div class="memtop">
      <div class="memname">Hailo-10H DRAM <span class="chip-tag tag-hailo">LPDDR5X</span></div>
      <span class="mempct" style="color:var(--muted);font-size:11px;">utilization N/A</span>
    </div>
    <div class="memtop" style="margin-bottom:6px;">
      <span class="memval acc">8.00 GB total</span>
      <span class="mu" style="font-size:12px;">${hNote}</span>
    </div>
    <div class="track"><div class="fill fill-ghost" style="width:100%"></div></div>
  </div>`;

  // Swap
  const st = mem && mem.swap_total, su = mem && mem.swap_used, sp = (mem && mem.swap_pct) || 0;
  if (st) {
    const suMb  = su  ? (su / MB).toFixed(0) : "0";
    const stGb  = (st / GB).toFixed(1);
    body += `<div class="memblock">
      <div class="memtop">
        <div class="memname">Swap <span class="chip-tag tag-swap">zram</span></div>
        <span class="mempct">${sp}%</span>
      </div>
      <div class="memtop" style="margin-bottom:6px;">
        <span class="memval">${suMb} MB used</span>
        <span class="mu" style="font-size:12px;font-family:monospace;">${suMb} MB / ${stGb} GB</span>
      </div>
      <div class="track"><div class="fill ${barClass(sp)}" style="width:${Math.min(sp,100)}%"></div></div>
    </div>`;
  }

  return `<div class="card span2"><div class="card-title"><span class="ti">🧠</span> Memory</div>${body}</div>`;
}

// ── CPU card ───────────────────────────────────────────────────────────────
function cardCpu(cpu) {
  let body = "";

  if (cpu.freq_mhz != null) body += row("Frequency", `<span class="info">${Math.round(cpu.freq_mhz)} MHz</span>`);
  if (cpu.core_v != null)   body += row("Core Voltage", `${cpu.core_v.toFixed(4)} V`);

  if (cpu.throttle_ok != null) {
    if (cpu.throttle_ok) {
      body += row("Throttle", `<span class="ok">✓ OK</span>`);
    } else {
      const flagsHtml = (cpu.throttle_flags || []).map(f => `<span class="flag">${f}</span>`).join("");
      body += row("Throttle", `<div class="flags">${flagsHtml || '<span class="err">Issues</span>'}</div>`);
    }
  }

  if (cpu.load_1 != null) {
    const lc = cpu.load_1 >= 4 ? "err" : cpu.load_1 >= 2 ? "warn" : "ok";
    body += row("Load avg",
      `<span class="${lc}">${cpu.load_1.toFixed(2)}</span> / ${cpu.load_5.toFixed(2)} / ${cpu.load_15.toFixed(2)}`);
  }

  return `<div class="card"><div class="card-title"><span class="ti">💻</span> Pi CPU</div>${body}</div>`;
}

// ── Fan card ───────────────────────────────────────────────────────────────
function cardFan(fan) {
  const rpm = fan && fan.rpm != null ? fan.rpm : null;
  const spinning = rpm != null && rpm > 0;
  const cls  = rpm == null ? "mu" : rpm > 3000 ? "warn" : spinning ? "ok" : "mu";
  const disp = rpm != null ? rpm.toLocaleString() : "—";
  const sub  = spinning ? "RPM" : "Off / Idle";
  return `<div class="card" style="text-align:center;">
    <div class="card-title" style="justify-content:center;"><span class="ti">${spinning?"🌀":"💨"}</span> Fan</div>
    <div class="fan-center">
      <div class="fan-ring ${spinning?"spinning":""}">
        <span class="fan-icon">${spinning?"🌀":"💤"}</span>
        <span class="fan-label">fan</span>
      </div>
      <div style="margin-top:4px;">
        <span class="bignum ${cls}" style="font-size:28px;">${disp}</span>
        <div style="font-size:11px;color:var(--muted);margin-top:2px;">${sub}</div>
      </div>
    </div>
  </div>`;
}

// ── System card ────────────────────────────────────────────────────────────
function cardSystem(sys) {
  let body = "";
  if (sys.model)     body += row("Model",  sys.model.replace("Raspberry Pi ","Pi "), "info");
  if (sys.uptime)    body += row("Uptime", `<span class="ok">${sys.uptime}</span>`);
  if (sys.cpu_count) body += row("CPUs",   sys.cpu_count);

  if (sys.disk_total != null) {
    const used = (sys.disk_used / GB).toFixed(0);
    const tot  = (sys.disk_total / GB).toFixed(0);
    const pct  = sys.disk_pct;
    const cls  = barClass(pct);
    body += `<div style="margin-top:10px;">
      <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px;">
        <span class="label">Disk /</span>
        <span class="mu" style="font-family:monospace;">${used} / ${tot} GB · ${pct}%</span>
      </div>
      <div class="track"><div class="fill ${cls}" style="width:${pct}%"></div></div>
    </div>`;
  }

  return `<div class="card"><div class="card-title"><span class="ti">🖥</span> System</div>${body}</div>`;
}

// ── PCIe card ──────────────────────────────────────────────────────────────
function cardPcie(sys) {
  const devs = (sys && sys.pcie_devices) || [];
  let rows = devs.map(d => `<tr><td>${d.addr}</td><td>${d.desc}</td></tr>`).join("");
  if (!rows) rows = `<tr><td colspan="2" class="mu">No devices found</td></tr>`;
  return `<div class="card">
    <div class="card-title"><span class="ti">🔌</span> PCIe Devices</div>
    <table class="pcietable"><tbody>${rows}</tbody></table>
  </div>`;
}

// ── Networks card ──────────────────────────────────────────────────────────
function cardNetworks(hailo) {
  const count = (hailo && hailo.loaded_networks) || 0;
  const names = (hailo && hailo.network_names) || [];
  let body = "";
  if (count > 0) {
    body = names.map(n => `<div class="net-tag">◈ ${n}</div>`).join("");
  } else {
    body = `<div style="color:var(--muted);font-size:13px;padding:4px 0;">No active inference detected</div>
            <div style="color:var(--muted);font-size:11px;margin-top:6px;">Detected via <code style="color:var(--cyan)">lsof /dev/hailo0</code>. Utilization monitoring not available on Hailo-10H SoC.</div>`;
  }
  const cntBadge = `<span class="ct-badge" style="background:rgba(92,168,255,0.12);color:var(--blue);">${count}</span>`;
  return `<div class="card">
    <div class="card-title"><span class="ti">🧩</span> Loaded Networks ${cntBadge}</div>
    ${body}
  </div>`;
}

// ── Inference perf card ────────────────────────────────────────────────────
function cardPerf(s) {
  const fps    = s && s.fps    != null ? s.fps    : null;
  const avgfps = s && s.avg_fps != null ? s.avg_fps : null;
  const drop   = s && s.drop_rate != null ? s.drop_rate : null;
  const ts     = s && s.ts != null ? s.ts : null;

  const stale  = ts != null && (Date.now() / 1000 - ts) > 10;
  const active = fps != null && !stale;

  let body = "";
  if (active) {
    const fpsCls = fps >= 25 ? "ok" : fps >= 15 ? "warn" : "err";
    body += `<div style="margin-bottom:10px;">
      <div class="bignum ${fpsCls}">${fps.toFixed(1)}<span class="unit">fps</span></div>
      <div class="bignumsub">current · avg ${avgfps != null ? avgfps.toFixed(1) : "—"} fps</div>
    </div>`;
    if (drop != null && drop > 0) {
      body += row("Drop rate", `<span class="warn">${(drop * 100).toFixed(1)}%</span>`);
    } else if (drop != null) {
      body += row("Drop rate", `<span class="ok">0%</span>`);
    }
  } else if (fps == null) {
    body = `<div class="row"><span class="mu" style="font-size:12px;">Sentinel not reachable — inference stats unavailable</span></div>`;
  } else {
    body = `<div class="row"><span class="mu" style="font-size:12px;">No recent FPS data (pipeline idle?)</span></div>`;
  }

  return `<div class="card"><div class="card-title"><span class="ti">⚡</span> Inference Performance</div>${body}</div>`;
}

// ── Render all ─────────────────────────────────────────────────────────────
let pollCount = 0;

async function refresh() {
  try {
    const res = await fetch("/api/stats");
    if (!res.ok) throw new Error(res.status);
    const d = await res.json();
    pollCount++;
    document.getElementById("pcnt").textContent = pollCount;

    const h   = d.hailo      || {};
    const hp  = d.hailo_perf || {};
    const cpu = d.cpu        || {};
    const mem = d.memory     || {};
    const fan = d.fan        || {};
    const sys = d.system     || {};
    const sen = d.sentinel   || {};

    // Update header badge
    const badge = document.getElementById("hbadge");
    const btext = document.getElementById("hbtext");
    if (h.firmware_ok) {
      badge.className = "badge badge-online";
      badge.querySelector(".dot").className = "dot dot-green";
      btext.textContent = h.architecture || "Online";
    } else if (h.present) {
      badge.className = "badge badge-warn";
      badge.querySelector(".dot").className = "dot dot-yellow";
      btext.textContent = "FW Not Ready";
    } else {
      badge.className = "badge badge-offline";
      badge.querySelector(".dot").className = "dot dot-red";
      btext.textContent = "Not Detected";
    }

    const ago = Math.round(Date.now() / 1000 - d.ts);
    document.getElementById("lupdate").textContent = `Updated ${ago}s ago`;

    // Build grid
    document.getElementById("grid").innerHTML =
      cardHailo(h)            +  // col 1
      cardTemp(h, cpu, hp)    +  // col 2
      cardPower(h, hp)        +  // col 3
      cardMemory(mem, h)      +  // span2 (cols 1-2)
      cardFan(fan)            +  // col 3
      cardCpu(cpu)            +  // col 1
      cardSystem(sys)         +  // col 2
      cardPcie(sys)           +  // col 3
      cardNetworks(h)         +  // col 1 (wraps)
      cardPerf(sen);             // col 2
    applyRevealState();

  } catch (e) {
    console.warn("Stats fetch error:", e);
  }
}

refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""

# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    host = "0.0.0.0"
    print(f"Hailo Stats dashboard → http://{host}:{port}/")
    app.run(host=host, port=port, debug=False, threaded=True)
