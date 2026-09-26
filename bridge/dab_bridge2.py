#!/usr/bin/env python3
"""API-native DAB+ bridge (v2) - no mediaclient dependency.

Built alongside dab_bridge.py (which keeps running unchanged) rather than
replacing it, so there's a known-good fallback while this is being proven
out live. Two structural differences from dab_bridge.py:

1. Cover art (MOT slideshow) and now-playing text (DLS) are decoded
   directly from the raw ctypes SDK (DAB_GET_DIGITAL_SERVICE_DATA, the
   two-phase probe+fetch call) instead of shelling out to `mediaclient
   --getdigitaldata`. See _mot_dls_loop() and the MOT/DLS parsing
   functions below - validated against real captured frames from this
   box (RTL RADIO, NDR 2 NDS) before being wired into the live loop.

2. This bridge does not stream anything itself. build_m3u() emits the
   same pipe://ffmpeg ... command per mux that dab_bridge.py (v1) always
   used, so TVHeadend spawns and owns each ffmpeg process exactly as
   before. An earlier version of this file (and a separate shared
   tuner_bridge.py arbiter) tried having a bridge process own the ffmpeg
   /stream/ connection instead - live-tested (2026-09-25) and reverted
   after subscribing to a station through TVHeadend's OWN pipe://
   mechanism failed in the exact same way at the time, proving the
   instability sits in the shared rtspd/tuner layer under heavy/rapid
   switching, not in whichever process happens to talk to it. This
   bridge's own job is purely to watch, via TVHeadend's subscriptions API
   (get_dab_now_playing()), which station is currently playing, and read
   MOT/DLS off the raw ctypes SDK for that station - see _mot_dls_loop().

3. build_m3u() orders stations by frequency (see _parse_station_url()),
   grouping every station on the same DAB ensemble together, so
   TVHeadend's own mux-scan visits them back to back instead of hopping
   between frequencies - requested after observing that touching one
   frequency's services consecutively behaves much better than jumping
   around, even though isolating an exact mechanism for that in rtspd
   itself wasn't possible from the outside.

Station list / scan / location management / TVHeadend channel mapping are
unchanged from dab_bridge.py - that part was never the problem.
"""
import base64
import ctypes
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
DABPLUS_BASE = "/storage/dabplus"

SUNDTEK_BASE = "http://127.0.0.1:22000"
# The stream URLs rtspd itself hands back via cmd=chlist carry whatever host
# the client that triggered the scan connected through (loopback if the scan
# was triggered from this same box, like _perform_dab_scan() does) - live
# comparison against dab_bridge.py's still-working v1 list showed its M3U
# consistently uses the box's real LAN IP instead, so pipe:// commands are
# rewritten to match that rather than trusting whatever chlist returned.
RTSPD_STREAM_HOST = "192.168.0.200"
DAB_DEVICE_ID = 0
DAB_DELSYS = 15

MCSIMPLE_LIB = f"{DABPLUS_BASE}/opt/lib/libmcsimple.so"
DAB_GET_TUNER = 68
DAB_HAS_LOCK = 0x01
DAB_GET_ENSEMBLE_INFO = 128
DAB_GET_SERVICE = 111
DAB_GET_DIGITAL_SERVICE_DATA = 119

ARTWORK_DIR = f"{BRIDGE_BASE}/artwork2"
DEFAULT_ARTWORK_TYPE = "cover"
DIGITALDATA_DEBUG_LOG = f"{BRIDGE_BASE}/digitaldata_debug2.log"

# Streaming itself is back to being TVHeadend's own job (a pipe:// command
# per mux, exactly like dab_bridge.py v1) - live-tested (2026-09-25) against
# a from-scratch StreamManager/tuner-arbiter design that had this bridge (or
# a shared tuner_bridge.py) own the ffmpeg process itself: that approach
# reliably broke down under TVHeadend's own multi-mux scan and, decisively,
# subscribing to a station through TVHeadend's OWN pipe:// mechanism failed
# in the exact same way at the time - proving the problem wasn't in this
# bridge's tuning logic, but the underlying rtspd/tuner layer misbehaves
# under heavy/rapid switching regardless of which process talks to it. Bare
# "ffmpeg" (not an absolute path) is correct here - TVHeadend's own process
# resolves it via its PATH, unlike a systemd service spawning ffmpeg itself.
FFMPEG_BIN = "ffmpeg"
AUDIO_BITRATE = "192k"
SERVICE_PROVIDER = "Radio channels"
LISTEN_PORT = 8091
EXCLUDE_FILE = f"{BRIDGE_BASE}/exclude.txt"          # shared with dab_bridge.py on purpose -
RENAME_FILE = f"{BRIDGE_BASE}/renames.txt"            # same station curation either bridge is used through
RESCAN_LOG = f"{BRIDGE_BASE}/dab2.log"

