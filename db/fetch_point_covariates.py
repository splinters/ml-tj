"""
Забирает реальные ковариаты из открытых источников и пишет их в БД:
  - pH почвы (SoilGrids REST API)     -> site_covariates, по складу
  - уклон/высота (Open-Topo-Data)     -> site_covariates, по складу
  - расстояние до воды (OSM Overpass) -> samples.dist_to_water_m, по точке

pH и уклон - по складу (10 запросов, не 169): на масштабе в несколько
сотен метров эти данные (SoilGrids 250м, SRTM 90м) физически не различают
точки внутри одной площадки - тянуть их для каждой точки только зря
нагружало бы бесплатные публичные API. Расстояние до воды - другое дело,
это геометрия, которая реально меняется от края до края склада.

ВАЖНО: запускать локально (у меня в песочнице эти домены недоступны -
тот же тип ограничения, что был с Overpass раньше). Скрипт кэширует
результат по складам, чтобы не дёргать публичные API повторно - удалите
кэш-файл covariates_cache.json, если нужно пересчитать с нуля.

pip install requests shapely geopandas sqlalchemy python-dotenv
"""
import os
import json
import time
import requests
import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point, LineString, MultiLineString
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.getenv("DATABASE_URL", "postgresql://localhost:5432/soil_ml")
engine = create_engine(DB_URL)

CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "covariates_cache.json")
UTM = "EPSG:32642"  # UTM 42N - метры


def load_cache():
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache):
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)


def request_with_retry(method, url, retries=3, backoff=3, **kwargs):
    """Публичные API (SoilGrids/Overpass/Open-Topo-Data) периодически отдают
    502/504 под нагрузкой - это не значит, что сервис недоступен, обычно
    вторая-третья попытка проходит нормально."""
    last_exc = None
    for attempt in range(retries):
        try:
            r = requests.request(method, url, **kwargs)
            r.raise_for_status()
            return r
        except requests.exceptions.RequestException as e:
            last_exc = e
            if attempt < retries - 1:
                wait = backoff * (attempt + 1)
                print(f"    попытка {attempt + 1}/{retries} не удалась ({e}), повтор через {wait}с...")
                time.sleep(wait)
    raise last_exc


def fetch_soil_ph(lat, lon):
    try:
        r = request_with_retry(
            "GET", "https://rest.isric.org/soilgrids/v2.0/properties/query",
            params={"lon": lon, "lat": lat, "property": "phh2o", "depth": "0-5cm", "value": "mean"},
            timeout=20,
        )
        ph_x10 = r.json()["properties"]["layers"][0]["depths"][0]["values"]["mean"]
        if ph_x10 is None:
            print("    SoilGrids ответил, но для этой точки нет данных (не ошибка сети) - pH = NaN")
            return None
        return ph_x10 / 10.0  # SoilGrids отдаёт pH*10
    except Exception as e:
        print(f"    SoilGrids недоступен ({e}) - pH = NaN")
        return None


def fetch_elevation(lat, lon):
    try:
        r = request_with_retry(
            "GET", "https://api.opentopodata.org/v1/srtm90m",
            params={"locations": f"{lat},{lon}"}, timeout=20,
        )
        return r.json()["results"][0]["elevation"]
    except Exception as e:
        print(f"    Open-Topo-Data недоступен ({e}) - высота = NaN")
        return None


def fetch_elevation_and_slope(lat, lon):
    d = 0.001  # ~100м в градусах
    e_center = fetch_elevation(lat, lon)
    time.sleep(1)
    e_north = fetch_elevation(lat + d, lon)
    time.sleep(1)
    e_east = fetch_elevation(lat, lon + d)
    time.sleep(1)
    if None in (e_center, e_north, e_east):
        return e_center, None
    dz_dy = (e_north - e_center) / (d * 111_320)
    dz_dx = (e_east - e_center) / (d * 111_320 * np.cos(np.radians(lat)))
    slope_deg = float(np.degrees(np.arctan(np.hypot(dz_dx, dz_dy))))
    return e_center, slope_deg


OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]


