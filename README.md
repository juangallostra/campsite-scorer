# Campsite Scorer

Prototipo de servicio web que evalúa automáticamente la idoneidad de un
terreno para acampar/vivaquear a partir de un modelo de elevación (DEM),
implementando como algoritmo la heurística de campo (pendiente, drenaje,
exposición al viento, aire frío nocturno, orientación solar, rugosidad).

## Cómo funciona (resumen técnico)

Todo el análisis geomorfológico está en `app/terrain.py`, implementado con
`numpy` + `scipy.ndimage` (sin GDAL/richdem obligatorios para el cálculo en sí,
lo que hace el build en Render mucho más ligero):

| Indicador | Método | Qué heurística de campo aproxima |
|---|---|---|
| Pendiente / orientación | Algoritmo de Horn (estándar en QGIS/ArcGIS) sobre ventana 3x3 | "¿Está inclinado?" |
| Curvatura | Laplaciano discreto | Zonas cóncavas = acumulan agua (mal drenaje) |
| TPI (Topographic Position Index) | Elevación relativa a la media de un entorno (radio configurable) | Cresta expuesta al viento vs. fondo de valle con aire frío |
| Rugosidad | Desviación estándar local | Terreno irregular (solo fiable con DEM de alta resolución) |

Estos indicadores se combinan con pesos (`DEFAULT_WEIGHTS` en `terrain.py`)
en un score 0-100 por celda, que se pinta como mapa de calor (rojo=malo,
verde=bueno).

**Limitación importante:** con un DEM de resolución media/baja (SRTM/Copernicus,
30m) el algoritmo capta bien pendiente, drenaje general y exposición, pero
**no puede detectar piedras sueltas, ramas muertas o riesgo de crecida puntual**
— eso requiere inspección visual, imagen satelital de muy alta resolución, o
datos aportados por usuarios (crowdsourcing).

## Estructura del proyecto

```
campsite-scorer/
├── app/
│   ├── main.py        # API FastAPI (endpoints /api/demo, /api/score)
│   ├── terrain.py      # Algoritmo: slope, curvature, TPI, roughness, scoring
│   └── demo_dem.py     # Genera un DEM sintético para probar sin datos reales
├── static/
│   └── index.html      # Frontend mínimo (sin frameworks)
├── requirements.txt
├── render.yaml          # Blueprint de despliegue en Render
└── README.md
```

## Probar en local

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
# abrir http://localhost:8000
```

Si no quieres instalar `rasterio` (requiere GDAL, a veces da problemas en
algunos sistemas), puedes quitarlo de `requirements.txt`: la demo sintética
(`/api/demo`) funciona igualmente sin él; solo se desactiva la subida de
GeoTIFF reales (`/api/score`), y el endpoint devuelve un mensaje claro en
vez de romperse.

## Desplegar en Render

**Opción A — Blueprint (recomendado):**
1. Sube esta carpeta a un repositorio de GitHub/GitLab.
2. En Render: **New > Blueprint**, apunta al repo. Render detecta `render.yaml`
   y configura el servicio automáticamente (build + start command).
3. Deploy. La URL pública sirve tanto la API como el frontend (`static/index.html`).

**Opción B — Web Service manual:**
1. New > Web Service > conecta el repo.
2. Build command: `pip install -r requirements.txt`
3. Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
4. Plan free vale para probar (se "duerme" tras inactividad; el primer
   request tras dormir tarda ~30-60s en despertar).

> Nota sobre `rasterio` en Render: normalmente instala bien (usa wheels
> precompilados con GDAL incluido), pero si el build falla por eso, elimínalo
> de `requirements.txt` — la demo seguirá funcionando y podrás añadir de
> nuevo la subida de GeoTIFF más adelante con otra estrategia (p. ej. un
> microservicio aparte solo para lectura de rasters).

## Próximos pasos naturales para llevarlo a producción real

1. **Fuentes de DEM reales**: integrar descarga automática de teselas por
   coordenadas (IGN MDT 5m/2m para España, Copernicus DEM 30m a nivel global)
   en vez de exigir que el usuario suba el archivo.
2. **Mapa interactivo real** (Leaflet/Mapbox GL) con el heatmap superpuesto
   georreferenciado, en vez de una imagen estática — permite hacer zoom/pan
   y clicar un punto para ver su desglose de sub-scores.
3. **Capas adicionales**: hidrografía (OSM/IGN) para detectar cauces cercanos
   con precisión, cobertura vegetal (Corine Land Cover / Sentinel-2) para
   masas boscosas, viento predominante (ERA5) para afinar la exposición.
4. **Caché de teselas procesadas** para no recalcular el mismo área varias veces.
5. **Ajuste de pesos por usuario**: exponer los pesos de `DEFAULT_WEIGHTS`
   como parámetros de la API para que cada persona priorice según su
   actividad (vivac ligero vs. tienda con piquetas vs. furgoneta).
