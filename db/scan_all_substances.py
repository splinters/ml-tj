"""
Прогоняет ЧЕСТНУЮ LOWO-CV методологию (baseline / +вода / +pH / +оба) по
каждому из 9 измеренных веществ, не только по Pb - чтобы ответить на вопрос
"есть ли смысл в комплексном анализе" фактами, а не предположением.

Запуск: python db/scan_all_substances.py
"""
import os
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

SUBSTANCES = ["Mo", "U", "Rb", "Th", "Pb", "As", "Hg", "Zn", "Co"]
BASELINE_FEATURES = ["signed_dist_boundary_m", "dist_centroid_m", "zone_code"]

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


def lowo_r2(df, y, feats):
    X_ = df[feats].values
    groups = df["site_id"].values
    logo = LeaveOneGroupOut()
    pred = np.zeros_like(y)
    for tr, te in logo.split(X_, y, groups):
        pred[te] = rf().fit(X_[tr], y[tr]).predict(X_[te])
    return r2_score(y, pred), np.sqrt(mean_squared_error(y, pred))


with engine.connect() as conn:
    feats = gpd.read_postgis(FEATURE_SQL, conn, geom_col="geom")
    feats["soil_ph"] = feats["soil_ph"].fillna(feats["soil_ph"].mean())

results = []
print("=" * 90)
print(f"{'Вещество':10s}{'ниже LOD':>10s}{'baseline':>12s}{'+ вода':>12s}{'+ pH':>12s}{'+ вода+pH':>12s}")
print("=" * 90)

for substance in SUBSTANCES:
    with engine.connect() as conn:
        target = pd.read_sql(
            text("SELECT sample_id, value, below_lod FROM measurements WHERE substance = :s"),
            conn, params={"s": substance},
        )
    df = feats.merge(target, on="sample_id", how="inner")
    lod_fallback = df.loc[df.value.notna(), "value"].min() / 2 if df.value.notna().any() else 0.01
    df["value_for_model"] = df["value"].fillna(lod_fallback)
    df["log_value"] = np.log(df["value_for_model"])
    df["rel_value"] = df["log_value"] - df.groupby("site_id")["log_value"].transform("median")
    y = df["rel_value"].values
    below_lod_pct = df["below_lod"].mean() * 100

    r2_base, _ = lowo_r2(df, y, BASELINE_FEATURES)
    r2_water, _ = lowo_r2(df, y, BASELINE_FEATURES + ["dist_to_water_m"])
    r2_ph, _ = lowo_r2(df, y, BASELINE_FEATURES + ["soil_ph"])
    r2_both, _ = lowo_r2(df, y, BASELINE_FEATURES + ["dist_to_water_m", "soil_ph"])

    print(f"{substance:10s}{below_lod_pct:9.0f}%{r2_base:+11.3f} {r2_water:+11.3f} {r2_ph:+11.3f} {r2_both:+11.3f}")
    results.append({"substance": substance, "below_lod_pct": below_lod_pct,
                     "r2_baseline": r2_base, "r2_water": r2_water, "r2_ph": r2_ph, "r2_both": r2_both})

out = pd.DataFrame(results).sort_values("r2_both", ascending=False)
out.to_csv("/tmp/substance_scan.csv", index=False)
print("\nОтсортировано по итоговому R² (вода+pH), лучшие сверху:")
print(out[["substance", "r2_both"]].to_string(index=False))
print("""
КАК ЧИТАТЬ:
- если у вещества R² существенно выше, чем у Pb - для него стоит завести
  отдельную полноценную модель (может понадобиться свой набор признаков,
  не обязательно тот же вода+pH, что сработал для Pb);
- если ниже LOD > 40-50% - на таком веществе честной модели вообще не
  построить, слишком много цензурированных значений;
- отрицательный R² по всем колонкам - вещество не показывает
  переносимого позиционного сигнала при текущем объёме данных, не
  обязательно "плохие данные", возможно просто другая физика распределения.
""")
