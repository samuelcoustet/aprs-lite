"""
Renderer géographique braille — Natural Earth GeoJSON.
Dessine coastlines, frontières terrestres et villes sur un BrailleCanvas.
"""
from __future__ import annotations
import json, math
from pathlib import Path

GEODATA_DIR = Path("/opt/aprs-lite/geodata")
_cache: dict = {}

def geo_available() -> bool:
    return any((GEODATA_DIR / f).exists() for f in (
        "ne_50m_coastline.geojson", "ne_110m_coastline.geojson"))

def _load(name: str) -> list:
    if name in _cache:
        return _cache[name]
    p = GEODATA_DIR / name
    if not p.exists():
        _cache[name] = []
        return []
    try:
        feats = json.loads(p.read_bytes().decode("utf-8", errors="replace"))["features"]
        _cache[name] = feats
        return feats
    except Exception:
        _cache[name] = []
        return []

def _first_available(*names: str) -> list:
    for n in names:
        feats = _load(n)
        if feats:
            return feats
    return []

def bbox_500km(lat: float, lon: float) -> tuple[float, float, float, float]:
    """Bounding box 500 km autour de (lat, lon). Retourne (min_lat, max_lat, min_lon, max_lon)."""
    dlat = 500.0 / 111.0
    dlon = 500.0 / max(0.01, 111.0 * math.cos(math.radians(lat)))
    return lat - dlat, lat + dlat, lon - dlon, lon + dlon

def _bresenham(canvas, x1: int, y1: int, x2: int, y2: int) -> None:
    dx, dy = abs(x2 - x1), abs(y2 - y1)
    sx = 1 if x1 < x2 else -1
    sy = 1 if y1 < y2 else -1
    err = dx - dy
    steps = dx + dy + 2
    for _ in range(steps):
        canvas.set_dot(x1, y1)
        if x1 == x2 and y1 == y2:
            break
        e2 = err * 2
        if e2 > -dy: err -= dy; x1 += sx
        if e2 <  dx: err += dx; y1 += sy

def draw_geo(canvas, min_lat: float, max_lat: float,
             min_lon: float, max_lon: float) -> None:
    """Dessine le fond géographique sur canvas (modifie canvas en place)."""
    dw = canvas._cw * 2
    dh = canvas._ch * 4
    lat_span = max_lat - min_lat
    lon_span = max_lon - min_lon

    def to_dot(lat, lon):
        x = int((lon - min_lon) / lon_span * (dw - 1))
        y = int((1.0 - (lat - min_lat) / lat_span) * (dh - 1))
        return x, y

    # Marge d'1 degré pour les segments qui croisent le bord
    lat_lo = min_lat - 1.0; lat_hi = max_lat + 1.0
    lon_lo = min_lon - 1.0; lon_hi = max_lon + 1.0

    def draw_linestring(coords):
        prev = None
        for c in coords:
            lo, la = c[0], c[1]
            if la < lat_lo or la > lat_hi or lo < lon_lo or lo > lon_hi:
                prev = None
                continue
            pt = to_dot(la, lo)
            if prev is not None and prev != pt:
                _bresenham(canvas, prev[0], prev[1], pt[0], pt[1])
            prev = pt

    def draw_feature(feat):
        geom = feat.get("geometry") or {}
        gt   = geom.get("type", "")
        co   = geom.get("coordinates", [])
        if   gt == "LineString":      draw_linestring(co)
        elif gt == "MultiLineString": [draw_linestring(s) for s in co]
        elif gt == "Polygon":         draw_linestring(co[0]) if co else None
        elif gt == "MultiPolygon":    [draw_linestring(p[0]) for p in co if p]

    # ── Coastlines ─────────────────────────────────────────────
    for f in _first_available("ne_50m_coastline.geojson",
                               "ne_110m_coastline.geojson"):
        draw_feature(f)

    # ── Frontières terrestres ──────────────────────────────────
    for f in _first_available("ne_50m_admin_0_boundary_lines_land.geojson",
                               "ne_110m_admin_0_boundary_lines_land.geojson"):
        draw_feature(f)

    # ── Villes majeures (SCALERANK ≤ 4) ───────────────────────
    for feat in _first_available("ne_50m_populated_places_simple.geojson",
                                  "ne_110m_populated_places_simple.geojson"):
        try:
            if feat.get("properties", {}).get("SCALERANK", 10) > 4:
                continue
            geom = feat.get("geometry", {})
            if geom.get("type") == "Point":
                lo, la = geom["coordinates"][0], geom["coordinates"][1]
                if min_lat <= la <= max_lat and min_lon <= lo <= max_lon:
                    x, y = to_dot(la, lo)
                    canvas.set_dot(x, y)
                    canvas.set_dot(x, y - 1)
                    canvas.set_dot(x, y + 1)
        except Exception:
            pass
