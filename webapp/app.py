"""
Минимальное веб-приложение для демонстрации: Flask отдаёт GeoJSON из
PostGIS, Leaflet на фронте рисует карту. Не продакшен-архитектура (та
по ТЗ - Laravel/API + фронтенд отдельно), а лёгкий стенд для показа
результатов на карте без поднятия полного стека.

Запуск: DEMO_PASSWORD=... python webapp/app.py   (из корня local_demo_stand/)
Открыть: http://127.0.0.1:5050

Защита - HTTP Basic Auth на все маршруты (webapp/security.py): логин
DEMO_USER (по умолчанию demo), пароль DEMO_PASSWORD. Без пароля стенд не
стартует; DEMO_AUTH=off отключает защиту явно (только локальная отладка).
Подпуть за nginx - APP_PREFIX, доверие к прокси - TRUST_PROXY=1.
Адрес привязки - DEMO_HOST (по умолчанию 127.0.0.1), режим отладки -
FLASK_DEBUG=1 (по умолчанию выключен).
"""
import os
import numpy as np
from flask import Flask, jsonify, render_template, request
from sqlalchemy import create_engine, text
import geopandas as gpd
import pandas as pd
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.getenv("DATABASE_URL", "postgresql://soil_ml:soil_ml_dev_password@localhost:5432/soil_ml")
engine = create_engine(DB_URL)

app = Flask(__name__)
app.config["ENGINE"] = engine

# Работа за reverse proxy (nginx):
#   APP_PREFIX=/testmap  - стенд опубликован под подпутём, все ссылки и fetch()
#                          строятся от него (request.script_root)
#   TRUST_PROXY=1        - доверять X-Forwarded-For/Proto/Host от прокси
#                          (нужно, чтобы лимит перебора пароля считал реальные IP)
_prefix = os.getenv("APP_PREFIX", "").rstrip("/")
if _prefix:
    _inner = app.wsgi_app

    def _with_prefix(environ, start_response):
        environ["SCRIPT_NAME"] = _prefix
        return _inner(environ, start_response)
    app.wsgi_app = _with_prefix
if os.getenv("TRUST_PROXY") == "1":
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

from security import init_auth
from i18n import init_i18n, nm, tr
from pages import bp as pages_bp
from climate import bp as climate_bp
from pop import bp as pop_bp
from notebooks import bp as notebooks_bp
from docs import bp as docs_bp

init_auth(app)
init_i18n(app)
app.register_blueprint(pages_bp)
app.register_blueprint(climate_bp)
app.register_blueprint(pop_bp)
app.register_blueprint(notebooks_bp)
app.register_blueprint(docs_bp)

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")
os.makedirs(STATIC_DIR, exist_ok=True)
WORLDPOP_TIF = os.path.join(HERE, "..", "db", "tjk_worldpop_2020.tif")
POP_OVERLAY_PNG = os.path.join(STATIC_DIR, "population_overlay.png")
_pop_bounds_cache = {}


def ensure_population_overlay():
    """Готовит PNG-подложку из уже скачанного WorldPop-растра (db/tjk_worldpop_2020.tif) -
    один раз, дальше отдаёт из кэша. Логарифмическая шкала - население по стране
    распределено крайне неравномерно, линейная шкала показала бы один яркий пиксель
    в Душанбе и почти ничего больше нигде."""
    if "bounds" in _pop_bounds_cache and os.path.exists(POP_OVERLAY_PNG):
        return _pop_bounds_cache["bounds"]
    if not os.path.exists(WORLDPOP_TIF):
        return None

    import rasterio
    import matplotlib
    from PIL import Image

    with rasterio.open(WORLDPOP_TIF) as src:
        arr = src.read(1).astype(float)
        nodata = src.nodata
        bounds = src.bounds

    if nodata is not None:
        arr = np.where(np.isclose(arr, nodata), np.nan, arr)
    arr = np.where(arr < 0, np.nan, arr)

    valid = arr[~np.isnan(arr)]
    if valid.size == 0:
        return None

    log_arr = np.log1p(arr)
    vmax = np.nanpercentile(log_arr, 99) or 1.0
    norm = np.clip(log_arr / vmax, 0, 1)

    cmap = matplotlib.colormaps["YlOrRd"]
    rgba = cmap(norm)
    rgba[..., 3] = np.where(np.isnan(arr), 0.0, 0.7)
    Image.fromarray((rgba * 255).astype("uint8"), mode="RGBA").save(POP_OVERLAY_PNG)

    b = {"south": bounds.bottom, "west": bounds.left, "north": bounds.top, "east": bounds.right}
    _pop_bounds_cache["bounds"] = b
    return b


