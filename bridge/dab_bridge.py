#!/usr/bin/env python3
import base64
import ctypes
import glob
import json
import os
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SUNDTEK_BASE = "http://127.0.0.1:22000"
DAB_DEVICE_ID = 0
DAB_DELSYS = 15  # Sundtek's delivery-system enum value for DAB/DAB+ on this stick

# Direct tuner access via Sundtek's own SDK (libmcsimple.so, ships with the
# driver under /storage/dabplus/opt/) instead of the HTTP servercmd.xhx API -
# used only for read-only signal/ensemble info, which the HTTP API doesn't
# expose at all. Constants/structs from
# /storage/dabplus/opt/include/mediacmds.h. See TVHEADEND_QUIRKS.md for why
# this is deliberately NOT used for anything beyond read-only queries (scan
# control etc. stay on the proven HTTP API).
MCSIMPLE_LIB = "/storage/dabplus/opt/lib/libmcsimple.so"
DAB_GET_TUNER = 68
DAB_HAS_LOCK = 0x01
DAB_GET_ENSEMBLE_INFO = 128
FFMPEG_BIN = "ffmpeg"
AUDIO_BITRATE = "192k"
SERVICE_PROVIDER = "Radio channels"
LISTEN_PORT = 8081
EXCLUDE_FILE = "/storage/dab-tvh-bridge/exclude.txt"
RENAME_FILE = "/storage/dab-tvh-bridge/renames.txt"
RESCAN_LOG = "/storage/dab-tvh-bridge/dab.log"

# Per-location saved station lists (mirrors the DVB-T bridge's per-location
# networks) - lets a previously visited location's list be reactivated
# instantly, without a fresh scan. Purely additive: the GPS-triggered
# /dab/rescan flow never names a location and always clears any active one,
# so it keeps working exactly as before and always wins with fresh data.
DAB_LOCATIONS_DIR = "/storage/dab-tvh-bridge/locations"
DAB_ACTIVE_LOCATION_FILE = "/storage/dab-tvh-bridge/active_location.txt"

TVH_HOST = "127.0.0.1"
TVH_PORT = 9981
TVH_USER = "claude"
TVH_PASS = "claude"
TVH_NETWORK_NAME = "DAB+"
MAPPER_POLL_SECONDS = 30
MAPPER_SETTINGS = {
    "check_availability": False,
    "encrypted": False,
    "merge_same_name": True,
    "merge_same_name_fuzzy": True,
    "tidy_channel_name": True,
    "type_tags": False,
    "provider_tags": False,
    "network_tags": False,
}
# TVHeadend channel tag this box already has set up; matched by name so a
# manual retag/recreate in the UI doesn't break this.
RADIO_CHANNELS_TAG_NAME = "Radio channels"


def _tvh_req(path, data=None, timeout=15):
    auth = base64.b64encode(f"{TVH_USER}:{TVH_PASS}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}"}
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        r = urllib.request.Request(f"http://{TVH_HOST}:{TVH_PORT}{path}", data=body, headers=headers, method="POST")
    else:
        r = urllib.request.Request(f"http://{TVH_HOST}:{TVH_PORT}{path}", headers=headers)
    return urllib.request.urlopen(r, timeout=timeout)


def map_unmapped_dab_services():
    """Finds DAB+ services without a mapped channel and maps them, merging
    same-named services (from SFN simulcasts) into a single channel."""
    with _tvh_req("/api/mpegts/service/grid?start=0&limit=500") as resp:
        grid = json.loads(resp.read())
    unmapped = [
        e["uuid"] for e in grid["entries"]
        if e.get("network") == TVH_NETWORK_NAME and not e.get("channel")
    ]
    if not unmapped:
        return 0
    node = {"services": unmapped, **MAPPER_SETTINGS}
    with _tvh_req("/api/service/mapper/save", data={"node": json.dumps(node)}):
        pass

    # DAB+ streams carry no real EPG data (synthetic ffmpeg-piped audio, no
    # EIT table) - TVHeadend's "auto EPG source" just wastes cycles searching
    # for a name match, so disable it on the channels this pass just created.
    with _tvh_req("/api/mpegts/service/grid?start=0&limit=500") as resp:
        grid_after = json.loads(resp.read())
    new_channel_uuids = sorted({
        c for e in grid_after["entries"]
        if e["uuid"] in unmapped
        for c in e.get("channel", [])
    })
    if new_channel_uuids:
        epgauto_node = json.dumps([{"uuid": u, "epgauto": False} for u in new_channel_uuids])
        with _tvh_req("/api/idnode/save", data={"node": epgauto_node}):
            pass

    return len(unmapped)


