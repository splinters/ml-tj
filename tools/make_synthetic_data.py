#!/usr/bin/env python3
"""
Синтетический набор данных для открытой версии: та же структура файлов и столбцов, что у данных
пилота, но без данных заказчика. Блокноты и модели запускаются на нём без исходных данных.

Как строится (запускается в рабочем проекте, где есть исходные данные):
  - структура: те же таблицы, столбцы, типы и доли пропусков;
  - идентификаторы, названия, тексты карточек, ссылки, лаборатории и отчёты заменены условными;
  - координаты созданы заново (случайные точки в условных прямоугольниках трёх областей),
    контуры складов - случайные прямоугольники; ни одна точка не совпадает с реальной;
  - числовые признаки: значения столбца перемешаны между строками независимо от других
    столбцов и умножены на случайный множитель, связь строк с реальными участками разорвана;
  - категории анкеты (варианты ответов формы) взяты из словаря формы с их частотами;
  - загрязнение задано заново формулой от синтетических признаков плюс шум, чтобы модели
    было что находить; значения метрик на синтетике не совпадают с результатами пилота.

Выход (--out, по умолчанию tools/public_release/data/synthetic):
  tajikistan_soil_ml_export.gpkg                слои sites, measurements_wide, site_covariates
  source_data/chemicals_tj/pop_layer/...        pop_sites, pop_samples, site_covariates_pop, pop_sources,
                                                child_risk/, sampling_plan/, metals_link/
Файлы модели скрининга (model/metrics.csv и др.) строятся затем обычными скриптами:
  export ML_TJ_DATA_ROOT=<out>
  python db/pop_screening_model.py 0_0_0 0_0_1 ... 0_6_0 0_6_1   (14 вариантов; повторять до «готово»)
  python db/pop_screening_model.py final
В выпуске оставить из model/runs/ только 0_6_0.json (его читает форма оценки нового участка на стенде).
Для демо-стенда синтетика загружается в PostgreSQL скриптом db/load_synthetic.py.
"""
import argparse
import json
import math
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.affinity import rotate
from shapely.geometry import Point, box

ROOT = Path(__file__).resolve().parent.parent
REAL_LAYER = ROOT / "source_data" / "chemicals_tj" / "pop_layer"
REAL_GPKG = next((p for p in (ROOT / "notebooks" / "data" / "tajikistan_soil_ml_export.gpkg",
                              ROOT / "tajikistan_soil_ml_export.gpkg") if p.exists()), None)
SEED = 20261112
UTM = 32642

# условные прямоугольники областей (lat_min, lat_max, lon_min, lon_max) - только для синтетики
REGION_BOX = {
    "Согдийская область": (39.9, 40.6, 69.0, 70.8),
    "Хатлонская область": (37.3, 38.3, 68.3, 69.9),
    "РРП": (38.4, 38.9, 68.0, 69.4),
}
FREE_TEXT_SITE = ["problem_description", "site_description", "impact", "notes", "doc_names", "photo_urls",
                  "doc_urls", "card_url", "years_mentioned_text", "site_name_full"]
FREE_TEXT_SAMPLE = ["sample_label", "sample_kind", "doc_file", "report_ref", "lab", "sample_key", "flags",
                    "tj_candidates", "depth_cm", "sample_date"]
NORM_DDT = 0.1   # мг/кг, норматив для ДДТ (как в пилоте)


def rng():
    return np.random.default_rng(SEED)


