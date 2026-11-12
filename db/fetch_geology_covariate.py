"""
Достаёт класс литологии (тип породы) по каждому из 10 складов из уже
скачанного GLiM-грида (db/glim_data/*.asc, см. db/fetch_geology_layer.py).

ВАЖНО: в .asc-файле не указан CRS (обычное дело для ESRI ASCII grid) -
проставляем EPSG:4326 вручную, имя файла ("wgs84") и bounds (-180..180,
-90..90 глобально) подтверждают, что это простая широта/долгота, не
какая-то проекция.

Категориальный признак (код породы), не непрерывный - в отличие от
воды/pH/высоты, которые тестировали до этого.

Запуск: python db/fetch_geology_covariate.py
"""
import os
import glob
import numpy as np
import pandas as pd
import rasterio
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.getenv("DATABASE_URL", "postgresql://soil_ml:soil_ml_dev_password@localhost:5432/soil_ml")
engine = create_engine(DB_URL)

HERE = os.path.dirname(os.path.abspath(__file__))
GLIM_DIR = os.path.join(HERE, "glim_data")


def find_asc_file():
    matches = glob.glob(os.path.join(GLIM_DIR, "*.asc"))
    if not matches:
        raise SystemExit(f"Нет .asc файла в {GLIM_DIR} - запустите db/fetch_geology_layer.py сначала.")
    return matches[0]


def main():
    asc_path = find_asc_file()
    print(f"Читаю {asc_path}")

    with rasterio.open(asc_path) as src:
        print(f"  размер: {src.width}x{src.height}, CRS в файле: {src.crs}, bounds: {src.bounds}")
        arr = src.read(1)
        nodata = src.nodata
        transform = src.transform
        # CRS в файле не задан - вручную фиксируем WGS84 (см. docstring)
        crs = src.crs or "EPSG:4326"

    print(f"  уникальных кодов породы в файле: {sorted(set(arr.flatten().tolist()))[:20]}"
          f"{' ...' if len(set(arr.flatten().tolist())) > 20 else ''}")

    with engine.connect() as conn:
        sites = pd.read_sql(
            "SELECT site_id, ST_Y(ST_Centroid(contour_geom)) AS lat, "
            "ST_X(ST_Centroid(contour_geom)) AS lon FROM sites", conn)

    print(f"\n{'Склад':<20s} {'lithology_class':>15s}")
    for _, row in sites.iterrows():
        # индекс пикселя по географическим координатам через обратную трансформацию
        col_idx, row_idx = ~transform * (row.lon, row.lat)
        col_idx, row_idx = int(col_idx), int(row_idx)
        if not (0 <= row_idx < arr.shape[0] and 0 <= col_idx < arr.shape[1]):
            print(f"{row.site_id:<20s} {'вне растра':>15s}")
            continue
        val = arr[row_idx, col_idx]
        lith = None if (nodata is not None and val == nodata) else int(val)
        print(f"{row.site_id:<20s} {lith if lith is not None else 'нет данных':>15}")

        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO site_covariates (site_id, lithology_class)
                VALUES (:site_id, :lith)
                ON CONFLICT (site_id) DO UPDATE SET lithology_class = EXCLUDED.lithology_class,
                                                     computed_at = now()
            """), {"site_id": row.site_id, "lith": lith})

    print("\nГотово. Проверка:")
    with engine.connect() as conn:
        print(pd.read_sql("SELECT site_id, lithology_class FROM site_covariates ORDER BY site_id", conn))


if __name__ == "__main__":
    main()