def disable_dab_mux_epg():
    """Muxes default to EPG-Abfrage "Aktivieren (auto)" - separate from and
    in addition to the per-channel epgauto flag above. Same reasoning: DAB+
    muxes never carry real EPG data, so leave this off for any mux (existing
    or newly (re)scanned) where it's still on."""
    with _tvh_req("/api/mpegts/mux/grid?start=0&limit=500") as resp:
        grid = json.loads(resp.read())
    stale = [e["uuid"] for e in grid["entries"] if e.get("network") == TVH_NETWORK_NAME and e.get("epg") != 0]
    if not stale:
        return 0
    node = json.dumps([{"uuid": u, "epg": 0} for u in stale])
    with _tvh_req("/api/idnode/save", data={"node": node}):
        pass
    return len(stale)


def _get_tag_uuid(name):
    with _tvh_req("/api/channeltag/grid?start=0&limit=200") as resp:
        grid = json.loads(resp.read())
    match = next((t for t in grid["entries"] if t.get("name") == name), None)
    return match["uuid"] if match else None


def tag_dab_channels_as_radio():
    """Every DAB+ channel is radio by definition - apply the "Radio
    channels" tag to any that's missing it (covers ones mapped before this
    existed, not just newly mapped ones)."""
    tag_uuid = _get_tag_uuid(RADIO_CHANNELS_TAG_NAME)
    if not tag_uuid:
        return 0
    with _tvh_req("/api/mpegts/service/grid?start=0&limit=500") as resp:
        grid = json.loads(resp.read())
    channel_uuids = sorted({c for e in grid["entries"] if e.get("network") == TVH_NETWORK_NAME for c in e.get("channel", [])})
    with _tvh_req("/api/channel/grid?start=0&limit=2000") as resp:
        chgrid = json.loads(resp.read())
    by_uuid = {c["uuid"]: c for c in chgrid["entries"]}
    missing = [u for u in channel_uuids if u in by_uuid and tag_uuid not in (by_uuid[u].get("tags") or [])]
    if not missing:
        return 0
    node = json.dumps([{"uuid": u, "tags": sorted(set((by_uuid[u].get("tags") or []) + [tag_uuid]))} for u in missing])
    with _tvh_req("/api/idnode/save", data={"node": node}):
        pass
    return len(missing)


def mapper_loop():
    while True:
        try:
            n = map_unmapped_dab_services()
            if n:
                print(f"[mapper] mapped {n} newly scanned DAB+ service(s) to channels")
        except Exception as e:
            print(f"[mapper] error: {e}")
        try:
            n = disable_dab_mux_epg()
            if n:
                print(f"[mapper] disabled EPG scan on {n} DAB+ mux(es)")
        except Exception as e:
            print(f"[mapper] error: {e}")
        try:
            n = tag_dab_channels_as_radio()
            if n:
                print(f"[mapper] tagged {n} DAB+ channel(s) as Radio channels")
        except Exception as e:
            print(f"[mapper] error: {e}")
        time.sleep(MAPPER_POLL_SECONDS)


def _sundtek_post(cmd_body, timeout=15):
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


def _device_serial():
    with urllib.request.urlopen(f"{SUNDTEK_BASE}/devices.json", timeout=10) as resp:
        devices = json.loads(resp.read())
    return devices[0][3]