def synth_col(s, n, r, noise=0.25):
    """Значения столбца без связи с исходными строками: та же доля пропусков,
    категории - по частотам, числа - перемешанные значения со случайным множителем."""
    na = r.random(n) < s.isna().mean()
    vals = s.dropna()
    if vals.empty:
        return pd.Series([np.nan] * n)
    if s.dtype == bool or set(map(str, vals.unique())) <= {"True", "False"}:
        out = r.choice(vals.values, n)
    elif pd.api.types.is_numeric_dtype(vals):
        out = r.choice(vals.values.astype(float), n)
        if vals.nunique() > 15:
            out = out * np.exp(r.normal(0, noise, n))
            if (vals.astype(float) % 1 == 0).all():       # счётчики и целые коды остаются целыми
                out = np.round(out)
            dec = max(0, min(4, int(np.nanmax([len(str(v).split(".")[-1]) if "." in str(v) else 0
                                                for v in vals.head(50)]))))
            out = np.round(out, dec)
    else:
        freq = vals.value_counts(normalize=True)
        out = r.choice(freq.index.values, n, p=freq.values)
    out = pd.Series(out, dtype=object if not pd.api.types.is_numeric_dtype(vals) else float)
    out[na] = np.nan
    return out


def rand_point(region, r):
    la0, la1, lo0, lo1 = REGION_BOX.get(region, REGION_BOX["Хатлонская область"])
    return round(r.uniform(la0, la1), 5), round(r.uniform(lo0, lo1), 5)


def hv_m(la1, lo1, la2, lo2):
    rr = np.radians
    h = np.sin(rr(la2 - la1) / 2) ** 2 + np.cos(rr(la1)) * np.cos(rr(la2)) * np.sin(rr(lo2 - lo1) / 2) ** 2
    return 2 * 6371000 * np.arcsin(np.sqrt(h))


def z(x):
    x = pd.to_numeric(pd.Series(x), errors="coerce").astype(float)
    return ((x - x.mean()) / (x.std() or 1)).fillna(0).values


