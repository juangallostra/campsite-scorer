"""
terrain.py
----------
Calculo de indicadores geomorfologicos a partir de un DEM (numpy 2D array)
y combinacion en un "score" de idoneidad para acampar/vivaquear.

Todas las funciones trabajan sobre arrays numpy 2D (filas=Y, columnas=X)
y asumen una resolucion de celda uniforme (cellsize, en metros).

No depende de GDAL/richdem: todo esta implementado con numpy + scipy.ndimage,
lo que hace el despliegue en servicios como Render mucho mas ligero y fiable.
"""

from __future__ import annotations
import numpy as np
from scipy.ndimage import uniform_filter

# float32 en vez de float64: la precision extra no aporta nada para pendientes/
# curvatura/TPI, y cada array intermedio ocupa la mitad de memoria. Con un DEM
# de 3000x3000, cada array float64 pesa ~72MB; en float32 son ~36MB, y en el
# pipeline coexisten varios a la vez (dem, dzdx, dzdy, curvature, tpi,
# roughness, mean, mean_sq, score...), asi que el ahorro se multiplica.
DTYPE = np.float32


# ---------------------------------------------------------------------------
# 1. Pendiente y orientacion (metodo de Horn, estandar en GIS: ArcGIS/QGIS)
# ---------------------------------------------------------------------------
def compute_slope_aspect(dem: np.ndarray, cellsize: float) -> tuple[np.ndarray, np.ndarray]:
    """
    Devuelve (slope_deg, aspect_deg).
    aspect_deg: 0=Norte, 90=Este, 180=Sur, 270=Oeste. -1 donde la pendiente es ~0.
    """
    z = np.pad(dem.astype(DTYPE, copy=False), 1, mode="edge")

    z1, z2, z3 = z[:-2, :-2], z[:-2, 1:-1], z[:-2, 2:]
    z4, z6 = z[1:-1, :-2], z[1:-1, 2:]
    z7, z8, z9 = z[2:, :-2], z[2:, 1:-1], z[2:, 2:]

    dzdx = ((z3 + 2 * z6 + z9) - (z1 + 2 * z4 + z7)) / (8 * cellsize)
    dzdy = ((z7 + 2 * z8 + z9) - (z1 + 2 * z2 + z3)) / (8 * cellsize)

    slope_rad = np.arctan(np.sqrt(dzdx ** 2 + dzdy ** 2))
    slope_deg = np.degrees(slope_rad)

    aspect_rad = np.arctan2(dzdy, -dzdx)
    aspect_deg = np.degrees(aspect_rad)
    aspect_deg = np.where(aspect_deg < 0, 90.0 - aspect_deg, np.where(
        aspect_deg > 90.0, 360.0 - aspect_deg + 90.0, 90.0 - aspect_deg))
    aspect_deg = np.mod(aspect_deg, 360)
    aspect_deg = np.where(slope_deg < 0.5, -1, aspect_deg)  # terreno ~plano: sin orientacion definida

    return slope_deg, aspect_deg


# ---------------------------------------------------------------------------
# 2. Curvatura general (proxy de drenaje): Laplaciano discreto
#    positivo = concavo -> tiende a acumular agua (vaguada)
#    negativo = convexo -> tiende a drenar bien (loma/cresta)
# ---------------------------------------------------------------------------
def compute_curvature(dem: np.ndarray, cellsize: float) -> np.ndarray:
    z = np.pad(dem.astype(DTYPE, copy=False), 1, mode="edge")
    up = z[:-2, 1:-1]
    down = z[2:, 1:-1]
    left = z[1:-1, :-2]
    right = z[1:-1, 2:]
    center = z[1:-1, 1:-1]
    curvature = ((up + down + left + right) - 4 * center) / (cellsize ** 2)
    return curvature


# ---------------------------------------------------------------------------
# 3. TPI (Topographic Position Index): elevacion relativa al entorno
#    TPI alto  -> crestas / lomas expuestas al viento
#    TPI ~0    -> ligeramente por encima del entorno inmediato (buena zona)
#    TPI bajo  -> vaguadas / fondos de valle (aire frio, acumulacion de agua)
# ---------------------------------------------------------------------------
def compute_tpi(dem: np.ndarray, radius_cells: int = 5) -> np.ndarray:
    size = 2 * radius_cells + 1
    dem32 = dem.astype(DTYPE, copy=False)
    neighborhood_mean = uniform_filter(dem32, size=size, mode="nearest")
    return dem32 - neighborhood_mean


# ---------------------------------------------------------------------------
# 4. Rugosidad local (proxy de superficie irregular: piedras, raices...)
#    Solo es fiable si el DEM tiene resolucion alta (<=2m). Con DEM de 30m
#    esto en la practica mide "variabilidad del relieve", no piedras sueltas.
#
#    IMPORTANTE: antes se calculaba con scipy.ndimage.generic_filter(dem,
#    np.std, ...), que invoca una funcion Python por cada ventana deslizante
#    -> ~60x mas lento y con mucho overhead de memoria (crea un array temporal
#    por cada pixel). Se sustituye por la identidad Var(X) = E[X^2] - E[X]^2,
#    calculable con dos uniform_filter vectorizados: mismo resultado exacto,
#    fraccion del tiempo y memoria.
# ---------------------------------------------------------------------------
def compute_roughness(dem: np.ndarray, radius_cells: int = 2) -> np.ndarray:
    size = 2 * radius_cells + 1
    dem32 = dem.astype(DTYPE, copy=False)
    mean = uniform_filter(dem32, size=size, mode="nearest")
    mean_sq = uniform_filter(dem32 * dem32, size=size, mode="nearest")
    variance = np.clip(mean_sq - mean * mean, 0, None)  # clip: evita negativos por error de redondeo
    return np.sqrt(variance, dtype=DTYPE)