class _DabTuner(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("signalstrength", ctypes.c_uint32),
        ("status", ctypes.c_uint32),
        ("rssi", ctypes.c_uint8),
        ("snr", ctypes.c_uint8),
        ("fic_quality", ctypes.c_uint8),
        ("cnr", ctypes.c_uint8),
    ]


class _DabEnsembleInfo(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("global_id", ctypes.c_uint8),
        ("lang", ctypes.c_uint8),
        ("charset_id", ctypes.c_uint8),
        ("label", ctypes.c_char * 17),
    ]


_mcsimple = None


def _get_mcsimple():
    global _mcsimple
    if _mcsimple is None:
        lib = ctypes.CDLL(MCSIMPLE_LIB)
        lib.net_open.restype = ctypes.c_int
        lib.net_open.argtypes = [ctypes.c_char_p, ctypes.c_int]
        lib.net_ioctl.restype = ctypes.c_int
        lib.net_ioctl.argtypes = [ctypes.c_int, ctypes.c_uint, ctypes.c_void_p]
        lib.net_close.restype = ctypes.c_int
        lib.net_close.argtypes = [ctypes.c_int]
        _mcsimple = lib
    return _mcsimple


def get_dab_signal_info():
    """Ensemble name + tuner lock/signal quality, read directly via the SDK
    (libmcsimple.so) rather than the HTTP API, which doesn't expose this at
    all. /dev/dabN device nodes accumulate across scans/tunes (stale ones
    fail to open) - try the most recently touched one first."""
    lib = _get_mcsimple()
    candidates = sorted(glob.glob("/dev/dab*"), key=lambda p: os.stat(p).st_mtime, reverse=True)
    for device in candidates:
        fd = lib.net_open(device.encode(), 0)
        if fd < 0:
            continue
        try:
            tuner = _DabTuner()
            lib.net_ioctl(fd, DAB_GET_TUNER, ctypes.byref(tuner))
            ens = _DabEnsembleInfo()
            lib.net_ioctl(fd, DAB_GET_ENSEMBLE_INFO, ctypes.byref(ens))
            return {
                "device": device,
                "locked": bool(tuner.status & DAB_HAS_LOCK),
                "rssi": tuner.rssi,
                "snr": tuner.snr,
                "fic_quality": tuner.fic_quality,
                "cnr": tuner.cnr,
                "ensemble": ens.label.decode("ascii", "replace").strip(),
            }
        finally:
            lib.net_close(fd)
    return {"device": None, "locked": False, "rssi": None, "snr": None, "fic_quality": None, "cnr": None, "ensemble": None}


def get_dab_now_playing():
    """Which DAB+ channel(s) are currently being streamed, straight from
    TVHeadend's own subscription table - far more reliable than trying to
    read the current service back off the tuner itself (the Sundtek SDK's
    DAB_GET_DIGITAL_SERVICE_DATA/dynamic-label path never showed real data
    in testing, see TVHEADEND_QUIRKS.md). TVHeadend already knows exactly
    which channel each active client subscribed to, since it's the one that
    set up the subscription - no separate tuner-state guessing needed."""
    with _tvh_req("/api/status/subscriptions") as resp:
        data = json.loads(resp.read())
    channels = sorted({
        e["channel"] for e in data.get("entries", [])
        if f"/{TVH_NETWORK_NAME}/" in (e.get("service") or "") and e.get("channel")
    })
    return {"playing": channels}


def _latest_scan_prid():
    # cmd=providers returns [[name, serial, 0, [prid, delsys, label], [prid, delsys, label], ...]]
    # scan history isn't append-only - prid indices get renumbered/pruned by
    # mediasrv, so this must be re-resolved on every use rather than cached.
    data = json.loads(_sundtek_post("cmd=providers"))
    if not data:
        return None
    scans = [e for e in data[0] if isinstance(e, list) and len(e) == 3]
    if not scans:
        return None
    return max(scans, key=lambda e: e[0])[0]


_rescan_lock = threading.Lock()
_rescan_state = {"running": False, "returncode": None}