# ----------------------------------------------------------------------------- СОЗ
def make_pop(out):
    r = rng()
    S0 = pd.read_csv(REAL_LAYER / "pop_sites.csv", encoding="utf-8-sig", low_memory=False)
    P0 = pd.read_csv(REAL_LAYER / "pop_samples.csv", encoding="utf-8-sig", low_memory=False)
    C0 = pd.read_csv(REAL_LAYER / "site_covariates_pop.csv", encoding="utf-8-sig")
    n = len(S0)
    S = pd.DataFrame({c: synth_col(S0[c], n, r) for c in S0.columns})
    S["tj_code"] = [f"SY-{1001 + i}" for i in range(n)]
    on_map = r.random(n) > (S0.on_map != True).mean()  # noqa: E712
    S["on_map"] = on_map
    S["map_id"] = np.where(on_map, np.cumsum(on_map), np.nan)
    S["site_name"] = [f"Синтетический участок {i + 1:03d}" for i in range(n)]
    S["site_name_full"] = [f"Объект {c}. {nm}" for c, nm in zip(S.tj_code, S.site_name)]
    for c in FREE_TEXT_SITE[:-1]:
        S[c] = np.nan
    S["card_url"] = np.nan
    S["country"] = "Таджикистан"
    S["region"] = S["region"].fillna("Хатлонская область")
    S.loc[~on_map, ["region", "latitude", "longitude"]] = np.nan
    st = S0.loc[S0.on_map == True, "pollution_status"].value_counts(normalize=True)  # noqa: E712
    S["pollution_status"] = np.where(on_map, r.choice(st.index.values, n, p=st.values), "нет на карте chemicals.tj")
    pts = [rand_point(reg, r) for reg in S.region.fillna("Хатлонская область")]
    S["latitude"] = np.where(on_map, [p[0] for p in pts], np.nan)
    S["longitude"] = np.where(on_map, [p[1] for p in pts], np.nan)
    S["site_date"] = pd.to_datetime("2011-01-01") + pd.to_timedelta(r.integers(0, 5000, n), "D")
    S["site_date"] = S.site_date.dt.strftime("%Y-%m-%d")
    S["created_at"] = "2025-10-01"
    S["coord_decimals"] = 5
    S["qc_coords_low_precision"] = False
    S["xrf_site"], S["xrf_site_dist_m"] = np.nan, np.nan

    # ковариаты участков (те же столбцы, значения перемешаны)
    m = S[on_map].copy()
    C = pd.DataFrame({c: synth_col(C0[c], len(m), r, noise=0.15) for c in C0.columns if c not in ("map_id", "tj_code")})
    C.insert(0, "tj_code", m.tj_code.values)
    C.insert(0, "map_id", m.map_id.astype(int).values)
    # климат и рельеф согласованы с координатами (южнее и ниже - теплее)
    C["elev_m"] = np.round(300 + (m.latitude.values - 37) * 250 + r.normal(0, 120, len(m))).clip(250, 2000)
    C["bio1_temp_c"] = np.round(18 - (C.elev_m - 300) / 160 + r.normal(0, .5, len(m)), 1)

    # загрязнение: латентная величина от синтетических признаков + шум
    lat_ = (0.8 * z(np.log1p(C.pop_500m.values)) - 0.5 * z(C.elev_m.values)
            + 0.6 * (m["pit_dug"].fillna("") == "Да").values
            + 0.5 * (m["smell"].fillna("") == "Да").values
            + 0.4 * z(np.log1p(pd.to_numeric(m["lived_500m"], errors="coerce").fillna(0)))
            + 0.6 * np.sin(m.latitude.values * 3.1) * np.cos(m.longitude.values * 2.3)
            + r.normal(0, 0.9, len(m)))
    log_burden = 1.15 + 1.05 * z(lat_)                 # log10 мг/кг, медиана около 14 мг/кг
    burden = pd.Series(10 ** log_burden, index=m.index)

    # пробы: столько же, сколько в пилоте; часть участков без анализов
    has_lab = r.random(len(m)) > 1 - (P0.map_id.dropna().nunique() / max(1, S0.on_map.sum()))
    sites_lab = m.index[has_lab]
    counts = r.choice(P0.groupby("map_id").size().values, len(sites_lab))
    rows = []
    for k, (idx, cnt) in enumerate(zip(sites_lab, counts)):
        top = burden[idx]
        vals = top * np.exp(-np.abs(r.normal(0, 1.2, cnt)))
        vals[0] = top
        for v in vals:
            rows.append((idx, v))
    nP = len(rows)
    P = pd.DataFrame({c: synth_col(P0[c], nP, r) for c in P0.columns})
    src = [f"SRC_{ch}" for ch in "ABCDEF"]
    P["sample_id"] = np.arange(1, nP + 1)
    P["source_id"] = r.choice(src, nP)
    P["lab"] = P.source_id.map(lambda s: f"Лаборатория {s[-1]}")
    P["report_ref"] = P.source_id.map(lambda s: f"Синтетический отчёт {s[-1]}")
    P["doc_file"] = P.source_id.str.lower() + ".pdf"
    for c in ("sample_label", "sample_kind", "flags", "tj_candidates", "depth_cm", "sample_date"):
        P[c] = np.nan
    site_idx = np.array([a for a, _ in rows])
    P["map_id"] = S.loc[site_idx, "map_id"].values
    P["tj_code"] = S.loc[site_idx, "tj_code"].values
    P["site_key"] = P.map_id.astype(int).astype(str)
    P["sample_key"] = P.source_id + "|" + P.tj_code + "|" + P.sample_id.astype(str)
    P["sample_label"] = "Проба " + P.sample_id.astype(str)
    d = np.abs(r.normal(0, 60, nP))
    ang = r.uniform(0, 2 * np.pi, nP)
    P["lat"] = np.round(S.loc[site_idx, "latitude"].values + d * np.cos(ang) / 111000, 6)
    P["lon"] = np.round(S.loc[site_idx, "longitude"].values + d * np.sin(ang) / 88000, 6)
    P["dist_to_site_m"] = np.round(d)
    v = np.array([b for _, b in rows])
    share_ddt = r.uniform(0.4, 0.95, nP)
    P["DDT_group_sum"] = np.round(np.where(r.random(nP) < .5, v, v * share_ddt), 4)
    P["HCH_sum"] = np.round(np.where(P.DDT_group_sum.values >= v * .999, v * (1 - share_ddt), v), 4)
    P["DDT"] = np.round(P.DDT_group_sum * r.uniform(.5, .95, nP), 4)
    P["DDD"] = np.round(P.DDT_group_sum * r.uniform(0, .2, nP), 4)
    P["DDE"] = np.round(P.DDT_group_sum - P.DDT - P.DDD, 4).clip(lower=0)
    P["g_HCH"] = np.round(P.HCH_sum * r.uniform(.1, .6, nP), 4)
    P["DDT_x_norm"] = np.round(P.DDT / NORM_DDT, 2)
    P["DDT_gt_norm"] = P.DDT > NORM_DDT
    for c in ("DDT", "DDD", "DDE", "g_HCH"):
        P[f"{c}_nd"] = P[c] <= 0.002

    # сводные поля участка по пробам
    g = P.groupby("map_id")
    agg = pd.DataFrame({"lab_n_samples": g.size(), "ddt_max": g.DDT.max(), "ddd_max": g.DDD.max(),
                        "dde_max": g.DDE.max(), "ddt_group_sum_max": g.DDT_group_sum.max(),
                        "ddt_x_norm_max": g.DDT_x_norm.max(), "n_samples_ddt_gt_norm": g.DDT_gt_norm.sum(),
                        "hch_sum_max": g.HCH_sum.max(), "g_hch_max": g.g_HCH.max(),
                        "lab_sources": g.source_id.agg(lambda x: "; ".join(sorted(set(x)))),
                        "lab_years": g.year.agg(lambda x: "; ".join(str(int(y)) for y in sorted(set(x.dropna())))) })
    S = S.drop(columns=[c for c in agg.columns if c in S]).merge(agg, left_on="map_id", right_index=True, how="left")
    S["lab_n_samples"] = S.lab_n_samples.fillna(0).astype(int)
    S["lab_n_samples_with_own_coords"] = S.lab_n_samples
    # ближайший соседний участок
    ok = S.latitude.notna()
    la, lo = S.latitude.values, S.longitude.values
    near, near_m = [], []
    for i in range(len(S)):
        if not ok.iloc[i]:
            near.append(np.nan); near_m.append(np.nan); continue
        dd = hv_m(la[i], lo[i], la, lo); dd[i] = np.inf; dd[~ok.values] = np.inf
        j = int(np.nanargmin(dd)); near.append(S.tj_code.iloc[j]); near_m.append(round(float(dd[j])))
    S["nearest_pop_site"], S["nearest_pop_site_m"] = near, near_m

    sources = pd.DataFrame({"source_id": src, "lab": [f"Лаборатория {s[-1]}" for s in src],
                            "report_ref": [f"Синтетический отчёт {s[-1]}" for s in src],
                            "n_values": [int((P.source_id == s).sum()) * 6 for s in src],
                            "n_sites": [int(P[P.source_id == s].map_id.nunique()) for s in src],
                            "rank_1_2": [1, 1, 2, 2, 2, 2]})

    L = out / "source_data" / "chemicals_tj" / "pop_layer"
    L.mkdir(parents=True, exist_ok=True)
    S[S0.columns].to_csv(L / "pop_sites.csv", index=False, encoding="utf-8-sig")
    P[P0.columns].to_csv(L / "pop_samples.csv", index=False, encoding="utf-8-sig")
    C.to_csv(L / "site_covariates_pop.csv", index=False, encoding="utf-8-sig")
    sources.to_csv(L / "pop_sources.csv", index=False, encoding="utf-8-sig")
    # вложения карточек (фото, документы) в синтетике отсутствуют - пустая таблица той же структуры
    F0 = pd.read_csv(REAL_LAYER / "pop_files.csv", encoding="utf-8-sig", nrows=1)
    pd.DataFrame(columns=F0.columns).to_csv(L / "pop_files.csv", index=False, encoding="utf-8-sig")
    S["n_photos"], S["n_docs"] = 0, 0
    S[S0.columns].to_csv(L / "pop_sites.csv", index=False, encoding="utf-8-sig")
    # «факты из текста карточек»: только синтетические поля участка, без текста
    facts = [{"tj_code": x.tj_code, "field": f, "value": getattr(x, f), "snippet": "синтетический пример",
              "text_source": "карточка участка", "map_id": x.map_id}
             for x in S.itertuples() for f in ("site_kind_text", "building_state_text", "current_use_text")
             if isinstance(getattr(x, f), str)]
    pd.DataFrame(facts, columns=["tj_code", "field", "value", "snippet", "text_source", "map_id"]) \
        .to_csv(L / "pop_text_facts.csv", index=False, encoding="utf-8-sig")
    return S, P, C, burden