DAB_LOCATIONS_DIR = f"{BRIDGE_BASE}/locations"        # shared with dab_bridge.py - same scan data either way
DAB_ACTIVE_LOCATION_FILE = f"{BRIDGE_BASE}/active_location.txt"

TVH_HOST = "127.0.0.1"
TVH_PORT = 9981
TVH_USER = "claude"
TVH_PASS = "claude"
TVH_NETWORK_NAME = "DAB+ v2"
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


def map_unmapped_dab_services():
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


def disable_dab_mux_epg():
    with _tvh_req("/api/mpegts/mux/grid?start=0&limit=500") as resp:
        grid = json.loads(resp.read())
    stale = [e["uuid"] for e in grid["entries"] if e.get("network") == TVH_NETWORK_NAME and e.get("epg") != 0]
    if not stale:
        return 0
    node = json.dumps([{"uuid": u, "epg": 0} for u in stale])
    with _tvh_req("/api/idnode/save", data={"node": node}):
        pass
    return len(stale)


def disable_dab_channel_epgauto():
    # map_unmapped_dab_services() only ever disables epgauto on channels it
    # JUST mapped in that same pass - a channel mapped in an earlier scan/
    # network-recreate cycle (i.e. almost all of them, across a session
    # with several rescans) is "already mapped" by the time later passes
    # run and never gets touched again, leaving epgauto=true. Live-confirmed
    # (2026-09-26) that this is what was causing periodic ~20-30s phantom
    # tune-ins across random DAB+ v2 channels - TVHeadend's EPG grabber
    # trying to fetch programme data these synthetic ffmpeg streams don't
    # have. Re-checking every channel every pass (like disable_dab_mux_epg()
    # already does at the mux level) is what actually keeps this off.
    with _tvh_req("/api/mpegts/service/grid?start=0&limit=500") as resp:
        grid = json.loads(resp.read())
    channel_uuids = sorted({c for e in grid["entries"] if e.get("network") == TVH_NETWORK_NAME for c in e.get("channel", [])})
    if not channel_uuids:
        return 0
    with _tvh_req("/api/channel/grid?start=0&limit=2000&all=1") as resp:
        chgrid = json.loads(resp.read())
    by_uuid = {c["uuid"]: c for c in chgrid["entries"]}
    stale = [u for u in channel_uuids if u in by_uuid and by_uuid[u].get("epgauto") is not False]
    if not stale:
        return 0
    node = json.dumps([{"uuid": u, "epgauto": False} for u in stale])
    with _tvh_req("/api/idnode/save", data={"node": node}):
        pass
    return len(stale)


def _get_tag_uuid(name):
    with _tvh_req("/api/channeltag/grid?start=0&limit=200") as resp:
        grid = json.loads(resp.read())
    match = next((t for t in grid["entries"] if t.get("name") == name), None)
    return match["uuid"] if match else None


def tag_dab_channels_as_radio():
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
            n = disable_dab_channel_epgauto()
            if n:
                print(f"[mapper] disabled epgauto on {n} DAB+ channel(s)")
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


class _DabService(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("id", ctypes.c_uint8),
        ("service_name", ctypes.c_char * 17),
        ("sid", ctypes.c_uint32),
        ("comp", ctypes.c_uint16 * 10),
        ("n_comp", ctypes.c_int8),
    ]


class _DabServiceData(ctypes.Structure):
    # matches struct dab_service_data in mediacmds.h exactly - status(u8),
    # type(u8, DLS=1/MOT=2), len(u16), bufq(u8, device-side queue depth),
    # data[] (flexible array member in C, declared here as a real 4096-byte
    # array). _pack_=1 is essential: without it ctypes pads len to a 4-byte
    # boundary and every field after it reads garbage (len ends up in the
    # billions) - this bit Claude once already this session, keep it.
    _pack_ = 1
    _fields_ = [
        ("status", ctypes.c_uint8),
        ("type", ctypes.c_uint8),
        ("len", ctypes.c_uint16),
        ("bufq", ctypes.c_uint8),
        ("data", ctypes.c_uint8 * 4096),
    ]


DLS_TYPE = 1
MOT_TYPE = 2

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