def _rescan_log(line):
    try:
        with open(RESCAN_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _perform_dab_scan():
    _rescan_log("restarting sundtek-mediasrv + sundtek-rtspd (required before a scan will persist) ...")
    subprocess.run(["systemctl", "restart", "sundtek-mediasrv.service"], check=True)
    time.sleep(3)
    subprocess.run(["systemctl", "restart", "sundtek-rtspd.service"], check=True)
    time.sleep(3)

    _rescan_log("triggering DAB+ scan and waiting for it to finish (up to 4 min) ...")
    _sundtek_post(f"scan=start&deviceid={DAB_DEVICE_ID}&mode={DAB_DELSYS}")

    last_freq = None
    stable = 0
    station_count = None
    for _ in range(48):
        time.sleep(5)
        try:
            arr = json.loads(_sundtek_post(f"cmd=scanstat&deviceid={DAB_DEVICE_ID}"))
        except Exception:
            continue
        freq = arr[6] if len(arr) > 6 else None
        station_count = arr[12] if len(arr) > 12 else station_count
        stable = stable + 1 if freq == last_freq else 0
        last_freq = freq
        if stable >= 3:
            break
    _rescan_log(f"scan settled at freq={last_freq}, {station_count} station(s) found")


def _restart_tvheadend():
    _rescan_log("restarting TVHeadend so it re-scans the DAB+ IPTV network ...")
    subprocess.run(["systemctl", "restart", "service.tvheadend43.service"], check=True)
    time.sleep(8)


def _run_rescan():
    try:
        _set_active_location(None)
        _rescan_log("[1/3] " + "restarting sundtek-mediasrv + sundtek-rtspd (required before a scan will persist) ...")
        _perform_dab_scan()
        _rescan_log("[2/3] scan done")
        _rescan_log("[3/3] " + "restarting TVHeadend so it re-scans the DAB+ IPTV network ...")
        _restart_tvheadend()

        n = len(prepare_stations(fetch_stations()))
        _rescan_log(f"done. station count now: {n}")
        _rescan_state["returncode"] = 0
    except Exception as e:
        _rescan_log(f"ERROR: {e}")
        _rescan_state["returncode"] = 1
    finally:
        _rescan_state["running"] = False


def _run_scan_new_location(name):
    try:
        _rescan_log(f"[1/3] scanning for DAB+ location '{name}' ...")
        _perform_dab_scan()

        stations = fetch_live_stations()
        save_location(name, stations)
        _set_active_location(name)
        _rescan_log(f"[2/3] saved {len(stations)} station(s) as location '{name}'")

        _rescan_log("[3/3] restarting TVHeadend so it re-scans the DAB+ IPTV network ...")
        _restart_tvheadend()

        n = len(prepare_stations(fetch_stations()))
        _rescan_log(f"done. station count now: {n}")
        _rescan_state["returncode"] = 0
    except Exception as e:
        _rescan_log(f"ERROR: {e}")
        _rescan_state["returncode"] = 1
    finally:
        _rescan_state["running"] = False


def _run_activate_location(name):
    try:
        data = load_location(name)  # raises if unknown - surfaces as an error below
        _set_active_location(name)
        _rescan_log(f"[1/2] activated saved location '{name}' ({len(data['stations'])} station(s), no rescan needed)")

        _rescan_log("[2/2] restarting TVHeadend so it re-scans the DAB+ IPTV network ...")
        _restart_tvheadend()

        n = len(prepare_stations(fetch_stations()))
        _rescan_log(f"done. station count now: {n}")
        _rescan_state["returncode"] = 0
    except Exception as e:
        _rescan_log(f"ERROR: {e}")
        _rescan_state["returncode"] = 1
    finally:
        _rescan_state["running"] = False


def _start_background(target, args):
    with _rescan_lock:
        if _rescan_state["running"]:
            return False
        _rescan_state["running"] = True
        _rescan_state["returncode"] = None
        try:
            open(RESCAN_LOG, "w").close()
        except OSError:
            pass
        threading.Thread(target=target, args=args, daemon=True).start()
        return True


def start_rescan():
    return _start_background(_run_rescan, ())


def start_scan_new_location(name):
    return _start_background(_run_scan_new_location, (name,))


def start_activate_location(name):
    return _start_background(_run_activate_location, (name,))


def rescan_status():
    with _rescan_lock:
        running = _rescan_state["running"]
        returncode = _rescan_state["returncode"]

    tail = ""
    try:
        with open(RESCAN_LOG, "r", encoding="utf-8", errors="replace") as f:
            tail = "".join(f.readlines()[-25:])
    except FileNotFoundError:
        pass

    station_count = None
    try:
        station_count = len(prepare_stations(fetch_stations()))
    except Exception:
        pass

    tvheadend_muxes = None
    try:
        with _tvh_req("/api/mpegts/mux/grid?limit=500", timeout=5) as resp:
            grid = json.loads(resp.read())
        tvheadend_muxes = sum(1 for e in grid["entries"] if e.get("network") == TVH_NETWORK_NAME)
    except Exception:
        pass

    return {
        "running": running,
        "returncode": returncode,
        "station_count": station_count,
        "tvheadend_muxes": tvheadend_muxes,
        "active_location": _get_active_location(),
        "locations": list_locations(),
        "log_tail": tail,
    }


def get_local_ip():
    # doesn't actually send traffic - just asks the kernel which local
    # interface/IP would be used to reach an external address
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _localize_url(url, local_ip):
    # mediasrv embeds whatever host it thinks it is reachable at, which may
    # not be the interface TVHeadend actually reaches it through
    parts = urllib.parse.urlsplit(url)
    host, _, port = parts.netloc.partition(":")
    new_netloc = f"{local_ip}:{port}" if port else local_ip
    return urllib.parse.urlunsplit((parts.scheme, new_netloc, parts.path, parts.query, parts.fragment))


def load_excluded():
    try:
        with open(EXCLUDE_FILE, "r", encoding="utf-8") as f:
            return {line.strip() for line in f if line.strip() and not line.startswith("#")}
    except FileNotFoundError:
        return set()


def save_excluded(names):
    os.makedirs(os.path.dirname(EXCLUDE_FILE), exist_ok=True)
    with open(EXCLUDE_FILE, "w", encoding="utf-8") as f:
        f.write("# DAB+ station names to leave out of the channel list entirely.\n")
        f.write("# Managed via /dab/exclude - editing by hand is also fine.\n")
        for name in sorted(names):
            f.write(name + "\n")


def set_dab_excluded(name, excluded):
    names = load_excluded()
    if excluded:
        names.add(name)
    else:
        names.discard(name)
    save_excluded(names)


def load_renames():
    # renames.txt: one "raw name = display name" per line.
    # Use this to canonicalize names that TVHeadend/DAB report slightly
    # differently for what is really the same station (e.g. "NDR 2 NDS" ->
    # "NDR 2"), so duplicate simulcast entries collapse into one station
    # before they ever reach TVHeadend.
    renames = {}
    try:
        with open(RENAME_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                raw, _, display = line.partition("=")
                renames[raw.strip()] = display.strip()
    except FileNotFoundError:
        pass
    return renames


def save_renames(renames):
    os.makedirs(os.path.dirname(RENAME_FILE), exist_ok=True)
    with open(RENAME_FILE, "w", encoding="utf-8") as f:
        f.write("# raw DAB name = display name\n")
        f.write("# Managed via /dab/rename - editing by hand is also fine.\n")
        for raw, display in sorted(renames.items()):
            f.write(f"{raw}={display}\n")


def set_dab_rename(raw_name, display_name):
    renames = load_renames()
    if display_name and display_name != raw_name:
        renames[raw_name] = display_name
    else:
        renames.pop(raw_name, None)
    save_renames(renames)


def list_dab_stations():
    """Raw (unfiltered) current station list with exclude/rename status, for
    building a management UI (e.g. in Home Assistant) on top of."""
    excluded = load_excluded()
    renames = load_renames()
    result = []
    for raw_name, _url in fetch_stations():
        display_name = renames.get(raw_name, raw_name)
        is_excluded = display_name in excluded or raw_name in excluded
        result.append({
            "raw_name": raw_name,
            "display_name": display_name,
            "excluded": is_excluded,
        })
    return result


def esc(value):
    return value.replace("\\", "\\\\").replace(" ", "\\ ")


def fetch_live_stations():
    prid = _latest_scan_prid()
    if prid is None:
        return []
    serial = _device_serial()
    data = json.loads(_sundtek_post(
        f"cmd=chlist&mode=scanlist&devserial={serial}&prid={prid}&filter=radio-sd-hd-pradio-psd-phd"
    ))
    if not data:
        return []
    local_ip = get_local_ip()
    stations = []
    for row in data[0][1:]:
        name = row[0].strip()
        # empty names are the ensemble/multiplex label itself (not a
        # station); "... EPG" entries are DAB program-guide data services,
        # not audio - neither belongs in the channel list
        if not name or "EPG" in name.upper():
            continue
        stations.append((name, _localize_url(row[1], local_ip)))
    return stations


def _location_path(name):
    safe = "".join(c if c.isalnum() or c in " _-" else "_" for c in name).strip()
    return os.path.join(DAB_LOCATIONS_DIR, f"{safe}.json")


def _get_active_location():
    try:
        with open(DAB_ACTIVE_LOCATION_FILE, "r", encoding="utf-8") as f:
            name = f.read().strip()
        return name or None
    except FileNotFoundError:
        return None


def _set_active_location(name):
    if name is None:
        try:
            os.remove(DAB_ACTIVE_LOCATION_FILE)
        except FileNotFoundError:
            pass
        return
    os.makedirs(os.path.dirname(DAB_ACTIVE_LOCATION_FILE), exist_ok=True)
    with open(DAB_ACTIVE_LOCATION_FILE, "w", encoding="utf-8") as f:
        f.write(name)


def save_location(name, stations):
    os.makedirs(DAB_LOCATIONS_DIR, exist_ok=True)
    with open(_location_path(name), "w", encoding="utf-8") as f:
        json.dump({"name": name, "saved_at": time.time(), "stations": stations}, f)


def load_location(name):
    with open(_location_path(name), "r", encoding="utf-8") as f:
        return json.load(f)


def delete_location(name):
    """Removes a saved location's file. If it was the active one, falls
    back to "live" (same as the GPS-triggered rescan does) rather than
    leaving the active-location pointer dangling."""
    path = _location_path(name)
    if not os.path.exists(path):
        return False
    os.remove(path)
    if _get_active_location() == name:
        _set_active_location(None)
    return True


def list_locations():
    if not os.path.isdir(DAB_LOCATIONS_DIR):
        return []
    result = []
    for fname in sorted(os.listdir(DAB_LOCATIONS_DIR)):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(DAB_LOCATIONS_DIR, fname), "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        result.append({
            "name": data.get("name"),
            "station_count": len(data.get("stations", [])),
            "saved_at": data.get("saved_at"),
        })
    return result


