"""
Достаёт гидрологические признаки (flow accumulation / flow direction) по
каждой точке отбора и по каждому складу из растров, посчитанных вручную в
ArcGIS Pro (DEM 150м, UTM 42N -> Fill -> Flow Direction (D8) -> Flow
Accumulation -> Project Raster обратно в EPSG:4326), см. переписку по этому
шагу. Файлы кладутся в <project_root>/source_data/flow/ (не внутри db/,
в отличие от geology/ph - там их положил сам пользователь после ArcGIS).

ВАЖНО про "промах мимо русла" (pour point snapping): flow accumulation -
крайне разрывный признак. У ячейки прямо на смоделированном русле может
быть значение в тысячи (много вышестоящих ячеек стекает через неё), а у
соседней ячейки в 150м - единицы, просто потому что она на склоне, а не
в русле. Реальная точка отбора почти никогда не попадает ровно на
пиксель русла. Поэтому тянем ДВА признака на точку:
  - flow_accumulation      - сырое значение в ровно той ячейке, где точка
  - flow_accumulation_max  - максимум в окне NxN вокруг точки (по
                              умолчанию радиус 2 ячейки ~300м), т.е.
                              "насколько близко есть выраженное русло"
Так же добавляем flow_direction (код D8: 1,2,4,8,16,32,64,128) - сырое
значение в точке, без оконного анализа (направление стока не имеет того
же "промаха мимо", что аккумуляция).

Признаки point-level (на каждую sample, не на склад) - по опыту проекта
(см. METHODOLOGY.md) point-level признаки стабильно превосходят
site-level на LOWO-CV, а site-level рискуют выродиться в замаскированный
site_id. Дополнительно кладём то же самое по центроиду склада в
site_covariates - для параллели с другими site-level ковариатами и для
будущих тестов "глобальной" модели (scan_absolute_vs_relative.py).

Единицы: сырое количество ячеек-водосбора (не км²) - для ML это не
важно (монотонное преобразование), а перевод в км² при интерпретации:
cells * 150м * 150м / 1e6. Распределение сильно скошено (пример по
центроидам складов: от 0 до ~3000) - при желании можно логарифмировать
на этапе построения модели, как это уже делается с target'ом в
run_model_and_store.py.

Запуск: python db/fetch_flow_covariate.py [--window-radius 2]
"""
import os
import sys
import glob
import argparse
import numpy as np
import pandas as pd
import geopandas as gpd
import rasterio
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.getenv("DATABASE_URL", "postgresql://soil_ml:soil_ml_dev_password@localhost:5432/soil_ml")
engine = create_engine(DB_URL)

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)

# гибкий поиск по имени файла - не гадаем точное имя вслепую, ищем по
# ключевым словам в нескольких вероятных местах (source_data/flow/,
# source_data/, db/, db/source_data/)
SEARCH_DIRS = [
    os.path.join(PROJECT_ROOT, "source_data", "flow"),
    os.path.join(PROJECT_ROOT, "source_data"),
    os.path.join(HERE, "source_data", "flow"),
    os.path.join(HERE, "source_data"),
    HERE,
]


def find_raster(keywords):
    """Ищет .tif файл, чьё имя (в нижнем регистре) содержит все keywords."""
    seen = set()
    for d in SEARCH_DIRS:
        if not os.path.isdir(d) or d in seen:
            continue
        seen.add(d)
        for path in glob.glob(os.path.join(d, "*.tif")) + glob.glob(os.path.join(d, "*.tiff")):
            base = os.path.basename(path).lower()
            if all(kw in base for kw in keywords):
                return path
    return None


def extract_point_and_window(arr, nodata, transform, lon, lat, window_radius_cells):
    """Возвращает (сырое_значение, максимум_в_окне) для точки (lon, lat).
    None, если точка вне растра или пиксель = nodata."""
    col, row = ~transform * (lon, lat)
    col, row = int(col), int(row)
    if not (0 <= row < arr.shape[0] and 0 <= col < arr.shape[1]):
        return None, None
    raw = arr[row, col]
    raw = None if (nodata is not None and raw == nodata) else float(raw)

    r0, r1 = max(0, row - window_radius_cells), min(arr.shape[0], row + window_radius_cells + 1)
    c0, c1 = max(0, col - window_radius_cells), min(arr.shape[1], col + window_radius_cells + 1)
    win = arr[r0:r1, c0:c1]
    if nodata is not None:
        win = win[win != nodata]
    wmax = float(win.max()) if win.size else None
    return raw, wmax


def extract_direction(arr, nodata, transform, lon, lat):
    col, row = ~transform * (lon, lat)
    col, row = int(col), int(row)
    if not (0 <= row < arr.shape[0] and 0 <= col < arr.shape[1]):
        return None
    val = arr[row, col]
    if nodata is not None and val == nodata:
        return None
    return int(val)


