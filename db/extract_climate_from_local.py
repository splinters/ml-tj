r"""
Извлекает климатические ковариаты по каждому складу из ЛОКАЛЬНЫХ, уже
обрезанных по Таджикистану растров (см. clip_climate_rasters.py) - без
единого сетевого запроса. Заменяет сетевую часть fetch_climate_covariates.py
(историческая часть) И fetch_climate_projections.py (будущие проекции),
если обе папки-результата clip_climate_rasters.py доступны локально.

Ожидаемая структура (как создаёт clip_climate_rasters.py):
    <past_dir>/wc2.1_30s_bio/wc2.1_30s_bio_1.tif
    <past_dir>/wc2.1_30s_bio/wc2.1_30s_bio_12.tif
    <past_dir>/wc2.1_30s_prec/wc2.1_30s_prec_01.tif ... _12.tif
    <past_dir>/wc2.1_30s_tavg/wc2.1_30s_tavg_01.tif ... _12.tif
    <future_dir>/wc2.1_30s_bioc_IPSL-CM6A-LR_sspXXX_YYYY-YYYY/..._bio01.tif, ..._bio12.tif

Использование:
    python db/extract_climate_from_local.py /path/to/past_clipped [/path/to/future_clipped]

Второй аргумент (future) необязателен - если его нет, обрабатывается
только историческая часть.
"""
import os
import sys
import glob
import numpy as np
import pandas as pd
import rasterio
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.getenv("DATABASE_URL", "postgresql://soil_ml:soil_ml_dev_password@localhost:5432/soil_ml")
engine = create_engine(DB_URL)

PERIODS = ["2021-2040", "2041-2060", "2061-2080", "2081-2100"]
SCENARIOS = ["ssp245", "ssp585"]


def sample_at(tif_path, lat, lon):
    if not os.path.exists(tif_path):
        return None
    with rasterio.open(tif_path) as src:
        row, col = src.index(lon, lat)
        if not (0 <= row < src.height and 0 <= col < src.width):
            return None
        val = src.read(1)[row, col]
        nodata = src.nodata
        if nodata is not None and np.isclose(val, nodata):
            return None
        return float(val)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    climate_dir = sys.argv[1]
    future_dir = sys.argv[2] if len(sys.argv) > 2 else None

    with engine.connect() as conn:
        sites = pd.read_sql(
            "SELECT site_id, ST_Y(ST_Centroid(contour_geom)) AS lat, "
            "ST_X(ST_Centroid(contour_geom)) AS lon FROM sites", conn)

    # добавляем ВСЕ нужные колонки, если их ещё нет - не полагаемся на то,
    # что fetch_climate_covariates.py уже запускался раньше (temp_annual_c/
    # precip_annual_mm создавались там, но этот скрипт может быть первым,
    # если вы сразу перешли на локальные файлы - именно так и получилась
    # ошибка "column does not exist")
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE site_covariates ADD COLUMN IF NOT EXISTS temp_annual_c NUMERIC"))
        conn.execute(text("ALTER TABLE site_covariates ADD COLUMN IF NOT EXISTS precip_annual_mm NUMERIC"))
        for i in range(1, 13):
            conn.execute(text(f"ALTER TABLE site_covariates ADD COLUMN IF NOT EXISTS "
                               f"prec_month_{i:02d}_mm NUMERIC"))
            conn.execute(text(f"ALTER TABLE site_covariates ADD COLUMN IF NOT EXISTS "
                               f"tavg_month_{i:02d}_c NUMERIC"))
        if future_dir:
            for period in PERIODS:
                p = period.replace("-", "_")
                for scenario in SCENARIOS:
                    for suffix in ["temp_c", "precip_mm"]:
                        conn.execute(text(
                            f"ALTER TABLE site_covariates ADD COLUMN IF NOT EXISTS "
                            f"future_{scenario}_{p}_{suffix} NUMERIC"
                        ))

    bio_dir = os.path.join(climate_dir, "wc2.1_30s_bio")
    prec_dir = os.path.join(climate_dir, "wc2.1_30s_prec")
    tavg_dir = os.path.join(climate_dir, "wc2.1_30s_tavg")

    for _, row in sites.iterrows():
        print(f"\n{row.site_id}:")

        temp = sample_at(os.path.join(bio_dir, "wc2.1_30s_bio_1.tif"), row.lat, row.lon)
        precip = sample_at(os.path.join(bio_dir, "wc2.1_30s_bio_12.tif"), row.lat, row.lon)
        print(f"  среднегодовая темп.: {temp}, годовые осадки: {precip}")

        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO site_covariates (site_id, temp_annual_c, precip_annual_mm)
                VALUES (:sid, :t, :p)
                ON CONFLICT (site_id) DO UPDATE
                SET temp_annual_c = EXCLUDED.temp_annual_c,
                    precip_annual_mm = EXCLUDED.precip_annual_mm,
                    computed_at = now()
            """), {"sid": row.site_id, "t": temp, "p": precip})

        updates = {}
        for i in range(1, 13):
            precip_m = sample_at(os.path.join(prec_dir, f"wc2.1_30s_prec_{i:02d}.tif"), row.lat, row.lon)
            tavg_m = sample_at(os.path.join(tavg_dir, f"wc2.1_30s_tavg_{i:02d}.tif"), row.lat, row.lon)
            updates[f"prec_month_{i:02d}_mm"] = precip_m
            updates[f"tavg_month_{i:02d}_c"] = tavg_m

        set_clause = ", ".join(f"{col} = :{col}" for col in updates)
        with engine.begin() as conn:
            conn.execute(text(f"UPDATE site_covariates SET {set_clause} WHERE site_id = :sid"),
                         {**updates, "sid": row.site_id})
        print(f"  + 24 помесячных значения (осадки/температура)")

        if future_dir:
            future_updates = {}
            for scenario in SCENARIOS:
                for period in PERIODS:
                    p = period.replace("-", "_")
                    subdir = os.path.join(future_dir, f"wc2.1_30s_bioc_IPSL-CM6A-LR_{scenario}_{period}")
                    temp_f = sample_at(os.path.join(subdir, f"wc2.1_30s_bioc_IPSL-CM6A-LR_{scenario}_{period}_bio01.tif"),
                                        row.lat, row.lon)
                    precip_f = sample_at(os.path.join(subdir, f"wc2.1_30s_bioc_IPSL-CM6A-LR_{scenario}_{period}_bio12.tif"),
                                          row.lat, row.lon)
                    future_updates[f"future_{scenario}_{p}_temp_c"] = temp_f
                    future_updates[f"future_{scenario}_{p}_precip_mm"] = precip_f
            fset_clause = ", ".join(f"{col} = :{col}" for col in future_updates)
            with engine.begin() as conn:
                conn.execute(text(f"UPDATE site_covariates SET {fset_clause} WHERE site_id = :sid"),
                             {**future_updates, "sid": row.site_id})
            print(f"  + {len(future_updates)} значений будущих проекций (2 сценария x 4 периода x 2 переменные)")

    print("\nГотово.")


if __name__ == "__main__":
    main()