WP_SRC_DIR = os.path.join(HERE, "..", "source_data", "worldpop_r2025a")
WP_BUILT_DIR = os.path.join(HERE, "..", "geoserver_rasters", "worldpop_2025")


def _wp(code):
    return os.path.join(WP_SRC_DIR, f"tjk_{code}_2025_CN_100m_R2025A_v1.tif")


# слой -> (растры со знаком +, растры со знаком -, палитра). Если уже собран
# geoserver/build_population_layers.py, берётся готовый geoserver_rasters/worldpop_2025/<слой>.tif
WP_OVERLAYS = {
    "children_u18_2025": ([_wp("T_Under_18")], [], "PuRd"),
    "children_u5_2025": ([_wp("t_00"), _wp("t_01")], [], "Oranges"),
    "population_2025": ([_wp("T_F"), _wp("T_M")], [], "YlOrRd"),
}
_wp_bounds_cache = {}


def ensure_wp_overlay(name, step=3):
    """PNG-подложка слоя WorldPop R2025A (100 м): сумма по блокам step x step (~300 м),
    логарифмическая шкала, прозрачно там, где людей нет. Строится один раз, дальше из кэша."""
    png = os.path.join(STATIC_DIR, f"wp_{name}.png")
    if name in _wp_bounds_cache and os.path.exists(png):
        return _wp_bounds_cache[name]
    if name not in WP_OVERLAYS:
        return None
    plus, minus, cmap_name = WP_OVERLAYS[name]
    built = os.path.join(WP_BUILT_DIR, f"{name}.tif")
    if os.path.exists(built):
        plus, minus = [built], []
    if not all(os.path.exists(f) for f in plus + minus):
        return None
    import rasterio
    import matplotlib
    from PIL import Image
    arr = None
    for k, f in enumerate(plus + minus):
        with rasterio.open(f) as src:
            a = src.read(1).astype("float32")
            bounds = src.bounds
        a[(a < 0) | ~np.isfinite(a)] = 0
        if arr is None:
            arr = a
        elif k < len(plus):
            arr += a
        else:
            arr -= a
        del a
    np.clip(arr, 0, None, out=arr)
    h, w = (arr.shape[0] // step) * step, (arr.shape[1] // step) * step
    agg = arr[:h, :w].reshape(h // step, step, w // step, step).sum(axis=(1, 3))
    log_arr = np.log1p(agg)
    norm = np.clip(log_arr / (np.percentile(log_arr[agg > 0], 99) or 1.0), 0, 1)
    rgba = matplotlib.colormaps[cmap_name](norm)
    rgba[..., 3] = np.where(agg > 0.5, 0.75, 0.0)
    Image.fromarray((rgba * 255).astype("uint8"), mode="RGBA").save(png, optimize=True)
    b = {"south": bounds.top - h * (bounds.top - bounds.bottom) / arr.shape[0], "west": bounds.left,
         "north": bounds.top, "east": bounds.left + w * (bounds.right - bounds.left) / arr.shape[1],
         "url": f"static/wp_{name}.png"}
    _wp_bounds_cache[name] = b
    return b


HYDRO_SRC = os.path.join(STATIC_DIR, "hydrology.geojson")
HYDRO_WEB = os.path.join(STATIC_DIR, "hydrology_web.geojson.gz")


def ensure_hydrology_web(tol=0.0003, min_area=1e-6):
    """Облегчённая копия слоя рек и водоёмов для карты: упрощение ~30 м, водоёмы меньше ~1 га
    отброшены, координаты 5 знаков, gzip. Исходный hydrology.geojson (34 МБ) не меняется:
    по нему считается расстояние до воды в модели. Строится один раз и при обновлении исходника."""
    if not os.path.exists(HYDRO_SRC):
        return None
    if os.path.exists(HYDRO_WEB) and os.path.getmtime(HYDRO_WEB) >= os.path.getmtime(HYDRO_SRC):
        return HYDRO_WEB
    import gzip
    import json
    from shapely.geometry import shape, mapping

    def rnd(c):
        return [rnd(x) for x in c] if isinstance(c[0], (list, tuple)) else [round(c[0], 5), round(c[1], 5)]

    with open(HYDRO_SRC, encoding="utf-8") as f:
        src = json.load(f)
    out = []
    for ft in src["features"]:
        g = shape(ft["geometry"])
        if g.geom_type in ("Polygon", "MultiPolygon") and g.area < min_area:
            continue
        g = g.simplify(tol, preserve_topology=True)
        if g.is_empty:
            continue
        m = mapping(g)
        out.append({"type": "Feature", "properties": {"kind": (ft.get("properties") or {}).get("kind")},
                    "geometry": {"type": m["type"], "coordinates": rnd(m["coordinates"])}})
    data = json.dumps({"type": "FeatureCollection", "features": out}, separators=(",", ":")).encode()
    tmp = HYDRO_WEB + ".tmp"
    with open(tmp, "wb") as f:
        f.write(gzip.compress(data, 6))
    os.replace(tmp, HYDRO_WEB)
    return HYDRO_WEB


PH_TIF = os.path.join(HERE, "..", "db", "tjk_ph.tif")
PH_OVERLAY_PNG = os.path.join(STATIC_DIR, "ph_overlay.png")
_ph_bounds_cache = {}


def ensure_ph_overlay():
    """PNG-подложка из растра db/tjk_ph.tif (см. db/fetch_ph_raster.py).
    Линейная шкала, не логарифмическая - диапазон pH узкий и уже линейно
    осмысленный (в отличие от населения). SoilGrids отдаёт pH*10 - делим."""
    if "bounds" in _ph_bounds_cache and os.path.exists(PH_OVERLAY_PNG):
        return _ph_bounds_cache["bounds"]
    if not os.path.exists(PH_TIF):
        return None

    import rasterio
    import matplotlib
    from PIL import Image

    with rasterio.open(PH_TIF) as src:
        arr = src.read(1).astype(float)
        nodata = src.nodata
        bounds = src.bounds

    if nodata is not None:
        arr = np.where(np.isclose(arr, nodata), np.nan, arr)
    arr = arr / 10.0  # SoilGrids: pH*10

    valid = arr[~np.isnan(arr)]
    if valid.size == 0:
        return None

    vmin, vmax = np.nanpercentile(valid, [2, 98])
    norm = np.clip((arr - vmin) / max(vmax - vmin, 0.01), 0, 1)

    cmap = matplotlib.colormaps["RdYlBu_r"]  # красный - кислее, синий - щелочнее
    rgba = cmap(norm)
    rgba[..., 3] = np.where(np.isnan(arr), 0.0, 0.7)
    Image.fromarray((rgba * 255).astype("uint8"), mode="RGBA").save(PH_OVERLAY_PNG)

    b = {"south": bounds.bottom, "west": bounds.left, "north": bounds.top, "east": bounds.right,
         "vmin": float(vmin), "vmax": float(vmax)}
    _ph_bounds_cache["bounds"] = b
    return b

SITES_SQL = """
WITH latest_model AS (
    SELECT model_version_id, lowo_r2, lowo_rmse, n_sites, n_points, trained_at
    FROM model_versions
    WHERE target_substance = 'Pb'
    ORDER BY trained_at DESC
    LIMIT 1
),
model_by_site AS (
    SELECT p.site_id,
           PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY p.risk_score) AS model_risk_score,
           mode() WITHIN GROUP (ORDER BY p.risk_level) AS model_risk_level
    FROM predictions p
    JOIN latest_model lm ON lm.model_version_id = p.model_version_id
    GROUP BY p.site_id
),
raw_stats AS (
    SELECT sam.site_id,
           PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY m.value)
               FILTER (WHERE m.substance = 'Pb') AS pb_median,
           PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY m.value)
               FILTER (WHERE m.substance = 'As') AS as_median,
           PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY m.value)
               FILTER (WHERE m.substance = 'Hg') AS hg_median,
           count(DISTINCT sam.sample_id) AS n_points
    FROM samples sam JOIN measurements m ON m.sample_id = sam.sample_id
    WHERE m.below_lod = FALSE
    GROUP BY sam.site_id
)
SELECT
    s.site_id, s.name, s.pollution_type, s.status, s.contour_geom AS geometry,
    s.priority_rank, s.priority_composite_index,
    raw_stats.pb_median, raw_stats.as_median, raw_stats.hg_median, raw_stats.n_points,
    model_by_site.model_risk_score, model_by_site.model_risk_level,
    latest_model.lowo_r2, latest_model.lowo_rmse, latest_model.trained_at,
    sc.soil_ph, sc.population_500m
FROM sites s
LEFT JOIN raw_stats ON raw_stats.site_id = s.site_id
LEFT JOIN model_by_site ON model_by_site.site_id = s.site_id
LEFT JOIN latest_model ON true
LEFT JOIN site_covariates sc ON sc.site_id = s.site_id
"""

SAMPLES_SQL = """
SELECT sam.sample_id, sam.site_id, sam.sample_date, sam.geom AS geometry,
       m.substance, m.value, m.below_lod
FROM samples sam
JOIN measurements m ON m.sample_id = sam.sample_id
WHERE m.substance = 'Pb'
"""


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/sites")
def api_sites():
    gdf = gpd.read_postgis(SITES_SQL, engine, geom_col="geometry")
    if "trained_at" in gdf.columns:
        gdf["trained_at"] = gdf["trained_at"].astype(str)
    gdf["label"] = gdf["name"].map(nm)  # название на языке интерфейса; name остаётся ключом
    return app.response_class(gdf.to_json(), mimetype="application/json")


@app.route("/api/buffers")
def api_buffers():
    # site_buffers - VIEW из schema.sql (ST_Buffer от контура "на лету"),
    # не отдельная таблица - поэтому буферы физически не могут разойтись
    # с контуром (см. схему и историю бага Кушониен/Шахринав)
    gdf = gpd.read_postgis(
        "SELECT site_id, buf_50m AS geometry, '50m' AS radius FROM site_buffers "
        "UNION ALL "
        "SELECT site_id, buf_100m AS geometry, '100m' AS radius FROM site_buffers",
        engine, geom_col="geometry"
    )
    gdf["site_label"] = gdf["site_id"].map(nm)
    return app.response_class(gdf.to_json(), mimetype="application/json")


@app.route("/api/samples")
def api_samples():
    gdf = gpd.read_postgis(SAMPLES_SQL, engine, geom_col="geometry")
    gdf["sample_date"] = gdf["sample_date"].astype(str)
    gdf["site_label"] = gdf["site_id"].map(nm)
    return app.response_class(gdf.to_json(), mimetype="application/json")


@app.route("/api/buildings/<site_id>")
def api_buildings(site_id):
    # то, что реально нашёл Microsoft ML для этого склада - см.
    # db/fetch_buildings.py и db/buildings_cache/. Пустой ответ здесь -
    # диагностика сама по себе: значит, датасет ничего не нашёл в радиусе
    # поиска для этой точки, независимо от того, есть ли там реальная
    # застройка по OSM/спутнику.
    cache_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "db", "buildings_cache", f"{site_id}.geojson"
    )
    if not os.path.exists(cache_path):
        return jsonify({"type": "FeatureCollection", "features": [], "note": "нет кэша для этого склада"})
    gdf = gpd.read_file(cache_path)
    return app.response_class(gdf.to_json(), mimetype="application/json")


@app.route("/api/population_bounds")
def api_population_bounds():
    b = ensure_population_overlay()
    if b is None:
        return jsonify({"error": "WorldPop растр не найден - запустите db/fetch_landcover_population.py"}), 404
    return jsonify(b)


@app.route("/api/children_bounds")
def api_children_bounds():
    return api_wp_bounds("children_u18_2025")


@app.route("/api/wp_bounds/<name>")
def api_wp_bounds(name):
    b = ensure_wp_overlay(name)
    if b is None:
        return jsonify({"error": f"нет растров WorldPop R2025A для слоя {name} (source_data/worldpop_r2025a/)"}), 404
    return jsonify(b)


@app.route("/api/ph_bounds")
def api_ph_bounds():
    b = ensure_ph_overlay()
    if b is None:
        return jsonify({"error": "pH-растр не найден - запустите db/fetch_ph_raster.py"}), 404
    return jsonify(b)


@app.route("/api/ph_at_point")
def api_ph_at_point():
    from flask import request
    try:
        lat = float(request.args.get("lat"))
        lon = float(request.args.get("lon"))
    except (TypeError, ValueError):
        return jsonify({"error": "нужны числовые параметры ?lat=..&lon=.."}), 400

    if not os.path.exists(PH_TIF):
        return jsonify({"error": "pH-растр не найден - запустите db/fetch_ph_raster.py"}), 404

    import rasterio
    with rasterio.open(PH_TIF) as src:
        row, col = src.index(lon, lat)
        if not (0 <= row < src.height and 0 <= col < src.width):
            return jsonify({"error": tr("точка вне покрытия растра", "point outside raster coverage")}), 404
        val = src.read(1)[row, col]
        nodata = src.nodata

    if (nodata is not None and np.isclose(val, nodata)) or np.isnan(val):
        # два разных случая пустоты объединены в одну явную ошибку:
        # объявленный nodata ИЛИ голый NaN в массиве (второе раньше тихо
        # ломало JSON на стороне браузера - Python сериализует NaN как
        # NaN, а это не валидный JSON-литерал, JS не может его распарсить)
        return jsonify({"error": tr("нет данных для этой точки (пропуск в покрытии SoilGrids - "
                                     "часто так у водоёмов и подобных участков)",
                                     "no data at this point (gap in SoilGrids coverage, typical for water bodies)")}), 404

    return jsonify({"ph": round(float(val) / 10.0, 2), "lat": lat, "lon": lon})


@app.route("/api/risk_zones")
def api_risk_zones():
    # зоны концентрации загрязнения внутри площадки (низкий/средний/высокий/
    # критический), построены на реальных пробах - см. db/build_risk_zones.py
    # и п.1.3 data_dictionary_metadata.md. Берём только самый свежий прогон
    # (model_versions не удаляются - отбираем max trained_at по нужному variant).
    gdf = gpd.read_postgis("""
        SELECT rz.zone_id, rz.site_id, rz.risk_level, rz.geom AS geometry
        FROM risk_zones rz
        WHERE rz.model_version_id = (
            SELECT model_version_id FROM model_versions
            WHERE target_variant = 'measured_composite_within_site_quartiles'
            ORDER BY trained_at DESC LIMIT 1
        )
    """, engine, geom_col="geometry")
    if gdf.empty:
        return jsonify({"type": "FeatureCollection", "features": [],
                         "note": "нет зон - запустите db/build_risk_zones.py"})
    gdf["site_label"] = gdf["site_id"].map(nm)
    return app.response_class(gdf.to_json(), mimetype="application/json")


@app.route("/api/hydrology")
def api_hydrology():
    path = ensure_hydrology_web()
    if path is None:
        return jsonify({"type": "FeatureCollection", "features": [],
                         "note": "нет файла - запустите db/fetch_hydrology_layer.py"})
    with open(path, "rb") as f:
        body = f.read()
    if "gzip" in request.headers.get("Accept-Encoding", ""):
        resp = app.response_class(body, mimetype="application/json")
        resp.headers["Content-Encoding"] = "gzip"
    else:
        import gzip
        resp = app.response_class(gzip.decompress(body), mimetype="application/json")
    resp.headers["Cache-Control"] = "private, max-age=86400"
    resp.headers["Vary"] = "Accept-Encoding"
    return resp


@app.route("/table")
def table_view():
    with engine.connect() as conn:
        sites_df = pd.read_sql(text("""
            SELECT s.site_id, s.name, s.status, sc.soil_ph, sc.population_500m,
                   raw.pb_median, raw.as_median, raw.hg_median, raw.n_points
            FROM sites s
            LEFT JOIN site_covariates sc ON sc.site_id = s.site_id
            LEFT JOIN (
                SELECT sam.site_id,
                       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY m.value) FILTER (WHERE m.substance='Pb') AS pb_median,
                       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY m.value) FILTER (WHERE m.substance='As') AS as_median,
                       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY m.value) FILTER (WHERE m.substance='Hg') AS hg_median,
                       count(DISTINCT sam.sample_id) AS n_points
                FROM samples sam JOIN measurements m ON m.sample_id = sam.sample_id
                WHERE m.below_lod = FALSE
                GROUP BY sam.site_id
            ) raw ON raw.site_id = s.site_id
            ORDER BY raw.pb_median DESC NULLS LAST
        """), conn)

        samples_df = pd.read_sql(text("""
            SELECT sam.sample_id, sam.site_id, sam.sample_date,
                   ST_Y(sam.geom) AS lat, ST_X(sam.geom) AS lon,
                   sam.dist_to_water_m, sam.dist_to_building_m,
                   m.substance, m.value, m.below_lod
            FROM samples sam
            JOIN measurements m ON m.sample_id = sam.sample_id
            ORDER BY sam.site_id, sam.sample_id, m.substance
        """), conn)

    return render_template(
        "table.html",
        sites_columns=list(sites_df.columns),
        sites_rows=sites_df.to_dict(orient="records"),
        samples_columns=list(samples_df.columns),
        samples_rows=samples_df.to_dict(orient="records"),
    )


if __name__ == "__main__":
    app.run(host=os.getenv("DEMO_HOST", "127.0.0.1"), port=int(os.getenv("DEMO_PORT", "5050")),
            debug=os.getenv("FLASK_DEBUG") == "1")
