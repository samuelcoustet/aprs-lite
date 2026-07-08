"""
Station tracker APRS — adapté de eusef/eusef-aprs-tui (MIT).
Maintient la table des stations entendues avec distance et cap.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field


@dataclass
class StationRecord:
    callsign:          str
    last_heard:        float       = 0.0   # time.monotonic()
    latitude:          float|None  = None
    longitude:         float|None  = None
    symbol_table:      str|None    = None
    symbol_code:       str|None    = None
    comment:           str|None    = None
    packet_count:      int         = 0
    last_info_type:    str         = ""
    distance_km:       float|None  = None
    bearing:           float|None  = None
    sources:           set         = field(default_factory=set)
    position_history:  list        = field(default_factory=list)


# ── Géodésie ──────────────────────────────────────────────────
def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat/2)**2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon/2)**2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def calc_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1, lon1 = math.radians(lat1), math.radians(lon1)
    lat2, lon2 = math.radians(lat2), math.radians(lon2)
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


_CARDINALS = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
              "S","SSO","SO","OSO","O","ONO","NO","NNO"]

def bearing_cardinal(b: float) -> str:
    return _CARDINALS[round(b / 22.5) % 16]


def time_ago(t: float) -> str:
    s = int(time.monotonic() - t)
    if s < 5:    return "maintenant"
    if s < 60:   return f"{s}s"
    if s < 3600: return f"{s//60}min"
    return f"{s//3600}h{(s%3600)//60:02d}m"


# ── StationTracker ────────────────────────────────────────────
class StationTracker:
    def __init__(self, own_lat: float|None = None, own_lon: float|None = None,
                 max_track: int = 50) -> None:
        self._stations: dict[str, StationRecord] = {}
        self._own_lat  = own_lat
        self._own_lon  = own_lon
        self._max_track = max_track

    def set_own_position(self, lat: float, lon: float) -> None:
        self._own_lat = lat
        self._own_lon = lon
        for s in self._stations.values():
            self._update_dist(s)

    def update(self, pkt) -> None:
        """Met à jour la table depuis un APRSPacket entrant."""
        if not pkt.source:
            return
        cs = pkt.source.upper()
        if cs not in self._stations:
            self._stations[cs] = StationRecord(callsign=cs)
        s = self._stations[cs]
        s.last_heard      = time.monotonic()
        s.packet_count   += 1
        s.last_info_type  = pkt.info_type
        if pkt.transport:
            s.sources.add(pkt.transport)
        if (pkt.info_type in ("position", "mic-e")
                and pkt.latitude is not None and pkt.longitude is not None):
            if pkt.latitude != s.latitude or pkt.longitude != s.longitude:
                s.position_history.append((pkt.latitude, pkt.longitude, time.time()))
                if len(s.position_history) > self._max_track:
                    s.position_history = s.position_history[-self._max_track:]
            s.latitude     = pkt.latitude
            s.longitude    = pkt.longitude
            s.symbol_table = pkt.symbol_table
            s.symbol_code  = pkt.symbol_code
            s.comment      = pkt.comment
            self._update_dist(s)

    def _update_dist(self, s: StationRecord) -> None:
        if (self._own_lat and self._own_lon
                and s.latitude is not None and s.longitude is not None):
            s.distance_km = haversine(self._own_lat, self._own_lon, s.latitude, s.longitude)
            s.bearing     = calc_bearing(self._own_lat, self._own_lon, s.latitude, s.longitude)

    def get_stations(self, sort_by: str = "last_heard") -> list[StationRecord]:
        stns = list(self._stations.values())
        if sort_by == "last_heard":
            stns.sort(key=lambda s: s.last_heard, reverse=True)
        elif sort_by == "distance":
            stns.sort(key=lambda s: s.distance_km if s.distance_km is not None else 1e9)
        elif sort_by == "callsign":
            stns.sort(key=lambda s: s.callsign)
        elif sort_by == "packet_count":
            stns.sort(key=lambda s: s.packet_count, reverse=True)
        return stns

    def get_station(self, callsign: str) -> StationRecord | None:
        return self._stations.get(callsign.upper())

    @property
    def count(self) -> int:
        return len(self._stations)
