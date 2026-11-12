"""
Уточнение КОНТУРА ЗАГРЯЗНЕНИЯ внутри площадки склада (п.3 ТЗ) - не
административная граница склада (contour_geom - она уже есть и QC'нута,
см. qc_geometry.py), а где именно внутри неё и буфера реально
сосредоточено загрязнение.

Основа - РЕАЛЬНЫЕ замеры в точках, не предсказания ML-модели. При LOWO
R²=0.157 модель даёт лишь грубый перенос сигнала на НОВЫЙ, не измеренный
склад - а здесь все точки на площадке УЖЕ измерены, у нас есть настоящие
данные, они надёжнее любого предсказания поверх них.

Тот же композитный индекс (Pb+As+Hg+Zn), что в rank_sites_priority.py, но
здесь нормирован ВНУТРИ склада (log-excess относительно медианы ЭТОГО
склада, не общестрановой) - вопрос тут "какая часть этой конкретной
площадки хуже других её частей", а не "какой склад хуже другого".

Зоны: точки делятся на 4 уровня (low/medium/high/critical - терминология
как в risk_zones.risk_level в schema.sql) по квартилям индекса ВНУТРИ
склада, буферизуются на ZONE_BUFFER_M и объединяются (dissolve) по
уровню, пересекаются с buf_100m (не даём зоне убежать за пределы
обследованной площади). Это НЕ интерполяция/кригинг - честная, но грубая
зона вокруг реально измеренных точек, не more того, что данные
позволяют утверждать.

Запуск: python db/build_risk_zones.py
"""
import os
import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.ops import unary_union
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.getenv("DATABASE_URL", "postgresql://soil_ml:soil_ml_dev_password@localhost:5432/soil_ml")
engine = create_engine(DB_URL)

TRUSTED_SUBSTANCES = ["Pb", "As", "Hg", "Zn"]
ZONE_BUFFER_M = 20  # радиус "зоны влияния" одной точки - порядок величины шага сетки проб
UTM = "EPSG:32642"  # метрические операции (буфер) - не в 4326, см. обоснование по гидрологии
RISK_LEVELS = ["low", "medium", "high", "critical"]  # по возрастанию, квартили 1..4