def make_child_risk(out, S, P, C, burden, r):
    """Индекс риска по формуле пилота: (H·E·V)^(1/3); дети - синтетические."""
    R0 = pd.read_csv(REAL_LAYER / "child_risk" / "child_risk_index.csv", encoding="utf-8-sig")
    m = S[S.on_map == True].copy()  # noqa: E712
    n = len(m)
    R = pd.DataFrame({c: synth_col(R0[c], n, r) for c in R0.columns})
    for c in ("map_id", "tj_code", "site_name", "region", "latitude", "longitude", "pollution_status"):
        R[c] = m[c].values
    lab = P.groupby("map_id")[["DDT_group_sum", "HCH_sum"]].max().max(axis=1)
    b = m.map_id.map(lab)
    est = 10 ** (np.log10(burden[m.index]) + r.normal(0, .6, n))         # «оценка модели» для участков без проб
    R["hazard_source"] = np.where(b.notna(), "анализы", "оценка скрининга")
    R["burden"] = np.round(np.where(b.notna(), b, est), 3)
    R["H"] = np.round(np.clip(np.log10(R.burden / 0.1) / 4, 0, 1), 3)
    kids = np.round(np.exp(r.normal(np.log(700), 1.2, n)))
    R["children_1000m"] = kids
    R["children_500m"] = np.round(kids * r.uniform(.15, .4, n))
    R["children_u5_1000m"] = np.round(kids * r.uniform(.25, .35, n))
    R["pop_1000m"] = np.round(kids / r.uniform(.38, .45, n))
    R["pop_500m"] = np.round(R.children_500m / .41)
    R["children_source"] = "синтетика"
    rank = lambda x: pd.Series(x).rank(pct=True).values  # noqa: E731
    R["E"] = np.round(0.6 * rank(kids) + 0.25 * rank(R.people_on_site_100m.fillna(0))
                      + 0.15 * R.school_or_children_mentioned.fillna(0).astype(float), 3)
    paths = ["path_drinking_water", "path_irrigation", "path_livestock", "path_crops", "path_easy_access",
             "path_flood_or_river", "path_shallow_groundwater"]
    names = ["питьевая вода", "орошение", "скот", "посевы", "лёгкий доступ", "паводок/река", "грунтовые воды"]
    for p in paths:
        R[p] = r.random(n) < 0.35
    R["V_pathways"] = np.round(R[paths].mean(axis=1), 3)
    R["poverty"] = np.round(r.uniform(0, 1, n), 3)
    R["V"] = np.round(0.7 * R.V_pathways + 0.3 * R.poverty, 3)
    R["pathways_list"] = R[paths].apply(lambda row: "; ".join(nm for nm, v in zip(names, row) if v), axis=1)
    R["child_risk"] = np.round(np.cbrt(R.H * R.E * R.V.clip(lower=.05)), 4)
    R["child_risk_rank"] = R.child_risk.rank(ascending=False, method="first").astype(int)
    q = R.child_risk.rank(pct=True)
    R["child_risk_class"] = np.select([q > .95, q > .80, q > .50], ["очень высокий", "высокий", "средний"], "низкий")
    R = R.sort_values("child_risk_rank")
    d = out / "source_data" / "chemicals_tj" / "pop_layer" / "child_risk"
    d.mkdir(parents=True, exist_ok=True)
    R[R0.columns].to_csv(d / "child_risk_index.csv", index=False, encoding="utf-8-sig")
    return R


