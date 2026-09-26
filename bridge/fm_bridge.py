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

# Same install-path convention as dab_bridge.py/dvbt_bridge.py.
BRIDGE_BASE = "/storage/dab-tvh-bridge"
DABPLUS_BASE = "/storage/dabplus"

SUNDTEK_BASE = "http://127.0.0.1:22000"
FM_DEVICE_ID = 0
# Sundtek's own delivery-system/mode value for FM radio - confirmed live
# (2026-09-23) via /devices.json, which reports this device's supported
# modes as {"15":"DAB/DAB+","255":"FMRADIO"}, and via a real scan
# (scan=start&deviceid=0&mode=255 correctly swept 87.9-107.5 MHz and
# produced a working stream URL
# "http://<box>:22000/stream/?delsys=255&freq=<Hz>&devid=0").
FM_DELSYS = 255

# Low-level SDK access (same libmcsimple.so as dab_bridge.py) - used here
# for RDS text (station name / radiotext) and live signal strength, which
# the HTTP servercmd.xhx API doesn't expose at all. Station list + stream
# URLs still come from the HTTP cmd=chlist API further below - the SDK has
# no equivalent call that returns a playable network URL, only frequency/
# signal/RDS-level info, so that part cannot move to the SDK no matter
# what (see SUNDTEK_INSTALL.md Abschnitt 4 for the DAB+ side of this same
# finding).
MCSIMPLE_LIB = f"{DABPLUS_BASE}/opt/lib/libmcsimple.so"
FM_SCAN_SETUP = 122
FM_SCAN_NEXT_FREQUENCY = 123
FM_SCAN_LOCKED = 1
FM_SCAN_SEARCHING = 2
FM_SCAN_ERROR = 4
FM_SCAN_COMPLETE = 8
FM_RDS_STATUS = 124


def _IOC(direction, type_char, nr, size):
    return (direction << 30) | (ord(type_char) << 8) | nr | (size << 16)


_IOC_WRITE = 1
_IOC_READWRITE = 3


class _V4l2Tuner(ctypes.Structure):
    # struct v4l2_tuner from videodev2.h - only "signal" is used here.
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


