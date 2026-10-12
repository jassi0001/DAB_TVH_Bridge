#!/usr/bin/env python3
"""API-native FM bridge (v2). FM never depended on mediaclient (RDS was
already pure ctypes via fm_bridge.py's _rds_reader_loop) - the only thing
rebuilt here to match dab_bridge2.py's own history (see that file's module
docstring, TVHEADEND_QUIRKS.md points 13-15):

1. This bridge does not stream anything itself. build_m3u() emits the
   same pipe://ffmpeg ... command per mux that fm_bridge.py (v1) always
   used, so TVHeadend spawns and owns each ffmpeg process. An earlier
   version of this file (and dab_bridge2.py, plus a shared tuner_bridge.py
   arbiter) had a bridge process own the ffmpeg /stream/ connection
   instead - live-tested and reverted after the same architecture proved
   no more reliable than TVHeadend's own pipe:// mechanism for DAB+, and
   the underlying rtspd instability it was meant to work around sits in
   the shared tuner layer, not in whichever process talks to it.

2. build_m3u() rewrites each stream URL's host to RTSPD_STREAM_HOST (the
   box's real LAN IP) rather than trusting whatever host Sundtek's own
   cmd=chlist API happens to embed (it reflects whichever client last
   triggered the scan - loopback if triggered from this same box, as
   _perform_fm_scan() does).

3. start_ordered_scan() (via POST /fm/scan_ordered) drives TVHeadend's mux
   scan itself, frequency by frequency, instead of leaving it to
   TVHeadend's own scan queue - confirmed by reading
   mpegts_network_scan.c/mpegts_mux.c upstream that TVHeadend's queue
   ignores mux/M3U order entirely (ties broken by each mux's own random
   UUID), so nothing in the M3U or network config can influence scan
   order - only forcing scan_state=3 on a chosen mux (the same mechanism
   dvbt_bridge.py already relies on) actually does.

Unlike DAB+, there was no working "FM v1" TVHeadend network to copy
proven-good settings from - "FM" has always had skipinitscan=true and
num_svc=0 (never actually completed a scan). "FM v2"'s network is
configured with the same values proven to work for "DAB+ v2" instead
(see TVHEADEND_QUIRKS.md point 13) as the best available reference.

Built alongside fm_bridge.py (unchanged, kept as fallback) rather than
replacing it.
"""
import base64
import ctypes
import fcntl
import glob
import json
import os
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BRIDGE_BASE = "/storage/dab-tvh-bridge"
DABPLUS_BASE = "/storage/dabplus"

SUNDTEK_BASE = "http://127.0.0.1:22000"
# See build_m3u()'s use - the host cmd=chlist embeds in stream URLs reflects
# whichever client triggered the scan, not necessarily the box's own address.
# RTSPD_STREAM_HOST is set from credentials.env further down (_load_env()).
FM_DEVICE_ID = 0
FM_DELSYS = 255

MCSIMPLE_LIB = f"{DABPLUS_BASE}/opt/lib/libmcsimple.so"
FM_RDS_STATUS = 124


def _IOC(direction, type_char, nr, size):
    return (direction << 30) | (ord(type_char) << 8) | nr | (size << 16)


_IOC_WRITE = 1
_IOC_READWRITE = 3


class _V4l2Tuner(ctypes.Structure):
    _fields_ = [
        ("index", ctypes.c_uint32),
        ("name", ctypes.c_uint8 * 32),
        ("type", ctypes.c_uint32),
        ("capability", ctypes.c_uint32),
        ("rangelow", ctypes.c_uint32),
        ("rangehigh", ctypes.c_uint32),
        ("rxsubchans", ctypes.c_uint32),
        ("audmode", ctypes.c_uint32),
        ("signal", ctypes.c_int32),
        ("afc", ctypes.c_int32),
        ("reserved", ctypes.c_uint32 * 4),
    ]


VIDIOC_G_TUNER = _IOC(_IOC_READWRITE, "V", 29, ctypes.sizeof(_V4l2Tuner))