# ---------------------------------------------------------------------------
# 5. Funciones de scoring: de cada indicador fisico a una nota 0-100
# ---------------------------------------------------------------------------
def _piecewise_score(values: np.ndarray, points: list[tuple[float, float]]) -> np.ndarray:
    """Interpola linealmente una lista de puntos (valor_entrada, score_salida)."""
    xs = np.array([p[0] for p in points], dtype=DTYPE)
    ys = np.array([p[1] for p in points], dtype=DTYPE)
    return np.clip(np.interp(values, xs, ys), 0, 100).astype(DTYPE, copy=False)


def score_slope(slope_deg: np.ndarray) -> np.ndarray:
    # Basado en la heuristica: 0-3 ideal, 3-5 aceptable, 5-8 malo, >8 descartar
    return _piecewise_score(slope_deg, [(0, 100), (3, 95), (5, 70), (8, 25), (12, 0)])


def score_drainage(curvature: np.ndarray) -> np.ndarray:
    # curvatura muy positiva (concava/vaguada) = mala. Ligeramente negativa (convexa) = bien.
    return _piecewise_score(curvature, [(-0.05, 90), (-0.01, 100), (0, 95), (0.01, 60), (0.03, 20), (0.06, 0)])


def score_position(tpi: np.ndarray, tpi_std: float) -> np.ndarray:
    # Preferimos "ligeramente elevado" (media ladera), penalizamos cresta expuesta
    # y fondo de valle. Curva en campana centrada un poco por encima de 0.
    if tpi_std <= 1e-6:
        return np.full_like(tpi, 90.0)
    z = (tpi - 0.3 * tpi_std) / (tpi_std + 1e-9)
    return np.clip(100 * np.exp(-0.5 * z ** 2), 0, 100)


def score_aspect(aspect_deg: np.ndarray, hemisphere: str = "N") -> np.ndarray:
    # Peso menor: preferimos Este (sol de manana, seca el rocio) sobre Norte umbrio.
    preferred = 90.0 if hemisphere == "N" else 270.0
    diff = np.abs(((aspect_deg - preferred + 180) % 360) - 180)
    score = np.where(aspect_deg < 0, 70.0, 100 - (diff / 180.0) * 40.0)  # rango 60-100
    return np.clip(score, 0, 100)


def score_roughness(roughness: np.ndarray) -> np.ndarray:
    return _piecewise_score(roughness, [(0, 100), (0.3, 80), (1.0, 40), (2.0, 0)])


# ---------------------------------------------------------------------------
# 6. Combinacion final
# ---------------------------------------------------------------------------
DEFAULT_WEIGHTS = {
    "slope": 0.38,
    "drainage": 0.22,
    "position": 0.22,   # exposicion al viento + aire frio nocturno (via TPI)
    "aspect": 0.08,
    "roughness": 0.10,
}


def compute_campsite_score(
    dem: np.ndarray,
    cellsize: float,
    tpi_radius_cells: int = 5,
    weights: dict | None = None,
    hemisphere: str = "N",
) -> dict:
    """
    Punto de entrada principal. Devuelve un dict con el score total (0-100)
    y cada subindicador, listo para pintar como mapa de calor.
    """
    w = {**DEFAULT_WEIGHTS, **(weights or {})}

    slope_deg, aspect_deg = compute_slope_aspect(dem, cellsize)
    curvature = compute_curvature(dem, cellsize)
    tpi = compute_tpi(dem, radius_cells=tpi_radius_cells)
    roughness = compute_roughness(dem, radius_cells=max(2, tpi_radius_cells // 2))

    s_slope = score_slope(slope_deg)
    s_drain = score_drainage(curvature)
    s_pos = score_position(tpi, float(np.std(tpi)))
    s_aspect = score_aspect(aspect_deg, hemisphere=hemisphere)
    s_rough = score_roughness(roughness)

    total = (
        w["slope"] * s_slope
        + w["drainage"] * s_drain
        + w["position"] * s_pos
        + w["aspect"] * s_aspect
        + w["roughness"] * s_rough
    )

    # Penalizacion dura: pendiente >12 grados nunca puede salir "bueno"
    total = np.where(slope_deg > 12, np.minimum(total, 15), total)

    return {
        "score": total,
        "slope_deg": slope_deg,
        "aspect_deg": aspect_deg,
        "curvature": curvature,
        "tpi": tpi,
        "roughness": roughness,
        "sub_scores": {
            "slope": s_slope,
            "drainage": s_drain,
            "position": s_pos,
            "aspect": s_aspect,
            "roughness": s_rough,
        },
    }


# ---------------------------------------------------------------------------
# 7. Colormap simple (rojo -> amarillo -> verde) sin depender de matplotlib
# ---------------------------------------------------------------------------
def score_to_rgb(score: np.ndarray) -> np.ndarray:
    """score 0-100 (2D) -> array RGB uint8 (H, W, 3). Rojo=malo, Verde=bueno."""
    s = np.clip(score, 0, 100) / 100.0
    r = np.where(s < 0.5, 255, np.round(255 * (1 - (s - 0.5) * 2)).astype(np.uint8))
    g = np.where(s < 0.5, np.round(255 * (s * 2)).astype(np.uint8), 255)
    b = np.full_like(r, 30, dtype=np.uint8)
    rgb = np.stack([r.astype(np.uint8), g.astype(np.uint8), b], axis=-1)
    return rgb
