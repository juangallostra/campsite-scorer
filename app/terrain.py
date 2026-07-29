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
from scipy.ndimage import uniform_filter, label

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
    total_w = sum(w.values())
    if total_w > 0:
        w = {k: v / total_w for k, v in w.items()}  # normaliza para que sigan sumando 1 aunque el usuario pase pesos propios
    else:
        w = DEFAULT_WEIGHTS

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
    total = np.clip(total, 0, 100)

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


# ---------------------------------------------------------------------------
# 8. Deteccion de zonas contiguas aptas (no solo puntos aislados)
# ---------------------------------------------------------------------------
def find_suitable_zones(
    score: np.ndarray,
    cellsize: float,
    min_score: float = 70.0,
    min_area_m2: float = 25.0,
    max_zones: int = 5,
) -> tuple[list[dict], np.ndarray]:
    """
    Agrupa las celdas con score >= min_score en regiones conectadas
    (scipy.ndimage.label: 4-conectividad por defecto) y descarta las que no
    llegan a un area minima real -- un solo pixel bueno rodeado de terreno
    malo no sirve para poner una tienda.

    Devuelve (zonas, labeled): la lista de zonas (en coordenadas de fila/
    columna del array, sin georreferenciar -- eso se hace en main.py, que es
    quien conoce los bounds), ordenadas de mejor a peor por su score MEDIO
    (la calidad global de la zona, no solo su pico); y el array `labeled`
    completo (mismo shape que `score`, cada pixel con el id de su zona o 0
    si no pertenece a ninguna) para poder pintar la forma EXACTA de cada
    zona en main.py, en vez de aproximarla con un circulo.

    LIMITACION: min_area_m2 solo es significativo si la resolucion del DEM
    es suficientemente fina (cellsize pequeno). Con cellsize=100m, un solo
    pixel ya cubre 10000 m2 y el filtro de area deja de discriminar nada.
    """
    if score.size == 0:
        return [], np.zeros_like(score, dtype=np.int32)

    mask = score >= min_score
    labeled, num_features = label(mask)
    if num_features == 0:
        return [], labeled

    px_area_m2 = float(cellsize) * float(cellsize)
    zones = []
    for zone_id in range(1, num_features + 1):
        region_mask = labeled == zone_id
        pixel_count = int(region_mask.sum())
        area_m2 = pixel_count * px_area_m2
        if area_m2 < min_area_m2:
            continue

        region_scores = np.where(region_mask, score, -np.inf)
        best_row, best_col = np.unravel_index(np.argmax(region_scores), score.shape)
        rows, cols = np.where(region_mask)

        zones.append({
            "zone_id": zone_id,
            "best_row": int(best_row),
            "best_col": int(best_col),
            "best_score": float(score[best_row, best_col]),
            "mean_score": float(score[region_mask].mean()),
            "area_m2": area_m2,
            "pixel_count": pixel_count,
            "centroid_row": float(rows.mean()),
            "centroid_col": float(cols.mean()),
        })

    zones.sort(key=lambda z: (z["mean_score"], z["area_m2"]), reverse=True)
    return zones[:max_zones], labeled


# ---------------------------------------------------------------------------
# 9. Pintar la forma EXACTA de las zonas recomendadas (relleno + borde),
#    como un array RGBA transparente salvo en los pixeles de esas zonas.
# ---------------------------------------------------------------------------
def _zone_color(mean_score: float) -> tuple[int, int, int]:
    """Mismo esquema de color que scoreLabel() en el frontend, para que coincidan."""
    if mean_score >= 80:
        return (46, 204, 113)   # #2ecc71 excelente
    if mean_score >= 60:
        return (163, 217, 119)  # #a3d977 bueno
    if mean_score >= 40:
        return (241, 196, 15)   # #f1c40f regular
    if mean_score >= 20:
        return (230, 126, 34)   # #e67e22 malo
    return (231, 76, 60)        # #e74c3c muy malo


def zones_to_rgba(shape: tuple[int, int], labeled: np.ndarray, zones: list[dict]) -> np.ndarray:
    """
    RGBA (H,W,4) uint8, transparente salvo en las zonas recomendadas:
    relleno semitransparente + borde solido en el contorno exacto de cada
    region (mask XOR erosion), no una aproximacion geometrica.
    """
    from scipy.ndimage import binary_erosion

    rgba = np.zeros((*shape, 4), dtype=np.uint8)
    for z in zones:
        region_mask = labeled == z["zone_id"]
        color = _zone_color(z["mean_score"])
        eroded = binary_erosion(region_mask, iterations=1)
        border = region_mask & ~eroded

        rgba[region_mask, 0] = color[0]
        rgba[region_mask, 1] = color[1]
        rgba[region_mask, 2] = color[2]
        rgba[region_mask, 3] = 70  # relleno semitransparente

        rgba[border, 0] = color[0]
        rgba[border, 1] = color[1]
        rgba[border, 2] = color[2]
        rgba[border, 3] = 235  # borde solido

    return rgba
