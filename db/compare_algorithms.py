"""
Сравнивает Random Forest (текущий алгоритм в run_model_and_store.py) с
градиентным бустингом (sklearn HistGradientBoostingRegressor) на ТОЙ ЖЕ
LOWO-схеме и на ТЕХ ЖЕ признаках, что уже прошли честную проверку
(baseline + вода + pH, см. diagnose_features.py). HistGradientBoosting -
часть самого sklearn, не отдельная библиотека (важно с учётом требования
"без сторонних сервисов/офлайн" - новая зависимость не добавляется).

Вопрос здесь не "какой признак включить", а "тот ли алгоритм используется".
При 169 точках и LOWO-фолдах по ~13-20 точек RF (усреднение по деревьям,
голосование) обычно устойчивее бустинга (последовательная подгонка под
остатки предыдущих деревьев - легче переобучается на малой выборке), но
это стоит проверить честно тем же протоколом, а не полагаться на общее
правило.

Гиперпараметры HGB намеренно консервативные (mало итераций, регуляризация,
min_samples_leaf побольше) - агрессивный бустинг на 150 обучающих точках
почти гарантированно переобучится; цель теста - "лучший разумный бустинг
против уже проверенного RF", не бустинг с дефолтными настройками.

Запуск: python db/compare_algorithms.py [Pb|As|Hg|...]  (по умолчанию Pb)
"""
import os
import sys
import numpy as np
import pandas as pd
import geopandas as gpd
from sklearn.ensemble import RandomForestRegressor, HistGradientBoostingRegressor
from sklearn.model_selection import LeaveOneGroupOut
from sklearn.metrics import mean_squared_error, r2_score
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.getenv("DATABASE_URL", "postgresql://soil_ml:soil_ml_dev_password@localhost:5432/soil_ml")
engine = create_engine(DB_URL)

TARGET_SUBSTANCE = sys.argv[1] if len(sys.argv) > 1 else "Pb"
BASELINE_FEATURES = ["signed_dist_boundary_m", "dist_centroid_m", "zone_code"]

# та же логика признаков/запроса, что в diagnose_features.py и
# run_model_and_store.py - продублирована намеренно, чтобы этот скрипт
# можно было читать и гонять независимо
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
    sc.soil_ph
FROM samples sam
JOIN sites s ON s.site_id = sam.site_id
JOIN site_buffers sb ON sb.site_id = sam.site_id
LEFT JOIN site_covariates sc ON sc.site_id = sam.site_id
"""


def rf():
    return RandomForestRegressor(n_estimators=300, min_samples_leaf=3, random_state=0)


def hgb():
    # консервативные настройки - см. docstring выше
    return HistGradientBoostingRegressor(
        max_iter=100, learning_rate=0.05, max_leaf_nodes=15,
        min_samples_leaf=10, l2_regularization=1.0, random_state=0,
    )


MODELS = {"Random Forest (текущий)": rf, "Gradient Boosting (HistGB, sklearn)": hgb}

print(f"\n{'='*100}\nВЕЩЕСТВО: {TARGET_SUBSTANCE} - СРАВНЕНИЕ АЛГОРИТМОВ (LOWO)\n{'='*100}")

with engine.connect() as conn:
    feats = gpd.read_postgis(FEATURE_SQL, conn, geom_col="geom")
    target = pd.read_sql(
        text("SELECT sample_id, value, below_lod FROM measurements WHERE substance = :s"),
        conn, params={"s": TARGET_SUBSTANCE},
    )

df = feats.merge(target, on="sample_id", how="inner")
if df.empty:
    raise SystemExit(f"Нет данных по веществу '{TARGET_SUBSTANCE}'.")

lod_pct = df["below_lod"].mean() * 100
print(f"Точек: {len(df)}, ниже предела обнаружения: {lod_pct:.0f}%")

lod_fallback = df.loc[df.value.notna(), "value"].min() / 2 if df.value.notna().any() else 0.01
df["value_for_model"] = df["value"].fillna(lod_fallback)
df["log_value"] = np.log(df["value_for_model"])
df["rel_value"] = df["log_value"] - df.groupby("site_id")["log_value"].transform("median")
df["soil_ph"] = df["soil_ph"].fillna(df["soil_ph"].mean())

y = df["rel_value"].values
groups = df["site_id"].values


def lowo(feature_list, model_fn):
    X_ = df[feature_list].values
    logo = LeaveOneGroupOut()
    pred = np.zeros_like(y)
    for tr, te in logo.split(X_, y, groups):
        pred[te] = model_fn().fit(X_[tr], y[tr]).predict(X_[te])
    return r2_score(y, pred), np.sqrt(mean_squared_error(y, pred))


VARIANTS = {
    "baseline (расстояние/зона)": BASELINE_FEATURES,
    "+ вода + pH (текущая продакшн-модель)": BASELINE_FEATURES + ["dist_to_water_m", "soil_ph"],
}

print("=" * 100)
print(f"{'Вариант признаков':60s}  {'Алгоритм':38s}  R²        RMSE")
print("=" * 100)
results = {}
for v_label, v_feats in VARIANTS.items():
    for m_label, m_fn in MODELS.items():
        r2, rmse = lowo(v_feats, m_fn)
        results[(v_label, m_label)] = r2
        print(f"{v_label:60s}  {m_label:38s}  {r2:+.3f}   {rmse:.3f}")
    print("-" * 100)

prod_rf = results[("+ вода + pH (текущая продакшн-модель)", "Random Forest (текущий)")]
prod_hgb = results[("+ вода + pH (текущая продакшн-модель)", "Gradient Boosting (HistGB, sklearn)")]
print(f"""
ИТОГ (продакшн-набор признаков вода+pH):
  Random Forest:      R²={prod_rf:+.3f}
  Gradient Boosting:   R²={prod_hgb:+.3f}
  Разница:             {prod_hgb - prod_rf:+.3f}

КАК ЧИТАТЬ:
- если разница в пределах ~0.02-0.03 - это шум перестановки, не считайте
  это доказательством превосходства одного алгоритма над другим на такой
  маленькой выборке (169 точек, LOWO-фолды по 13-20);
- если Gradient Boosting заметно ХУЖЕ - ожидаемо для малой выборки,
  RF остаётся в продакшне без изменений;
- если Gradient Boosting заметно ЛУЧШЕ - стоит перепроверить на нескольких
  random_state (сейчас только 0), прежде чем менять продакшн-алгоритм -
  один прогон не повод менять то, что уже проверено на 0.157.
""")