def fetch_stations():
    active = _get_active_location()
    if active:
        try:
            data = load_location(active)
            return [tuple(s) for s in data["stations"]]
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            pass  # saved file is gone/corrupt - fall back to a live scan
    return fetch_live_stations()


def prepare_stations(stations):
    excluded = load_excluded()
    renames = load_renames()
    seen = set()
    result = []
    for raw_name, url in stations:
        name = renames.get(raw_name, raw_name)
        if name in excluded or raw_name in excluded:
            continue
        if name in seen:
            # same station simulcast on another frequency (SFN) - keep only
            # the first one so it doesn't create a duplicate TVHeadend channel
            continue
        seen.add(name)
        result.append((name, url))
    return result


def build_m3u(stations):
    lines = ["#EXTM3U"]
    for name, url in prepare_stations(stations):
        cmd = (
            f"pipe://{FFMPEG_BIN} -loglevel fatal -i {url} "
            f"-metadata {esc('service_name=' + name)} "
            f"-metadata {esc('service_provider=' + SERVICE_PROVIDER)} "
            f"-mpegts_service_type advanced_codec_digital_radio "
            f"-vn -c:a mp2 -b:a {AUDIO_BITRATE} -f mpegts pipe:1"
        )
        lines.append(f"#EXTINF:-1,{name}")
        lines.append(cmd)
    return "\n".join(lines) + "\n"


