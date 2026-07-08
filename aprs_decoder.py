"""
APRS packet decoder — adapté de eusef/eusef-aprs-tui (MIT).
Standalone, requiert aprslib dans le venv.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


# ── APRSPacket ─────────────────────────────────────────────────
@dataclass(frozen=True)
class APRSPacket:
    raw:            str
    source:         str            = ""
    destination:    str            = ""
    path:           tuple          = ()
    info_type:      str            = "unknown"
    timestamp:      datetime|None  = None
    transport:      str            = ""
    parse_error:    str|None       = None
    latitude:       float|None     = None
    longitude:      float|None     = None
    symbol_table:   str|None       = None
    symbol_code:    str|None       = None
    altitude:       float|None     = None
    speed:          float|None     = None
    course:         int|None       = None
    comment:        str|None       = None
    addressee:      str|None       = None
    message_text:   str|None       = None
    message_id:     str|None       = None
    is_ack:         bool           = False
    is_rej:         bool           = False
    wx_temperature: float|None     = None
    wx_humidity:    int|None       = None
    wx_pressure:    float|None     = None
    wx_wind_speed:  float|None     = None
    wx_wind_dir:    int|None       = None
    wx_rain_1h:     float|None     = None
    object_name:    str|None       = None
    alive:          bool           = True
    status_text:    str|None       = None
    parsed:         dict           = field(default_factory=dict)


# ── Helpers ────────────────────────────────────────────────────
def _sf(d, k):
    v = d.get(k)
    try:    return float(v) if v is not None else None
    except: return None

def _si(d, k):
    v = d.get(k)
    try:    return int(v) if v is not None else None
    except: return None

_m2ft  = lambda v: v / 0.3048  if v is not None else None
_kph2k = lambda v: v / 1.852   if v is not None else None
_c2f   = lambda v: v * 1.8 + 32 if v is not None else None
_ms2mp = lambda v: v / 0.44704 if v is not None else None
_mm2in = lambda v: v / 25.4    if v is not None else None


# ── Extracteurs par format aprslib ─────────────────────────────
def _pos(d): return {
    "info_type":    "position",
    "latitude":     _sf(d, "latitude"),
    "longitude":    _sf(d, "longitude"),
    "symbol_table": d.get("symbol_table"),
    "symbol_code":  d.get("symbol"),
    "altitude":     _m2ft(_sf(d, "altitude")),
    "comment":      d.get("comment"),
}

def _mice(d): return {
    "info_type":    "mic-e",
    "latitude":     _sf(d, "latitude"),
    "longitude":    _sf(d, "longitude"),
    "symbol_table": d.get("symbol_table"),
    "symbol_code":  d.get("symbol"),
    "speed":        _kph2k(_sf(d, "speed")),
    "course":       _si(d, "course"),
    "altitude":     _m2ft(_sf(d, "altitude")),
    "comment":      d.get("comment"),
}

def _msg(d):
    addr = (d.get("addresse") or d.get("addressee") or "").strip()
    return {
        "info_type":    "message",
        "addressee":    addr or None,
        "message_text": d.get("message_text"),
        "message_id":   d.get("msgNo"),
        "is_ack":       d.get("response") == "ack",
        "is_rej":       d.get("response") == "rej",
    }

def _wx(d):
    wx = d.get("weather", {}); f: dict[str, Any] = {"info_type": "weather"}
    if isinstance(wx, dict):
        f.update(
            wx_temperature = _c2f(_sf(wx, "temperature")),
            wx_humidity    = _si(wx, "humidity"),
            wx_pressure    = _sf(wx, "pressure"),
            wx_wind_speed  = _ms2mp(_sf(wx, "wind_speed")),
            wx_wind_dir    = _si(wx, "wind_direction"),
            wx_rain_1h     = _mm2in(_sf(wx, "rain_1h")),
        )
    f.update(latitude=_sf(d,"latitude"), longitude=_sf(d,"longitude"), comment=d.get("comment"))
    return f

def _obj(d): return {
    "info_type":    "object",
    "object_name":  (d.get("object_name") or "").strip() or None,
    "alive":        d.get("alive", True),
    "latitude":     _sf(d, "latitude"),
    "longitude":    _sf(d, "longitude"),
    "symbol_table": d.get("symbol_table"),
    "symbol_code":  d.get("symbol"),
    "comment":      d.get("comment"),
}

_FMT: dict[str, Any] = {
    "uncompressed": _pos, "compressed": _pos,
    "mic-e":        _mice,
    "message":      _msg,
    "wx":           _wx,
    "object":       _obj,
    "status":       lambda d: {"info_type": "status", "status_text": d.get("status")},
}


# ── Point d'entrée public ──────────────────────────────────────
def _hdr(raw: str):
    if ":" not in raw or ">" not in raw: return "", "", ()
    hdr = raw.split(":", 1)[0]
    src, _, rest = hdr.partition(">")
    parts = rest.split(",")
    return src, parts[0] if parts else "", tuple(parts[1:])


def decode_packet(raw: str, transport: str = "") -> APRSPacket:
    """Décode un paquet APRS texte → APRSPacket. Ne lève jamais."""
    now = datetime.now(UTC)
    src, dst, path = _hdr(raw)
    try:
        import aprslib
        data = aprslib.parse(raw)
    except ImportError:
        return APRSPacket(raw=raw, source=src, destination=dst, path=path,
                          transport=transport, timestamp=now,
                          parse_error="aprslib non installé")
    except Exception as e:
        return APRSPacket(raw=raw, source=src, destination=dst, path=path,
                          transport=transport, timestamp=now, parse_error=str(e))
    src  = data.get("from", src)
    dst  = data.get("to",   dst)
    rp   = data.get("path", [])
    path = tuple(rp) if isinstance(rp, (list, tuple)) else ()
    extra = _FMT.get(data.get("format", ""), lambda d: {"info_type": "raw"})(data)
    return APRSPacket(raw=raw, source=src, destination=dst, path=path,
                      transport=transport, timestamp=now, parsed=dict(data), **extra)