def make_sampling_plan(out, R, S, r):
    P0 = pd.read_csv(REAL_LAYER / "sampling_plan" / "sampling_plan.csv", encoding="utf-8-sig")
    groups = [("Первичная оценка (нет данных о ДДТ/ГХЦГ)", 14, 4), ("Контроль очистки", 9, 4),
              ("Оконтуривание очага для ремедиации", 8, 10), ("Обновление данных 2012–2016 гг.", 6, 3)]
    no_lab = R[R.hazard_source != "анализы"]
    lab = R[R.hazard_source == "анализы"]
    pick = [no_lab.head(14), lab.sample(9, random_state=1), lab.head(30).sample(8, random_state=2),
            lab.tail(60).sample(6, random_state=3)]
    rows, prio = [], 1
    for gi, ((gname, _, ns), df) in enumerate(zip(groups, pick), 1):
        for _, x in df.iterrows():
            rows.append({"priority": prio, "group": gi, "group_name": gname, "tj_code": x.tj_code,
                         "site_name": x.site_name, "region": x.region, "latitude": x.latitude,
                         "longitude": x.longitude, "reason": "синтетический пример", "child_risk_class": x.child_risk_class,
                         "child_risk": x.child_risk, "children_1000m": x.children_1000m, "hazard_source": x.hazard_source,
                         "burden": x.burden, "screen_class": x.screen_class,
                         "expected_strong_share": round(float(r.uniform(.1, .6)), 2) if gi == 1 else np.nan,
                         "lab_years": S.set_index("tj_code").lab_years.get(x.tj_code), "pollution_status": x.pollution_status,
                         "xrf_site": np.nan, "n_samples": ns, "analytes": "ДДТ и метаболиты; ГХЦГ (изомеры)",
                         "pathways_list": x.pathways_list, "data_issues": np.nan})
            prio += 1
    plan = pd.DataFrame(rows)[P0.columns]
    d = out / "source_data" / "chemicals_tj" / "pop_layer" / "sampling_plan"
    d.mkdir(parents=True, exist_ok=True)
    plan.to_csv(d / "sampling_plan.csv", index=False, encoding="utf-8-sig")
    summ = {"n_sites": len(plan), "n_samples": int(plan.n_samples.sum()),
            "by_group": {g: {"sites": int((plan.group_name == g).sum()),
                             "samples": int(plan[plan.group_name == g].n_samples.sum())} for g, _, _ in groups},
            "children_1000m_total": int(plan.children_1000m.sum()),
            "group1_expected_strong": round(float(plan[plan.group == 1].expected_strong_share.sum()), 1)}
    (d / "summary.json").write_text(json.dumps(summ, ensure_ascii=False, indent=1), encoding="utf-8")


