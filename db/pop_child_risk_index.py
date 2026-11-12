"""
Индекс риска для детей от участков СОЗ (по логике CCRI-DRM UNICEF: риск = опасность x
воздействие x уязвимость). Прозрачный расчёт без обучения, все веса в начале файла.

Вход:  source_data/chemicals_tj/pop_layer/pop_sites.csv, pop_samples.csv
       source_data/chemicals_tj/pop_layer/model/site_predictions.csv (скрининг, db/pop_screening_model.py)
       db/tjk_worldpop_2020.tif (WorldPop 2020, население на пиксель ~90 м)
Выход: source_data/chemicals_tj/pop_layer/child_risk/child_risk_index.csv, .gpkg (точки)

Опасность H (0..1):
  есть анализы  -> burden = max по пробам (ДДТ+ДДД+ДДЕ; сумма ГХЦГ), H = log10(burden/0,1)/4,
                   обрезано в [0;1] (0,1 мг/кг = норматив -> 0; 1000 мг/кг -> 1)
  нет анализов  -> средняя H обследованных участков того же класса скрининга (низкий/средний/высокий);
                   источник помечен в hazard_source
Воздействие на детей E (0..1):
  дети в радиусе 1 км: растр WorldPop R2025A «до 18 лет» (source_data/worldpop_r2025a/), если он есть;
  иначе население WorldPop 2020 x доля детей 0-17 лет по области (UNFPA/Таджикстат 2019,
  source_data/tjk_admpop_adm1_2019.csv), а без этого файла - CHILD_SHARE по стране;
  люди на участке и в 100 м по анкете; упоминание школы/детей в карточке
Уязвимость V (0..1) = W_V["pathways"] x доля путей воздействия из PATHWAYS
  + W_V["poverty"] x бедность по индексу относительного благосостояния Meta (RWI, ячейки ~2,4 км,
  source_data/tjk_relative_wealth_index.csv): среднее RWI ячеек в радиусе RWI_RADIUS_M (или ближайшей),
  бедность = 1 - процентиль среди всех ячеек страны. Ошибка RWI (~0,32) сопоставима с разбросом
  индекса (0,38): компонент полезен для сравнения групп участков, не соседних.
Риск = (H x E x V)^(1/3) - среднее геометрическое (как в CCRI); классы по квантилям.
Отдельно: climate_flood_amplifier - участок в зоне затопления или ближе 300 м к реке
(паводок разносит загрязнение).
"""
from pathlib import Path

import numpy as np
import pandas as pd
import rasterio
from rasterio.windows import Window

ROOT = Path(__file__).resolve().parent.parent
LAYER = ROOT / "source_data/chemicals_tj/pop_layer"
OUT = LAYER / "child_risk"

# доля детей до 18 лет: «более 40 % населения» - UNICEF Tajikistan, пресс-релиз 29.05.2026
# (Situation Analysis of Children of Tajikistan). Заменить на растр WorldPop по возрастам, когда он будет.
CHILD_SHARE = 0.40
ADM1_SHARES = ROOT / "source_data/tjk_admpop_adm1_2019.csv"
REGION_TO_ADM1 = {"Хатлонская область": "KHATLON REGION", "Согдийская область": "SUGHD REGION",
                  "РРП": "DISTRICT SUBORDINATION OF THE REPUBLIC", "ГБАО": "GBAO", "Душанбе": "DUSHANBE"}