def _find_active_dab_device():
    """Only returns a device that's actually locked right now (DAB_GET_TUNER
    status & DAB_HAS_LOCK) - unlike the plain "most recently touched, opens
    OK" heuristic dab_bridge.py uses, which live-tested fine for read-only
    signal queries but isn't precise enough for MOT/DLS capture: an
    unlocked-but-openable stale device node returns rv=0/len=0 forever
    rather than erroring, silently starving the capture loop."""
    lib = _get_mcsimple()
    candidates = sorted(glob.glob("/dev/dab*"), key=lambda p: os.stat(p).st_mtime, reverse=True)
    for device in candidates:
        fd = lib.net_open(device.encode(), 0)
        if fd < 0:
            continue
        try:
            t = _DabTuner()
            lib.net_ioctl(fd, DAB_GET_TUNER, ctypes.byref(t))
            if t.status & DAB_HAS_LOCK:
                return device
        finally:
            lib.net_close(fd)
    return None


def _current_station_frequency(channel_name):
    if not channel_name:
        return None
    try:
        renames = load_renames()
        for raw_name, url in fetch_stations():
            display_name = renames.get(raw_name, raw_name)
            if display_name == channel_name or raw_name == channel_name:
                parsed = urllib.parse.urlsplit(url)
                query = urllib.parse.parse_qs(parsed.query)
                freq = (query.get("freq") or [None])[0]
                return int(freq) if freq else None
    except Exception:
        return None
    return None


def get_dab_signal_info():
    # Checking "FM isn't active" alone wasn't sufficient - even opening
    # /dev/dab* with nothing else going on live-confirmed (2026-09-26) as
    # enough to force a tuner mode switch. The correct condition is "DAB+
    # is actually the one being streamed by TVHeadend right now", matching
    # _mot_dls_loop()'s own gate. Trade-off: the signal/lock tile in HA now
    # only shows real values while a DAB+ v2 station is actually playing,
    # not just idly - deliberate, since polling it otherwise is exactly
    # what was causing unwanted tuner switches.
    if not get_dab_now_playing()["playing"]:
        return {
            "device": None, "locked": False, "rssi": None, "snr": None,
            "fic_quality": None, "cnr": None, "ensemble": None,
            "sid": None, "service_name": None, "frequency": None,
        }
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
            svc = _DabService()
            lib.net_ioctl(fd, DAB_GET_SERVICE, ctypes.byref(svc))
            service_name = svc.service_name.decode("ascii", "replace").strip()
            channel = get_dab_now_playing()["playing"]
            channel_name = channel[0] if channel else None
            return {
                "device": device,
                "locked": bool(tuner.status & DAB_HAS_LOCK),
                "rssi": tuner.rssi,
                "snr": tuner.snr,
                "fic_quality": tuner.fic_quality,
                "cnr": tuner.cnr,
                "ensemble": ens.label.decode("ascii", "replace").strip(),
                "sid": f"0x{svc.sid:X}" if svc.sid else None,
                "service_name": service_name or None,
                "frequency": _current_station_frequency(channel_name),
            }
        finally:
            lib.net_close(fd)
    return {
        "device": None, "locked": False, "rssi": None, "snr": None,
        "fic_quality": None, "cnr": None, "ensemble": None,
        "sid": None, "service_name": None, "frequency": None,
    }


def get_dab_now_playing():
    with _tvh_req("/api/status/subscriptions") as resp:
        data = json.loads(resp.read())
    channels = sorted({
        e["channel"] for e in data.get("entries", [])
        if f"/{TVH_NETWORK_NAME}/" in (e.get("service") or "") and e.get("channel")
    })
    return {"playing": channels}


def _fm_v2_is_playing():
    # The physical tuner is exclusive across DAB+ and FM (Sundtek's own
    # wiki: using one locks the other) - mutual guard alongside
    # fm_bridge2.py's matching _dab_v2_is_playing() so this bridge's own
    # background loop never touches the device while FM is the one
    # actually holding it, even if TVH's own DAB+ v2 subscription state is
    # momentarily stale/racy.
    try:
        with _tvh_req("/api/status/subscriptions") as resp:
            data = json.loads(resp.read())
        return any("/FM v2/" in (e.get("service") or "") for e in data.get("entries", []))
    except Exception:
        return False


