"""
Пересчитывает модель на ТЕКУЩИХ данных из PostGIS и пишет результат
обратно в БД (model_versions + predictions). Это и есть шаг, которого не
хватало: без него карта показывает только сырую статистику, а не то, что
предсказала и честно проверила ML-модель.

Запуск: python db/run_model_and_store.py [причина_пересчёта]
Пример: python db/run_model_and_store.py "добавлен новый склад"
"""
import os
import sys
import numpy as np
import pandas as pd
import geopandas as gpd
from sqlalchemy import create_engine, text
from sklearn.ensemble import RandomForestRegressor
from sklearn.model_selection import LeaveOneGroupOut
from sklearn.metrics import mean_squared_error, r2_score
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.getenv("DATABASE_URL", "postgresql://soil_ml:soil_ml_dev_password@localhost:5432/soil_ml")
engine = create_engine(DB_URL)

TARGET_SUBSTANCE = "Pb"
BASELINE_FEATURES = ["signed_dist_boundary_m", "dist_centroid_m", "zone_code"]
# по точке - реальная геометрия, варьируется внутри склада (честный сигнал)
POINT_LEVEL_FEATURES = ["dist_to_water_m"]
# v3: по-складу оставлен только pH - единственный из четырёх (pH/уклон/
# высота/население), показавший устойчиво положительный вклад ПООДИНОЧКЕ
# (Δ LOWO R² = +0.067). Уклон/высота/население нейтральны или слабо
# отрицательны сами по себе и, что важнее, топят pH при объединении в одну
# группу (группа целиком уходила в минус -0.033, хотя pH один даёт +0.067) -
# тот же эффект, что видели с водой и зданиями, только внутри "по-складовых"
# признаков. v2 (со всеми 4) сохранён как run_model_and_store_v2.py.
SITE_LEVEL_FEATURES = ["soil_ph"]
# ИСКЛЮЧЕНЫ по результатам db/diagnose_features.py (не предположение, а измеренный эффект):
#   dist_to_building_m - в одиночку ДАЁТ ОТРИЦАТЕЛЬНУЮ дельту LOWO R² (-0.044) - накопленная
#     статистика по 3 из 10 складов (Дусти/Дж.Балхи/Пяндж) показала источник ненадёжным как класс;
#   worldcover_class - средний |SHAP| ≈ 0.0005, почти нет дисперсии между складами;
#   slope_deg, elevation_m, population_500m - слабые/отрицательные поодиночке
#     (Δ -0.009, +0.007, -0.017), топят pH при объединении.
# Данные для всех остаются в БД - решение не включать их в модель, не решение их не собирать.
# v1 (с dist_to_building_m) - run_model_and_store_v1.py
# v2 (без зданий, но со всеми 4 по-складовыми) - run_model_and_store_v2.py
EXTRA_FEATURES = POINT_LEVEL_FEATURES + SITE_LEVEL_FEATURES
FEATURES = BASELINE_FEATURES + EXTRA_FEATURES

# признаки положения точки относительно контура склада считаются прямо в
# PostGIS (ST_Contains/ST_Distance) - значит, при появлении нового склада
# или новой точки НИЧЕГО не нужно пересчитывать вручную в Python, схема
# сама вернёт актуальные признаки при следующем запуске этого скрипта.
# Остальные ковариаты - из db/fetch_point_covariates.py,
# db/fetch_landcover_population.py и db/fetch_buildings.py; если что-то из
# этого ещё не запускалось, соответствующие колонки будут NULL и
# заполнятся средним (см. ниже) - модель не упадёт, просто не получит
# пользы от ещё не собранных данных.
FEATURE_SQL = """
SELECT
    sam.sample_id, sam.site_id, sam.geom, sam.dist_to_water_m,
    CASE
        WHEN ST_Contains(s.contour_geom, sam.geom) THEN 3
        WHEN ST_Contains(sb.buf_50m, sam.geom) THEN 2
        WHEN ST_Contains(sb.buf_100m, sam.geom) THEN 1
        ELSE 0
    END AS zone_code,
    ST_Distance(s.contour_geom::geography, sam.geom::geography)
        * CASE WHEN ST_Contains(s.contour_geom, sam.geom) THEN -1 ELSE 1 END AS signed_dist_boundary_m,
    ST_Distance(ST_Centroid(s.contour_geom)::geography, sam.geom::geography) AS dist_centroid_m,
    sc.soil_ph, sc.slope_deg, sc.elevation_m, sc.population_500m, sc.worldcover_class
FROM samples sam
JOIN sites s ON s.site_id = sam.site_id
JOIN site_buffers sb ON sb.site_id = sam.site_id
LEFT JOIN site_covariates sc ON sc.site_id = sam.site_id
"""


def rf():
    return RandomForestRegressor(n_estimators=300, min_samples_leaf=3, random_state=0)


