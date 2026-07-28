"""
main.py
-------
API web (FastAPI) que expone el algoritmo de scoring de terrenos de acampada.

Endpoints:
  GET  /                -> sirve el frontend estatico (static/index.html)
  GET  /api/demo         -> calcula el score sobre un DEM sintetico de ejemplo
  POST /api/score        -> calcula el score sobre un GeoTIFF (DEM) subido por el usuario
  GET  /api/health       -> healthcheck para Render
"""

import io
import base64
import numpy as np
from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image

from app.terrain import compute_campsite_score, score_to_rgb
from app.demo_dem import generate_demo_dem
from app.dem_sources import fetch_dem_by_point, DemSourceError

app = FastAPI(title="Campsite Suitability Scorer", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _array_to_png_base64(rgb: np.ndarray) -> str:
    img = Image.fromarray(rgb, mode="RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _hillshade_png_base64(dem: np.ndarray) -> str:
    """Sombreado simple del relieve, solo para dar contexto visual."""
    gy, gx = np.gradient(dem)
    slope = np.pi / 2 - np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    az, alt = np.radians(315), np.radians(45)
    shade = np.sin(alt) * np.sin(slope) + np.cos(alt) * np.cos(slope) * np.cos(az - aspect)
    shade = np.clip(shade, 0, 1)
    gray = (shade * 255).astype(np.uint8)
    rgb = np.stack([gray, gray, gray], axis=-1)
    return _array_to_png_base64(rgb)


def _downsample_grid(arr: np.ndarray, target: int = 30) -> list:
    """Reduce un array 2D a como mucho target x target celdas (para mandar por JSON)."""
    rows, cols = arr.shape
    step_r = max(1, rows // target)
    step_c = max(1, cols // target)
    small = arr[::step_r, ::step_c]
    return np.round(small, 1).tolist()


def _result_payload(dem: np.ndarray, cellsize: float, hemisphere: str = "N", bounds: dict | None = None) -> dict:
    result = compute_campsite_score(dem, cellsize=cellsize, hemisphere=hemisphere)
    score = result["score"]

    best_idx = np.unravel_index(np.argmax(score), score.shape)
    worst_idx = np.unravel_index(np.argmin(score), score.shape)

    payload = {
        "shape": list(dem.shape),
        "cellsize": cellsize,
        "bounds": bounds,  # None si no esta georreferenciado (demo sintetica / GeoTIFF sin CRS claro)
        "score_heatmap_png_base64": _array_to_png_base64(score_to_rgb(score)),
        "hillshade_png_base64": _hillshade_png_base64(dem),
        "stats": {
            "score_mean": float(np.mean(score)),
            "score_p90": float(np.percentile(score, 90)),
            "pct_area_good_ge_70": float(np.mean(score >= 70) * 100),
            "pct_area_bad_lt_30": float(np.mean(score < 30) * 100),
        },
        "best_cell": {
            "row": int(best_idx[0]), "col": int(best_idx[1]),
            "score": float(score[best_idx]),
            "slope_deg": float(result["slope_deg"][best_idx]),
        },
        "worst_cell": {
            "row": int(worst_idx[0]), "col": int(worst_idx[1]),
            "score": float(score[worst_idx]),
            "slope_deg": float(result["slope_deg"][worst_idx]),
        },
    }

    if bounds is not None:
        # grid submuestreado para poder inspeccionar sub-scores al hacer click en el mapa
        payload["inspect_grid"] = {
            "score": _downsample_grid(score),
            "slope_deg": _downsample_grid(result["slope_deg"]),
            "sub_scores": {
                "slope": _downsample_grid(result["sub_scores"]["slope"]),
                "drainage": _downsample_grid(result["sub_scores"]["drainage"]),
                "position": _downsample_grid(result["sub_scores"]["position"]),
                "aspect": _downsample_grid(result["sub_scores"]["aspect"]),
                "roughness": _downsample_grid(result["sub_scores"]["roughness"]),
            },
        }

    return payload


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/demo")
def demo(size: int = Query(200, ge=50, le=400), cellsize: float = Query(5.0, gt=0)):
    dem = generate_demo_dem(size=size, cellsize=cellsize)
    return _result_payload(dem, cellsize=cellsize)


@app.get("/api/score_by_location")
def score_by_location(
    lat: float = Query(..., ge=-85, le=85),
    lon: float = Query(..., ge=-180, le=180),
    radius_km: float = Query(3.0, gt=0, le=20),
    hemisphere: str | None = Query(None, pattern="^[NS]$"),
):
    """
    Descarga un DEM real (teselas de elevacion publicas) alrededor del punto
    dado y calcula el mapa de idoneidad, georreferenciado (con 'bounds' para
    poder pintarlo como overlay sobre un mapa Leaflet/Mapbox).
    """
    hemi = hemisphere or ("N" if lat >= 0 else "S")
    try:
        dem_result = fetch_dem_by_point(lat=lat, lon=lon, radius_km=radius_km)
    except DemSourceError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    return _result_payload(
        dem_result.dem,
        cellsize=dem_result.cellsize_m,
        hemisphere=hemi,
        bounds=dem_result.bounds,
    )


MAX_DIM_DEFAULT = 800    # lado maximo (px) que se procesa por defecto (mas conservador: Render free = 512MB)
MAX_DIM_HARD_CAP = 1800  # nadie puede pedir mas que esto via query param, ni con max_dim
MAX_UPLOAD_MB = 80        # rechazar el archivo ANTES de abrirlo con GDAL si pesa mas que esto


@app.post("/api/score")
async def score_uploaded_dem(
    file: UploadFile = File(...),
    hemisphere: str = Query("N", pattern="^[NS]$"),
    max_dim: int = Query(
        MAX_DIM_DEFAULT, ge=100, le=MAX_DIM_HARD_CAP,
        description="Lado maximo en pixeles al que se remuestrea el DEM antes de procesarlo. "
                     "Bajalo si el servidor se queda sin memoria con archivos grandes.",
    ),
):
    """
    Acepta un GeoTIFF (DEM) y devuelve el mapa de idoneidad.
    Requiere rasterio para leer la georreferenciacion y la resolucion real.

    Importante para memoria: nunca se lee el raster a su resolucion nativa
    completa. Se consulta primero el tamano (metadata, barato) y se hace una
    lectura DECIMADA directamente con GDAL (src.read(..., out_shape=...)),
    que remuestrea durante la propia decodificacion en vez de cargar todo el
    array y luego reducirlo con numpy -- eso ultimo duplicaria el pico de
    memoria justo cuando mas grande es el archivo.

    LIMITACION conocida: si el GeoTIFF no tiene "overviews" (piramides)
    internas y esta muy comprimido, GDAL puede necesitar decodificar bloques
    a resolucion nativa igualmente antes de promediarlos, aunque el array
    final que llega a Python ya sea pequeno. Por eso ademas se rechaza el
    archivo por tamano en bytes ANTES de intentar abrirlo (ver MAX_UPLOAD_MB),
    como ultima red de seguridad independiente de max_dim.
    """
    try:
        import rasterio
        from rasterio.enums import Resampling
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="rasterio no esta instalado en el servidor. Anadelo a requirements.txt para habilitar la subida de GeoTIFF reales.",
        )

    content = await file.read()

    size_mb = len(content) / (1024 * 1024)
    if size_mb > MAX_UPLOAD_MB:
        del content
        raise HTTPException(
            status_code=413,
            detail=f"El archivo pesa {size_mb:.0f}MB, por encima del limite de {MAX_UPLOAD_MB}MB. "
                    "Recorta el area del GeoTIFF antes de subirlo (p.ej. con gdal_translate -projwin) "
                    "o genera un overview interno (gdaladdo) para que la lectura decimada sea mas ligera.",
        )

    try:
        with rasterio.io.MemoryFile(content) as memfile:
            with memfile.open() as src:
                native_h, native_w = src.height, src.width
                native_cellsize = abs(src.transform.a)

                # factor de reduccion para que el lado mayor quede <= max_dim
                scale = min(1.0, max_dim / max(native_h, native_w))
                out_h = max(1, int(round(native_h * scale)))
                out_w = max(1, int(round(native_w * scale)))

                dem = src.read(
                    1,
                    out_shape=(out_h, out_w),
                    resampling=Resampling.average,
                ).astype(np.float32, copy=False)

                # el tamano de celda crece en la misma proporcion en que se redujo la imagen
                cellsize = native_cellsize / scale if scale > 0 else native_cellsize
                nodata = src.nodata
    except HTTPException:
        raise
    except MemoryError:
        raise HTTPException(
            status_code=413,
            detail="El archivo es demasiado grande para procesarlo con la memoria disponible. "
                   "Prueba a bajar el parametro max_dim (ej. max_dim=600) o recorta el area del GeoTIFF antes de subirlo.",
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"No se pudo leer el GeoTIFF: {exc}")

    del content  # ya no hace falta el archivo original en memoria

    if nodata is not None:
        valid = dem != nodata
        if valid.any():
            dem = np.where(valid, dem, np.nanmin(dem[valid])).astype(np.float32, copy=False)

    if dem.size == 0 or np.all(np.isnan(dem)):
        raise HTTPException(status_code=400, detail="El DEM subido esta vacio o no tiene datos validos.")

    try:
        return _result_payload(dem, cellsize=cellsize, hemisphere=hemisphere)
    except MemoryError:
        raise HTTPException(
            status_code=413,
            detail="Sin memoria suficiente para calcular el score. Prueba a bajar max_dim.",
        )


app.mount("/", StaticFiles(directory="static", html=True), name="static")