WP_DIR = ROOT / "source_data/worldpop_r2025a"
WP_U18 = "tjk_T_Under_18_2025_CN_100m_R2025A_v1.tif"
WP_U5 = ("tjk_t_00_2025_CN_100m_R2025A_v1.tif", "tjk_t_01_2025_CN_100m_R2025A_v1.tif")
WP_TOTAL = ("tjk_T_F_2025_CN_100m_R2025A_v1.tif", "tjk_T_M_2025_CN_100m_R2025A_v1.tif")
RWI_CSV = ROOT / "source_data/tjk_relative_wealth_index.csv"
RWI_RADIUS_M = 2000
W_V = {"pathways": 0.7, "poverty": 0.3}
RADII_M = (500, 1000)
W_EXPOSURE = {"children_1km": 0.6, "people_on_site_100m": 0.25, "school_or_children_mentioned": 0.15}
PATHWAYS = {
    "drinking_water": "вода с участка или рядом используется для питья",
    "irrigation": "орошение из источников рядом",
    "livestock": "доступ скота / пищевые животные на участке",
    "crops": "посевы на участке или в 10 м",
    "easy_access": "лёгкий доступ людей",
    "flood_or_river": "зона затопления или река ближе 300 м",
    "shallow_groundwater": "грунтовые воды ближе 10 м",
}


def pop_within(r, lon, lat, radius_m):
    rx = 111320 * np.cos(np.radians(lat)) * r.res[0]
    ry = 111320 * r.res[1]
    hx, hy = int(np.ceil(radius_m / rx)), int(np.ceil(radius_m / ry))
    row, col = r.index(lon, lat)
    a = r.read(1, window=Window(col - hx, row - hy, 2 * hx + 1, 2 * hy + 1), boundless=True, fill_value=0).astype(float)
    a[(a < 0) | ~np.isfinite(a)] = 0
    yy, xx = np.mgrid[-hy:hy + 1, -hx:hx + 1]
    return float(a[(xx * rx) ** 2 + (yy * ry) ** 2 <= radius_m ** 2].sum())


def child_shares():
    """Доля детей 0-17 лет по областям: 0-14 полностью + 3/5 группы 15-19."""
    if not ADM1_SHARES.exists():
        return {}
    a = pd.read_csv(ADM1_SHARES, encoding="utf-8-sig")
    a["name"] = a.ADM1_EN.str.strip()
    k = a[["T_00_00", "T_01_04", "T_05_09", "T_10_14"]].sum(axis=1) + 0.6 * a.T_15_19
    return dict(zip(a.name, (k / a.T_TL).round(3)))


def sum_rasters(names, lon, lat, rad):
    tot = 0.0
    for n in names:
        with rasterio.open(WP_DIR / n) as r:
            tot += pop_within(r, lon, lat, rad)
    return tot


def rwi_for_sites(T):
    """Среднее RWI ячеек в радиусе RWI_RADIUS_M (иначе ближайшая), бедность 0..1, ошибка."""
    if not RWI_CSV.exists():
        return None
    from sklearn.neighbors import BallTree
    r = pd.read_csv(RWI_CSV)
    bt = BallTree(np.radians(r[["latitude", "longitude"]].values), metric="haversine")
    xy = np.radians(T[["latitude", "longitude"]].values)
    idx = bt.query_radius(xy, r=RWI_RADIUS_M / 6371000)
    d, near = bt.query(xy, k=1)
    vals, errs = [], []
    for i, ix in enumerate(idx):
        ix = ix if len(ix) else near[i]
        vals.append(r.rwi.values[ix].mean()); errs.append(r.error.values[ix].mean())
    ref = np.sort(r.rwi.values)
    pct = np.searchsorted(ref, vals) / len(ref)
    return pd.DataFrame({"rwi": np.round(vals, 3), "rwi_error": np.round(errs, 3),
                         "rwi_nearest_km": np.round(d[:, 0] * 6371, 2), "poverty": np.round(1 - pct, 3)}, index=T.index)


def children_at(lon, lat, rad, region=None):
    """Дети 0-17 в радиусе rad м для произвольной точки (форма «новый участок» на стенде)."""
    if (WP_DIR / WP_U18).exists():
        return sum_rasters([WP_U18], lon, lat, rad)
    if not (ROOT / "db/tjk_worldpop_2020.tif").exists():
        return None                      # растров населения нет
    share = child_shares().get(REGION_TO_ADM1.get(region), CHILD_SHARE)
    with rasterio.open(ROOT / "db/tjk_worldpop_2020.tif") as r:
        return pop_within(r, lon, lat, rad) * share