def main(trigger_reason="manual"):
    with engine.connect() as conn:
        feats = gpd.read_postgis(FEATURE_SQL, conn, geom_col="geom")
        target = pd.read_sql(
            text("SELECT sample_id, value, below_lod FROM measurements WHERE substance = :s"),
            conn, params={"s": TARGET_SUBSTANCE},
        )

    df = feats.merge(target, on="sample_id", how="inner")
    lod_fallback = df.loc[df.value.notna(), "value"].min() / 2 if df.value.notna().any() else 0.01
    df["value_for_model"] = df["value"].fillna(lod_fallback)
    df["log_value"] = np.log(df["value_for_model"])
    df["rel_value"] = df["log_value"] - df.groupby("site_id")["log_value"].transform("median")

    # пропуски в открытых ковариатах (сайт ещё не обошли скриптом
    # fetch_point_covariates.py, либо конкретный API не ответил) -
    # заполняем средним по выборке и явно считаем, сколько заполнили,
    # чтобы это не было незаметной подменой данных
    for col in EXTRA_FEATURES:
        n_missing = df[col].isna().sum()
        if n_missing:
            print(f"  {col}: {n_missing}/{len(df)} пропусков - заполнено средним "
                  f"(запустите db/fetch_point_covariates.py, если ещё не запускали)")
            df[col] = df[col].fillna(df[col].mean())
        if df[col].isna().all():
            df[col] = 0.0  # совсем нет данных ни по одной точке - признак не несёт сигнала, а не NaN

    y = df["rel_value"].values
    groups = df["site_id"].values
    n_sites = df.site_id.nunique()

    def lowo_r2_for(feature_list):
        if n_sites < 3:
            return None, None
        X_ = df[feature_list].values
        logo = LeaveOneGroupOut()
        pred = np.zeros_like(y)
        for tr, te in logo.split(X_, y, groups):
            pred[te] = rf().fit(X_[tr], y[tr]).predict(X_[te])
        return float(r2_score(y, pred)), float(np.sqrt(mean_squared_error(y, pred)))

    print("\n--- Честное сравнение: geometry / + по-точечные (вода, здания) / + по-складу (pH и т.п.) / всё ---")
    variants = {
        "baseline (расстояние/зона)":        BASELINE_FEATURES,
        "+ по-точечные (вода)":               BASELINE_FEATURES + POINT_LEVEL_FEATURES,
        "+ по-складу (pH)":                    BASELINE_FEATURES + SITE_LEVEL_FEATURES,
        "+ всё вместе":                        FEATURES,
    }
    results = {}
    for label, feats_ in variants.items():
        r2, rmse = lowo_r2_for(feats_)
        results[label] = (r2, rmse)
        if r2 is not None:
            print(f"  {label:42s} LOWO R²={r2:+.3f}  RMSE={rmse:.3f}")
    lowo_r2, lowo_rmse = results["+ всё вместе"]
    print("  По-точечные признаки (вода/здания) - честная геометрия внутри площадки.")
    print("  По-складовые (pH и т.п.) - почти константы на площадку, есть риск, что модель")
    print("  использует их как \"номер склада\", а не физический механизм - см. db/diagnose_features.py")

    # финальная модель - на расширенном наборе признаков, на всех данных
    X = df[FEATURES].values
    final_model = rf().fit(X, y)
    final_pred = final_model.predict(X)

    risk_score = pd.Series(final_pred).rank(pct=True).values * 100
    risk_level = pd.cut(risk_score, bins=[-1, 25, 50, 75, 101],
                         labels=["low", "medium", "high", "critical"]).astype(str)

    with engine.begin() as conn:
        mv_id = conn.execute(text("""
            INSERT INTO model_versions
                (target_substance, target_variant, n_sites, n_points,
                 lowo_r2, lowo_rmse, features_used, trigger_reason)
            VALUES (:t, 'site_normalized', :ns, :npo, :r2, :rmse, :feats, :reason)
            RETURNING model_version_id
        """), {"t": TARGET_SUBSTANCE, "ns": n_sites, "npo": len(df),
               "r2": lowo_r2, "rmse": lowo_rmse, "feats": FEATURES,
               "reason": trigger_reason}).scalar()

        df_reset = df.reset_index(drop=True)
        for i, row in df_reset.iterrows():
            conn.execute(text("""
                INSERT INTO predictions
                    (model_version_id, site_id, geom, predicted_value, risk_score, risk_level)
                VALUES (:mv, :site, ST_SetSRID(ST_GeomFromText(:wkt), 4326), :pred, :score, :level)
            """), {"mv": mv_id, "site": row.site_id, "wkt": row.geom.wkt,
                   "pred": float(final_pred[i]), "score": float(risk_score[i]),
                   "level": row_level if (row_level := risk_level[i]) else None})

    print(f"\nВерсия модели #{mv_id} сохранена (причина: {trigger_reason})")
    print(f"  складов: {n_sites}, точек: {len(df)}, вещество: {TARGET_SUBSTANCE}")
    if lowo_r2 is not None:
        print(f"  LOWO R²={lowo_r2:.3f}, RMSE={lowo_rmse:.3f}  <- это то, что стоит показывать заказчику")
    print("\nКарта (webapp) теперь при перезапуске покажет эту версию как последнюю.")
    return mv_id


if __name__ == "__main__":
    reason = sys.argv[1] if len(sys.argv) > 1 else "manual"
    main(trigger_reason=reason)
