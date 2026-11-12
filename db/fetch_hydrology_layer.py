"""
Гидрология для карты (весь Таджикистан) - реки и каналы через OSM Overpass,
не HydroRIVERS. Решение осознанное: HydroRIVERS для Азии - это десятки-
сотни МБ на скачивание ради одной страны, тогда как для слоя на карте
(не для точных per-point расстояний, где HydroRIVERS был бы уместнее)
достаточно того же надёжного Overpass-пайплайна, что уже работает для
воды/зданий по конкретным складам - с ретраями и зеркалом.

Запуск: python db/fetch_hydrology_layer.py
"""
import os
import time
import requests
import geopandas as gpd
from shapely.geometry import LineString, Polygon

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(HERE, "..", "webapp", "static", "hydrology.geojson")
os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)

# bbox Таджикистана с запасом (тот же, что и в остальных страновых скриптах)
BBOX = (36.4, 67.0, 41.1, 75.3)  # south, west, north, east - формат Overpass bbox

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
                print(f"  попытка {attempt + 1}/{retries} не удалась ({e}), повтор через {wait}с...")
                time.sleep(wait)
    raise last_exc


def main():
    s, w, n, e = BBOX
    # только реки/каналы, не все waterway (ручьи/дренажи дали бы огромный
    # объём и загромоздили бы карту странового масштаба)
    query = f"""[out:json][timeout:180];
(
  way["waterway"~"river|canal"]({s},{w},{n},{e});
  way["natural"="water"]({s},{w},{n},{e});
);
out geom;"""

    elements = None
    for mirror in OVERPASS_MIRRORS:
        try:
            print(f"Запрашиваю {mirror} (страна целиком, может занять минуту-две)...")
            r = request_with_retry("GET", mirror, params={"data": query},
                                    headers={"User-Agent": "tj-soil-ml-demo/1.0"}, timeout=180)
            elements = r.json().get("elements", [])
            break
        except Exception as ex:
            print(f"  {mirror} недоступен ({ex}), пробую следующее зеркало...")

    if not elements:
        raise SystemExit("Оба зеркала Overpass недоступны - попробуйте позже.")

    geoms, kinds = [], []
    for el in elements:
        if el.get("type") != "way" or "geometry" not in el:
            continue
        coords = [(pt["lon"], pt["lat"]) for pt in el["geometry"]]
        tags = el.get("tags", {})
        if len(coords) < 2:
            continue
        if tags.get("natural") == "water" and len(coords) >= 3:
            geoms.append(Polygon(coords))
            kinds.append("water_body")
        else:
            geoms.append(LineString(coords))
            kinds.append(tags.get("waterway", "waterway"))

    print(f"Объектов гидрологии: {len(geoms)}")
    gdf = gpd.GeoDataFrame({"kind": kinds}, geometry=geoms, crs=4326)
    gdf.to_file(OUT_PATH, driver="GeoJSON")
    print(f"Сохранено: {OUT_PATH}")
    print("Слой раздаётся статически из webapp/static/hydrology.geojson - "
          "перезапускать app.py для его обновления не нужно, только перезагрузить страницу.")


if __name__ == "__main__":
    main()
