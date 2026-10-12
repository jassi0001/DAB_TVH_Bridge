#!/usr/bin/python3
"""Sundtek tuner watchdog (port 8094).

The Sundtek DAB+/FM stick occasionally stops delivering data: rtspd (port
22000) still answers, but streams stay empty, scans do nothing and the tuner
never locks. This service

  * checks every CHECK_INTERVAL seconds whether the stick is present on the
    USB bus, mediasrv sees a tuner, rtspd answers and DAB+/FM streams in
    TVHeadend actually receive data (a subscription without a single byte for
    STARVE_SECONDS counts as starving),
  * recovers automatically with an escalation: (1) restart rtspd only,
    (2) restart mediasrv + rtspd, (3) USB unbind/bind of the stick plus
    restart, (4) reboot of the box (only when auto_reboot is switched on).
    Every step ends with a real stream probe; restarting mediasrv makes the
    stick moody, so the cheap rtspd restart comes first,
  * saves a diagnostic snapshot for every incident
    (/storage/dab-tvh-bridge/tuner_incidents/) and a one-line metrics sample
    per minute (tuner_metrics.log) to find the cause of the crashes,
  * offers a small API for Home Assistant (status, manual restart, switches).

It never probes the tuner with a test stream (that would steal the shared
tuner from a listener) and respects the shared scan lock
(tuner_scan.lock, see ARCHITECTURE.md): while a DAB+/FM scan holds it, no
recovery is started.
"""

import base64
import fcntl
import glob
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BRIDGE_BASE = "/storage/dab-tvh-bridge"
LISTEN_PORT = 8094
SUNDTEK_BASE = "http://127.0.0.1:22000"
DAB_DEVICE_ID = 0

LOCK_PATH = f"{BRIDGE_BASE}/tuner_scan.lock"
STATE_PATH = f"{BRIDGE_BASE}/tuner_watchdog_state.json"
LOG_PATH = f"{BRIDGE_BASE}/tuner_watchdog.log"
METRICS_PATH = f"{BRIDGE_BASE}/tuner_metrics.log"
INCIDENT_DIR = f"{BRIDGE_BASE}/tuner_incidents"

SUNDTEK_USB_ID = ("2659", "1602")
TVH_HOST = "127.0.0.1"
TVH_PORT = 9981
TVH_NETWORKS = ("/DAB+ v2/", "/FM v2/")

CHECK_INTERVAL = 15          # seconds between checks
FAIL_THRESHOLD = 3           # consecutive failed checks before recovery (~45 s)
STARVE_SECONDS = 60          # subscription without any data for this long = starving
HEALTHY_RESET = 300          # healthy for this long -> escalation starts over
MAX_AUTO_PER_HOUR = 4
REBOOT_MIN_INTERVAL = 6 * 3600
METRICS_INTERVAL = 60
METRICS_KEEP_LINES = 3000
INCIDENTS_KEEP = 30


def _load_credentials(path=f"{BRIDGE_BASE}/credentials.env"):
    """TVHeadend login from credentials.env (same file as the bridges)."""
    creds = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    creds[key.strip()] = value.strip().strip("'\"")
    except OSError:
        print(f"WARNING: {path} not readable - TVHeadend checks disabled")
    return creds.get("TVH_USER"), creds.get("TVH_PASS")


TVH_USER, TVH_PASS = _load_credentials()

_lock = threading.RLock()
_status = {
    "state": "starting",
    "healthy": None,
    "problem": None,
    "consecutive_failures": 0,
    "last_check": None,
    "last_ok": None,
    "checks": {},
}
_config = {"auto_recover": True, "auto_reboot": False}
_history = []          # [{at, level, reason, success, manual}]
_escalation = 0        # unsuccessful automatic attempts since the last healthy period
_recovering = threading.Lock()


# --- persistence / logging ---------------------------------------------------

def _log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        if os.path.getsize(LOG_PATH) > 1_000_000:
            with open(LOG_PATH, encoding="utf-8") as fh:
                tail = fh.readlines()[-2000:]
            with open(LOG_PATH, "w", encoding="utf-8") as fh:
                fh.writelines(tail)
    except OSError:
        pass


