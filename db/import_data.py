"""
Загружает уже подготовленные данные (data_fixed/*.geojson + enriched CSV)
в локальную PostGIS-базу по схеме schema.sql.

ВНИМАНИЕ: этот скрипт предназначен для ПЕРВОЙ загрузки. TRUNCATE ... CASCADE
на sites стирает не только sites/samples/measurements, но и всё, что на них
ссылается внешним ключом - site_covariates (pH/уклон/население/
землепользование) и predictions. Если вы уже запускали
fetch_point_covariates.py / fetch_landcover_population.py / fetch_buildings.py,
повторный запуск этого скрипта БЕЗ --force прервётся с предупреждением,
чтобы не стереть эту работу случайно.

Запуск (из папки local_demo_stand/, после того как поднят docker-compose
и переменная DATABASE_URL доступна через .env):

    python db/import_data.py            # только если sites ещё пуст
    python db/import_data.py --force    # осознанно стереть всё и перезалить

"""
import os
import sys
import pandas as pd
import geopandas as gpd
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.getenv("DATABASE_URL", "postgresql://soil_ml:soil_ml_dev_password@localhost:5432/soil_ml")
engine = create_engine(DB_URL)

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "source_data")

METALS = ["Mo", "U", "Rb", "Th", "Pb", "As", "Hg", "Zn", "Co"]

with engine.connect() as conn:
    n_existing = conn.execute(text("SELECT count(*) FROM sites")).scalar()

if n_existing and "--force" not in sys.argv:
    print(f"В sites уже есть {n_existing} площадок. Повторный запуск сотрёт их и всё, что от них "
          f"зависит (site_covariates: pH/уклон/население/землепользование; predictions), из-за "
          f"TRUNCATE ... CASCADE.\n"
          f"Если это осознанно (например, меняете исходные данные складов) - запустите:\n"
          f"  python db/import_data.py --force\n"
          f"и затем заново прогоните fetch_point_covariates.py / fetch_landcover_population.py / "
          f"fetch_buildings.py для восстановления ковариат.")
    sys.exit(1)

# ---------------------------------------------------------------- sites ---
import shapely

wh = gpd.read_file(f"{SRC}/warehouses.geojson")
# исходный файл содержит Z=0 у каждой точки контура - GDAL из-за этого
# определяет CRS как EPSG:4979 (3D), а не 4326 (2D), которого ждёт схема
# БД (to_postgis иначе откажется писать: "CRS ... differs"). Снимаем Z
# и явно фиксируем 2D CRS.
wh["geometry"] = wh["geometry"].apply(shapely.force_2d)
wh = wh.set_crs(4326, allow_override=True)

# ВАЖНО: выбираем только нужные колонки ДО переименования - в исходном
# файле уже есть пустая колонка "name" (от экспорта из ГИС-редактора)
# отдельно от "назва_місцевості"; если переименовать вслепую, получится
# два столбца "name" и pandas откажется писать в него ("Cannot set a
# DataFrame with multiple columns to the single column").
wh = wh[["назва_місцевості", "geometry"]].copy()
wh = wh.rename(columns={"назва_місцевості": "name"})
wh["site_id"] = wh["name"]  # пока имя = id; замените на реальный TJ-XXXX, если он есть у заказчика
wh["pollution_type"] = "heavy_metals"
wh["status"] = "active"
wh["source"] = "склад-обследование (XRF)"
wh = wh.rename(columns={"geometry": "contour_geom"}).set_geometry("contour_geom")

with engine.begin() as conn:
    conn.execute(text("TRUNCATE measurements, samples, sites RESTART IDENTITY CASCADE"))

wh[["site_id", "name", "pollution_type", "contour_geom", "status", "source"]].to_postgis(
    "sites", engine, if_exists="append", index=False
)
print(f"sites: загружено {len(wh)} площадок")

# -------------------------------------------------------------- samples ---
pts = pd.read_csv(f"{SRC}/warehouse_points_enriched.csv")
pts_gdf = gpd.GeoDataFrame(
    pts, geometry=gpd.points_from_xy(pts.lon, pts.lat), crs="EPSG:4326"
).rename(columns={"geometry": "geom"}).set_geometry("geom")

pts_gdf["sample_date"] = pd.Timestamp.today().date()  # в реальных данных - берите дату замера, если она есть
pts_gdf["method"] = "XRF"
pts_gdf["duration_sec"] = pts_gdf.get("Duration", None)
pts_gdf["site_id"] = pts_gdf["site"]

samples_cols = ["site_id", "geom", "sample_date", "method", "duration_sec"]
pts_gdf[samples_cols].to_postgis("samples", engine, if_exists="append", index=False)
print(f"samples: загружено {len(pts_gdf)} точек")

# --------------------------------------------------------- measurements ---
with engine.connect() as conn:
    sample_ids = pd.read_sql("SELECT sample_id, site_id, ST_AsText(geom) AS wkt FROM samples", conn)

# сопоставляем обратно по порядку вставки (для демо-стенда этого достаточно;
# в проде - вставляйте через явные ключи/round_number, чтобы не полагаться на порядок)
pts_gdf = pts_gdf.reset_index(drop=True)
sample_ids = sample_ids.reset_index(drop=True)
pts_gdf["sample_id"] = sample_ids["sample_id"]

rows = []
for _, r in pts_gdf.iterrows():
    for m in METALS:
        val = r.get(m)
        rows.append({
            "sample_id": r["sample_id"],
            "substance": m,
            "value": None if pd.isna(val) else float(val),
            "unit": "mg/kg",
            "below_lod": bool(pd.isna(val)),
        })

meas_df = pd.DataFrame(rows)
meas_df.to_sql("measurements", engine, if_exists="append", index=False)
print(f"measurements: загружено {len(meas_df)} строк ({len(METALS)} веществ x {len(pts_gdf)} точек)")

print("\nГотово. Проверка:")
with engine.connect() as conn:
    for tbl in ["sites", "samples", "measurements"]:
        n = conn.execute(text(f"SELECT count(*) FROM {tbl}")).scalar()
        print(f"  {tbl}: {n} строк")