def main():
    with engine.connect() as conn:
        meas = pd.read_sql(text("""
            SELECT sam.sample_id, sam.site_id, m.substance, m.value
            FROM measurements m JOIN samples sam ON sam.sample_id = m.sample_id
            WHERE m.substance = ANY(:subs)
        """), conn, params={"subs": TRUSTED_SUBSTANCES})
        samples_geo = gpd.read_postgis(
            "SELECT sample_id, site_id, geom FROM samples", conn, geom_col="geom")
        buf100 = gpd.read_postgis(
            "SELECT site_id, buf_100m AS geom FROM site_buffers", conn, geom_col="geom")

    if meas.empty:
        raise SystemExit(f"Нет данных по веществам {TRUSTED_SUBSTANCES}.")

    parts = []
    for subst in TRUSTED_SUBSTANCES:
        sub = meas[meas.substance == subst].copy()
        if sub.empty:
            continue
        lod_fallback = sub.loc[sub.value.notna(), "value"].min() / 2 if sub.value.notna().any() else 0.01
        sub["value_for_model"] = sub["value"].fillna(lod_fallback)
        sub["log_value"] = np.log(sub["value_for_model"])
        parts.append(sub[["sample_id", "site_id", "log_value"]].assign(substance=subst))
    combined = pd.concat(parts, ignore_index=True)

    # нормировка ВНУТРИ склада - другая ось сравнения, чем в rank_sites_priority.py
    combined["site_median_log"] = combined.groupby(["site_id", "substance"])["log_value"].transform("median")
    combined["log_excess_within_site"] = combined["log_value"] - combined["site_median_log"]

    point_index = (combined.groupby(["sample_id", "site_id"])["log_excess_within_site"]
                   .mean().reset_index(name="composite_index"))

    # регистрируем "запуск" в model_versions для traceability - это не обученная
    # ML-модель, а описательная зональность по измеренным данным
    with engine.begin() as conn:
        mv_id = conn.execute(text("""
            INSERT INTO model_versions (target_substance, target_variant, n_sites, n_points,
                                         features_used, trigger_reason)
            VALUES (:subs, 'measured_composite_within_site_quartiles', :n_sites, :n_points,
                    :feats, 'ТЗ п.3 - уточнение контура загрязнения по факту замеров (не ML-предсказание)')
            RETURNING model_version_id
        """), {"subs": "+".join(TRUSTED_SUBSTANCES), "n_sites": point_index.site_id.nunique(),
               "n_points": len(point_index), "feats": TRUSTED_SUBSTANCES}).scalar()
    print(f"Зарегистрирован model_version_id={mv_id} (описательная зональность, не ML-модель)")

    merged = samples_geo.merge(point_index, on=["sample_id", "site_id"])
    merged_utm = merged.to_crs(UTM)
    buf100_utm = buf100.to_crs(UTM)

    zones_out = []
    print("\n" + "=" * 100)
    print("ЗОНЫ ПО СКЛАДАМ")
    print("=" * 100)
    for site_id, group in merged_utm.groupby("site_id"):
        if len(group) < 4:
            print(f"{site_id}: только {len(group)} точек - недостаточно для квартилей, пропускаю")
            continue
        # квартили ВНУТРИ склада: qcut может упасть на дубликатах значений - duplicates='drop' подстрахует
        try:
            tiers = pd.qcut(group["composite_index"], 4, labels=RISK_LEVELS, duplicates="drop")
        except ValueError:
            print(f"{site_id}: слишком много одинаковых значений индекса, зоны не построены")
            continue
        group = group.assign(risk_level=tiers)

        site_clip = buf100_utm.loc[buf100_utm.site_id == site_id, "geom"]
        clip_geom = site_clip.iloc[0] if len(site_clip) else None

        for level in RISK_LEVELS:
            level_pts = group[group["risk_level"] == level]
            if level_pts.empty:
                continue
            buffered = level_pts.geometry.buffer(ZONE_BUFFER_M)
            dissolved = unary_union(list(buffered))
            if clip_geom is not None:
                dissolved = dissolved.intersection(clip_geom)
            if dissolved.is_empty:
                continue
            zones_out.append({"site_id": site_id, "risk_level": str(level),
                               "n_points": len(level_pts), "geom": dissolved})
        print(f"{site_id}: {len(group)} точек -> "
              f"{group['risk_level'].value_counts().reindex(RISK_LEVELS).fillna(0).astype(int).to_dict()}")

    zones_gdf = gpd.GeoDataFrame(zones_out, geometry="geom", crs=UTM).to_crs(4326)

    # risk_zones.geom типизирована как MultiPolygon (см. schema.sql) - dissolve
    # несмежных буферов мог дать как Polygon, так и MultiPolygon, приводим единообразно
    from shapely.geometry import MultiPolygon

    with engine.begin() as conn:
        # старые версии зон НЕ удаляем - тот же принцип, что и у model_versions
        # ("при каждом пересчёте новая строка, старые не удаляются", см. schema.sql);
        # у каждой зоны есть свой model_version_id, так что путаницы не будет
        for _, row in zones_gdf.iterrows():
            geom = row.geom
            if geom.geom_type == "Polygon":
                geom = MultiPolygon([geom])
            conn.execute(text("""
                INSERT INTO risk_zones (model_version_id, site_id, risk_level, geom)
                VALUES (:mv, :site_id, :risk_level, ST_GeomFromText(:wkt, 4326))
            """), {"mv": mv_id, "site_id": row.site_id, "risk_level": row.risk_level, "wkt": geom.wkt})

    print(f"\nЗаписано {len(zones_gdf)} зон в risk_zones (model_version_id={mv_id}).")
    print("""
КАК ЧИТАТЬ: 'critical'/'high' зоны - где реально измеренные точки этого
склада хуже медианы ЭТОГО ЖЕ склада по Pb+As+Hg+Zn; это ПОЛОЖЕНИЕ ВНУТРИ
площадки, а не абсолютная опасность (склад с priority_rank=10 из
rank_sites_priority.py всё равно будет иметь свою 'critical' зону - это
его наихудшая часть, а не сигнал что он опасен в абсолютном смысле).
Смотреть оба скрипта вместе: rank_sites_priority.py - какой склад хуже
других, build_risk_zones.py - какая часть КОНКРЕТНОГО склада хуже.

Дальше эти зоны можно подгрузить слоем на карту (webapp) поверх
существующих model risk / raw-stats слоёв.
""")


if __name__ == "__main__":
    main()