def fetch_water_features(lat, lon, radius_m=3000):
    q = f"""[out:json][timeout:60];
(
  way["natural"="water"](around:{radius_m},{lat},{lon});
  way["waterway"~"river|canal|stream"](around:{radius_m},{lat},{lon});
);
out geom;"""
    elements = None
    for mirror in OVERPASS_MIRRORS:
        try:
            r = request_with_retry(
                "GET", mirror, params={"data": q},
                headers={"User-Agent": "tj-soil-ml-demo/1.0"}, timeout=60,
                retries=2,  # на каждое зеркало по 2 попытки, не 3х2=6 - не затягивать общий прогон
            )
            elements = r.json().get("elements", [])
            break
        except Exception as e:
            print(f"    {mirror} недоступен ({e}), пробую следующее зеркало...")
    if elements is None:
        print("    все зеркала Overpass недоступны сейчас - вода = NaN (перезапустите скрипт позже, "
              "этот склад не закэшируется как окончательный и попробуется снова)")
        return None

    lines = []
    for el in elements:
        coords = [(pt["lon"], pt["lat"]) for pt in el.get("geometry", [])]
        if len(coords) >= 2:
            lines.append(LineString(coords))
    if not lines:
        return None
    return gpd.GeoSeries(lines, crs=4326).to_crs(UTM).union_all()


def main():
    cache = load_cache()

    with engine.connect() as conn:
        sites = pd.read_sql(
            "SELECT site_id, ST_Y(ST_Centroid(contour_geom)) AS lat, "
            "ST_X(ST_Centroid(contour_geom)) AS lon FROM sites", conn)
        samples = gpd.read_postgis("SELECT sample_id, site_id, geom FROM samples", conn, geom_col="geom")

    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO site_covariates (site_id, elevation_m, slope_deg, soil_ph)
            VALUES (:site_id, NULL, NULL, NULL)
            ON CONFLICT (site_id) DO NOTHING
        """), [{"site_id": s} for s in sites.site_id])

    samples_utm = samples.to_crs(UTM)

    for _, row in sites.iterrows():
        site = row.site_id
        print(f"\n{site}:")

        cached_ok = site in cache and cache[site].get("ph") is not None and cache[site].get("water_wkt") is not None
        if cached_ok:
            print("  из кэша (удалите covariates_cache.json, чтобы пересчитать)")
            entry = cache[site]
        else:
            ph = fetch_soil_ph(row.lat, row.lon)
            print(f"  pH почвы: {ph}")
            time.sleep(1)

            elev, slope = fetch_elevation_and_slope(row.lat, row.lon)
            print(f"  высота: {elev} м, уклон: {slope}")

            water_geom = fetch_water_features(row.lat, row.lon)
            print(f"  водные объекты рядом: {'найдены' if water_geom is not None else 'не найдены'}")
            time.sleep(1)

            entry = {"ph": ph, "elevation_m": elev, "slope_deg": slope,
                     "water_wkt": water_geom.wkt if water_geom is not None else None}
            cache[site] = entry
            save_cache(cache)

        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE site_covariates
                SET soil_ph = :ph, elevation_m = :elev, slope_deg = :slope, computed_at = now()
                WHERE site_id = :site_id
            """), {"ph": entry["ph"], "elev": entry["elevation_m"], "slope": entry["slope_deg"], "site_id": site})

        if entry["water_wkt"]:
            from shapely import wkt as shapely_wkt
            water_geom = shapely_wkt.loads(entry["water_wkt"])
            site_samples = samples_utm[samples_utm.site_id == site]
            for sid, geom in zip(site_samples.sample_id, site_samples.geom):
                dist = geom.distance(water_geom)
                with engine.begin() as conn:
                    conn.execute(text("UPDATE samples SET dist_to_water_m = :d WHERE sample_id = :sid"),
                                 {"d": float(dist), "sid": int(sid)})

    print("\nГотово. Проверка:")
    with engine.connect() as conn:
        cov = pd.read_sql("SELECT * FROM site_covariates", conn)
        print(cov)
        n_water = conn.execute(text("SELECT count(*) FROM samples WHERE dist_to_water_m IS NOT NULL")).scalar()
        print(f"\nТочек с посчитанным dist_to_water_m: {n_water} / {len(samples)}")


if __name__ == "__main__":
    main()
