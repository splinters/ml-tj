"""
Скачивает контуры зданий Microsoft Global ML Building Footprints для
каждого склада и считает расстояние от каждой точки замера до ближайшего
здания - ПО ТОЧКЕ (как вода), не по складу.

ОБНОВЛЕНИЕ: реальная проверка на Дж. Балхи показала, что Microsoft ML
может давать пробелы покрытия для конкретных quadkey-тайлов, даже когда
здания там точно есть (подтверждено через OSM - десятки зданий, включая
building=industrial рядом со складом). Поэтому теперь: если Microsoft ML
ничего не нашёл, пробуем OSM как запасной источник, и только если ОБА
источника пусты - пишем консервативное floor-значение (см. Дусти, где
это подтверждено спутниковым снимком - поля и водоёмы, действительно
пусто).

ВАЖНО: прямой доступ к blob-контейнеру minedbuildings.blob.core.windows.net
Microsoft закрыл публично (PublicAccessNotPermitted). Те же данные
доступны через Microsoft Planetary Computer - но с нюансом: ссылка на
ассет имеет схему abfs:// (Azure Data Lake), и её подписывания через
sign_item() одного недостаточно - geopandas/adlfs дополнительно нужен
table:storage_options из extra_fields ассета, иначе adlfs не понимает,
чем авторизоваться (см. обсуждение microsoft/PlanetaryComputer#201).

pip install pystac-client planetary-computer geopandas pyarrow adlfs requests
"""
import os
import time
import requests
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point, Polygon
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.getenv("DATABASE_URL", "postgresql://localhost:5432/soil_ml")
engine = create_engine(DB_URL)

HERE = os.path.dirname(os.path.abspath(__file__))
UTM = "EPSG:32642"
SEARCH_RADIUS_KM = 3
CACHE_DIR = os.path.join(HERE, "buildings_cache")
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
os.makedirs(CACHE_DIR, exist_ok=True)


def request_with_retry(method, url, retries=2, backoff=3, **kwargs):
    last_exc = None
    for attempt in range(retries):
        try:
            r = requests.request(method, url, **kwargs)
            r.raise_for_status()
            return r
        except requests.exceptions.RequestException as e:
            last_exc = e
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
    raise last_exc


def fetch_osm_buildings_near(lat, lon, radius_m):
    """Запасной источник, если Microsoft ML ничего не нашёл (см. подтверждённый
    пробел покрытия на Дж. Балхи - OSM там реально знает про десятки зданий)."""
    q = f"""[out:json][timeout:60];
(
  way["building"](around:{radius_m},{lat},{lon});
  node["building"](around:{radius_m},{lat},{lon});
);
out geom;"""
    elements = None
    for mirror in OVERPASS_MIRRORS:
        try:
            r = request_with_retry("GET", mirror, params={"data": q},
                                    headers={"User-Agent": "tj-soil-ml-demo/1.0"}, timeout=60)
            elements = r.json().get("elements", [])
            break
        except Exception as e:
            print(f"    OSM/{mirror} недоступен ({e}), пробую следующее зеркало...")
    if not elements:
        return None

    geoms = []
    for el in elements:
        if el.get("type") == "way" and "geometry" in el:
            coords = [(pt["lon"], pt["lat"]) for pt in el["geometry"]]
            if len(coords) >= 3:
                geoms.append(Polygon(coords))
        elif el.get("type") == "node":
            geoms.append(Point(el["lon"], el["lat"]))
    if not geoms:
        return None
    return gpd.GeoDataFrame({"source": "osm_fallback"}, index=range(len(geoms)), geometry=geoms, crs=4326)


