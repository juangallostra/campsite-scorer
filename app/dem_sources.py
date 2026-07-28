"""
dem_sources.py
--------------
Descarga automatica de un DEM real a partir de unas coordenadas, usando las
"Elevation Tiles" publicas (formato Terrarium, antes Mapzen, hoy mantenidas
como parte de AWS Open Data / Tilezen). Cobertura global, sin necesidad de
API key, derivadas de SRTM/ETOPO1/ArcticDEM/EUDEM segun la zona.

Para Espana existe el MDT del IGN (5m/2m, mayor precision), pero requiere
WCS + reproyeccion y complica mucho el despliegue (GDAL, autenticacion,
sistemas de referencia). Se deja como mejora futura (ver README) y aqui se
implementa la fuente global, que funciona en cualquier parte del mundo y es
mucho mas sencilla de mantener en un servicio como Render.

Referencia formato Terrarium:
  elevation_m = (R * 256 + G + B / 256) - 32768
  URL: https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png
"""

from __future__ import annotations
import math
import io
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
import requests
from PIL import Image

TILE_SIZE = 256
TILE_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
MAX_TILES = 36  # limite de seguridad (rendimiento / memoria en el free tier de Render); 36 = hasta 6x6 teselas ~ 1536x1536 px
_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "campsite-scorer/0.1 (prototipo educativo)"})


class DemSourceError(Exception):
    pass


@dataclass
class DemResult:
    dem: np.ndarray          # elevacion en metros, shape (rows, cols)
    cellsize_m: float        # resolucion aproximada en metros/pixel
    bounds: dict              # {"south":.., "west":.., "north":.., "east":..}


def _deg2num(lat_deg: float, lon_deg: float, zoom: int) -> tuple[int, int]:
    lat_rad = math.radians(lat_deg)
    n = 2.0 ** zoom
    xtile = int((lon_deg + 180.0) / 360.0 * n)
    ytile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    xtile = max(0, min(int(n) - 1, xtile))
    ytile = max(0, min(int(n) - 1, ytile))
    return xtile, ytile


def _num2deg(xtile: int, ytile: int, zoom: int) -> tuple[float, float]:
    """Devuelve (lat, lon) de la esquina NOROESTE de la tesela (xtile, ytile)."""
    n = 2.0 ** zoom
    lon_deg = xtile / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * ytile / n)))
    lat_deg = math.degrees(lat_rad)
    return lat_deg, lon_deg


def _meters_per_pixel(lat_deg: float, zoom: int) -> float:
    return 156543.03392 * math.cos(math.radians(lat_deg)) / (2 ** zoom)


def _zoom_for_radius(radius_km: float) -> int:
    """Elige un zoom que mantenga el numero de teselas manejable."""
    if radius_km <= 2:
        return 13
    if radius_km <= 5:
        return 12
    if radius_km <= 10:
        return 11
    return 10


@lru_cache(maxsize=150)  # 150 teselas de 256x256 float32 ~= 40MB en el peor caso: razonable dado el limite de memoria de Render
def _fetch_tile(x: int, y: int, z: int) -> np.ndarray:
    url = TILE_URL.format(z=z, x=x, y=y)
    try:
        resp = _SESSION.get(url, timeout=10)
    except requests.RequestException as exc:
        raise DemSourceError(f"No se pudo contactar con el servidor de teselas: {exc}") from exc

    if resp.status_code != 200:
        raise DemSourceError(
            f"Tesela ({z}/{x}/{y}) no disponible (status {resp.status_code}). "
            "Puede que esta zona no tenga cobertura de elevacion."
        )

    img = Image.open(io.BytesIO(resp.content)).convert("RGB")
    arr = np.asarray(img).astype(np.float32)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    elevation = (r * 256 + g + b / 256.0) - 32768.0
    return elevation.astype(np.float32, copy=False)


def fetch_dem_by_bbox(south: float, west: float, north: float, east: float, zoom: int) -> DemResult:
    if not (-85 < south < north < 85 and -180 <= west < east <= 180):
        raise DemSourceError("Bounding box invalido.")

    x_min, y_min = _deg2num(north, west, zoom)   # tesela superior-izquierda
    x_max, y_max = _deg2num(south, east, zoom)   # tesela inferior-derecha
    x_min, x_max = min(x_min, x_max), max(x_min, x_max)
    y_min, y_max = min(y_min, y_max), max(y_min, y_max)

    n_tiles_x = x_max - x_min + 1
    n_tiles_y = y_max - y_min + 1
    if n_tiles_x * n_tiles_y > MAX_TILES:
        raise DemSourceError(
            f"El area solicitada requiere demasiadas teselas ({n_tiles_x * n_tiles_y}). "
            "Reduce el radio o aumenta el zoom."
        )

    mosaic = np.zeros((n_tiles_y * TILE_SIZE, n_tiles_x * TILE_SIZE), dtype=np.float32)
    for ty in range(y_min, y_max + 1):
        for tx in range(x_min, x_max + 1):
            tile = _fetch_tile(tx, ty, zoom)
            row0 = (ty - y_min) * TILE_SIZE
            col0 = (tx - x_min) * TILE_SIZE
            mosaic[row0:row0 + TILE_SIZE, col0:col0 + TILE_SIZE] = tile

    north_actual, west_actual = _num2deg(x_min, y_min, zoom)
    south_actual, east_actual = _num2deg(x_max + 1, y_max + 1, zoom)

    center_lat = (north_actual + south_actual) / 2
    cellsize = _meters_per_pixel(center_lat, zoom)

    return DemResult(
        dem=mosaic,
        cellsize_m=cellsize,
        bounds={"south": south_actual, "west": west_actual, "north": north_actual, "east": east_actual},
    )


def fetch_dem_by_point(lat: float, lon: float, radius_km: float = 3.0) -> DemResult:
    radius_km = max(0.5, min(radius_km, 20.0))
    zoom = _zoom_for_radius(radius_km)

    # aproximacion simple grados <-> km (suficiente para recortar el area de descarga)
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(0.15, math.cos(math.radians(lat))))

    return fetch_dem_by_bbox(
        south=lat - dlat, west=lon - dlon, north=lat + dlat, east=lon + dlon, zoom=zoom
    )
