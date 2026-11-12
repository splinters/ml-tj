"""
Забирает worldcover_class (ESA WorldCover) и population_500m (WorldPop)
по каждому складу - в site_covariates, ПО СКЛАДУ, не по точке.

Это та же осознанная компромиссная схема, что была для pH/уклона/высоты:
разрешение WorldCover 10м и WorldPop 100м всё равно намного грубее, чем
имеет смысл различать внутри одной площадки в 100-300м, поэтому точечные
запросы на каждую из 169 точек не дали бы дополнительной информации, а
только зря нагрузили бы сервисы. Если после diagnose_features.py окажется,
что и эти два признака работают как "фингерпринт склада" - см. предыдущий
разбор с pH - к ним стоит отнестись с той же осторожностью.

pip install pystac-client planetary-computer rioxarray rasterio requests
"""
import os
import requests
import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.getenv("DATABASE_URL", "postgresql://localhost:5432/soil_ml")
engine = create_engine(DB_URL)

HERE = os.path.dirname(os.path.abspath(__file__))
WORLDPOP_PATH = os.path.join(HERE, "tjk_worldpop_2020.tif")
WORLDPOP_URL = ("https://data.worldpop.org/GIS/Population/Global_2000_2020_Constrained/"
                "2020/BSGM/TJK/tjk_ppp_2020_constrained.tif")


def ensure_worldpop_downloaded():
    if os.path.exists(WORLDPOP_PATH):
        print(f"WorldPop уже скачан: {WORLDPOP_PATH}")
        return
    print("Скачиваю WorldPop для Таджикистана (один раз, файл небольшой для одной страны)...")
    r = requests.get(WORLDPOP_URL, stream=True, timeout=120)
    r.raise_for_status()
    with open(WORLDPOP_PATH, "wb") as f:
        for chunk in r.iter_content(chunk_size=1 << 20):
            f.write(chunk)
    print(f"Готово: {WORLDPOP_PATH} ({os.path.getsize(WORLDPOP_PATH) / 1e6:.1f} МБ)")


def population_near(rds, lat, lon, radius_m=500):
    """Сумма населения в буфере вокруг склада, а не значение в одном пикселе
    в центре - потому что люди живут РЯДОМ со складом, а не на нём самом,
    и один 100-метровый пиксель в центроиде может законно быть нулевым
    (сам склад - не жилая застройка), даже если рядом полно домов."""
    # ~на глаз в градусах для средних широт Таджикистана, для точности
    # можно заменить на честный geodesic-буфер через geopandas/pyproj
    deg_lat = radius_m / 111_320
    deg_lon = radius_m / (111_320 * np.cos(np.radians(lat)))
    window = rds.sel(x=slice(lon - deg_lon, lon + deg_lon), y=slice(lat + deg_lat, lat - deg_lat))
    vals = window.values
    nodata = rds.rio.nodata
    if nodata is not None:
        vals = np.where(np.isclose(vals, nodata), np.nan, vals)
    if np.all(np.isnan(vals)):
        return None
    return float(np.nansum(vals))


def worldcover_class_at(lat, lon):
    """Точечный запрос к ESA WorldCover через Microsoft Planetary Computer -
    без скачивания растра на всю страну: читается только нужный пиксель
    через частичное чтение облачно-оптимизированного GeoTIFF (COG)."""
    try:
        import pystac_client
        import planetary_computer
        import rioxarray

        catalog = pystac_client.Client.open(
            "https://planetarycomputer.microsoft.com/api/stac/v1",
            modifier=planetary_computer.sign_inplace,
        )
        search = catalog.search(
            collections=["esa-worldcover"],
            intersects={"type": "Point", "coordinates": [lon, lat]},
        )
        items = list(search.items())
        if not items:
            print("    WorldCover: не нашлось тайла для этой точки")
            return None
        href = items[0].assets["map"].href
        da = rioxarray.open_rasterio(href)
        try:
            val = da.sel(x=lon, y=lat, method="nearest").values
            return int(np.asarray(val).squeeze())
        finally:
            da.close()
    except Exception as e:
        print(f"    WorldCover недоступен ({e})")
        return None


WORLDCOVER_LABELS = {
    10: "деревья", 20: "кустарники", 30: "травяной покров", 40: "сельхозземли",
    50: "застройка", 60: "голая почва", 80: "вода", 90: "водно-болотные",
    95: "мангры", 100: "мхи/лишайники",
}


def main():
    ensure_worldpop_downloaded()
    import rioxarray
    worldpop = rioxarray.open_rasterio(WORLDPOP_PATH).squeeze()

    with engine.connect() as conn:
        sites = pd.read_sql(
            "SELECT site_id, ST_Y(ST_Centroid(contour_geom)) AS lat, "
            "ST_X(ST_Centroid(contour_geom)) AS lon FROM sites", conn)

    for _, row in sites.iterrows():
        print(f"\n{row.site_id}:")
        pop = population_near(worldpop, row.lat, row.lon, radius_m=500)
        print(f"  население в радиусе 500м (WorldPop, чел.): {pop}")

        wc_class = worldcover_class_at(row.lat, row.lon)
        wc_label = WORLDCOVER_LABELS.get(wc_class, "?")
        print(f"  землепользование (WorldCover): {wc_class} ({wc_label})")

        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO site_covariates (site_id, population_500m, worldcover_class)
                VALUES (:site_id, :pop, :wc)
                ON CONFLICT (site_id) DO UPDATE
                SET population_500m = EXCLUDED.population_500m,
                    worldcover_class = EXCLUDED.worldcover_class,
                    computed_at = now()
            """), {"site_id": row.site_id, "pop": pop, "wc": wc_class})

    print("\nГотово. Проверка:")
    with engine.connect() as conn:
        print(pd.read_sql("SELECT site_id, population_500m, worldcover_class FROM site_covariates", conn))

    worldpop.close()


if __name__ == "__main__":
    main()