def _debug_log(msg):
    try:
        with open(DIGITALDATA_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# MOT slideshow + DLS decoding, straight off the ctypes SDK.
#
# Validated live against this box (2026-09-24): a full-band DAB scan
# followed by capturing DAB_GET_DIGITAL_SERVICE_DATA frames while streaming
# RTL RADIO reassembled a complete, valid JPEG (segments 0-28, correct SOI
# 0xFFD8 and EOI 0xFFD9, ContentName parameter decoded as "99.JPG") purely
# from these functions - see the DAB_TVH_Bridge project's dev notes for the
# full capture/parse transcript.
#
# Data Group header parsing (flags/type/continuity/segment field/transport
# ID) follows ETSI EN 300 401 5.3.3.1 and is exact - cross-checked against
# ebu/python-dabmsc's decoder and confirmed bit-for-bit against real
# captured bytes. The MOT header's *internal* body_size/header_size field
# offsets did not decode to sane values against the same reference (some
# discrepancy in the user-access-field length accounting that wasn't worth
# chasing further), so rather than trust an offset that's already proven
# wrong once, the two things this bridge actually needs are found directly
# and self-verified instead:
#   - the body image's start offset, via its own magic bytes (JPEG/PNG)
#   - the ContentName parameter, via its distinctive leading byte
#     (PLI=3 + param_id=12 packs into one byte, 0b11001100 = 0xCC),
#     followed by a plausible short-form length + printable text
# Both are self-checking (image magic bytes / printable-text check), so a
# false match is essentially impossible even without the exact header
# offsets.
# ---------------------------------------------------------------------------

_IMAGE_MAGIC = [
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
]


def _bit(data_int, total_bits, pos):
    return (data_int >> (total_bits - 1 - pos)) & 1


def _bits_range(data_int, total_bits, start, end):
    width = end - start
    shift = total_bits - end
    return (data_int >> shift) & ((1 << width) - 1)


def _parse_datagroup(raw):
    """Returns None for anything too short/malformed to be a real Data
    Group - live traffic includes occasional non-Data-Group PAD filler
    (observed dg type 0 with no segment/user-access fields at all)."""
    n = len(raw)
    if n < 2:
        return None
    total_bits = n * 8
    val = int.from_bytes(raw, "big")

    ext_flag = _bit(val, total_bits, 0)
    crc_flag = _bit(val, total_bits, 1)
    seg_flag = _bit(val, total_bits, 2)
    uaf_flag = _bit(val, total_bits, 3)
    dg_type = _bits_range(val, total_bits, 4, 8)

    header_size = 16
    if ext_flag:
        header_size += 16

    last = None
    segment_index = None
    if seg_flag:
        if header_size + 16 > total_bits:
            return None
        last = bool(_bit(val, total_bits, header_size))
        segment_index = _bits_range(val, total_bits, header_size + 1, header_size + 16)
        header_size += 16

    transport_id = None
    if uaf_flag and total_bits >= 56:
        transport_id = _bits_range(val, total_bits, 40, 56)

    payload_end_bits = total_bits - (16 if crc_flag else 0)
    payload_end_byte = max(payload_end_bits // 8, 0)

    return {
        "type": dg_type, "last": last, "segment_index": segment_index,
        "transport_id": transport_id, "region": raw[:payload_end_byte],
    }


def _find_content_name(region):
    for i in range(len(region) - 2):
        if region[i] != 0xCC:  # PLI=3 (0b11) + param_id=12 (0b001100)
            continue
        length_byte = region[i + 1]
        if length_byte & 0x80:
            continue  # long form (>127 bytes) - not expected for a filename, skip
        length = length_byte & 0x7F
        if not (2 <= length <= 40) or i + 2 + length > len(region):
            continue
        data = region[i + 2: i + 2 + length]
        name = data[1:].decode("latin-1", errors="replace")
        if name.isprintable() and len(name.strip()) >= 3:
            return name.strip()
    return None


def _find_image_offset(region):
    for magic, ext in _IMAGE_MAGIC:
        idx = region.find(magic)
        if idx != -1:
            return idx, ext
    return None, None


_ARTWORK_TYPE_RE = re.compile(r"^\d{8}_\d{6}Z_([A-Za-z0-9]+)_\d+\.\w+$")


def _artwork_type_from_filename(name):
    m = _ARTWORK_TYPE_RE.match(name or "")
    return m.group(1).lower() if m else DEFAULT_ARTWORK_TYPE


# Only ever one current image on disk - a new one replaces it outright
# (single fixed path, atomic os.replace()) rather than keeping one file per
# artwork type around, so HA/the dashboard always shows just the latest
# slide instead of accumulating every type a station has ever sent.
CURRENT_ARTWORK_PATH = f"{ARTWORK_DIR}/current.jpg"

_digitaldata_lock = threading.Lock()
_artwork_state = {"type": None, "updated_at": None, "channel": None}
_digitaldata_channel = None
_digitaldata_generation = 0

# Dynamic Label (song/programme title) state, keyed by nothing (one tuner,
# one current station) - {"text": str, "updated_at": ts, "channel": name}.
# DLS format per the driver header's own comment ("first two bytes are
# header, EN 300 401 7.4.5.2, 128 + '0'") - not live-verified on this box
# yet (no station tested so far actually broadcasts DLS at the moment this
# was written), so kept defensive: anything that doesn't decode to a
# plausible printable string is simply dropped rather than shown.
_dls_state = {"text": None, "updated_at": None, "channel": None}


def _decode_dls(raw):
    if len(raw) < 3:
        return None
    # byte0: toggle/first/last/command flags + field 1 (best-effort, not
    # relied on); byte1: field 2 / charset; text follows, often
    # null-padded to a fixed segment size.
    text = raw[2:].split(b"\x00", 1)[0].decode("latin-1", errors="replace").strip()
    if text and text.isprintable() and len(text) >= 2:
        return text
    return None


class _MotAssembly:
    __slots__ = ("name", "bodies", "body_offset", "ext", "last_seg", "first_seen")

    def __init__(self):
        self.name = None
        self.bodies = {}
        self.body_offset = None
        self.ext = None
        self.last_seg = None
        self.first_seen = time.time()


def _mot_dls_loop():
    """Mirrors dab_bridge.py's digital_data_loop() channel-tracking (same
    TVHeadend-subscription-based detection, same debounce), but decodes
    MOT/DLS directly instead of managing a mediaclient subprocess."""
    global _digitaldata_channel, _digitaldata_generation
    lib = _get_mcsimple()
    pending_channel = None
    fd = -1
    open_device = None
    assemblies = {}  # transport_id -> _MotAssembly, for the current channel only

    while True:
        try:
            playing = get_dab_now_playing()["playing"]
            channel = playing[0] if playing and not _fm_v2_is_playing() else None

            if channel != _digitaldata_channel:
                if pending_channel != channel:
                    pending_channel = channel
                    channel = _digitaldata_channel
            else:
                pending_channel = channel

            if channel != _digitaldata_channel:
                _debug_log(f"[loop] channel change: {_digitaldata_channel!r} -> {channel!r}")
                _digitaldata_channel = channel
                assemblies = {}
                with _digitaldata_lock:
                    _artwork_state.update(type=None, updated_at=None, channel=None)
                    _digitaldata_generation += 1
                    _dls_state.update(text=None, updated_at=None, channel=channel)
                try:
                    os.remove(CURRENT_ARTWORK_PATH)
                except OSError:
                    pass
                if fd >= 0:
                    lib.net_close(fd)
                    fd = -1
                    open_device = None

            if not channel:
                time.sleep(1)
                continue

            device = _find_active_dab_device()
            if device != open_device:
                if fd >= 0:
                    lib.net_close(fd)
                fd = lib.net_open(device.encode(), 0) if device else -1
                open_device = device

            if fd < 0:
                time.sleep(1)
                continue

            probe = _DabServiceData()
            probe.status = 1
            rv = lib.net_ioctl(fd, DAB_GET_DIGITAL_SERVICE_DATA, ctypes.byref(probe))
            if rv == 0 and probe.len > 0:
                fetch = _DabServiceData()
                fetch.status = 0
                fetch.type = 0
                fetch.len = probe.len
                rv2 = lib.net_ioctl(fd, DAB_GET_DIGITAL_SERVICE_DATA, ctypes.byref(fetch))
                if rv2 == 0:
                    raw = bytes(fetch.data[:fetch.len])
                    if fetch.type == MOT_TYPE:
                        _handle_mot_frame(raw, channel, assemblies)
                    elif fetch.type == DLS_TYPE:
                        text = _decode_dls(raw)
                        if text:
                            with _digitaldata_lock:
                                _dls_state.update(text=text, updated_at=time.time(), channel=channel)
            else:
                time.sleep(0.02)

            # opportunistically drop assemblies that stalled out (missed
            # segments, station switched slides mid-transfer) so the dict
            # doesn't grow forever across a long session
            stale_cutoff = time.time() - 60
            for tid in [t for t, a in assemblies.items() if a.first_seen < stale_cutoff]:
                del assemblies[tid]
        except Exception as e:
            _debug_log(f"[loop] error: {e!r}")
            time.sleep(1)


def _handle_mot_frame(raw, channel, assemblies):
    dg = _parse_datagroup(raw)
    if dg is None or dg["transport_id"] is None:
        return
    tid = dg["transport_id"]
    entry = assemblies.setdefault(tid, _MotAssembly())

    if dg["type"] == 3:  # MOT header info
        name = _find_content_name(dg["region"])
        if name:
            entry.name = name
    elif dg["type"] == 4:  # MOT body
        region = dg["region"]
        if entry.body_offset is None:
            off, ext = _find_image_offset(region)
            if off is not None:
                entry.body_offset = off
                entry.ext = ext
        if entry.body_offset is not None and dg["segment_index"] is not None:
            entry.bodies[dg["segment_index"]] = region[entry.body_offset:]
        if dg["last"]:
            entry.last_seg = dg["segment_index"]

    if entry.last_seg is None or entry.body_offset is None:
        return
    n_segs = entry.last_seg + 1
    if any(i not in entry.bodies for i in range(n_segs)):
        return  # not complete yet - a missed segment might still arrive

    data = b"".join(entry.bodies[i] for i in range(n_segs))
    if not (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"):
        _debug_log(f"[mot] tid={tid} assembled {len(data)} bytes but bad magic - discarded")
        del assemblies[tid]
        return
    if entry.ext == "jpg" and data[-2:] != b"\xff\xd9":
        _debug_log(f"[mot] tid={tid} assembled {len(data)} bytes but missing JPEG EOI - discarded")
        del assemblies[tid]
        return

    artwork_type = _artwork_type_from_filename(entry.name)
    try:
        os.makedirs(ARTWORK_DIR, exist_ok=True)
        tmp_path = f"{CURRENT_ARTWORK_PATH}.{tid}.tmp"
        with open(tmp_path, "wb") as f:
            f.write(data)
        os.replace(tmp_path, CURRENT_ARTWORK_PATH)
        with _digitaldata_lock:
            _artwork_state.update(type=artwork_type, updated_at=time.time(), channel=channel)
        _debug_log(f"[mot] tid={tid} name={entry.name!r} channel={channel!r} type={artwork_type!r} SUCCESS size={len(data)}")
    except OSError as e:
        _debug_log(f"[mot] tid={tid} write failed: {e}")
    del assemblies[tid]


def get_dab_artwork_info():
    with _digitaldata_lock:
        state = dict(_artwork_state)
    return {"available": state["updated_at"] is not None, **state}


def get_dab_now_playing_text():
    with _digitaldata_lock:
        return dict(_dls_state)


# ---------------------------------------------------------------------------
# Scan / station list / location management - unchanged from dab_bridge.py.
# ---------------------------------------------------------------------------

_rescan_lock = threading.Lock()
_rescan_state = {"running": False, "returncode": None}
_dab_scan_progress = {
    "dab_scanning": False, "current_freq": None, "current_stations": None,
    "last_freq": None, "last_stations": None, "last_finished_at": None,
}


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

    with _rescan_lock:
        _dab_scan_progress["dab_scanning"] = True
        _dab_scan_progress["current_freq"] = None
        _dab_scan_progress["current_stations"] = None

    last_freq = None
    stable = 0
    station_count = None
    try:
        for _ in range(48):
            time.sleep(5)
            try:
                arr = json.loads(_sundtek_post(f"cmd=scanstat&deviceid={DAB_DEVICE_ID}"))
            except Exception:
                continue
            freq = arr[6] if len(arr) > 6 else None
            station_count = arr[12] if len(arr) > 12 else station_count
            with _rescan_lock:
                _dab_scan_progress["current_freq"] = freq
                _dab_scan_progress["current_stations"] = station_count
            stable = stable + 1 if freq == last_freq else 0
            last_freq = freq
            if stable >= 3:
                break
        _rescan_log(f"scan settled at freq={last_freq}, {station_count} station(s) found")
    finally:
        with _rescan_lock:
            _dab_scan_progress["dab_scanning"] = False
            _dab_scan_progress["current_freq"] = None
            _dab_scan_progress["current_stations"] = None
            _dab_scan_progress["last_freq"] = last_freq
            _dab_scan_progress["last_stations"] = station_count
            _dab_scan_progress["last_finished_at"] = time.time()


def _restart_tvheadend():
    _rescan_log("restarting TVHeadend so it re-scans the DAB+ v2 IPTV network ...")
    subprocess.run(["systemctl", "restart", "service.tvheadend43.service"], check=True)
    time.sleep(8)


def _run_rescan():
    try:
        _set_active_location(None)
        _rescan_log("[1/3] restarting sundtek-mediasrv + sundtek-rtspd ...")
        _perform_dab_scan()
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
        _rescan_log(f"[1/3] scanning for DAB+ location '{name}' ...")
        _perform_dab_scan()
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


# Cross-process lock shared with fm_bridge2.py (same path, same mechanism)
# - DAB+ and FM scans both drive the same exclusive physical tuner one
# frequency at a time, so only one of the two bridges may actively scan
# at once, same reasoning as the streaming-side mutual guard
# (_fm_v2_is_playing()/_dab_v2_is_playing()). flock() rather than a plain
# lock file so a crashed holder can't leave this stuck forever - the OS
# releases it automatically when the holding process exits, however it
# exits.
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
    """TVHeadend's own scan-queue timer (fires every 120s, confirmed by
    reading mpegts_network_scan.c upstream) processes DAB+ v2 and FM v2
    independently and concurrently - TVHeadend has no idea the two
    networks share one physical tuner (they're just two ordinary,
    unrelated IPTV networks from its point of view). The explicit lock in
    _start_background() only covers scans a bridge itself triggers; this
    covers TVHeadend's own autonomous background scanning of already-
    existing muxes (init-scan retries, idle-scan sweeps, a freshly
    refetched M3U's new entries), which neither bridge otherwise controls.

    Only acts while no explicit scan of our own is running (that case is
    already covered by _start_background() holding the lock) - if our own
    network has muxes TVHeadend itself put in a pending/active scan state
    and FM currently holds the shared lock, this defers them
    (scan_state -> 0/INACTIVE, pulling them back out of TVHeadend's own
    queue) until FM releases it, then lets them go pending again next
    poll.
    """
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
                _debug_log(f"[tvh-watchdog] skipped {len(already_ok)} already-OK DAB+ v2 mux(es) - services unchanged")

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
                _debug_log(f"[tvh-watchdog] deferred {len(pending)} pending DAB+ v2 mux(es) - FM holds the shared tuner lock")
        except Exception as e:
            _debug_log(f"[tvh-watchdog] error: {e!r}")


def _start_background(target, args):
    with _rescan_lock:
        if _rescan_state["running"]:
            return False
        if not _try_acquire_tuner_scan_lock():
            _rescan_log("FM is currently scanning/streaming-scanning - refusing to start (shared tuner)")
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


def _tvh_dab_mux_grid():
    with _tvh_req("/api/mpegts/mux/grid?start=0&limit=500") as resp:
        grid = json.loads(resp.read())
    return [e for e in grid["entries"] if e.get("network") == TVH_NETWORK_NAME]


def _freq_from_mux(mux):
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(mux.get("iptv_url") or "").query)
    return (q.get("freq") or [None])[0]


def _run_ordered_scan():
    # TVHeadend's own scan queue does NOT respect mux/M3U order at all - its
    # tie-break for equal-weight (all init-scan) muxes is a comparison of
    # each mux's own randomly-assigned internal UUID (confirmed by reading
    # mpegts_network_scan.c/mpegts_mux.c upstream), so it hops between
    # frequencies effectively at random. Forcing scan_state=3 (ACTIVE) on a
    # mux queues it with SUBSCRIPTION_PRIO_SCAN_USER - the same mechanism
    # dvbt_bridge.py already relies on (see TVHEADEND_QUIRKS.md point 5) -
    # which outranks the ordinary init-scan weight, so driving that
    # frequency-group by frequency-group here actually determines the order,
    # where nothing in TVHeadend's own configuration can.
    try:
        muxes = _tvh_dab_mux_grid()
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
                fresh_by_uuid = {m["uuid"]: m for m in _tvh_dab_mux_grid()}
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
        dab_scan = dict(_dab_scan_progress)

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
        dab_muxes = [e for e in grid["entries"] if e.get("network") == TVH_NETWORK_NAME]
        tvheadend_muxes = len(dab_muxes)
        tvh_mux_scan_pending = sum(1 for e in dab_muxes if e.get("scan_state") in (1, 2))
        tvh_mux_scan_active = sum(1 for e in dab_muxes if e.get("scan_state") == 3)
    except Exception:
        pass

    return {
        "running": running, "returncode": returncode, "station_count": station_count,
        "tvheadend_muxes": tvheadend_muxes,
        "dab_scanning": dab_scan["dab_scanning"],
        "dab_scan_current_freq": dab_scan["current_freq"],
        "dab_scan_current_stations": dab_scan["current_stations"],
        "dab_last_scan_freq": dab_scan["last_freq"],
        "dab_last_scan_stations": dab_scan["last_stations"],
        "dab_last_scan_finished_at": dab_scan["last_finished_at"],
        "tvh_mux_scan_pending": tvh_mux_scan_pending,
        "tvh_mux_scan_active": tvh_mux_scan_active,
        "tvh_scanning": bool((tvh_mux_scan_pending or 0) + (tvh_mux_scan_active or 0)),
        "active_location": _get_active_location(),
        "locations": list_locations(),
        "log_tail": tail,
    }


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
    excluded = load_excluded()
    renames = load_renames()
    result = []
    for raw_name, _url in fetch_stations():
        display_name = renames.get(raw_name, raw_name)
        is_excluded = display_name in excluded or raw_name in excluded
        result.append({"raw_name": raw_name, "display_name": display_name, "excluded": is_excluded})
    return result


def esc(value):
    return value.replace("\\", "\\\\").replace(" ", "\\ ")


def _fetch_rtspd_station_list():
    """Authoritative station name -> stream URL list straight from rtspd's
    own M3U export (despite the name, /dab.m3u contains every known
    station regardless of mode - DAB+ and FM mixed together, filtered by
    delsys below).

    This replaces reconstructing URLs from cmd=chlist, which always
    omitted the `comp=` query parameter (the DAB service's audio component
    index within its multiplex) and formatted `sid` differently
    (0x-prefixed hex). Live-confirmed (2026-09-26): a real M3U downloaded
    directly from rtspd for comparison has both of these, streams built
    from chlist's own URL format were consistently unreliable, and this
    exact format - fetched here instead of reconstructed - just works.
    """
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
        if (q.get("delsys") or [None])[0] != str(DAB_DELSYS):
            continue
        if not name or "EPG" in name.upper():
            continue
        stations.append((name, url))
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


def _parse_station_url(url):
    """Extracts (delsys, freq, sid) from a raw sundtek stream URL - sid is
    None for FM-style URLs (not used by this DAB bridge, but kept generic)."""
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    delsys = (q.get("delsys") or [None])[0]
    freq = (q.get("freq") or [None])[0]
    sid = (q.get("sid") or [None])[0]
    return delsys, freq, sid


def _with_rtspd_host(url):
    return urllib.parse.urlunsplit(
        urllib.parse.urlsplit(url)._replace(netloc=f"{RTSPD_STREAM_HOST}:22000")
    )


def build_m3u(stations):
    lines = ["#EXTM3U"]
    entries = []
    for name, url in prepare_stations(stations):
        delsys, freq, sid = _parse_station_url(url)
        if not delsys or not freq:
            continue
        entries.append((freq, name, _with_rtspd_host(url)))
    # group same-frequency stations together (stable sort keeps
    # prepare_stations()'s own ordering within each frequency group) so
    # TVHeadend's mux scan visits one ensemble's services back to back
    # instead of jumping between frequencies for every mux
    entries.sort(key=lambda e: e[0])
    for freq, name, url in entries:
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
        if path == "/dab/now_playing_text":
            _send_json(self, 200, get_dab_now_playing_text())
            return
        if path == "/dab/artwork_info":
            _send_json(self, 200, get_dab_artwork_info())
            return
        if path == "/dab/digitaldata_debug":
            try:
                with open(DIGITALDATA_DEBUG_LOG, "r", encoding="utf-8", errors="replace") as f:
                    tail = f.readlines()[-200:]
            except FileNotFoundError:
                tail = []
            _send_json(self, 200, {"log_tail": "".join(tail)})
            return
        if path == "/dab/artwork":
            if not os.path.exists(CURRENT_ARTWORK_PATH):
                self.send_response(404)
                self.end_headers()
                return
            with open(CURRENT_ARTWORK_PATH, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
            return
        if path not in ("/DAB2.m3u", "/"):
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

        if path in ("/dab/rescan", "/rescan"):
            started = start_rescan()
            _send_json(self, 202 if started else 409, {"started": started})
            return
        if path == "/dab/scan_ordered":
            started = start_ordered_scan()
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
    threading.Thread(target=_mot_dls_loop, daemon=True).start()
    threading.Thread(target=_tvh_scan_watchdog_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()
