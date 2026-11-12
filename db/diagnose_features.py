"""
Раскладывает прирост LOWO R² по кусочкам: помогла именно вода (варьируется
ВНУТРИ склада, честный позиционный сигнал) или это в основном pH/уклон/
высота, которые на самом деле почти постоянны по складу и могут работать
как замаскированный "номер склада", а не как физический механизм.

Параметризован по веществу - для Pb уже знаем, что "работает" вода+pH,
но для нового вещества (As и т.п.) правильный набор признаков неизвестен
заранее, поэтому скрипт всегда показывает ПОЛНУЮ раскладку по всем
кандидатам, а не только пруненный для Pb набор.

Запуск: python db/diagnose_features.py [Pb|As|Hg|...]  (по умолчанию Pb)
"""
import os
import sys
import numpy as np
import pandas as pd
import geopandas as gpd
import shap
from sklearn.ensemble import RandomForestRegressor
from sklearn.model_selection import LeaveOneGroupOut
from sklearn.metrics import mean_squared_error, r2_score
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.getenv("DATABASE_URL", "postgresql://soil_ml:soil_ml_dev_password@localhost:5432/soil_ml")
engine = create_engine(DB_URL)

TARGET_SUBSTANCE = sys.argv[1] if len(sys.argv) > 1 else "Pb"
BASELINE_FEATURES = ["signed_dist_boundary_m", "dist_centroid_m", "zone_code"]
# flow_accumulation/flow_accumulation_max/flow_direction - новые кандидаты
# (ArcGIS Pro: DEM 150м/UTM42N -> Fill -> FlowDir D8 -> FlowAcc, см.
# db/fetch_flow_covariate.py). Point-level, как и вода - по той же причине
# (см. docstring) им a priori больше доверия, чем site-level кандидатам.
POINT_LEVEL_FEATURES = ["dist_to_water_m", "flow_accumulation", "flow_accumulation_max", "flow_direction"]
# полный список кандидатов "по складу" - для Pb уже знаем, что из них
# выживает только pH (см. run_model_and_store.py), но для нового вещества
# правильный набор заранее неизвестен, поэтому тут всегда все четыре
# temp_annual_c/precip_annual_mm - исторические климатические нормы
# (WorldClim, см. data_dictionary_metadata.md); ЕЩЁ site-level, тот же
# риск замаскированного site_id, что и у pH/высоты/уклона - именно
# поэтому тестируем их здесь по тому же протоколу, а не сразу в модели.
# worldcover_class - код класса землепользования (ESA WorldCover: 10=лес,
# 20=кустарник, 30=трава, 40=пашня, 50=застройка, 60=голая земля,
# 80=вода, 90=водно-болотные...). Не непрерывная величина и не честно
# ординальная (соседние коды не значат "похожий" класс) - тот же нюанс,
# что и у flow_direction, RF всё равно может находить пороги по коду.
# Помесячные (prec_month_*/tavg_month_*) и БУДУЩИЕ климатические проекции
# (future_ssp*) намеренно НЕ включены сюда: будущий климат (2021-2100)
# не может причинно объяснять уже измеренное текущее загрязнение - это
# было бы бессмысленно с точки зрения физики, а не только статистики;
# помесячные нормы - производные от годовых, добавлять 24 колонки на
# 10 складов означает "рыбалку" за случайной корреляцией при таком N.
SITE_LEVEL_FEATURES_FULL = ["soil_ph", "slope_deg", "elevation_m", "population_500m",
                             "temp_annual_c", "precip_annual_mm", "worldcover_class"]
ALL_EXTRA = POINT_LEVEL_FEATURES + SITE_LEVEL_FEATURES_FULL

# та же логика признаков, что в run_model_and_store.py - продублирована
# намеренно, чтобы этот скрипт можно было читать и гонять независимо,
# не разбираясь, что по цепочке импортов подключается из другого файла
FEATURE_SQL = """
SELECT
    sam.sample_id, sam.site_id, sam.geom, sam.dist_to_water_m,
    sam.flow_accumulation, sam.flow_accumulation_max, sam.flow_direction,
    CASE
        WHEN ST_Contains(s.contour_geom, sam.geom) THEN 3
        WHEN ST_Contains(sb.buf_50m, sam.geom) THEN 2
        WHEN ST_Contains(sb.buf_100m, sam.geom) THEN 1
        ELSE 0
    END AS zone_code,
    ST_Distance(s.contour_geom::geography, sam.geom::geography)
        * CASE WHEN ST_Contains(s.contour_geom, sam.geom) THEN -1 ELSE 1 END AS signed_dist_boundary_m,
    ST_Distance(ST_Centroid(s.contour_geom)::geography, sam.geom::geography) AS dist_centroid_m,
    sc.soil_ph, sc.slope_deg, sc.elevation_m, sc.population_500m, sc.worldcover_class,
    sc.temp_annual_c, sc.precip_annual_mm
FROM samples sam
JOIN sites s ON s.site_id = sam.site_id
JOIN site_buffers sb ON sb.site_id = sam.site_id
LEFT JOIN site_covariates sc ON sc.site_id = sam.site_id
"""


def rf():
    return RandomForestRegressor(n_estimators=300, min_samples_leaf=3, random_state=0)


print(f"\n{'='*100}\nВЕЩЕСТВО: {TARGET_SUBSTANCE}\n{'='*100}")

with engine.connect() as conn:
    feats = gpd.read_postgis(FEATURE_SQL, conn, geom_col="geom")
    target = pd.read_sql(
        text("SELECT sample_id, value, below_lod FROM measurements WHERE substance = :s"),
        conn, params={"s": TARGET_SUBSTANCE},
    )