def build_plain_m3u(stations):
    lines = ["#EXTM3U"]
    for name, url in prepare_stations(stations):
        lines.append(f'#EXTINF:-1 radio="true",{name}')
        lines.append("#EXTVLCOPT:network-caching=1000")
        lines.append(url)
    return "\n".join(lines) + "\n"


def _send_json(handler, status, payload):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/dab/status", "/rescan/status"):
            _send_json(self, 200, rescan_status())
            return
        if path == "/dab/locations":
            _send_json(self, 200, list_locations())
            return
        if path == "/dab/stations":
            try:
                _send_json(self, 200, {"stations": list_dab_stations()})
            except Exception as e:
                _send_json(self, 502, {"error": str(e)})
            return
        if path == "/dab/signal":
            try:
                _send_json(self, 200, get_dab_signal_info())
            except Exception as e:
                _send_json(self, 502, {"error": str(e)})
            return
        if path == "/dab/now_playing":
            try:
                _send_json(self, 200, get_dab_now_playing())
            except Exception as e:
                _send_json(self, 502, {"error": str(e)})
            return
        if path not in ("/DAB_mp2.m3u", "/DAB_plain.m3u", "/"):
            self.send_response(404)
            self.end_headers()
            return
        try:
            stations = fetch_stations()
            if path == "/DAB_plain.m3u":
                body = build_plain_m3u(stations).encode("utf-8")
            else:
                body = build_m3u(stations).encode("utf-8")
        except Exception as e:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(str(e).encode())
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/x-mpegurl")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = self.path.split("?")[0]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

        if path in ("/dab/rescan", "/rescan"):
            started = start_rescan()
            _send_json(self, 202 if started else 409, {"started": started})
            return

        if path == "/dab/scan_new_location":
            name = (query.get("name") or [None])[0]
            if not name:
                _send_json(self, 400, {"error": "missing 'name' query parameter"})
                return
            started = start_scan_new_location(name)
            _send_json(self, 202 if started else 409, {"started": started, "name": name})
            return

        if path == "/dab/activate":
            name = (query.get("name") or [None])[0]
            if not name:
                _send_json(self, 400, {"error": "missing 'name' query parameter"})
                return
            if not os.path.exists(_location_path(name)):
                _send_json(self, 404, {"error": f"no saved location named {name!r}"})
                return
            started = start_activate_location(name)
            _send_json(self, 202 if started else 409, {"started": started, "name": name})
            return

        if path == "/dab/delete_location":
            name = (query.get("name") or [None])[0]
            if not name:
                _send_json(self, 400, {"error": "missing 'name' query parameter"})
                return
            deleted = delete_location(name)
            _send_json(self, 200 if deleted else 404, {"deleted": deleted, "name": name})
            return

        if path == "/dab/exclude":
            name = (query.get("name") or [None])[0]
            excluded = (query.get("excluded") or ["true"])[0].lower() not in ("false", "0", "no")
            if not name:
                _send_json(self, 400, {"error": "missing 'name' query parameter"})
                return
            set_dab_excluded(name, excluded)
            _send_json(self, 200, {"name": name, "excluded": excluded})
            return

        if path == "/dab/rename":
            raw_name = (query.get("raw_name") or [None])[0]
            display_name = (query.get("display_name") or [""])[0]
            if not raw_name:
                _send_json(self, 400, {"error": "missing 'raw_name' query parameter"})
                return
            set_dab_rename(raw_name, display_name)
            _send_json(self, 200, {"raw_name": raw_name, "display_name": display_name or raw_name})
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=mapper_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()