def ensure_columns():
    with engine.begin() as conn:
        conn.execute(text("""
            ALTER TABLE samples
                ADD COLUMN IF NOT EXISTS flow_accumulation NUMERIC,
                ADD COLUMN IF NOT EXISTS flow_accumulation_max NUMERIC,
                ADD COLUMN IF NOT EXISTS flow_direction INT
        """))
        conn.execute(text("""
            ALTER TABLE site_covariates
                ADD COLUMN IF NOT EXISTS flow_accumulation NUMERIC,
                ADD COLUMN IF NOT EXISTS flow_accumulation_max NUMERIC,
                ADD COLUMN IF NOT EXISTS flow_direction INT
        """))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-radius", type=int, default=2,
                     help="радиус окна в ячейках для flow_accumulation_max (по умолчанию 2 ~ 300м при 150м ячейке)")
    args = ap.parse_args()

    acc_path = find_raster(["flowacc"]) or find_raster(["flow", "acc"])
    dir_path = find_raster(["flowdir"]) or find_raster(["flow", "dir"])

    if not acc_path:
        raise SystemExit(
            f"Не нашёл растр flow accumulation (искал *flowacc*.tif / *flow*acc*.tif) в:\n  "
            + "\n  ".join(SEARCH_DIRS) +
            "\nПоложите FlowAcc*.tif в source_data/flow/ и запустите снова."
        )
    print(f"Flow accumulation: {acc_path}")
    if dir_path:
        print(f"Flow direction:    {dir_path}")
    else:
        print("Flow direction не найден - пропускаю (это не критично, accumulation важнее).")

    print("Обновляю схему (ALTER TABLE ... ADD COLUMN IF NOT EXISTS)...")
    ensure_columns()

    with engine.connect() as conn:
        sites = pd.read_sql(
            "SELECT site_id, ST_Y(ST_Centroid(contour_geom)) AS lat, "
            "ST_X(ST_Centroid(contour_geom)) AS lon FROM sites", conn)
        samples = gpd.read_postgis(
            "SELECT sample_id, site_id, geom, ST_Y(geom) AS lat, ST_X(geom) AS lon FROM samples",
            conn, geom_col="geom")

    with rasterio.open(acc_path) as src:
        acc_arr = src.read(1)
        acc_nodata = src.nodata
        acc_transform = src.transform
        print(f"  acc растр: {src.width}x{src.height}, разрешение ~{abs(src.transform.a):.5f}°/пиксель, "
              f"nodata={acc_nodata}")

    dir_arr = dir_nodata = dir_transform = None
    if dir_path:
        with rasterio.open(dir_path) as src:
            dir_arr = src.read(1)
            dir_nodata = src.nodata
            dir_transform = src.transform

    # --- по точкам отбора (point-level) ---
    print(f"\nТочки отбора: {len(samples)}, окно для max = {args.window_radius*2+1}x{args.window_radius*2+1} ячеек")
    rows_out = []
    with engine.begin() as conn:
        for _, row in samples.iterrows():
            raw, wmax = extract_point_and_window(
                acc_arr, acc_nodata, acc_transform, row.lon, row.lat, args.window_radius)
            fdir = None
            if dir_arr is not None:
                fdir = extract_direction(dir_arr, dir_nodata, dir_transform, row.lon, row.lat)

            conn.execute(text("""
                UPDATE samples
                SET flow_accumulation = :acc, flow_accumulation_max = :accmax, flow_direction = :fdir
                WHERE sample_id = :sid
            """), {"acc": raw, "accmax": wmax, "fdir": fdir, "sid": int(row.sample_id)})
            rows_out.append((row.site_id, raw, wmax, fdir))

    df = pd.DataFrame(rows_out, columns=["site_id", "flow_accumulation", "flow_accumulation_max", "flow_direction"])
    print("\nПо складам (среднее/медиана по точкам):")
    print(df.groupby("site_id")[["flow_accumulation", "flow_accumulation_max"]].agg(["mean", "median"]).round(1))

    # --- по центроиду склада (site-level, для параллели с другими ковариатами) ---
    print("\nЦентроиды складов:")
    with engine.begin() as conn:
        for _, row in sites.iterrows():
            raw, wmax = extract_point_and_window(
                acc_arr, acc_nodata, acc_transform, row.lon, row.lat, args.window_radius)
            fdir = None
            if dir_arr is not None:
                fdir = extract_direction(dir_arr, dir_nodata, dir_transform, row.lon, row.lat)

            print(f"  {row.site_id:<20s} raw={raw!s:>12s}  max={wmax!s:>12s}  dir={fdir!s:>5s}")

            conn.execute(text("""
                INSERT INTO site_covariates (site_id, flow_accumulation, flow_accumulation_max, flow_direction)
                VALUES (:site_id, :acc, :accmax, :fdir)
                ON CONFLICT (site_id) DO UPDATE SET
                    flow_accumulation = EXCLUDED.flow_accumulation,
                    flow_accumulation_max = EXCLUDED.flow_accumulation_max,
                    flow_direction = EXCLUDED.flow_direction,
                    computed_at = now()
            """), {"site_id": row.site_id, "acc": raw, "accmax": wmax, "fdir": fdir})

    print("\nГотово. Проверка (samples):")
    with engine.connect() as conn:
        print(pd.read_sql(
            "SELECT count(*) AS n, "
            "avg(flow_accumulation) AS avg_acc, avg(flow_accumulation_max) AS avg_acc_max "
            "FROM samples WHERE flow_accumulation IS NOT NULL", conn))
        print(pd.read_sql(
            "SELECT site_id, flow_accumulation, flow_accumulation_max, flow_direction "
            "FROM site_covariates ORDER BY site_id", conn))

    print("\nСледующий шаг: протестировать признак(и) через db/diagnose_features.py "
          "(добавить 'flow_accumulation'/'flow_accumulation_max' в список кандидатов) "
          "ПЕРЕД тем как включать в run_model_and_store.py - по методологии проекта "
          "каждый новый признак сначала проверяется отдельно на LOWO-CV.")


if __name__ == "__main__":
    main()