class _V4l2Frequency(ctypes.Structure):
    _fields_ = [
        ("tuner", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("frequency", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 8),
    ]


VIDIOC_G_TUNER = _IOC(_IOC_READWRITE, "V", 29, ctypes.sizeof(_V4l2Tuner))
VIDIOC_S_FREQUENCY = _IOC(_IOC_WRITE, "V", 57, ctypes.sizeof(_V4l2Frequency))
V4L2_TUNER_RADIO = 1


class _FmScanSetup(ctypes.Structure):
    _fields_ = [("default_setup", ctypes.c_uint8)]


class _FmScanParameters(ctypes.Structure):
    _fields_ = [
        ("RSSI", ctypes.c_int8),
        ("SNR", ctypes.c_int8),
        ("MULT", ctypes.c_uint8),
        ("VALID", ctypes.c_uint8),
        ("READFREQ", ctypes.c_uint32),
        ("status", ctypes.c_uint8),
    ]


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
    """/dev/radioN device nodes accumulate the same way /dev/dabN does
    (stale ones fail to open) - the most recently touched one is live."""
    lib = _get_mcsimple()
    candidates = sorted(glob.glob("/dev/radio*"), key=lambda p: os.stat(p).st_mtime, reverse=True)
    for device in candidates:
        fd = lib.net_open(device.encode(), 0)
        if fd >= 0:
            lib.net_close(fd)
            return device
    return None


def get_fm_signal_info():
    """Live FM tuner signal strength via VIDIOC_G_TUNER (standard V4L2
    ioctl, "signal" field, 0-65535 relative scale) - there's no Sundtek-
    specific FM equivalent of DAB_GET_TUNER's rssi/snr/cnr trio, this V4L2
    field is the only live post-tune signal indicator this SDK exposes for
    FM."""
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


# EBU Latin-1 -> unicode table for the handful of accented characters RDS
# actually uses (index = RDS code point 0x00-0x1F below the printable ASCII
# range that RDS repurposes for these, per EN 50067). Deliberately only the
# common German/Western-European subset rather than the full EBU table -
# good enough for station names/radiotext on this box's actual reception
# area; anything outside this falls back to '?' rather than crashing.
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
_rds_state = {}  # device -> {"ps": str, "radiotext": str, "pi": int, "pty": int, "updated_at": float}


def _rds_reader_loop():
    """Runs continuously in the background, polling FM_RDS_STATUS on
    whichever /dev/radioN is currently active and reassembling the PS
    (8-char station name, group type 0) and RadioText (64-char, group type
    2) fields per the RDS standard (EN 50067) - mirrors the reassembly
    logic from Sundtek's own wiki example, simplified to plain
    ASCII+the common accented subset above rather than the full EBU table."""
    lib = _get_mcsimple()
    last_device = None
    fd = -1
    last_group = None
    ps_buf = bytearray(8)
    rt_buf = bytearray(64)
    rt_ab_flag = None
    while True:
        try:
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
    device = _find_active_fm_device()
    with _rds_lock:
        state = _rds_state.get(device)
    if not state:
        return {"device": device, "ps": None, "radiotext": None, "pi": None, "pty": None, "updated_at": None}
    return {"device": device, **state}


def get_fm_now_playing():
    """Same pattern as dab_bridge.py:get_dab_now_playing() - straight from
    TVHeadend's own subscription table rather than guessing from the tuner
    state."""
    with _tvh_req("/api/status/subscriptions") as resp:
        data = json.loads(resp.read())
    channels = sorted({
        e["channel"] for e in data.get("entries", [])
        if f"/{TVH_NETWORK_NAME}/" in (e.get("service") or "") and e.get("channel")
    })
    return {"playing": channels}


FFMPEG_BIN = "ffmpeg"
AUDIO_BITRATE = "192k"
SERVICE_PROVIDER = "FM Radio"
LISTEN_PORT = 8083
EXCLUDE_FILE = f"{BRIDGE_BASE}/fm_exclude.txt"
RENAME_FILE = f"{BRIDGE_BASE}/fm_renames.txt"
RESCAN_LOG = f"{BRIDGE_BASE}/fm.log"
FM_LOCATIONS_DIR = f"{BRIDGE_BASE}/fm_locations"
FM_ACTIVE_LOCATION_FILE = f"{BRIDGE_BASE}/fm_active_location.txt"

TVH_HOST = "127.0.0.1"
TVH_PORT = 9981
TVH_USER = "claude"
TVH_PASS = "claude"
TVH_NETWORK_NAME = "FM"
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


def _device_serial():
    with urllib.request.urlopen(f"{SUNDTEK_BASE}/devices.json", timeout=10) as resp:
        devices = json.loads(resp.read())
    return devices[0][3]


def _latest_scan_prid():
    data = json.loads(_sundtek_post("cmd=providers"))
    if not data:
        return None
    scans = [e for e in data[0] if isinstance(e, list) and len(e) == 3 and e[1] == FM_DELSYS]
    if not scans:
        return None
    return max(scans, key=lambda e: e[0])[0]


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _localize_url(url, local_ip):
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
        f.write("# FM station names to leave out of the channel list entirely.\n")
        f.write("# Managed via /fm/exclude - editing by hand is also fine.\n")
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
        f.write("# Managed via /fm/rename - editing by hand is also fine.\n")
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
        if not name:
            continue
        stations.append((name, _localize_url(row[1], local_ip)))
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


def build_m3u(stations):
    lines = ["#EXTM3U"]
    for name, url in prepare_stations(stations):
        cmd = (
            f"pipe://{FFMPEG_BIN} -loglevel fatal -i {url} "
            f"-metadata {esc('service_name=' + name)} "
            f"-metadata {esc('service_provider=' + SERVICE_PROVIDER)} "
            f"-mpegts_service_type digital_radio "
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


def _favlist_groups_param(names):
    # The Sundtek web UI's own list-management page (captured via the
    # user's HAR export) sends this parameter DOUBLE url-encoded, e.g.
    # groups=%255B%2522FM%2522%255D for groups=["FM"] - %25 is an encoded
    # "%", so the server expects to urldecode the whole POST body once
    # (standard form decoding) and then urldecode the resulting %-escaped
    # JSON string a second time itself. Plain single-encoding is rejected.
    inner = json.dumps(names)
    return urllib.parse.quote(urllib.parse.quote(inner, safe=""), safe="")


def fetch_favorite_stations(group_name="FM"):
    """Stations from a group the user curated by hand in the Sundtek web
    UI's own "Listenverwaltung" (List management / favorites groups)
    feature - a distinct mechanism from our own scan-based fetch_stations()
    and from fm_exclude.txt/fm_renames.txt, so no dedup/rename logic from
    those is applied here beyond dropping exact duplicate names (the
    favlist response itself can contain the same entry twice)."""
    body = f"cmd=chlist&mode=favlist&groups={_favlist_groups_param([group_name])}&filter=radio-sd-hd-pradio-psd-phd"
    data = json.loads(_sundtek_post(body))
    if not data or len(data[0]) <= 1:
        return []
    local_ip = get_local_ip()
    seen = set()
    stations = []
    for row in data[0][1:]:
        name = row[0].strip()
        if not name or name in seen:
            continue
        seen.add(name)
        stations.append((name, _localize_url(row[1], local_ip)))
    return stations


def build_favorites_m3u(stations):
    lines = ["#EXTM3U"]
    for name, url in stations:
        cmd = (
            f"pipe://{FFMPEG_BIN} -loglevel fatal -i {url} "
            f"-metadata {esc('service_name=' + name)} "
            f"-metadata {esc('service_provider=' + SERVICE_PROVIDER)} "
            f"-mpegts_service_type digital_radio "
            f"-vn -c:a mp2 -b:a {AUDIO_BITRATE} -f mpegts pipe:1"
        )
        lines.append(f"#EXTINF:-1,{name}")
        lines.append(cmd)
    return "\n".join(lines) + "\n"


def build_favorites_plain_m3u(stations):
    lines = ["#EXTM3U"]
    for name, url in stations:
        lines.append(f'#EXTINF:-1 radio="true",{name}')
        lines.append("#EXTVLCOPT:network-caching=1000")
        lines.append(url)
    return "\n".join(lines) + "\n"


_rescan_lock = threading.Lock()
_rescan_state = {"running": False, "returncode": None}

_fm_scan_progress = {
    "fm_scanning": False,
    "current_freq": None,
    "current_stations": None,
    "last_freq": None,
    "last_stations": None,
    "last_finished_at": None,
}


def _rescan_log(line):
    try:
        with open(RESCAN_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _perform_fm_scan():
    _rescan_log("restarting sundtek-mediasrv + sundtek-rtspd (required before a scan will persist) ...")
    subprocess.run(["systemctl", "restart", "sundtek-mediasrv.service"], check=True)
    time.sleep(3)
    subprocess.run(["systemctl", "restart", "sundtek-rtspd.service"], check=True)
    time.sleep(3)

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
    _rescan_log("restarting TVHeadend so it re-scans the FM IPTV network ...")
    subprocess.run(["systemctl", "restart", "service.tvheadend43.service"], check=True)
    time.sleep(8)


def _run_rescan():
    try:
        _set_active_location(None)
        _rescan_log("[1/3] restarting sundtek-mediasrv + sundtek-rtspd (required before a scan will persist) ...")
        _perform_fm_scan()
        _rescan_log("[2/3] scan done")
        _rescan_log("[3/3] restarting TVHeadend so it re-scans the FM IPTV network ...")
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
        _rescan_log("[3/3] restarting TVHeadend so it re-scans the FM IPTV network ...")
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
        _rescan_log(f"[1/2] activated saved location '{name}' ({len(data['stations'])} station(s), no rescan needed)")
        _rescan_log("[2/2] restarting TVHeadend so it re-scans the FM IPTV network ...")
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
        "running": running,
        "returncode": returncode,
        "station_count": station_count,
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
        if path in ("/fm/status",):
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
        if path in ("/FM_favorites.m3u", "/FM_favorites_plain.m3u"):
            try:
                stations = fetch_favorite_stations()
                if path == "/FM_favorites_plain.m3u":
                    body = build_favorites_plain_m3u(stations).encode("utf-8")
                else:
                    body = build_favorites_m3u(stations).encode("utf-8")
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
        if path not in ("/FM.m3u", "/FM_plain.m3u", "/"):
            self.send_response(404)
            self.end_headers()
            return
        try:
            stations = fetch_stations()
            if path == "/FM_plain.m3u":
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

        if path == "/fm/rescan":
            started = start_rescan()
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
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()
