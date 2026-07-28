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


def _result_payload(dem: np.ndarray, cellsize: float, hemisphere: str = "N") -> dict:
    result = compute_campsite_score(dem, cellsize=cellsize, hemisphere=hemisphere)
    score = result["score"]

    best_idx = np.unravel_index(np.argmax(score), score.shape)
    worst_idx = np.unravel_index(np.argmin(score), score.shape)

    return {
        "shape": list(dem.shape),
        "cellsize": cellsize,
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


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/demo")
def demo(size: int = Query(200, ge=50, le=400), cellsize: float = Query(5.0, gt=0)):
    dem = generate_demo_dem(size=size, cellsize=cellsize)
    return _result_payload(dem, cellsize=cellsize)


@app.post("/api/score")
async def score_uploaded_dem(
    file: UploadFile = File(...),
    hemisphere: str = Query("N", regex="^[NS]$"),
):
    """
    Acepta un GeoTIFF (DEM) y devuelve el mapa de idoneidad.
    Requiere rasterio para leer la georreferenciacion y la resolucion real.
    """
    try:
        import rasterio  # import perezoso: solo hace falta si se usa este endpoint
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="rasterio no esta instalado en el servidor. Anadelo a requirements.txt para habilitar la subida de GeoTIFF reales.",
        )

    content = await file.read()
    try:
        with rasterio.io.MemoryFile(content) as memfile:
            with memfile.open() as src:
                dem = src.read(1).astype(np.float64)
                cellsize = abs(src.transform.a)
                nodata = src.nodata
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"No se pudo leer el GeoTIFF: {exc}")

    if nodata is not None:
        dem = np.where(dem == nodata, np.nanmin(dem[dem != nodata]), dem)

    if dem.size == 0 or np.all(np.isnan(dem)):
        raise HTTPException(status_code=400, detail="El DEM subido esta vacio o no tiene datos validos.")

    return _result_payload(dem, cellsize=cellsize, hemisphere=hemisphere)


app.mount("/", StaticFiles(directory="static", html=True), name="static")
