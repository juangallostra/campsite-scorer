# Campsite Scorer

Prototipo de servicio web que evalúa automáticamente la idoneidad de un
terreno para acampar/vivaquear a partir de un modelo de elevación (DEM),
implementando como algoritmo la heurística de campo (pendiente, drenaje,
exposición al viento, aire frío nocturno, orientación solar, rugosidad).

## Novedades: DEM real por coordenadas + mapa interactivo

Ya implementados (puntos 1 y 2 de la lista de próximos pasos original):

1. **Descarga automática de DEM real** (`app/dem_sources.py`): dado un
   `lat`/`lon`, descarga y stitchea automáticamente las teselas de elevación
   públicas necesarias (formato Terrarium — cobertura global, sin API key),
   calcula la resolución real en metros/pixel según la latitud y el zoom, y
   expone el resultado georreferenciado (`bounds`: south/west/north/east).
   Nuevo endpoint: `GET /api/score_by_location?lat=..&lon=..&radius_km=..`

2. **Mapa interactivo real** (Leaflet, sin necesidad de API key de Mapbox):
   en `static/index.html`, la sección superior muestra un mapa OpenStreetMap.
   Al hacer clic en cualquier punto, se llama al endpoint anterior, se
   descarga el relieve real de esa zona y se pinta el mapa de calor como
   `L.imageOverlay` georreferenciado sobre el mapa. Un segundo clic sobre el
   overlay abre un popup con el desglose de sub-scores (pendiente, drenaje,
   posición/TPI, orientación, rugosidad) del punto exacto, usando un grid
   submuestreado (`inspect_grid` en la respuesta) que se manda junto al PNG
   para no tener que repetir peticiones al servidor por cada clic.

El modo "offline" original (terreno sintético + subida de GeoTIFF, con
imágenes estáticas sin mapa) se mantiene debajo, útil para pruebas sin
depender de la red o de coordenadas concretas.

**Fuente de datos:** teselas de elevación públicas tipo Terrarium (derivadas
de SRTM y otros DEM globales), servidas vía S3 sin necesidad de autenticación.
Para España, el MDT del IGN (5-2m) da mucha más precisión pero exige WCS +
GDAL y complica el despliegue — queda como mejora futura (ver más abajo).
La resolución real depende del zoom elegido según el radio pedido
(`_zoom_for_radius` en `dem_sources.py`): a más radio, menor resolución, para
mantener manejable el número de teselas descargadas (límite `MAX_TILES=64`).

## Zonas aptas contiguas y puntos recomendados

En vez de fiarse de un solo píxel bueno (que puede estar rodeado de terreno
malo y no dejar espacio real para una tienda), `find_suitable_zones()` en
`terrain.py` agrupa las celdas con score alto en regiones conectadas
(`scipy.ndimage.label`), descarta las que no llegan a un área mínima real, y
devuelve las mejores ordenadas por su score **medio** (no solo su pico).

- El área mínima es **adaptativa a la resolución**: por defecto exige al
  menos ~2 píxeles conectados (`max(25, 2 * cellsize²)` m²), no un número
  fijo — con la resolución típica del mapa (20-150m/píxel) un umbral fijo
  bajo (ej. 25m²) no filtraría nada, porque un solo píxel ya supera esa área.
- Nuevos query params en `/api/score_by_location`: `zone_min_score` (umbral
  de "apto", 70 por defecto), `zone_min_area_m2` (override manual del área
  mínima), `zone_max_count` (cuántas zonas devolver, 5 por defecto).
- El frontend pinta cada zona con su **forma exacta a nivel de píxel**
  (`zones_to_rgba()` en `terrain.py`: relleno semitransparente + borde
  sólido calculado con `scipy.ndimage.binary_erosion`, no una aproximación
  geométrica como un círculo) superpuesta sobre el mapa, más un círculo
  numerado en el mejor punto *dentro* de esa zona. Clicar el número abre el
  mismo panel de desglose que un clic normal. Checkbox para activar/
  desactivar toda la capa sin volver a pedir datos al servidor.

**Limitación**: una zona "apta" según el DEM puede tener rocas, vegetación
densa o ser propiedad privada — sigue sin sustituir la inspección visual.
Zonas que tocan el borde del área analizada pueden aparecer artificialmente
cortadas (el análisis no ve lo que hay justo fuera del radio elegido).

## Más funcionalidades añadidas

- **Buscador de topónimos**: campo de búsqueda que usa Nominatim (geocoding
  gratuito de OpenStreetMap, sin API key) para saltar directamente a un lugar
  por nombre. Nota: Nominatim tiene una política de uso pensada para volumen
  bajo/moderado; para un uso intensivo en producción, considera montar tu
  propio servicio de geocoding o usar un proveedor con SLA.