class _FmRdsData(ctypes.Structure):
    _fields_ = [
        ("rdssync", ctypes.c_uint8),
        ("pivalid", ctypes.c_uint8),
        ("tpptyvalid", ctypes.c_uint8),
        ("PI", ctypes.c_uint16),
        ("PTY", ctypes.c_uint8),
        ("data", ctypes.c_uint8 * 8),
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


def _find_active_fm_device():
    lib = _get_mcsimple()
    candidates = sorted(glob.glob("/dev/radio*"), key=lambda p: os.stat(p).st_mtime, reverse=True)
    for device in candidates:
        fd = lib.net_open(device.encode(), 0)
        if fd >= 0:
            lib.net_close(fd)
            return device
    return None


def get_fm_signal_info():
    # HA polls this every 20s regardless of whether FM is actually playing -
    # live-confirmed (2026-09-26) that even just opening /dev/radio* here is
    # enough to force the shared tuner into FM mode, even with nothing else
    # going on. Checking "DAB+ isn't active" alone (an earlier version of
    # this guard) wasn't sufficient - the correct condition is "FM is
    # actually the one being streamed by TVHeadend right now", matching
    # _rds_reader_loop()'s own gate exactly.
    if not bool(get_fm_now_playing()["playing"]):
        return {"device": None, "signal": None}
    lib = _get_mcsimple()
    device = _find_active_fm_device()
    if not device:
        return {"device": None, "signal": None}
    fd = lib.net_open(device.encode(), 0)
    if fd < 0:
        return {"device": device, "signal": None}
    try:
        tuner = _V4l2Tuner()
        lib.net_ioctl(fd, VIDIOC_G_TUNER, ctypes.byref(tuner))
        return {"device": device, "signal": tuner.signal}
    finally:
        lib.net_close(fd)


_EBU_EXTRA = {
    0x0F: "š", 0x19: "ü", 0x1A: "é", 0x1B: "â",
    0x1C: "ä", 0x1D: "û", 0x1E: "á", 0x1F: "Ë",
}


def _rds_char(b):
    if b in _EBU_EXTRA:
        return _EBU_EXTRA[b]
    if 0x20 <= b <= 0x7E:
        return chr(b)
    return ""


_rds_lock = threading.Lock()
_rds_state = {}


def _rds_reader_loop():
    # Only probe/open the FM device while TVHeadend's own subscriptions say
    # a station is actually playing - mirrors dab_bridge2.py's
    # _mot_dls_loop() channel-tracking guard. Without this check, this loop
    # called _find_active_fm_device() (itself an open+close cycle) and then
    # opened the device for continuous ioctl polling every 200ms, forever,
    # regardless of whether anything was ever streaming - live-confirmed
    # (2026-09-26) to force the shared tuner into FM mode repeatedly on its
    # own, fighting with DAB+ for the same exclusive hardware node even
    # when nobody was listening to FM at all.
    lib = _get_mcsimple()
    last_device = None
    fd = -1
    last_group = None
    ps_buf = bytearray(8)
    rt_buf = bytearray(64)
    rt_ab_flag = None
    was_playing = False
    while True:
        try:
            playing = bool(get_fm_now_playing()["playing"]) and not _dab_v2_is_playing()
            if not playing:
                if was_playing and fd >= 0:
                    lib.net_close(fd)
                    fd = -1
                    last_device = None
                was_playing = False
                time.sleep(1)
                continue
            was_playing = True

            device = _find_active_fm_device()
            if device != last_device:
                if fd >= 0:
                    lib.net_close(fd)
                    fd = -1
                last_device = device
                last_group = None
                ps_buf = bytearray(8)
                rt_buf = bytearray(64)
                rt_ab_flag = None
                if device:
                    fd = lib.net_open(device.encode(), 0)
            if device and fd >= 0:
                rds = _FmRdsData()
                lib.net_ioctl(fd, FM_RDS_STATUS, ctypes.byref(rds))
                if rds.rdssync:
                    group_bytes = bytes(rds.data)
                    if group_bytes != last_group:
                        last_group = group_bytes
                        group_type = group_bytes[2] >> 4
                        if group_type == 0:
                            idx = (group_bytes[3] & 0x3) * 2
                            ps_buf[idx] = group_bytes[6] & 0x7F
                            ps_buf[idx + 1] = group_bytes[7] & 0x7F
                        elif group_type == 2:
                            ab = group_bytes[3] & 0x10
                            if ab != rt_ab_flag:
                                rt_ab_flag = ab
                                rt_buf = bytearray(64)
                            idx = (group_bytes[3] & 0xF) * 4
                            for i, b in enumerate(group_bytes[4:8]):
                                if idx + i < 64:
                                    rt_buf[idx + i] = b & 0x7F
                        ps = "".join(_rds_char(b) for b in ps_buf).strip()
                        radiotext = "".join(_rds_char(b) for b in rt_buf).strip()
                        with _rds_lock:
                            _rds_state[device] = {
                                "ps": ps or None,
                                "radiotext": radiotext or None,
                                "pi": rds.PI if rds.pivalid else None,
                                "pty": rds.PTY if rds.tpptyvalid else None,
                                "updated_at": time.time(),
                            }
        except Exception as e:
            print(f"[rds] error: {e}")
        time.sleep(0.2)


def get_fm_rds():
    if not bool(get_fm_now_playing()["playing"]):
        return {"device": None, "ps": None, "radiotext": None, "pi": None, "pty": None, "updated_at": None}
    device = _find_active_fm_device()
    with _rds_lock:
        state = _rds_state.get(device)
    if not state:
        return {"device": device, "ps": None, "radiotext": None, "pi": None, "pty": None, "updated_at": None}
    return {"device": device, **state}


def get_fm_now_playing():
    with _tvh_req("/api/status/subscriptions") as resp:
        data = json.loads(resp.read())
    channels = sorted({
        e["channel"] for e in data.get("entries", [])
        if f"/{TVH_NETWORK_NAME}/" in (e.get("service") or "") and e.get("channel")
    })
    return {"playing": channels}


def _dab_v2_is_playing():
    # Mutual guard alongside dab_bridge2.py's matching _fm_v2_is_playing() -
    # the physical tuner is exclusive across DAB+ and FM (Sundtek's own
    # wiki: using one locks the other), so this bridge's own background
    # loop must never touch the device while DAB+ is the one actually
    # holding it.
    try:
        with _tvh_req("/api/status/subscriptions") as resp:
            data = json.loads(resp.read())
        return any("/DAB+ v2/" in (e.get("service") or "") for e in data.get("entries", []))
    except Exception:
        return False


# Bare "ffmpeg" (not an absolute path) is correct here - TVHeadend's own
# process resolves it via its PATH, unlike a systemd service spawning
# ffmpeg itself.
FFMPEG_BIN = "ffmpeg"
AUDIO_BITRATE = "192k"
SERVICE_PROVIDER = "FM Radio"
LISTEN_PORT = 8093
EXCLUDE_FILE = f"{BRIDGE_BASE}/fm_exclude.txt"
RENAME_FILE = f"{BRIDGE_BASE}/fm_renames.txt"
RESCAN_LOG = f"{BRIDGE_BASE}/fm2.log"
FM_LOCATIONS_DIR = f"{BRIDGE_BASE}/fm_locations"
FM_ACTIVE_LOCATION_FILE = f"{BRIDGE_BASE}/fm_active_location.txt"
DIGITALDATA_DEBUG_LOG = f"{BRIDGE_BASE}/fm_debug2.log"

TVH_HOST = "127.0.0.1"
TVH_PORT = 9981
def _load_env(path=f"{BRIDGE_BASE}/credentials.env"):
    """Site-specific settings from credentials.env (KEY=VALUE per line, '#' =
    comment) next to the bridge: TVH_USER, TVH_PASS and RTSPD_STREAM_HOST.
    Environment variables of the same name override the file. Nothing
    site-specific lives in the source."""
    env = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    env[key.strip()] = value.strip().strip("'\"")
    except OSError:
        print(f"WARNING: {path} not readable - using placeholder defaults")
    for key in ("TVH_USER", "TVH_PASS", "RTSPD_STREAM_HOST"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


_ENV = _load_env()
TVH_USER = _ENV.get("TVH_USER", "claude")
TVH_PASS = _ENV.get("TVH_PASS", "claude")
# Host rtspd stream URLs are rewritten to (see build_m3u()); the box's own LAN
# address, set in credentials.env. Loopback works too when TVHeadend runs on
# the same box.
RTSPD_STREAM_HOST = _ENV.get("RTSPD_STREAM_HOST", "127.0.0.1")
TVH_NETWORK_NAME = "FM v2"
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
RADIO_CHANNELS_TAG_NAME = "Radio channels"


def _debug_log(msg):
    try:
        with open(DIGITALDATA_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def _tvh_req(path, data=None, timeout=15):
    auth = base64.b64encode(f"{TVH_USER}:{TVH_PASS}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}"}
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        r = urllib.request.Request(f"http://{TVH_HOST}:{TVH_PORT}{path}", data=body, headers=headers, method="POST")
    else:
        r = urllib.request.Request(f"http://{TVH_HOST}:{TVH_PORT}{path}", headers=headers)
    return urllib.request.urlopen(r, timeout=timeout)


def map_unmapped_fm_services():
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


def disable_fm_mux_epg():
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


def tag_fm_channels_as_radio():
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
            n = map_unmapped_fm_services()
            if n:
                print(f"[mapper] mapped {n} newly scanned FM service(s) to channels")
        except Exception as e:
            print(f"[mapper] error: {e}")
        try:
            n = disable_fm_mux_epg()
            if n:
                print(f"[mapper] disabled EPG scan on {n} FM mux(es)")
        except Exception as e:
            print(f"[mapper] error: {e}")
        try:
            n = tag_fm_channels_as_radio()
            if n:
                print(f"[mapper] tagged {n} FM channel(s) as Radio channels")
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


def load_excluded():
    try:
        with open(EXCLUDE_FILE, "r", encoding="utf-8") as f:
            return {line.strip() for line in f if line.strip() and not line.startswith("#")}
    except FileNotFoundError:
        return set()


def save_excluded(names):
    os.makedirs(os.path.dirname(EXCLUDE_FILE), exist_ok=True)
    with open(EXCLUDE_FILE, "w", encoding="utf-8") as f:
        f.write("# FM station names to leave out of the channel list entirely.\n")
        for name in sorted(names):
            f.write(name + "\n")


def set_fm_excluded(name, excluded):
    names = load_excluded()
    if excluded:
        names.add(name)
    else:
        names.discard(name)
    save_excluded(names)


def load_renames():
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
        f.write("# raw FM name = display name\n")
        for raw, display in sorted(renames.items()):
            f.write(f"{raw}={display}\n")


def set_fm_rename(raw_name, display_name):
    renames = load_renames()
    if display_name and display_name != raw_name:
        renames[raw_name] = display_name
    else:
        renames.pop(raw_name, None)
    save_renames(renames)


def list_fm_stations():
    excluded = load_excluded()
    renames = load_renames()
    result = []
    for raw_name, _url in fetch_stations():
        display_name = renames.get(raw_name, raw_name)
        is_excluded = display_name in excluded or raw_name in excluded
        result.append({"raw_name": raw_name, "display_name": display_name, "excluded": is_excluded})
    return result


def _fetch_rtspd_station_list():
    """Authoritative station name -> stream URL list straight from rtspd's
    own M3U export - see dab_bridge2.py's identical function for why
    (despite the name, /dab.m3u contains every known station regardless of
    mode; reconstructing URLs from cmd=chlist instead was live-confirmed,
    2026-09-26, to be the source of DAB+'s streaming unreliability - a
    missing `comp=` parameter - so both bridges now use this same source
    for consistency even though FM's own URLs never needed `comp`)."""
    with urllib.request.urlopen(f"{SUNDTEK_BASE}/dab.m3u", timeout=15) as resp:
        text = resp.read().decode(errors="replace")
    stations = []
    name = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF"):
            name = line.rsplit(",", 1)[-1].strip()
        elif line.startswith("http://") or line.startswith("https://"):
            if name:
                stations.append((name, line))
            name = None
    return stations


def fetch_live_stations():
    stations = []
    for name, url in _fetch_rtspd_station_list():
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        if (q.get("delsys") or [None])[0] != str(FM_DELSYS):
            continue
        if not name:
            continue
        stations.append((name, url))
    return stations


def _location_path(name):
    safe = "".join(c if c.isalnum() or c in " _-" else "_" for c in name).strip()
    return os.path.join(FM_LOCATIONS_DIR, f"{safe}.json")


def _get_active_location():
    try:
        with open(FM_ACTIVE_LOCATION_FILE, "r", encoding="utf-8") as f:
            name = f.read().strip()
        return name or None
    except FileNotFoundError:
        return None


def _set_active_location(name):
    if name is None:
        try:
            os.remove(FM_ACTIVE_LOCATION_FILE)
        except FileNotFoundError:
            pass
        return
    os.makedirs(os.path.dirname(FM_ACTIVE_LOCATION_FILE), exist_ok=True)
    with open(FM_ACTIVE_LOCATION_FILE, "w", encoding="utf-8") as f:
        f.write(name)


def save_location(name, stations):
    os.makedirs(FM_LOCATIONS_DIR, exist_ok=True)
    with open(_location_path(name), "w", encoding="utf-8") as f:
        json.dump({"name": name, "saved_at": time.time(), "stations": stations}, f)


def load_location(name):
    with open(_location_path(name), "r", encoding="utf-8") as f:
        return json.load(f)


def delete_location(name):
    path = _location_path(name)
    if not os.path.exists(path):
        return False
    os.remove(path)
    if _get_active_location() == name:
        _set_active_location(None)
    return True


def list_locations():
    if not os.path.isdir(FM_LOCATIONS_DIR):
        return []
    result = []
    for fname in sorted(os.listdir(FM_LOCATIONS_DIR)):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(FM_LOCATIONS_DIR, fname), "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        result.append({"name": data.get("name"), "station_count": len(data.get("stations", [])), "saved_at": data.get("saved_at")})
    return result


def fetch_stations():
    active = _get_active_location()
    if active:
        try:
            data = load_location(active)
            return [tuple(s) for s in data["stations"]]
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            pass
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
            continue
        seen.add(name)
        result.append((name, url))
    return result


def esc(value):
    return value.replace("\\", "\\\\").replace(" ", "\\ ")


def _parse_freq(url):
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    return (q.get("freq") or [None])[0]


def _with_rtspd_host(url):
    return urllib.parse.urlunsplit(
        urllib.parse.urlsplit(url)._replace(netloc=f"{RTSPD_STREAM_HOST}:22000")
    )


def build_m3u(stations):
    lines = ["#EXTM3U"]
    for name, url in prepare_stations(stations):
        freq = _parse_freq(url)
        if not freq:
            continue
        cmd = (
            f"pipe://{FFMPEG_BIN} -loglevel fatal -i {_with_rtspd_host(url)} "
            f"-metadata {esc('service_name=' + name)} "
            f"-metadata {esc('service_provider=' + SERVICE_PROVIDER)} "
            f"-mpegts_service_type digital_radio "
            f"-vn -c:a mp2 -b:a {AUDIO_BITRATE} -f mpegts pipe:1"
        )
        lines.append(f"#EXTINF:-1,{name}")
        lines.append(cmd)
    return "\n".join(lines) + "\n"


def _favlist_groups_param(names):
    inner = json.dumps(names)
    return urllib.parse.quote(urllib.parse.quote(inner, safe=""), safe="")


def fetch_favorite_stations(group_name="FM"):
    body = f"cmd=chlist&mode=favlist&groups={_favlist_groups_param([group_name])}&filter=radio-sd-hd-pradio-psd-phd"
    data = json.loads(_sundtek_post(body))
    if not data or len(data[0]) <= 1:
        return []
    seen = set()
    stations = []
    for row in data[0][1:]:
        name = row[0].strip()
        if not name or name in seen:
            continue
        seen.add(name)
        stations.append((name, row[1]))
    return stations


_rescan_lock = threading.Lock()
_rescan_state = {"running": False, "returncode": None}
_fm_scan_progress = {
    "fm_scanning": False, "current_freq": None, "current_stations": None,
    "last_freq": None, "last_stations": None, "last_finished_at": None,
}


def _rescan_log(line):
    try:
        with open(RESCAN_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _wait_tuner_ready(timeout=40, stable=2):
    """Wait until mediasrv reports the tuner (scanstat with 13+ fields) on
    `stable` consecutive polls. The tuner vanishes from mediasrv for a few
    seconds after every mediasrv AND rtspd (re)start, and rtspd reads the
    device table only once at startup - starting rtspd or scanning too early
    leaves rtspd deaf (404 or empty streams). Same logic as tuner_watchdog.py."""
    end = time.time() + timeout
    good = 0
    while time.time() < end:
        try:
            arr = json.loads(_sundtek_post(f"cmd=scanstat&deviceid={FM_DEVICE_ID}", timeout=4))
            good = good + 1 if isinstance(arr, list) and len(arr) >= 13 else 0
        except Exception:
            good = 0
        if good >= stable:
            return True
        time.sleep(2)
    return False


def _restart_sundtek_services():
    subprocess.run(["systemctl", "restart", "sundtek-mediasrv.service"], check=True)
    time.sleep(3)
    if not _wait_tuner_ready():
        _rescan_log("WARNING: mediasrv does not report the tuner yet - continuing anyway")
    subprocess.run(["systemctl", "restart", "sundtek-rtspd.service"], check=True)
    time.sleep(4)
    if not _wait_tuner_ready():
        _rescan_log("WARNING: tuner not reported after rtspd start - continuing anyway")
    time.sleep(3)


def _perform_fm_scan():
    _rescan_log("restarting sundtek-mediasrv + sundtek-rtspd (required before a scan will persist) ...")
    _restart_sundtek_services()

    _rescan_log("triggering FM scan and waiting for it to finish (up to 4 min) ...")
    _sundtek_post(f"scan=start&deviceid={FM_DEVICE_ID}&mode={FM_DELSYS}")

    with _rescan_lock:
        _fm_scan_progress["fm_scanning"] = True
        _fm_scan_progress["current_freq"] = None
        _fm_scan_progress["current_stations"] = None

    last_freq = None
    stable = 0
    station_count = None
    try:
        for _ in range(48):
            time.sleep(5)
            try:
                arr = json.loads(_sundtek_post(f"cmd=scanstat&deviceid={FM_DEVICE_ID}"))
            except Exception:
                continue
            freq = arr[6] if len(arr) > 6 else None
            station_count = arr[12] if len(arr) > 12 else station_count
            with _rescan_lock:
                _fm_scan_progress["current_freq"] = freq
                _fm_scan_progress["current_stations"] = station_count
            stable = stable + 1 if freq == last_freq else 0
            last_freq = freq
            if stable >= 3:
                break
        _rescan_log(f"scan settled at freq={last_freq}, {station_count} station(s) found")
    finally:
        with _rescan_lock:
            _fm_scan_progress["fm_scanning"] = False
            _fm_scan_progress["current_freq"] = None
            _fm_scan_progress["current_stations"] = None
            _fm_scan_progress["last_freq"] = last_freq
            _fm_scan_progress["last_stations"] = station_count
            _fm_scan_progress["last_finished_at"] = time.time()


def _restart_tvheadend():
    _rescan_log("restarting TVHeadend so it re-scans the FM v2 IPTV network ...")
    subprocess.run(["systemctl", "restart", "service.tvheadend43.service"], check=True)
    time.sleep(8)


def _run_rescan():
    try:
        _set_active_location(None)
        _rescan_log("[1/3] restarting sundtek-mediasrv + sundtek-rtspd ...")
        _perform_fm_scan()
        _rescan_log("[2/3] scan done")
        _rescan_log("[3/3] restarting TVHeadend ...")
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
        _rescan_log(f"[1/3] scanning for FM location '{name}' ...")
        _perform_fm_scan()
        stations = fetch_live_stations()
        save_location(name, stations)
        _set_active_location(name)
        _rescan_log(f"[2/3] saved {len(stations)} station(s) as location '{name}'")
        _rescan_log("[3/3] restarting TVHeadend ...")
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
        data = load_location(name)
        _set_active_location(name)
        _rescan_log(f"[1/2] activated saved location '{name}' ({len(data['stations'])} station(s))")
        _rescan_log("[2/2] restarting TVHeadend ...")
        _restart_tvheadend()
        n = len(prepare_stations(fetch_stations()))
        _rescan_log(f"done. station count now: {n}")
        _rescan_state["returncode"] = 0
    except Exception as e:
        _rescan_log(f"ERROR: {e}")
        _rescan_state["returncode"] = 1
    finally:
        _rescan_state["running"] = False


# Cross-process lock shared with dab_bridge2.py (same path, same
# mechanism) - see that file's comment above its copy of this function for
# the full reasoning.
TUNER_SCAN_LOCK_PATH = f"{BRIDGE_BASE}/tuner_scan.lock"
_tuner_scan_lock_fd = None


def _try_acquire_tuner_scan_lock():
    global _tuner_scan_lock_fd
    fd = open(TUNER_SCAN_LOCK_PATH, "a")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fd.close()
        return False
    _tuner_scan_lock_fd = fd
    return True


def _release_tuner_scan_lock():
    global _tuner_scan_lock_fd
    if _tuner_scan_lock_fd is not None:
        try:
            fcntl.flock(_tuner_scan_lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        _tuner_scan_lock_fd.close()
        _tuner_scan_lock_fd = None


def _tvh_scan_watchdog_loop():
    """See dab_bridge2.py's copy of this function for the full reasoning -
    TVHeadend's own scan-queue timer processes DAB+ v2 and FM v2
    independently and concurrently with no idea they share one physical
    tuner; this defers FM v2's own TVHeadend-driven pending muxes back to
    scan_state=0 whenever DAB+ holds the shared tuner lock, and otherwise
    holds the lock itself while FM v2 has muxes TVHeadend is trying to
    scan on its own."""
    held_by_watchdog = False
    while True:
        try:
            time.sleep(15)
            if _rescan_state["running"]:
                continue
            with _tvh_req("/api/mpegts/mux/grid?start=0&limit=500") as resp:
                grid = json.loads(resp.read())
            own = [e for e in grid["entries"] if e.get("network") == TVH_NETWORK_NAME]

            # A mux that already has services doesn't need re-scanning just
            # because TVHeadend's own idle-scan queued it up again - only a
            # mux with num_svc==0 (never scanned, or its station list
            # actually changed identity) is worth spending shared-tuner time
            # on.
            already_ok = [e["uuid"] for e in own
                          if e.get("scan_state") in (1, 2, 3) and e.get("num_svc", 0) > 0]
            if already_ok:
                node = json.dumps([{"uuid": u, "scan_state": 0} for u in already_ok])
                with _tvh_req("/api/idnode/save", data={"node": node}):
                    pass
                _debug_log(f"[tvh-watchdog] skipped {len(already_ok)} already-OK FM v2 mux(es) - services unchanged")

            pending = [e["uuid"] for e in own
                       if e.get("scan_state") in (1, 2, 3) and e.get("num_svc", 0) == 0]
            if not pending:
                if held_by_watchdog:
                    _release_tuner_scan_lock()
                    held_by_watchdog = False
                continue
            if not held_by_watchdog:
                held_by_watchdog = _try_acquire_tuner_scan_lock()
            if not held_by_watchdog:
                node = json.dumps([{"uuid": u, "scan_state": 0} for u in pending])
                with _tvh_req("/api/idnode/save", data={"node": node}):
                    pass
                _debug_log(f"[tvh-watchdog] deferred {len(pending)} pending FM v2 mux(es) - DAB+ holds the shared tuner lock")
        except Exception as e:
            _debug_log(f"[tvh-watchdog] error: {e!r}")


def _start_background(target, args):
    with _rescan_lock:
        if _rescan_state["running"]:
            return False
        if not _try_acquire_tuner_scan_lock():
            _rescan_log("DAB+ is currently scanning/streaming-scanning - refusing to start (shared tuner)")
            return False
        _rescan_state["running"] = True
        _rescan_state["returncode"] = None
        try:
            open(RESCAN_LOG, "w").close()
        except OSError:
            pass

        def _run_and_release():
            try:
                target(*args)
            finally:
                _release_tuner_scan_lock()

        threading.Thread(target=_run_and_release, daemon=True).start()
        return True


def start_rescan():
    return _start_background(_run_rescan, ())


def start_scan_new_location(name):
    return _start_background(_run_scan_new_location, (name,))


def start_activate_location(name):
    return _start_background(_run_activate_location, (name,))


def _tvh_fm_mux_grid():
    with _tvh_req("/api/mpegts/mux/grid?start=0&limit=500") as resp:
        grid = json.loads(resp.read())
    return [e for e in grid["entries"] if e.get("network") == TVH_NETWORK_NAME]


def _freq_from_mux(mux):
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(mux.get("iptv_url") or "").query)
    return (q.get("freq") or [None])[0]


def _run_ordered_scan():
    # See dab_bridge2.py's _run_ordered_scan() for why this is needed at
    # all - TVHeadend's own scan queue ignores mux/M3U order completely.
    # FM has no sid/multiplex concept (one station per frequency), so each
    # "frequency group" here is always exactly one mux, but the forced
    # scan_state=3 mechanism is identical.
    try:
        muxes = _tvh_fm_mux_grid()
        groups = {}
        for m in muxes:
            freq = _freq_from_mux(m)
            if freq:
                groups.setdefault(freq, []).append(m)

        _rescan_log(f"ordered scan: {len(groups)} frequencies, {len(muxes)} mux(es) total")
        for freq in sorted(groups, key=int):
            pending = [m for m in groups[freq] if m.get("num_svc", 0) == 0]
            if not pending:
                continue
            uuids = [m["uuid"] for m in pending]
            _rescan_log(f"  freq {freq}: activating {len(uuids)} mux(es) ...")
            node = json.dumps([{"uuid": u, "scan_state": 3} for u in uuids])
            with _tvh_req("/api/idnode/save", data={"node": node}):
                pass

            deadline = time.time() + 40
            fresh_by_uuid = {}
            while time.time() < deadline:
                time.sleep(3)
                fresh_by_uuid = {m["uuid"]: m for m in _tvh_fm_mux_grid()}
                still_busy = any(fresh_by_uuid.get(u, {}).get("scan_state") in (1, 3) for u in uuids)
                if not still_busy:
                    break
            ok = sum(1 for u in uuids if fresh_by_uuid.get(u, {}).get("num_svc", 0) > 0)
            _rescan_log(f"    -> {ok}/{len(uuids)} got services")
        _rescan_log("ordered scan done")
        _rescan_state["returncode"] = 0
    except Exception as e:
        _rescan_log(f"ERROR: {e}")
        _rescan_state["returncode"] = 1
    finally:
        _rescan_state["running"] = False


def start_ordered_scan():
    return _start_background(_run_ordered_scan, ())


def rescan_status():
    with _rescan_lock:
        running = _rescan_state["running"]
        returncode = _rescan_state["returncode"]
        fm_scan = dict(_fm_scan_progress)

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
    tvh_mux_scan_pending = None
    tvh_mux_scan_active = None
    try:
        with _tvh_req("/api/mpegts/mux/grid?limit=500", timeout=5) as resp:
            grid = json.loads(resp.read())
        fm_muxes = [e for e in grid["entries"] if e.get("network") == TVH_NETWORK_NAME]
        tvheadend_muxes = len(fm_muxes)
        tvh_mux_scan_pending = sum(1 for e in fm_muxes if e.get("scan_state") in (1, 2))
        tvh_mux_scan_active = sum(1 for e in fm_muxes if e.get("scan_state") == 3)
    except Exception:
        pass

    return {
        "running": running, "returncode": returncode, "station_count": station_count,
        "tvheadend_muxes": tvheadend_muxes,
        "fm_scanning": fm_scan["fm_scanning"],
        "fm_scan_current_freq": fm_scan["current_freq"],
        "fm_scan_current_stations": fm_scan["current_stations"],
        "fm_last_scan_freq": fm_scan["last_freq"],
        "fm_last_scan_stations": fm_scan["last_stations"],
        "fm_last_scan_finished_at": fm_scan["last_finished_at"],
        "tvh_mux_scan_pending": tvh_mux_scan_pending,
        "tvh_mux_scan_active": tvh_mux_scan_active,
        "tvh_scanning": bool((tvh_mux_scan_pending or 0) + (tvh_mux_scan_active or 0)),
        "active_location": _get_active_location(),
        "locations": list_locations(),
        "log_tail": tail,
    }


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
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

        if path == "/fm/status":
            _send_json(self, 200, rescan_status())
            return
        if path == "/fm/locations":
            _send_json(self, 200, list_locations())
            return
        if path == "/fm/stations":
            try:
                _send_json(self, 200, {"stations": list_fm_stations()})
            except Exception as e:
                _send_json(self, 502, {"error": str(e)})
            return
        if path == "/fm/signal":
            try:
                _send_json(self, 200, get_fm_signal_info())
            except Exception as e:
                _send_json(self, 502, {"error": str(e)})
            return
        if path == "/fm/now_playing":
            try:
                _send_json(self, 200, get_fm_now_playing())
            except Exception as e:
                _send_json(self, 502, {"error": str(e)})
            return
        if path == "/fm/rds":
            try:
                _send_json(self, 200, get_fm_rds())
            except Exception as e:
                _send_json(self, 502, {"error": str(e)})
            return
        if path == "/fm/favorites":
            try:
                _send_json(self, 200, {"stations": [n for n, _u in fetch_favorite_stations()]})
            except Exception as e:
                _send_json(self, 502, {"error": str(e)})
            return
        if path == "/FM_favorites2.m3u":
            try:
                stations = fetch_favorite_stations()
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
            return
        if path not in ("/FM2.m3u", "/"):
            self.send_response(404)
            self.end_headers()
            return
        try:
            body = build_m3u(fetch_stations()).encode("utf-8")
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

        if path == "/fm/rescan":
            started = start_rescan()
            _send_json(self, 202 if started else 409, {"started": started})
            return
        if path == "/fm/scan_ordered":
            started = start_ordered_scan()
            _send_json(self, 202 if started else 409, {"started": started})
            return
        if path == "/fm/scan_new_location":
            name = (query.get("name") or [None])[0]
            if not name:
                _send_json(self, 400, {"error": "missing 'name' query parameter"})
                return
            started = start_scan_new_location(name)
            _send_json(self, 202 if started else 409, {"started": started, "name": name})
            return
        if path == "/fm/activate":
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
        if path == "/fm/delete_location":
            name = (query.get("name") or [None])[0]
            if not name:
                _send_json(self, 400, {"error": "missing 'name' query parameter"})
                return
            deleted = delete_location(name)
            _send_json(self, 200 if deleted else 404, {"deleted": deleted, "name": name})
            return
        if path == "/fm/exclude":
            name = (query.get("name") or [None])[0]
            excluded = (query.get("excluded") or ["true"])[0].lower() not in ("false", "0", "no")
            if not name:
                _send_json(self, 400, {"error": "missing 'name' query parameter"})
                return
            set_fm_excluded(name, excluded)
            _send_json(self, 200, {"name": name, "excluded": excluded})
            return
        if path == "/fm/rename":
            raw_name = (query.get("raw_name") or [None])[0]
            display_name = (query.get("display_name") or [""])[0]
            if not raw_name:
                _send_json(self, 400, {"error": "missing 'raw_name' query parameter"})
                return
            set_fm_rename(raw_name, display_name)
            _send_json(self, 200, {"raw_name": raw_name, "display_name": display_name or raw_name})
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=mapper_loop, daemon=True).start()
    threading.Thread(target=_rds_reader_loop, daemon=True).start()
    threading.Thread(target=_tvh_scan_watchdog_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()