# ----------------------------------------------------------------------------- металлы
def make_metals(out, r):
    sites0 = gpd.read_file(REAL_GPKG, layer="sites")
    pts0 = gpd.read_file(REAL_GPKG, layer="measurements_wide")
    cov0 = gpd.read_file(REAL_GPKG, layer="site_covariates")
    names = [f"Склад {i:02d}" for i in range(1, len(sites0) + 1)]
    counts = r.permutation(pts0.groupby("site_id").size().values)
    polys, cents = [], []
    for i, nm in enumerate(names):
        reg = "РРП" if i < 4 else "Хатлонская область"
        la, lo = rand_point(reg, r)
        c = gpd.GeoSeries([Point(lo, la)], crs=4326).to_crs(UTM).iloc[0]
        w, h = r.uniform(60, 160), r.uniform(40, 120)
        poly = rotate(box(c.x - w / 2, c.y - h / 2, c.x + w / 2, c.y + h / 2), r.uniform(0, 90), origin="center")
        polys.append(poly); cents.append(c)
    sites = gpd.GeoDataFrame({"site_id": names, "name": names}, geometry=polys, crs=UTM)

    elements = ["As", "Co", "Hg", "Mo", "Pb", "Rb", "Th", "U", "Zn"]
    med = {e: float(np.median(np.log(pts0[f"{e}_mgkg"].clip(lower=.01)))) for e in elements}
    rows = []
    for nm, poly, c, cnt in zip(names, polys, cents, counts):
        site_eff = r.normal(0, 0.75)
        water0 = float(np.exp(r.normal(np.log(400), 1.0)))
        for k in range(cnt):
            if r.random() < .7:
                minx, miny, maxx, maxy = poly.bounds
                while True:
                    p = Point(r.uniform(minx, maxx), r.uniform(miny, maxy))
                    if poly.contains(p):
                        break
            else:
                ang, d = r.uniform(0, 2 * np.pi), r.uniform(5, 120)
                q = poly.exterior.interpolate(r.uniform(0, poly.exterior.length))
                p = Point(q.x + d * np.cos(ang), q.y + d * np.sin(ang))
            inside = poly.contains(p)
            dist_b = poly.distance(p)
            water = max(5.0, water0 + r.normal(0, water0 * .15))
            lpb = (med["Pb"] + site_eff + 0.45 * inside - 0.004 * dist_b
                   - 0.25 * (np.log(water) - np.log(water0)) + r.normal(0, .45))
            rec = {"site_id": nm, "geometry": p, "dist_to_water_m": water, "lpb": lpb}
            for e in elements:
                if e == "Pb":
                    rec["Pb_mgkg"] = round(float(np.exp(lpb)), 2)
                else:
                    rho = {"Zn": .6, "As": .4, "Hg": .3, "Co": .2}.get(e, 0)
                    v = med[e] + rho * (lpb - med["Pb"]) + r.normal(0, .3)
                    rec[f"{e}_mgkg"] = round(float(np.exp(v)), 2)
            rows.append(rec)
    pts = gpd.GeoDataFrame(rows, geometry="geometry", crs=UTM)
    n = len(pts)
    for c in ("dist_to_building_m", "dist_to_road_m", "flow_accumulation", "flow_accumulation_max",
              "flow_direction", "duration_sec"):
        pts[c] = synth_col(pts0[c], n, r).values
    pts["sample_id"] = np.arange(1, n + 1)
    pts["sample_date"] = "2026-08-14"
    pts["depth_cm"], pts["sampler_name"] = np.nan, None
    pts["method"], pts["round_number"] = "XRF", 1
    pts = pts.drop(columns="lpb")[[c for c in pts0.columns]].to_crs(4326)

    medpb = pts.groupby("site_id").Pb_mgkg.median()
    sites["pollution_type"], sites["status"] = "heavy_metals", "active"
    sites["source"], sites["date_added"], sites["notes"] = "склад-обследование (XRF)", "2026-08-14", None
    sites["priority_composite_index"] = sites.site_id.map((medpb - medpb.min()) / (medpb.max() - medpb.min())).round(3)
    sites["priority_rank"] = sites.priority_composite_index.rank(ascending=False, method="first").astype(int)
    sites = sites[[c for c in sites0.columns]].to_crs(4326)

    cov = gpd.GeoDataFrame({c: synth_col(cov0[c], len(names), r, noise=.1).values
                            for c in cov0.columns if c not in ("site_id", "geometry", "computed_at")},
                           geometry=gpd.GeoSeries(cents, crs=UTM).to_crs(4326).values, crs=4326)
    cov.insert(0, "site_id", names)
    cov["computed_at"] = "2026-09-23"
    cov = cov[[c for c in cov0.columns]]

    g = out / "tajikistan_soil_ml_export.gpkg"
    if g.exists():
        g.unlink()
    sites.to_file(g, layer="sites", driver="GPKG")
    pts.to_file(g, layer="measurements_wide", driver="GPKG")
    cov.to_file(g, layer="site_covariates", driver="GPKG")
    return sites, pts


