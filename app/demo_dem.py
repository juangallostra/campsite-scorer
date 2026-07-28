"""
demo_dem.py
-----------
Genera un DEM sintetico con una vaguada, una ladera y una cresta, para poder
demostrar el algoritmo sin depender de descargar un GeoTIFF real (IGN,
Copernicus, etc.). Sirve como "hola mundo" del pipeline.
"""

import numpy as np


def generate_demo_dem(size: int = 200, cellsize: float = 5.0, seed: int = 42) -> np.ndarray:
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:size, 0:size]

    # Ladera general ascendente de oeste a este
    base_slope = x * 0.35

    # Una vaguada (cauce) diagonal que cruza el mapa
    valley = 18 * np.exp(-((x - (0.5 * y + 20)) ** 2) / (2 * 8 ** 2))

    # Una cresta/loma redondeada
    ridge = 22 * np.exp(-(((x - 150) ** 2 + (y - 60) ** 2)) / (2 * 30 ** 2))

    # Zona de media ladera, relativamente plana: candidata a "buen sitio"
    plateau = 6 * np.exp(-(((x - 110) ** 2 + (y - 140) ** 2)) / (2 * 25 ** 2))

    # Micro-relieve / rugosidad de baja amplitud
    noise = rng.normal(0, 0.4, size=(size, size))
    noise = _smooth(noise, iterations=2)

    dem = base_slope - valley + ridge + plateau + noise
    dem = dem - dem.min()
    return dem.astype(np.float32)


def _smooth(arr: np.ndarray, iterations: int = 1) -> np.ndarray:
    out = arr.copy()
    for _ in range(iterations):
        out = (
            out
            + np.roll(out, 1, axis=0)
            + np.roll(out, -1, axis=0)
            + np.roll(out, 1, axis=1)
            + np.roll(out, -1, axis=1)
        ) / 5.0
    return out
