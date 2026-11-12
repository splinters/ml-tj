"""
Честная проверка ПЕРЕД тем, как строить "глобальную модель на всю страну":
можно ли вообще предсказывать АБСОЛЮТНУЮ концентрацию Pb (не относительную
к медиане склада, как в текущей модели) по признакам, которые в принципе
можно посчитать в любой точке страны (вода, pH) - без привязки к контуру
известного склада?

Ожидание, основанное на всей предыдущей работе: абсолютное предсказание
должно быть заметно хуже относительного, потому что межскладовая разница
базового уровня (Вахш ~65-70ppm против Пянджа ~12ppm) не полностью
объясняется водой/pH - именно поэтому изначально перешли на нормировку.
Вопрос не "будет ли хуже", а "насколько хуже, и остаётся ли сигнал вообще".

Запуск: python db/scan_absolute_vs_relative.py
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

TARGET_SUBSTANCE = "Pb"
# признаки, которые реально можно посчитать в ЛЮБОЙ точке страны - без
# zone_code/signed_dist_boundary_m/dist_centroid_m, им неоткуда взяться
# без контура известного склада.
COUNTRY_WIDE_FEATURES = ["dist_to_water_m", "soil_ph"]
COUNTRY_WIDE_WITH_ROADS = ["dist_to_water_m", "soil_ph", "dist_to_road_m"]
# рельеф уже проверялся для ОТНОСИТЕЛЬНОЙ модели (Δ около нуля) - но не
# для абсолютной/глобальной, это отдельный, легитимный вопрос
COUNTRY_WIDE_WITH_RELIEF = ["dist_to_water_m", "soil_ph", "elevation_m", "slope_deg"]
# реалистичный сценарий "глобальной модели": НОВЫЙ склад с уже нарисованным
# контуром (значит, геометрия посчитать МОЖНО), но без истории проб (значит,
# посчитать медиану для нормировки НЕЛЬЗЯ) - поэтому геометрия остаётся,
# а цель меняется с relative на абсолютную
FULL_FEATURES_ABS_TARGET = ["signed_dist_boundary_m", "dist_centroid_m", "zone_code",
                             "dist_to_water_m", "soil_ph"]

FEATURE_SQL = """
SELECT
    sam.sample_id, sam.site_id, sam.geom, sam.dist_to_water_m, sam.dist_to_road_m,
    CASE
        WHEN ST_Contains(s.contour_geom, sam.geom) THEN 3
        WHEN ST_Contains(sb.buf_50m, sam.geom) THEN 2
        WHEN ST_Contains(sb.buf_100m, sam.geom) THEN 1
        ELSE 0
    END AS zone_code,
    ST_Distance(s.contour_geom::geography, sam.geom::geography)
        * CASE WHEN ST_Contains(s.contour_geom, sam.geom) THEN -1 ELSE 1 END AS signed_dist_boundary_m,
    ST_Distance(ST_Centroid(s.contour_geom)::geography, sam.geom::geography) AS dist_centroid_m,
    sc.soil_ph, sc.elevation_m, sc.slope_deg, sc.lithology_class
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
    target = pd.read_sql(
        text("SELECT sample_id, value, below_lod FROM measurements WHERE substance = :s"),
        conn, params={"s": TARGET_SUBSTANCE},
    )

df = feats.merge(target, on="sample_id", how="inner")
for col in ["dist_to_water_m", "soil_ph", "dist_to_road_m", "elevation_m", "slope_deg"]:
    df[col] = df[col].fillna(df[col].mean())
# категориальный - median-fill безопаснее mean() (не создаёт несуществующий
# дробный класс породы, если вдруг где-то пропуск)
df["lithology_class"] = df["lithology_class"].fillna(df["lithology_class"].median())

lod_fallback = df.loc[df.value.notna(), "value"].min() / 2 if df.value.notna().any() else 0.01
df["value_for_model"] = df["value"].fillna(lod_fallback)
df["log_value"] = np.log(df["value_for_model"])
df["rel_value"] = df["log_value"] - df.groupby("site_id")["log_value"].transform("median")