def poverty_at(lat, lon):
    """Бедность 0..1 по RWI для произвольной точки; None, если файла RWI нет."""
    rw = rwi_for_sites(pd.DataFrame({"latitude": [lat], "longitude": [lon]}))
    return None if rw is None else float(rw.poverty.iloc[0])


def has(v, *words):
    return isinstance(v, str) and any(w in v for w in words)


def main():
    OUT.mkdir(exist_ok=True)
    T = pd.read_csv(LAYER / "pop_sites.csv", encoding="utf-8-sig", low_memory=False)
    T = T[T.on_map == True].copy()  # noqa: E712
    S = pd.read_csv(LAYER / "pop_samples.csv", encoding="utf-8-sig", low_memory=False)
    S = S[S.map_id.notna()]
    S["burden"] = S[["DDT_group_sum", "HCH_sum"]].max(axis=1)
    T = T.merge(S.groupby("map_id").burden.max().rename("burden"), left_on="map_id", right_index=True, how="left")
    P = pd.read_csv(LAYER / "model" / "site_predictions.csv", encoding="utf-8-sig")[["map_id", "screen_class", "score_used"]]
    T = T.merge(P, on="map_id", how="left")

    # --- опасность ---
    T["H_measured"] = np.clip(np.log10(T.burden.clip(lower=0.01) / 0.1) / 4, 0, 1)
    by_class = T[T.burden.notna()].groupby("screen_class").H_measured.mean()
    T["H"] = T.H_measured.fillna(T.screen_class.map(by_class))
    T["hazard_source"] = np.where(T.burden.notna(), "анализы", "прогноз скрининга (класс " + T.screen_class.astype(str) + ")")

    # --- воздействие на детей ---
    if (WP_DIR / WP_U18).exists():
        src = "WorldPop R2025A (2025), растр «до 18 лет»"
        for rad in RADII_M:
            T[f"children_{rad}m"] = [round(sum_rasters([WP_U18], lo, la, rad)) for la, lo in zip(T.latitude, T.longitude)]
            if all((WP_DIR / n).exists() for n in WP_TOTAL):
                T[f"pop_{rad}m"] = [sum_rasters(WP_TOTAL, lo, la, rad) for la, lo in zip(T.latitude, T.longitude)]
        if all((WP_DIR / n).exists() for n in WP_U5):
            T["children_u5_1000m"] = [round(sum_rasters(WP_U5, lo, la, 1000)) for la, lo in zip(T.latitude, T.longitude)]
    else:
        shares = child_shares()
        T["child_share"] = T.region.map(REGION_TO_ADM1).map(shares).fillna(CHILD_SHARE)
        src = ("WorldPop 2020 x доля детей по области (UNFPA/Таджикстат 2019)" if shares
               else f"WorldPop 2020 x {CHILD_SHARE}")
        with rasterio.open(ROOT / "db/tjk_worldpop_2020.tif") as r:
            for rad in RADII_M:
                T[f"pop_{rad}m"] = [pop_within(r, lo, la, rad) for la, lo in zip(T.latitude, T.longitude)]
        for rad in RADII_M:
            T[f"children_{rad}m"] = (T[f"pop_{rad}m"] * T.child_share).round()
    T["children_source"] = src
    near = T[["lived_on_site", "lived_50m", "lived_100m"]].apply(pd.to_numeric, errors="coerce").fillna(0).sum(axis=1)
    T["people_on_site_100m"] = near
    T["school_or_children_mentioned"] = (T.get("mentions_school").fillna(False).astype(bool) |
                                         T.get("mentions_children").fillna(False).astype(bool) |
                                         T.get("dist_school_m_text").notna()).astype(int)
    rk = lambda s: s.rank(pct=True, method="average")  # noqa: E731
    T["E"] = (W_EXPOSURE["children_1km"] * rk(np.log1p(T.children_1000m)) +
              W_EXPOSURE["people_on_site_100m"] * rk(np.log1p(T.people_on_site_100m)) +
              W_EXPOSURE["school_or_children_mentioned"] * T.school_or_children_mentioned)

    # --- уязвимость: пути воздействия ---
    pw = pd.DataFrame(index=T.index)
    pw["drinking_water"] = (T.groundwaters_using.apply(has, args=("Питьев",)) | T.surface_water_usage.apply(has, args=("Пить",)) |
                            T.get("mentions_drinking_water").fillna(False).astype(bool))
    pw["irrigation"] = (T.groundwaters_using.apply(has, args=("Ороше",)) | T.surface_water_usage.apply(has, args=("Ороше",)) |
                        T.get("mentions_irrigation").fillna(False).astype(bool))
    pw["livestock"] = T.animals_food.apply(has, args=("животн",)) | T.get("mentions_livestock_access").fillna(False).astype(bool)
    pw["crops"] = T.agriculture_near.apply(has, args=("На загрязненной", "10 м"))
    pw["easy_access"] = T.access.apply(has, args=("Легкий",))
    pw["flood_or_river"] = T.flood_zone.eq("Да") | T.river_distance.eq("от 0 до 300 м")
    pw["shallow_groundwater"] = T.groundwater_depth.apply(has, args=("Мелководье", "Средняя"))
    for c in pw:
        T[f"path_{c}"] = pw[c].astype(int)
    T["V_pathways"] = pw.astype(int).mean(axis=1)
    rw = rwi_for_sites(T)
    if rw is not None:
        T = T.join(rw)
        T["V"] = W_V["pathways"] * T.V_pathways + W_V["poverty"] * T.poverty
    else:
        T["V"] = T.V_pathways
    T["pathways_list"] = pw.apply(lambda r: "; ".join(PATHWAYS[c] for c in pw.columns if r[c]), axis=1)
    T["climate_flood_amplifier"] = pw["flood_or_river"].astype(int)

    # --- риск ---
    f = 0.05  # нижняя граница, чтобы нулевая компонента не обнуляла всё
    T["child_risk"] = np.cbrt(T.H.clip(lower=f) * T.E.clip(lower=f) * T.V.clip(lower=f)).round(3)
    T["child_risk_rank"] = T.child_risk.rank(ascending=False, method="min").astype(int)
    T["child_risk_class"] = pd.qcut(T.child_risk, [0, .5, .8, .95, 1], labels=["низкий", "средний", "высокий", "очень высокий"]).astype(str)
    cols = ["map_id", "tj_code", "site_name", "region", "latitude", "longitude", "pollution_status",
            "child_risk_rank", "child_risk_class", "child_risk", "H", "hazard_source", "burden", "screen_class",
            "E", "children_500m", "children_1000m", "pop_500m", "pop_1000m", "people_on_site_100m",
            "school_or_children_mentioned", "children_source", "V", "V_pathways", "pathways_list"] + \
           [c for c in ("children_u5_1000m", "child_share", "rwi", "rwi_error", "rwi_nearest_km", "poverty") if c in T] + \
           [f"path_{c}" for c in pw.columns] + ["climate_flood_amplifier"]
    R = T[cols].sort_values("child_risk_rank")
    R.to_csv(OUT / "child_risk_index.csv", index=False, encoding="utf-8-sig")
    import geopandas as gpd
    import shutil
    import tempfile
    g = gpd.GeoDataFrame(R, geometry=gpd.points_from_xy(R.longitude, R.latitude), crs=4326)
    tmp = Path(tempfile.mkdtemp()) / "child_risk_index.gpkg"
    g.to_file(tmp, layer="child_risk_index", driver="GPKG", engine="pyogrio")
    shutil.copyfile(tmp, OUT / "child_risk_index.gpkg")
    print(R.child_risk_class.value_counts().to_dict())
    print(R.head(15)[["tj_code", "site_name", "child_risk_class", "child_risk", "H", "hazard_source", "children_1000m", "V"]].to_string())


if __name__ == "__main__":
    main()
