"""
Экспортирует текущее состояние проекта из живой БД в один файл GeoPackage
(.gpkg) - открытый, непроприетарный формат (OGC), читается в QGIS,
ArcGIS Pro, любой библиотеке на geopandas/fiona без дополнительных плагинов.

Запускать НА МАШИНЕ, где реально работает PostGIS со всеми накопленными
данными - только тут есть актуальные значения всех ковариат (pH, климат,
геология, дороги и т.д.), которые собирались по ходу проекта.

Слои в итоговом файле:
    sites            - 10 складов, контуры
    site_buffers     - буферы 50м/100м (материализованы из VIEW для GeoPackage -
                        сам GeoPackage не поддерживает VIEW как отдельный слой)
    samples          - 169 точек замеров + поточечные ковариаты (вода/здания/дороги)
    measurements_wide - те же точки, но замеры развёрнуты по столбцам (Pb, As, Hg...)
                        для удобства чтения человеком - длинный формат остаётся
                        источником истины в самой БД
    site_covariates  - все собранные ковариаты по складу (pH, климат, геология...)
    risk_zones       - зоны концентрации загрязнения внутри площадок (low/medium/
                        high/critical), см. db/build_risk_zones.py; sites несёт
                        priority_rank/priority_composite_index (ранжирование
                        складов между собой, см. db/rank_sites_priority.py)

Запуск: python db/export_geopackage.py [путь_к_файлу.gpkg]
"""
import os
import sys
import pandas as pd
import geopandas as gpd
from sqlalchemy import create_engine
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.getenv("DATABASE_URL", "postgresql://soil_ml:soil_ml_dev_password@localhost:5432/soil_ml")
engine = create_engine(DB_URL)

OUT_PATH = sys.argv[1] if len(sys.argv) > 1 else "tajikistan_soil_ml_export.gpkg"


def main():
    if os.path.exists(OUT_PATH):
        os.remove(OUT_PATH)  # GeoPackage не любит дописывать поверх старого при повторном экспорте слоёв

    print(f"Экспортирую в {OUT_PATH}\n")

    # --- sites ---
    sites = gpd.read_postgis("SELECT * FROM sites", engine, geom_col="contour_geom")
    sites.to_file(OUT_PATH, layer="sites", driver="GPKG")
    print(f"  sites: {len(sites)} записей")

    # --- site_buffers (материализуем из VIEW - GeoPackage не хранит VIEW) ---
    buffers_50 = gpd.read_postgis("SELECT site_id, buf_50m AS geometry, '50m' AS radius "
                                   "FROM site_buffers", engine, geom_col="geometry")
    buffers_100 = gpd.read_postgis("SELECT site_id, buf_100m AS geometry, '100m' AS radius "
                                    "FROM site_buffers", engine, geom_col="geometry")
    buffers = pd.concat([buffers_50, buffers_100], ignore_index=True)
    buffers = gpd.GeoDataFrame(buffers, geometry="geometry", crs=buffers_50.crs)
    buffers.to_file(OUT_PATH, layer="site_buffers", driver="GPKG")
    print(f"  site_buffers: {len(buffers)} записей (материализовано из VIEW)")

    # --- samples (точки + поточечные ковариаты) ---
    samples = gpd.read_postgis("SELECT * FROM samples", engine, geom_col="geom")
    samples.to_file(OUT_PATH, layer="samples", driver="GPKG")
    print(f"  samples: {len(samples)} записей")

    # --- measurements, развёрнутые по столбцам для читаемости ---
    with engine.connect() as conn:
        long_df = pd.read_sql("SELECT sample_id, substance, value, below_lod FROM measurements", conn)
    wide_df = long_df.pivot_table(index="sample_id", columns="substance", values="value", aggfunc="first")
    wide_df.columns = [f"{c}_mgkg" for c in wide_df.columns]
    wide_df = wide_df.reset_index()

    measurements_wide = samples.merge(wide_df, on="sample_id", how="left")
    measurements_wide.to_file(OUT_PATH, layer="measurements_wide", driver="GPKG")
    print(f"  measurements_wide: {len(measurements_wide)} записей, "
          f"{len(wide_df.columns) - 1} веществ развёрнуто по столбцам")

    # --- site_covariates (не геопространственный слой сам по себе - обычная таблица,
    # но GeoPackage умеет хранить и не-геометрические таблицы тоже) ---
    with engine.connect() as conn:
        covariates = pd.read_sql("SELECT * FROM site_covariates", conn)
    # приклеиваем геометрию центроида, чтобы слой можно было открыть на карте,
    # а не только как таблицу атрибутов
    centroids = sites[["site_id", "contour_geom"]].copy()
    centroids["geometry"] = centroids.contour_geom.centroid
    centroids = centroids.drop(columns=["contour_geom"])
    covariates_geo = centroids.merge(covariates, on="site_id", how="left")
    covariates_geo = gpd.GeoDataFrame(covariates_geo, geometry="geometry", crs=sites.crs)
    covariates_geo.to_file(OUT_PATH, layer="site_covariates", driver="GPKG")
    print(f"  site_covariates: {len(covariates_geo)} записей, {len(covariates.columns)} полей "
          f"(включая климат/геологию/pH - если соответствующие fetch-скрипты уже запускались)")

    # --- risk_zones (зоны концентрации загрязнения внутри площадок, если
    # build_risk_zones.py уже запускался; экспортируем самую свежую версию
    # по каждому складу/уровню - model_versions не удаляются, отбираем max) ---
    zones = gpd.read_postgis("""
        SELECT rz.zone_id, rz.site_id, rz.risk_level, rz.geom,
               mv.trained_at AS zones_built_at
        FROM risk_zones rz
        JOIN model_versions mv ON mv.model_version_id = rz.model_version_id
        WHERE rz.model_version_id = (
            SELECT model_version_id FROM model_versions
            WHERE target_variant = 'measured_composite_within_site_quartiles'
            ORDER BY trained_at DESC LIMIT 1
        )
    """, engine, geom_col="geom")
    if len(zones):
        zones.to_file(OUT_PATH, layer="risk_zones", driver="GPKG")
        print(f"  risk_zones: {len(zones)} записей (последний прогон build_risk_zones.py)")
    else:
        print("  risk_zones: пусто - запустите db/build_risk_zones.py перед экспортом, если нужен этот слой")

    size_mb = os.path.getsize(OUT_PATH) / 1e6
    print(f"\nГотово: {OUT_PATH} ({size_mb:.1f} МБ)")


if __name__ == "__main__":
    main()