- **Geolocalización**: botón "📍 Mi ubicación" que usa la API de
  geolocalización del navegador para analizar automáticamente dónde estás.
- **Pesos ajustables por el usuario**: panel desplegable con sliders para dar
  más o menos importancia a cada criterio (pendiente, drenaje, posición,
  orientación, rugosidad). Se normalizan solos en el backend
  (`compute_campsite_score` en `terrain.py`), así que no hace falta que sumen
  100. Nuevos query params en `/api/demo`, `/api/score` y
  `/api/score_by_location`: `w_slope`, `w_drainage`, `w_position`,
  `w_aspect`, `w_roughness`.
- **Descarga del resultado**: botón para descargar el heatmap actual como PNG.
- **Historial de puntos analizados**: lista de los últimos puntos que has
  mirado en esta sesión (solo en memoria del navegador, se pierde al recargar
  la página). Clicar una fila restaura ese resultado sin volver a llamar al
  servidor.
- **Caché de teselas en el backend** (`dem_sources.py`, `lru_cache` sobre
  `_fetch_tile`): si dos análisis se solapan geográficamente (p. ej. clics
  cercanos), las teselas ya descargadas se reutilizan en vez de pedirlas otra
  vez. Limitada a 150 teselas en memoria (~40MB) para no comprometer el
  presupuesto de memoria del free tier de Render.

## Optimizaciones de memoria (importante si despliegas en el free tier de Render)

El servicio está pensado para no reventar la memoria disponible (Render free
tier = 512MB):

- **`float32` en todo el pipeline** (`terrain.py`, `dem_sources.py`,
  `demo_dem.py`) en vez de `float64` — mitad de memoria por array, sin
  pérdida de precisión relevante para este caso de uso.
- **Rugosidad vectorizada**: se sustituyó `scipy.ndimage.generic_filter(dem,
  np.std, ...)` (invoca una función Python por ventana — muy lento y con
  mucho overhead) por la identidad `Var(X) = E[X²] − E[X]²` calculada con
  `uniform_filter` vectorizado. Mismo resultado, ~60x más rápido.
- **Lectura decimada de GeoTIFF** (`POST /api/score`): nunca se carga el
  raster a su resolución nativa completa. Se consulta el tamaño por metadata
  primero, y se pide a GDAL una lectura ya remuestreada (`out_shape=...`)
  para que el lado mayor no supere `max_dim` (1200px por defecto,
  configurable por query param, tope duro 2500px).
- **Límite de teselas** en la descarga por coordenadas (`MAX_TILES=36` en
  `dem_sources.py`) para no descargar/mosaiquear áreas arbitrariamente grandes.
- Errores de memoria se capturan y devuelven como HTTP 413 con un mensaje
  claro, en vez de tumbar el proceso.

Si aun así el servicio se queda sin memoria con un archivo concreto, baja
`max_dim` (ej. `POST /api/score?max_dim=600`) o sube de plan en Render.

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
│   ├── main.py         # API FastAPI (endpoints /api/demo, /api/score, /api/score_by_location)
│   ├── terrain.py      # Algoritmo: slope, curvature, TPI, roughness, scoring
│   ├── demo_dem.py     # Genera un DEM sintético para probar sin datos reales
│   └── dem_sources.py  # Descarga y stitching de DEM real por coordenadas (teselas Terrarium)
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

1. ~~Fuentes de DEM reales~~ ✅ implementado (`app/dem_sources.py`, teselas
   globales). Pendiente como mejora: añadir el MDT del IGN (5m/2m) como
   fuente de mayor precisión específica para España.
2. ~~Mapa interactivo real~~ ✅ implementado (Leaflet + overlay georreferenciado
   + inspección por clic). Pendiente como mejora: buscador de topónimos/direcciones
   (geocoding) para no depender solo del clic manual.
3. **Capas adicionales**: hidrografía (OSM/IGN) para detectar cauces cercanos
   con precisión, cobertura vegetal (Corine Land Cover / Sentinel-2) para
   masas boscosas, viento predominante (ERA5) para afinar la exposición.
4. **Caché de teselas procesadas** para no recalcular el mismo área varias veces
   (ahora mismo cada clic vuelve a descargar y recalcular desde cero).
5. **Ajuste de pesos por usuario**: exponer los pesos de `DEFAULT_WEIGHTS`
   como parámetros de la API para que cada persona priorice según su
   actividad (vivac ligero vs. tienda con piquetas vs. furgoneta).