def fetch_buildings_near(lat, lon, radius_km=SEARCH_RADIUS_KM):
    import pystac_client
    import planetary_computer as pc

    catalog = pystac_client.Client.open("https://planetarycomputer.microsoft.com/api/stac/v1")
    d = radius_km / 111.0  # грубо, градусы
    bbox = [lon - d, lat - d, lon + d, lat + d]
    search = catalog.search(collections=["ms-buildings"], bbox=bbox)
    all_items = list(search.items())
    if not all_items:
        print("    ms-buildings: нет тайлов для этой точки")
        return None

    def href_of(item):
        keys = list(item.assets.keys())
        asset = item.assets.get("data") or item.assets[keys[0]]
        return asset.href

    # ключевая причина "зависания": в поиске попадаются и старые
    # legacy-тайлы "global/2022-..." - это ЦЕЛЫЙ регион/страна одним
    # файлом (сотни МБ - ГБ, поэтому чтение одной такой точки и "висит"),
    # и новые "delta/2023-..." - партиционированы по quadkey, маленькие.
    # Берём только delta - global для точечных запросов непригодны.
    items = [it for it in all_items if "/delta/" in href_of(it)]
    skipped = len(all_items) - len(items)
    if skipped:
        print(f"    пропущено {skipped} legacy 'global' тайлов (целиком на регион/страну, "
              f"гигантские) - используем только 'delta' (по quadkey, маленькие)")
    if not items:
        print("    после фильтрации не осталось тайлов 'delta' для этой точки")
        return None

    frames = []
    for item in items:
        signed_item = pc.sign_item(item)
        asset_keys = list(signed_item.assets.keys())
        asset = signed_item.assets.get("data") or signed_item.assets[asset_keys[0]]
        # ключевая недостающая часть: adlfs не может понять, чем
        # авторизоваться, только по подписанной href - нужен ещё
        # отдельный storage_options с SAS-токеном из STAC table-расширения
        storage_options = asset.extra_fields.get("table:storage_options")

        try:
            gdf = gpd.read_parquet(asset.href, storage_options=storage_options)
        except Exception as e:
            print(f"    не удалось прочитать тайл ({e}) - assets были: {asset_keys}, "
                  f"storage_options {'найден' if storage_options else 'НЕ найден в extra_fields'}")
            continue

        gdf = gdf.clip([bbox[0], bbox[1], bbox[2], bbox[3]])
        if len(gdf):
            frames.append(gdf)

    if not frames:
        return None
    return pd.concat(frames, ignore_index=True)


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
            print(f"  из кэша: {cache_path} (удалите файл, чтобы перекачать заново)")
            buildings = gpd.read_file(cache_path)
        else:
            buildings = fetch_buildings_near(row.lat, row.lon)
            if buildings is not None and len(buildings):
                buildings.to_file(cache_path, driver="GeoJSON")
                print(f"  сохранено в кэш: {cache_path}")

        if buildings is None or len(buildings) == 0:
            print(f"  Microsoft ML: зданий не найдено в радиусе {SEARCH_RADIUS_KM}км - "
                  f"пробую OSM как запасной источник...")
            buildings = fetch_osm_buildings_near(row.lat, row.lon, radius_m=SEARCH_RADIUS_KM * 1000)
            if buildings is not None and len(buildings):
                buildings.to_file(cache_path, driver="GeoJSON")
                print(f"  OSM нашёл {len(buildings)} зданий - используем их вместо floor-значения "
                      f"(сохранено в тот же кэш: {cache_path})")

        if buildings is None or len(buildings) == 0:
            # НЕ оставляем NULL: дальше по пайплайну NULL заполняется средним
            # по выборке (~300м), а это неверно - мы точно знаем, что реальная
            # дистанция здесь НЕ МЕНЬШЕ радиуса поиска (и оба источника - и
            # Microsoft ML, и OSM - сошлись на том, что рядом ничего нет).
            floor_m = SEARCH_RADIUS_KM * 1000
            print(f"  оба источника (Microsoft ML и OSM) не нашли зданий - "
                  f"пишем консервативную нижнюю границу {floor_m}м (не NULL, не среднее)")
            site_samples = samples_utm[samples_utm.site_id == site]
            with engine.begin() as conn:
                for sid in site_samples.sample_id:
                    conn.execute(text("UPDATE samples SET dist_to_building_m = :d WHERE sample_id = :sid"),
                                 {"d": float(floor_m), "sid": int(sid)})
            time.sleep(1)
            continue
        print(f"  найдено зданий рядом: {len(buildings)}")

        buildings_utm = buildings.to_crs(UTM)
        site_samples = samples_utm[samples_utm.site_id == site]
        updates = []
        for sid, geom in zip(site_samples.sample_id, site_samples.geometry):
            dist = float(buildings_utm.distance(geom).min())
            updates.append({"d": dist, "sid": int(sid)})

        with engine.begin() as conn:
            for u in updates:
                conn.execute(text("UPDATE samples SET dist_to_building_m = :d WHERE sample_id = :sid"), u)
        time.sleep(1)

    print("\nГотово. Проверка:")
    with engine.connect() as conn:
        print(pd.read_sql(
            "SELECT count(*) AS n, avg(dist_to_building_m) AS avg_dist_m "
            "FROM samples WHERE dist_to_building_m IS NOT NULL", conn))


if __name__ == "__main__":
    main()