def make_metals_link(out, sites, pts, S, r):
    """Связка XRF-складов и ближайших участков СОЗ (как metals_link в пилоте), на синтетике."""
    from scipy.stats import spearmanr
    lab = S[S.lab_n_samples > 0].sample(9, random_state=4)
    medv = {e: pts.groupby("site_id")[f"{e}_mgkg"].median() for e in ("Pb", "As", "Hg", "Zn")}
    rows = []
    for (nm, rank, idx), (_, s) in zip(sites[["site_id", "priority_rank", "priority_composite_index"]].values, lab.iterrows()):
        rows.append({"xrf_site": nm, "tj_code": s.tj_code, "pop_site_name": s.site_name,
                     "dist_site_points_m": round(float(r.uniform(5, 300))), "metals_priority_rank": rank,
                     "metals_composite_index": idx, "xrf_points": int((pts.site_id == nm).sum()),
                     **{f"xrf_{e}_median": round(float(medv[e][nm]), 2) for e in medv},
                     "pop_samples": int(s.lab_n_samples), "pop_samples_with_coords": int(s.lab_n_samples),
                     "pop_burden_max_mgkg": round(float(max(s.ddt_group_sum_max or 0, s.hch_sum_max or 0)), 3),
                     "pop_ddt_max_mgkg": s.ddt_max, "pop_sources": s.lab_sources})
    sl = pd.DataFrame(rows)
    d = out / "source_data" / "chemicals_tj" / "pop_layer" / "metals_link"
    d.mkdir(parents=True, exist_ok=True)
    sl.to_csv(d / "site_level.csv", index=False, encoding="utf-8-sig")
    rho, p = spearmanr(sl.metals_priority_rank, sl.pop_burden_max_mgkg)
    summ = {"n_shared_sites": len(sl), "n_sites_with_pop_burden": len(sl),
            "site_level_spearman": {"n": len(sl), "rho": round(float(rho), 2), "p": round(float(p), 3)},
            "point_pairs": 0, "point_level_spearman": {}}
    (d / "summary.json").write_text(json.dumps(summ, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "public_release" / "data" / "synthetic")
    a = ap.parse_args()
    out = a.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    r = rng()
    S, P, C, burden = make_pop(out)
    R = make_child_risk(out, S, P, C, burden, r)
    make_sampling_plan(out, R, S, r)
    sites, pts = make_metals(out, r)
    make_metals_link(out, sites, pts, S, r)
    (out / "README.md").write_text(
        "# Synthetic data / Синтетические данные\n\n"
        "Generated by `tools/make_synthetic_data.py`. Same files and columns as the pilot data, no client data: "
        "names, texts and coordinates are invented, numeric values are shuffled across rows and perturbed, "
        "contamination is re-generated from synthetic features. Results on these data do not reproduce the pilot results.\n\n"
        "Создано скриптом `tools/make_synthetic_data.py`. Те же файлы и столбцы, что у данных пилота, без данных "
        "заказчика: названия, тексты и координаты придуманы, числа перемешаны между строками и изменены, загрязнение "
        "задано заново по синтетическим признакам. Результаты на этих данных не совпадают с результатами пилота.\n",
        encoding="utf-8")
    print(f"Синтетические данные: {out}")
    print(f"  участков СОЗ {len(S)}, проб {len(P)}, складов XRF {len(sites)}, точек XRF {len(pts)}")


if __name__ == "__main__":
    main()