y_abs = df["log_value"].values      # АБСОЛЮТНАЯ цель - то, что нужно для глобальной модели
y_rel = df["rel_value"].values      # относительная - то, что уже работает (для сравнения)

print("=" * 90)
print("АБСОЛЮТНОЕ (для глобальной модели) vs ОТНОСИТЕЛЬНОЕ (текущая модель) предсказание Pb")
print("=" * 90)

r2_abs_narrow, rmse_abs_narrow = lowo_r2(df, y_abs, COUNTRY_WIDE_FEATURES)
r2_rel_narrow, rmse_rel_narrow = lowo_r2(df, y_rel, COUNTRY_WIDE_FEATURES)
r2_abs_full, rmse_abs_full = lowo_r2(df, y_abs, FULL_FEATURES_ABS_TARGET)
r2_abs_roads, rmse_abs_roads = lowo_r2(df, y_abs, COUNTRY_WIDE_WITH_ROADS)
r2_abs_road_only, rmse_abs_road_only = lowo_r2(df, y_abs, ["dist_to_road_m"])
r2_abs_elev_only, rmse_abs_elev_only = lowo_r2(df, y_abs, ["elevation_m"])
r2_abs_slope_only, rmse_abs_slope_only = lowo_r2(df, y_abs, ["slope_deg"])
r2_abs_relief, rmse_abs_relief = lowo_r2(df, y_abs, COUNTRY_WIDE_WITH_RELIEF)
r2_abs_lith_only, rmse_abs_lith_only = lowo_r2(df, y_abs, ["lithology_class"])
r2_abs_lith, rmse_abs_lith = lowo_r2(df, y_abs, COUNTRY_WIDE_FEATURES + ["lithology_class"])

print(f"{'Абсолютная, только вода+pH (голая точка без контура)':55s} "
      f"LOWO R²={r2_abs_narrow:+.3f}  RMSE={rmse_abs_narrow:.3f}")
print(f"{'Относительная, только вода+pH (для сравнения)':55s} "
      f"LOWO R²={r2_rel_narrow:+.3f}  RMSE={rmse_rel_narrow:.3f}")
print(f"{'Абсолютная, геометрия+вода+pH (новый склад с контуром)':55s} "
      f"LOWO R²={r2_abs_full:+.3f}  RMSE={rmse_abs_full:.3f}")
print(f"{'Абсолютная, только дороги':55s} "
      f"LOWO R²={r2_abs_road_only:+.3f}  RMSE={rmse_abs_road_only:.3f}")
print(f"{'Абсолютная, вода+pH+дороги':55s} "
      f"LOWO R²={r2_abs_roads:+.3f}  RMSE={rmse_abs_roads:.3f}")
print(f"{'Абсолютная, только высота':55s} "
      f"LOWO R²={r2_abs_elev_only:+.3f}  RMSE={rmse_abs_elev_only:.3f}")
print(f"{'Абсолютная, только уклон':55s} "
      f"LOWO R²={r2_abs_slope_only:+.3f}  RMSE={rmse_abs_slope_only:.3f}")
print(f"{'Абсолютная, вода+pH+высота+уклон':55s} "
      f"LOWO R²={r2_abs_relief:+.3f}  RMSE={rmse_abs_relief:.3f}")
print(f"{'Абсолютная, только геология (8/10 складов - один класс)':55s} "
      f"LOWO R²={r2_abs_lith_only:+.3f}  RMSE={rmse_abs_lith_only:.3f}")
print(f"{'Абсолютная, вода+pH+геология':55s} "
      f"LOWO R²={r2_abs_lith:+.3f}  RMSE={rmse_abs_lith:.3f}")

print(f"""
КАК ЧИТАТЬ: тот же принцип, что и раньше - признак сначала один, потом в
связке с уже проверенными вода+pH. Если "вода+pH+рельеф" выше, чем просто
"вода+pH" (0.074 в прошлый раз) - рельеф реально добавляет что-то новое
для глобальной модели, не просто повторяет то же самое, что вода/pH уже
объясняют. Если ниже - та же история, что с дорогами и по-складовой
группой: физически как будто должно помогать, но на 10 складах не
окупается.
""")
