"""
Расстояние до ближайшей дороги - ПО ТОЧКЕ (как вода), не по складу.
Физическое обоснование именно для Pb: придорожный свинец от этилированного
бензина - известный, десятилетиями документированный механизм загрязнения
почвы вдоль дорог по всему миру, не выдумка ради лишнего признака.

Тот же паттерн, что и для воды: Overpass с ретраями и зеркалом, кэш на
диске, чтобы не дёргать сервис повторно при перезапуске.

Запуск: python db/fetch_roads_covariate.py
"""
import os
import time
import requests
import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import LineString
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.getenv("DATABASE_URL", "postgresql://soil_ml:soil_ml_dev_password@localhost:5432/soil_ml")
engine = create_engine(DB_URL)

HERE = os.path.dirname(os.path.abspath(__file__))
UTM = "EPSG:32642"
SEARCH_RADIUS_M = 3000
CACHE_DIR = os.path.join(HERE, "roads_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]


def request_with_retry(method, url, retries=2, backoff=5, **kwargs):
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


def fetch_roads_near(lat, lon, radius_m=SEARCH_RADIUS_M):
    # основные категории дорог - не тропинки/пешеходные, у них не тот
    # физический механизм (транспортный поток), что нас интересует
    q = f"""[out:json][timeout:60];
(
  way["highway"~"motorway|trunk|primary|secondary|tertiary|unclassified|residential"]
     (around:{radius_m},{lat},{lon});
);
out geom;"""
    for mirror in OVERPASS_MIRRORS:
        try:
            r = request_with_retry("GET", mirror, params={"data": q},
                                    headers={"User-Agent": "tj-soil-ml-demo/1.0"}, timeout=60)
            elements = r.json().get("elements", [])
            break
        except Exception as e:
            print(f"    {mirror} недоступен ({e}), пробую следующее зеркало...")
    else:
        return None

    geoms = []
    for el in elements:
        if el.get("type") == "way" and "geometry" in el:
            coords = [(pt["lon"], pt["lat"]) for pt in el["geometry"]]
            if len(coords) >= 2:
                geoms.append(LineString(coords))
    if not geoms:
        return None
    return gpd.GeoDataFrame(geometry=geoms, crs=4326)


def main():
    with engine.connect() as conn:
        sites = pd.read_sql(
            "SELECT site_id, ST_Y(ST_Centroid(contour_geom)) AS lat, "
            "ST_X(ST_Centroid(contour_geom)) AS lon FROM sites", conn)
        samples = gpd.read_postgis("SELECT sample_id, site_id, geom FROM samples", conn, geom_col="geom")
    samples_utm = samples.to_crs(UTM)

    for _, row in sites.iterrows():
        site = row.site_id
        print(f"\n{site}:")
        cache_path = os.path.join(CACHE_DIR, f"{site}.geojson")

        if os.path.exists(cache_path):
            print(f"  из кэша: {cache_path}")
            roads = gpd.read_file(cache_path)
        else:
            roads = fetch_roads_near(row.lat, row.lon)
            if roads is not None and len(roads):
                roads.to_file(cache_path, driver="GeoJSON")
                print(f"  сохранено в кэш: {cache_path}")
            time.sleep(1)

        if roads is None or len(roads) == 0:
            print(f"  дорог не найдено в радиусе {SEARCH_RADIUS_M}м - пишем консервативную "
                  f"нижнюю границу (тот же приём, что и с зданиями/Дусти)")
            floor_m = SEARCH_RADIUS_M
            site_samples = samples_utm[samples_utm.site_id == site]
            with engine.begin() as conn:
                for sid in site_samples.sample_id:
                    conn.execute(text("UPDATE samples SET dist_to_road_m = :d WHERE sample_id = :sid"),
                                 {"d": float(floor_m), "sid": int(sid)})
            continue

        print(f"  найдено сегментов дорог: {len(roads)}")
        roads_utm = roads.to_crs(UTM)
        site_samples = samples_utm[samples_utm.site_id == site]
        with engine.begin() as conn:
            for sid, geom in zip(site_samples.sample_id, site_samples.geometry):
                dist = float(roads_utm.distance(geom).min())
                conn.execute(text("UPDATE samples SET dist_to_road_m = :d WHERE sample_id = :sid"),
                             {"d": dist, "sid": int(sid)})

    print("\nГотово. Проверка:")
    with engine.connect() as conn:
        print(pd.read_sql(
            "SELECT count(*) AS n, avg(dist_to_road_m) AS avg_dist_m "
            "FROM samples WHERE dist_to_road_m IS NOT NULL", conn))


if __name__ == "__main__":
    main()