df = feats.merge(target, on="sample_id", how="inner")
if df.empty:
    raise SystemExit(f"Нет данных по веществу '{TARGET_SUBSTANCE}' - проверьте название "
                      f"(должно совпадать с measurements.substance, например 'Pb', 'As', 'Hg').")

lod_pct = df["below_lod"].mean() * 100
print(f"Точек: {len(df)}, ниже предела обнаружения: {lod_pct:.0f}%")
if lod_pct > 40:
    print("ВНИМАНИЕ: больше 40% значений ниже предела обнаружения - результат ниже "
          "стоит воспринимать с повышенной осторожностью, слишком много цензурированных данных.")

lod_fallback = df.loc[df.value.notna(), "value"].min() / 2 if df.value.notna().any() else 0.01
df["value_for_model"] = df["value"].fillna(lod_fallback)
df["log_value"] = np.log(df["value_for_model"])
df["rel_value"] = df["log_value"] - df.groupby("site_id")["log_value"].transform("median")

for col in ALL_EXTRA:
    df[col] = df[col].fillna(df[col].mean())

y = df["rel_value"].values
groups = df["site_id"].values


def lowo(feature_list):
    X_ = df[feature_list].values
    logo = LeaveOneGroupOut()
    pred = np.zeros_like(y)
    for tr, te in logo.split(X_, y, groups):
        pred[te] = rf().fit(X_[tr], y[tr]).predict(X_[te])
    return r2_score(y, pred), np.sqrt(mean_squared_error(y, pred))

VARIANTS = {
    "baseline (расстояние/зона)": BASELINE_FEATURES,
    "+ только вода (по точке - честный тест)": BASELINE_FEATURES + ["dist_to_water_m"],
    "+ только pH": BASELINE_FEATURES + ["soil_ph"],
    "+ только уклон": BASELINE_FEATURES + ["slope_deg"],
    "+ только высота": BASELINE_FEATURES + ["elevation_m"],
    "+ только население (500м)": BASELINE_FEATURES + ["population_500m"],
    "+ только flow_accumulation (сырое, по точке)": BASELINE_FEATURES + ["flow_accumulation"],
    "+ только flow_accumulation_max (окно 5x5, по точке)": BASELINE_FEATURES + ["flow_accumulation_max"],
    "+ только flow_direction (по точке)": BASELINE_FEATURES + ["flow_direction"],
    "+ только среднегод. температура (WorldClim)": BASELINE_FEATURES + ["temp_annual_c"],
    "+ только годовые осадки (WorldClim)": BASELINE_FEATURES + ["precip_annual_mm"],
    "+ только землепользование (WorldCover)": BASELINE_FEATURES + ["worldcover_class"],
    "+ все по-складу, полный список": BASELINE_FEATURES + SITE_LEVEL_FEATURES_FULL,
    "+ вода + pH (текущая продакшн-модель)": BASELINE_FEATURES + ["dist_to_water_m", "soil_ph"],
    "+ вода + pH + flow_accumulation_max": BASELINE_FEATURES + ["dist_to_water_m", "soil_ph", "flow_accumulation_max"],
    "+ вода + pH + климат (темп+осадки)": BASELINE_FEATURES + ["dist_to_water_m", "soil_ph", "temp_annual_c", "precip_annual_mm"],
    "+ всё вместе (вода+flow+климат + все по-складу)": BASELINE_FEATURES + ALL_EXTRA,
}

print("=" * 100)
print("РАЗЛОЖЕНИЕ ПРИРОСТА LOWO R² ПО ПРИЗНАКАМ")
print("=" * 100)
base_r2 = None
for label, feats_ in VARIANTS.items():
    r2, rmse = lowo(feats_)
    if base_r2 is None:
        base_r2 = r2
    print(f"{label:75s} R²={r2:+.3f}  RMSE={rmse:.3f}  (Δ от baseline: {r2 - base_r2:+.3f})")

print("""
КАК ЧИТАТЬ:
- если "только вода" почти не отличается от baseline, а весь прирост даёт
  "только pH/уклон/высота" - это тревожный звонок: модель, вероятно, учится
  различать САМИ СКЛАДЫ по их (почти постоянному) elevation/pH, а не
  находит перенос имый позиционный сигнал внутри площадки;
- если наоборот, "только вода" уже даёт большую часть прироста - это
  честный, физически интерпретируемый сигнал, доверять можно больше.
""")

# SHAP на модели с полным набором кандидатов - что именно она использует.
# Дальше по этим цифрам (как раньше с Pb) стоит решить, какие признаки
# оставить в run_model_and_store.py именно для этого вещества.
ALL_FEATURES = BASELINE_FEATURES + ALL_EXTRA
X_full = df[ALL_FEATURES].values
final_model = rf().fit(X_full, y)
explainer = shap.TreeExplainer(final_model)
shap_values = explainer.shap_values(X_full)
mean_abs_shap = np.abs(shap_values).mean(axis=0)
importance = pd.Series(mean_abs_shap, index=ALL_FEATURES)
importance = importance.sort_values(ascending=False)
print("Средний |SHAP| по признакам (больше = модель опирается на него сильнее):")
print(importance.to_string())
print(f"\nСледующий шаг: если для {TARGET_SUBSTANCE} видна та же картина, что была для "
      f"Pb (один-два признака явно положительны поодиночке, остальные топят их в группе) - "
      f"добавьте отдельный TARGET_SUBSTANCE='{TARGET_SUBSTANCE}' прогон в run_model_and_store.py "
      f"с этим же пруненным набором.")