def _load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        _config.update({k: bool(v) for k, v in data.get("config", {}).items() if k in _config})
        _history.extend(data.get("history", [])[-100:])
    except (OSError, ValueError):
        pass


def _save_state():
    try:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"config": _config, "history": _history[-100:]}, fh)
        os.replace(tmp, STATE_PATH)
    except OSError:
        pass


# --- helpers: system / sundtek / tvheadend ----------------------------------------

def _run(cmd, timeout=20):
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (res.stdout + res.stderr).strip()
    except Exception as e:  # noqa: BLE001 - diagnostics must never raise
        return f"<{cmd[0]} failed: {e}>"


def _sundtek_usb_dir():
    for d in glob.glob("/sys/bus/usb/devices/*"):
        try:
            with open(f"{d}/idVendor") as v, open(f"{d}/idProduct") as p:
                if (v.read().strip(), p.read().strip()) == SUNDTEK_USB_ID:
                    return d
        except OSError:
            continue
    return None


def _sundtek_post(cmd_body, timeout=5):
    req = urllib.request.Request(
        f"{SUNDTEK_BASE}/servercmd.xhx",
        data=cmd_body.encode(),
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode(errors="replace")


def _tvh_get(path, timeout=8):
    if not TVH_USER:
        return None
    auth = base64.b64encode(f"{TVH_USER}:{TVH_PASS}".encode()).decode()
    req = urllib.request.Request(
        f"http://{TVH_HOST}:{TVH_PORT}{path}", headers={"Authorization": f"Basic {auth}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception:  # noqa: BLE001
        return None


def _pids(name):
    out = []
    for d in glob.glob("/proc/[0-9]*"):
        try:
            with open(f"{d}/comm") as fh:
                if fh.read().strip() == name:
                    out.append(int(os.path.basename(d)))
        except OSError:
            continue
    return out


def _proc_info(pid):
    info = {"pid": pid}
    try:
        with open(f"/proc/{pid}/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    info["rss_kb"] = int(line.split()[1])
                elif line.startswith("Threads:"):
                    info["threads"] = int(line.split()[1])
                elif line.startswith("State:"):
                    info["state"] = line.split()[1]
        info["fds"] = len(os.listdir(f"/proc/{pid}/fd"))
    except OSError:
        pass
    return info


def _tcp_states(port=22000):
    """Count TCP sockets by state for the rtspd port (leak / pile-up hint)."""
    names = {"01": "established", "06": "time_wait", "08": "close_wait", "0A": "listen"}
    hexport = f":{port:04X}"
    counts = {}
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path) as fh:
                next(fh)
                for line in fh:
                    f = line.split()
                    if f[1].endswith(hexport) or f[2].endswith(hexport):
                        key = names.get(f[3], f[3])
                        counts[key] = counts.get(key, 0) + 1
        except (OSError, StopIteration):
            continue
    return counts


def _starving_subscriptions():
    """DAB+/FM subscriptions that have not received a single byte for too long."""
    data = _tvh_get("/api/status/subscriptions")
    if data is None:
        return None
    now = time.time()
    out = []
    for e in data.get("entries", []):
        service = e.get("service") or ""
        if not any(n in service for n in TVH_NETWORKS):
            continue
        age = now - (e.get("start") or now)
        if age > STARVE_SECONDS and not e.get("total_in") and not e.get("in"):
            out.append({"channel": e.get("channel"), "client": e.get("client"),
                        "age_s": int(age), "state": e.get("state")})
    return out


# --- checks --------------------------------------------------------------------

def run_checks():
    checks = {}
    usb_dir = _sundtek_usb_dir()
    checks["usb_present"] = usb_dir is not None

    try:
        with urllib.request.urlopen(f"{SUNDTEK_BASE}/dab.m3u", timeout=5) as resp:
            resp.read(256)
        checks["rtspd_ok"] = True
    except Exception as e:  # noqa: BLE001
        checks["rtspd_ok"] = False
        checks["rtspd_error"] = str(e)[:80]

    try:
        arr = json.loads(_sundtek_post(f"cmd=scanstat&deviceid={DAB_DEVICE_ID}"))
        checks["mediasrv_ok"] = isinstance(arr, list) and len(arr) >= 13
    except Exception as e:  # noqa: BLE001
        checks["mediasrv_ok"] = False
        checks["mediasrv_error"] = str(e)[:80]

    starving = _starving_subscriptions()
    checks["starving"] = starving or []
    checks["tvh_reachable"] = starving is not None

    if not checks["usb_present"]:
        problem = "usb_missing"
    elif not checks["mediasrv_ok"]:
        problem = "mediasrv_no_tuner"
    elif not checks["rtspd_ok"]:
        problem = "rtspd_unresponsive"
    elif checks["starving"]:
        problem = "stream_starving"
    else:
        problem = None
    return problem, checks


def metrics_sample():
    sample = {
        "ts": int(time.time()),
        "usb": _sundtek_usb_dir() is not None,
        "mediasrv": [_proc_info(p) for p in _pids("mediasrv")],
        "rtspd": [_proc_info(p) for p in _pids("rtspd")],
        "tcp22000": _tcp_states(),
        "dev_nodes": len(glob.glob("/dev/dab*")) + len(glob.glob("/dev/radio*")),
        "throttled": _run(["vcgencmd", "get_throttled"], 3).replace("throttled=", ""),
    }
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as fh:
            sample["temp_c"] = round(int(fh.read()) / 1000, 1)
    except (OSError, ValueError):
        pass
    subs = _tvh_get("/api/status/subscriptions")
    if subs is not None:
        sample["subs"] = [
            {"ch": e.get("channel"), "state": e.get("state"), "in": e.get("total_in"),
             "net": "DAB" if "/DAB+ v2/" in (e.get("service") or "") else
                    "FM" if "/FM v2/" in (e.get("service") or "") else "other"}
            for e in subs.get("entries", [])
        ]
    return sample


def _write_metrics(sample):
    try:
        with open(METRICS_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(sample, separators=(",", ":")) + "\n")
        if os.path.getsize(METRICS_PATH) > 1_500_000:
            with open(METRICS_PATH, encoding="utf-8") as fh:
                tail = fh.readlines()[-METRICS_KEEP_LINES:]
            with open(METRICS_PATH, "w", encoding="utf-8") as fh:
                fh.writelines(tail)
    except OSError:
        pass


def save_incident(reason, checks):
    try:
        os.makedirs(INCIDENT_DIR, exist_ok=True)
        path = f"{INCIDENT_DIR}/{time.strftime('%Y%m%d_%H%M%S')}_{reason}.txt"
        quiet = r"checking|GOT DELSYS|comparing|iterating|poller remove|DEVICE MODE"
        parts = [
            f"reason: {reason}",
            f"time: {time.strftime('%Y-%m-%d %H:%M:%S')}  uptime: {_run(['uptime'])}",
            "checks: " + json.dumps(checks, indent=1),
            "metrics: " + json.dumps(metrics_sample(), indent=1),
            "--- usb / dev nodes", _run(["lsusb"]), _run(["sh", "-c", "ls -l /dev/dab* /dev/radio* 2>&1"]),
            "--- dmesg (last 60)", _run(["sh", "-c", "dmesg | tail -60"]),
            "--- journal rtspd", _run(["sh", "-c", f"journalctl -u sundtek-rtspd --no-pager | grep -vE '{quiet}' | tail -40"]),
            "--- journal mediasrv", _run(["sh", "-c", "journalctl -u sundtek-mediasrv --no-pager | tail -30"]),
            "--- journal tvheadend (iptv/subscription)",
            _run(["sh", "-c", "journalctl -u service.tvheadend43 --no-pager | grep -iE 'iptv|spawn|subscription' | tail -30"]),
            "--- ps", _run(["sh", "-c", "ps | grep -E 'mediasrv|rtspd|ffmpeg' | grep -v grep"]),
            "--- last metrics lines", _run(["sh", "-c", f"tail -5 {METRICS_PATH}"]),
        ]
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(parts) + "\n")
        files = sorted(glob.glob(f"{INCIDENT_DIR}/*.txt"))
        for old in files[:-INCIDENTS_KEEP]:
            os.remove(old)
        return path
    except OSError as e:
        _log(f"incident snapshot failed: {e}")
        return None


# --- recovery ------------------------------------------------------------------------

def _systemctl(action, *units):
    return _run(["systemctl", action, *units], timeout=40)


def _wait_mediasrv_ready(timeout=40, stable=2):
    """Wait until mediasrv reports the tuner (scanstat has 13+ fields) on
    `stable` consecutive polls. The tuner vanishes from mediasrv for a few
    seconds after every mediasrv AND rtspd (re)start, and rtspd reads the
    device table only once at startup - starting or probing too early leaves
    rtspd deaf (404 or empty streams)."""
    end = time.time() + timeout
    good = 0
    while time.time() < end:
        try:
            arr = json.loads(_sundtek_post(f"cmd=scanstat&deviceid={DAB_DEVICE_ID}", timeout=4))
            good = good + 1 if isinstance(arr, list) and len(arr) >= 13 else 0
        except Exception:  # noqa: BLE001
            good = 0
        if good >= stable:
            return True
        time.sleep(2)
    return False


_last_good_freq = [None]


def _freq_of(url):
    m = re.search(r"freq=(\d+)", url)
    return m.group(1) if m else None


def probe_stream(timeout=20):
    """Pull a few KB from a real DAB+ stream. Only used during a recovery (it
    takes the shared tuner), never by the periodic check.

    A cold tune can take 10+ s before the first byte, and rtspd answers 404 for
    a few seconds after a session was closed (tuner still being released), so
    the probe is patient, prefers the last frequency that worked and then tries
    the ensembles with the most stations first."""
    try:
        with urllib.request.urlopen(f"{SUNDTEK_BASE}/dab.m3u", timeout=5) as resp:
            lines = resp.read().decode(errors="replace").splitlines()
    except Exception as e:  # noqa: BLE001
        return False, f"playlist: {str(e)[:60]}"
    urls = [l for l in lines if l.startswith("http") and "delsys=15" in l]
    if not urls:
        return False, "no DAB+ station in rtspd playlist"
    by_freq = {}
    for u in urls:
        by_freq.setdefault(_freq_of(u), []).append(u)
    order = sorted(by_freq, key=lambda f: (f != _last_good_freq[0], -len(by_freq[f])))
    last = "no attempt"
    for freq in order[:2]:
        url = by_freq[freq][0]
        for _ in range(2):
            try:
                with urllib.request.urlopen(url, timeout=timeout) as resp:
                    data = resp.read(2048)
                if data:
                    _last_good_freq[0] = freq
                    return True, f"{len(data)} bytes on {freq}"
                last = f"empty on {freq}"
                break
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code} on {freq}"
                time.sleep(8)          # tuner still being released - retry once
            except Exception as e:  # noqa: BLE001
                last = f"{str(e)[:50]} on {freq}"
                time.sleep(10)
                break
    return False, last


def _start_rtspd_and_probe():
    """(Re)start rtspd once mediasrv is ready, wait for the tuner to settle again,
    then probe a real stream. Up to three rounds."""
    info = "not run"
    for attempt in (1, 2, 3):
        ready = _wait_mediasrv_ready()
        if not ready:
            _log(f"  attempt {attempt}: mediasrv does not report the tuner")
        _systemctl("restart", "sundtek-rtspd.service")
        time.sleep(4)
        _wait_mediasrv_ready()
        time.sleep(6)
        ok, info = probe_stream()
        _log(f"  attempt {attempt}: stream probe {'ok' if ok else 'FAILED'} ({info})")
        if ok:
            return True, info
    return False, info


def _restart_rtspd_only():
    _log("recovery: restarting sundtek-rtspd only (mediasrv untouched)")
    return _start_rtspd_and_probe()


def _restart_services():
    _log("recovery: restarting sundtek-mediasrv, waiting for the tuner, then sundtek-rtspd")
    _systemctl("restart", "sundtek-mediasrv.service")
    time.sleep(3)
    return _start_rtspd_and_probe()


def _usb_rebind():
    _log("recovery: stopping services, USB unbind/bind of the Sundtek stick")
    _systemctl("stop", "sundtek-rtspd.service", "sundtek-mediasrv.service")
    time.sleep(2)
    d = _sundtek_usb_dir()
    if d:
        busid = os.path.basename(d)
        for action in ("unbind", "bind"):
            try:
                with open(f"/sys/bus/usb/drivers/usb/{action}", "w") as fh:
                    fh.write(busid)
            except OSError as e:
                _log(f"  usb {action} {busid} failed: {e}")
            time.sleep(3)
    else:
        _log("  stick not on the USB bus - nothing to rebind (needs a reboot or replug)")
    _systemctl("start", "sundtek-mediasrv.service")
    time.sleep(3)
    return _start_rtspd_and_probe()


def _reboot():
    _log("recovery: REBOOT of the box")
    try:
        os.sync()
    except OSError:
        pass
    subprocess.Popen(["sh", "-c", "sleep 3; reboot"])


LEVELS = {"rtspd": 1, "services": 2, "usb": 3, "reboot": 4}


def _acquire_scan_lock():
    fd = open(LOCK_PATH, "a")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fd.close()
        return None
    return fd


def _recoveries_in(seconds, auto_only=True):
    cutoff = time.time() - seconds
    return [h for h in _history if h["at"] >= cutoff and (not auto_only or not h.get("manual"))]


def recover(level_name, reason, manual=False):
    """Run one recovery step. Returns (started, message)."""
    if not _recovering.acquire(blocking=False):
        return False, "recovery already running"
    lock_fd = None
    try:
        lock_fd = _acquire_scan_lock()
        if lock_fd is None and level_name != "reboot":
            return False, "a DAB+/FM scan holds the tuner lock - not touching the tuner now"
        if level_name == "reboot":
            last = max((h["at"] for h in _history if h["level"] == "reboot"), default=0)
            if not manual and time.time() - last < REBOOT_MIN_INTERVAL:
                return False, "reboot suppressed (last reboot too recent)"

        _, before = run_checks()
        incident = save_incident(reason, before)
        entry = {"at": time.time(), "level": level_name, "reason": reason,
                 "success": None, "manual": manual, "incident": incident}
        _history.append(entry)
        _save_state()
        with _lock:
            _status["state"] = "recovering"

        probe = (False, "not run")
        if level_name == "rtspd":
            probe = _restart_rtspd_only()
        elif level_name == "services":
            probe = _restart_services()
        elif level_name == "usb":
            probe = _usb_rebind()
        elif level_name == "reboot":
            _reboot()
            return True, "rebooting"
        else:
            return False, f"unknown level {level_name}"

        problem, checks = run_checks()
        # success = the probe stream delivered data (a starving Kodi subscription
        # needs a moment to show data again, so it is not counted here)
        entry["success"] = bool(probe[0]) and problem in (None, "stream_starving")
        entry["probe"] = probe[1]
        _save_state()
        _log(f"recovery '{level_name}' finished: {'ok' if entry['success'] else 'NOT ok (' + str(problem) + ')'}")
        return True, "ok" if entry["success"] else f"still failing: {problem}"
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                lock_fd.close()
        _recovering.release()


def _next_auto_level():
    steps = ("rtspd", "services", "usb")
    if _escalation < len(steps):
        return steps[_escalation]
    return "reboot" if _config["auto_reboot"] else None


# --- main loop ------------------------------------------------------------------------

def watchdog_loop():
    global _escalation
    fails = 0
    healthy_since = time.time()
    last_metrics = 0.0
    gave_up_logged = False
    while True:
        try:
            if time.time() - last_metrics >= METRICS_INTERVAL:
                _write_metrics(metrics_sample())
                last_metrics = time.time()

            problem, checks = run_checks()
            now = time.time()
            with _lock:
                _status.update({"last_check": now, "checks": checks, "problem": problem,
                                "healthy": problem is None})
                if problem is None:
                    _status["last_ok"] = now
            if problem is None:
                fails = 0
                gave_up_logged = False
                if now - healthy_since >= HEALTHY_RESET:
                    _escalation = 0
                with _lock:
                    _status["state"] = "ok"
                    _status["consecutive_failures"] = 0
            else:
                if fails == 0:
                    healthy_since = now
                    _log(f"problem detected: {problem} {json.dumps(checks)[:300]}")
                fails += 1
                with _lock:
                    _status["state"] = "degraded"
                    _status["consecutive_failures"] = fails
                if fails >= FAIL_THRESHOLD and _config["auto_recover"]:
                    level = _next_auto_level()
                    if level is None:
                        if not gave_up_logged:
                            _log("automatic recovery exhausted (reboot not enabled) - waiting for manual action")
                            gave_up_logged = True
                        with _lock:
                            _status["state"] = "failed"
                    elif len(_recoveries_in(3600)) >= MAX_AUTO_PER_HOUR:
                        if not gave_up_logged:
                            _log("too many automatic recoveries in the last hour - pausing")
                            gave_up_logged = True
                        with _lock:
                            _status["state"] = "failed"
                    else:
                        started, msg = recover(level, problem)
                        if started:
                            _escalation += 1
                            fails = 0
                            healthy_since = time.time()
                        else:
                            _log(f"recovery not started: {msg}")
        except Exception as e:  # noqa: BLE001 - the watchdog itself must not die
            _log(f"watchdog loop error: {e!r}")
        time.sleep(CHECK_INTERVAL)


# --- HTTP API ----------------------------------------------------------------------------

def status_payload():
    with _lock:
        out = dict(_status)
    day = _recoveries_in(86400, auto_only=False)
    out["auto_recover"] = _config["auto_recover"]
    out["auto_reboot"] = _config["auto_reboot"]
    out["recoveries_24h"] = len(day)
    out["recoveries_total"] = len(_history)
    out["last_recovery"] = _history[-1] if _history else None
    out["escalation_step"] = _escalation
    out["uptime_s"] = int(float(open("/proc/uptime").read().split()[0]))
    return out


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/tuner/status":
            self._send(200, status_payload())
        elif url.path == "/tuner/log":
            n = int(urllib.parse.parse_qs(url.query).get("lines", ["50"])[0])
            try:
                with open(LOG_PATH, encoding="utf-8") as fh:
                    lines = fh.readlines()[-max(1, min(n, 500)):]
            except OSError:
                lines = []
            self._send(200, {"lines": [l.rstrip("\n") for l in lines]})
        elif url.path == "/tuner/incidents":
            files = sorted(glob.glob(f"{INCIDENT_DIR}/*.txt"))
            self._send(200, {"incidents": [os.path.basename(f) for f in files[-20:]]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        url = urllib.parse.urlsplit(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        if url.path == "/tuner/restart":
            level = q.get("level", "rtspd")
            if level not in LEVELS:
                self._send(400, {"error": "level must be rtspd|services|usb|reboot"})
                return
            threading.Thread(target=lambda: recover(level, f"manual_{level}", manual=True),
                             daemon=True).start()
            self._send(202, {"started": True, "level": level})
        elif url.path == "/tuner/config":
            for key in ("auto_recover", "auto_reboot"):
                if key in q:
                    _config[key] = q[key].lower() in ("1", "true", "on", "yes")
            _save_state()
            _log(f"config changed: {_config}")
            self._send(200, dict(_config))
        else:
            self._send(404, {"error": "not found"})


def main():
    os.makedirs(BRIDGE_BASE, exist_ok=True)
    _load_state()
    _log(f"tuner watchdog starting, port {LISTEN_PORT}, config {_config}")
    threading.Thread(target=watchdog_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
