"""
Данные и модели темы «СОЗ (пестициды)» для демо-стенда.

Всё читается из файлов слоя source_data/chemicals_tj/pop_layer/ (результат db/build_pop_layer.py,
db/pop_screening_model.py, db/pop_child_risk_index.py). Модель скрининга обучается заново при
первом обращении (логистическая регрессия на ~160 участках - доли секунды), поэтому стенду не
нужен сохранённый pickle и он не зависит от версии scikit-learn на сервере.
Сеть не нужна: ковариаты новой точки считаются по локальным растрам.
"""
import json
import math
import os
import sys
import threading
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
# данные: ML_TJ_DATA_ROOT, иначе проект (данные пилота), иначе синтетический набор data/synthetic
_LAYER_REL = Path("source_data") / "chemicals_tj" / "pop_layer"
DATA = Path(os.getenv("ML_TJ_DATA_ROOT") or (ROOT if (ROOT / _LAYER_REL / "pop_sites.csv").exists()
                                              else ROOT / "data" / "synthetic"))
LAYER = DATA / _LAYER_REL
sys.path.insert(0, str(ROOT / "db"))

import pop_child_risk_index as CR  # noqa: E402
import pop_screening_model as SM  # noqa: E402
import pop_site_covariates as COVS  # noqa: E402

_lock = threading.Lock()

SUBST_RU = {"DDT": "ДДТ", "DDD": "ДДД", "DDE": "ДДЕ", "a_HCH": "α-ГХЦГ", "b_HCH": "β-ГХЦГ", "g_HCH": "γ-ГХЦГ (линдан)",
            "d_HCH": "δ-ГХЦГ", "HCH_ab": "ГХЦГ α+β", "HCH_unspecified": "ГХЦГ (изомер не указан)", "heptachlor": "гептахлор",
            "heptachlor_epoxide": "гептахлор-эпоксид", "aldrin": "альдрин", "dieldrin": "дильдрин", "endrin": "эндрин",
            "endrin_aldehyde": "эндрин-альдегид", "endosulfan_1": "эндосульфан I", "endosulfan_2": "эндосульфан II",
            "endosulfan_sulfate": "эндосульфан-сульфат", "methoxychlor": "метоксихлор",
            "pesticides_total": "пестициды (сумма)", "unspecified_1": "загрязнитель 1 (не указан)",
            "unspecified_2": "загрязнитель 2 (не указан)"}
SOURCE_RU = {"AFI126_2019": "ФГБНУ АФИ, протокол №126 (2019)", "AFI8_2021": "ФГБНУ АФИ, протокол №8 (отбор 2020)",
             "ILIM42_2017": "НПО ИЛИМ, отчёт №42 (отбор 2016)", "KUKANIEV_2012": "Институт химии им. Никитина (2012)",
             "NIKITIN_2017": "Институт химии им. Никитина (2017)", "JOURNAL": "журнал отбора проб"}
FACT_RU = {"site_kind_text": "Тип объекта", "building_state_text": "Состояние", "current_use_text": "Использование",
           "pesticide_mentioned": "Пестицид в тексте", "dist_houses_m": "До жилья, м", "dist_water_m": "До воды, м",
           "dist_school_m": "До школы/детсада, м", "dist_road_m": "До дороги, м", "dist_fields_m": "До посевов, м",
           "count_people_mention": "Людей (упоминание)", "count_children_mention": "Детей (упоминание)",
           "pesticide_quantity_kg_mention": "Пестицидов, кг (упоминание)",
           "mentions_houses_built_on_or_near_site": "Дома на участке или рядом",
           "mentions_health_complaints": "Жалобы на здоровье", "mentions_animal_deaths": "Гибель животных",
           "mentions_drinking_water": "Питьевая вода", "mentions_buried_pesticides": "Захоронение",
           "mentions_bags_containers": "Мешки/тара", "mentions_remediation": "Ремедиация"}


def _clean(v):
    if v is None or v is pd.NA:
        return None
    if isinstance(v, (float, np.floating)):
        return None if not math.isfinite(v) else round(float(v), 4)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    return v


def _records(df):
    return [{k: _clean(v) for k, v in r.items()} for r in df.to_dict("records")]


@lru_cache(maxsize=1)
def data():
    sites = pd.read_csv(LAYER / "pop_sites.csv", encoding="utf-8-sig", low_memory=False)
    samples = pd.read_csv(LAYER / "pop_samples.csv", encoding="utf-8-sig", low_memory=False)
    files = pd.read_csv(LAYER / "pop_files.csv", encoding="utf-8-sig", low_memory=False)
    facts = pd.read_csv(LAYER / "pop_text_facts.csv", encoding="utf-8-sig", low_memory=False)
    risk = pd.read_csv(LAYER / "child_risk" / "child_risk_index.csv", encoding="utf-8-sig")
    pred = pd.read_csv(LAYER / "model" / "site_predictions.csv", encoding="utf-8-sig")
    summary = json.loads((LAYER / "model" / "summary.json").read_text(encoding="utf-8"))
    metrics = pd.read_csv(LAYER / "model" / "metrics.csv", encoding="utf-8-sig")
    curve = pd.read_csv(LAYER / "model" / "selection_curve.csv", encoding="utf-8-sig")
    for df in (sites, samples, facts, risk, pred):
        df["map_id"] = pd.to_numeric(df["map_id"], errors="coerce").astype("Int64")
    files["site_id"] = pd.to_numeric(files["site_id"], errors="coerce").astype("Int64")
    for c in ("n_docs", "n_photos", "lab_n_samples"):
        sites[c] = pd.to_numeric(sites[c], errors="coerce").astype("Int64")
    return dict(sites=sites, samples=samples, files=files, facts=facts, risk=risk, pred=pred,
                summary=summary, metrics=metrics, curve=curve)


# ------------------------------- слои для карты ---------------------------------
def sites_geojson():
    d = data()
    s = d["sites"]
    keep = ["map_id", "tj_code", "site_name", "region", "pollution_status", "site_date", "latitude", "longitude",
            "lab_n_samples", "lab_sources", "lab_years", "ddt_max", "ddd_max", "dde_max", "hch_sum_max",
            "aldrin_max", "dieldrin_max", "heptachlor_max", "substances_detected", "xrf_site", "n_photos", "n_docs",
            "site_kind_text", "building_state_text", "qc_possible_duplicate", "qc_tj_code_repeated_on_map"]
    m = s[s.on_map == True][keep]  # noqa: E712
    r = d["risk"][["map_id", "child_risk_rank", "child_risk_class", "child_risk", "H", "hazard_source", "burden",
                   "E", "children_500m", "children_1000m", "people_on_site_100m", "V", "pathways_list",
                   "climate_flood_amplifier"] + [c for c in ("children_u5_1000m", "poverty", "V_pathways")
                                                 if c in d["risk"]]]
    p = d["pred"][["map_id", "screen_class", "score_used", "score_type"]]
    m = m.merge(r, on="map_id", how="left").merge(p, on="map_id", how="left")
    feats = []
    for rec in _records(m):
        lon, lat = rec.pop("longitude"), rec.pop("latitude")
        rec["site_date"] = (rec.get("site_date") or "")[:10]
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]}, "properties": rec})
    off = s[s.on_map != True][["tj_code", "site_name", "lab_n_samples", "ddt_max", "hch_sum_max"]]  # noqa: E712
    return {"type": "FeatureCollection", "features": feats, "off_map": _records(off)}


def samples_geojson():
    S = data()["samples"]
    own = S[S.lat.notna() & ~S.coord_source.fillna("").str.startswith("точка участка")]
    feats = []
    for r in own.itertuples():
        burden = np.nanmax([getattr(r, "DDT_group_sum", np.nan), getattr(r, "HCH_sum", np.nan)])
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [float(r.lon), float(r.lat)]},
                      "properties": {"sample_id": int(r.sample_id), "tj_code": r.tj_code, "map_id": _clean(r.map_id),
                                     "label": str(r.sample_label), "source": SOURCE_RU.get(r.source_id, r.source_id),
                                     "year": _clean(r.year), "DDT": _clean(r.DDT), "DDT_group_sum": _clean(r.DDT_group_sum),
                                     "HCH_sum": _clean(r.HCH_sum), "burden": _clean(burden),
                                     "coord_source": _clean(r.coord_source)}})
    return {"type": "FeatureCollection", "features": feats,
            "n_total": int(len(S)), "n_shown": len(feats)}


def site_card(map_id):
    d = data()
    s = d["sites"]
    row = s[s.map_id == map_id]
    if row.empty:
        return None
    site = row.iloc[0]
    S = d["samples"][d["samples"].map_id == map_id].copy()
    cols = [c for c in SUBST_RU if c in S.columns and S[c].notna().any()]
    samples = []
    for r in S.itertuples():
        vals = {}
        for c in cols:
            v = getattr(r, c)
            nd = getattr(r, f"{c}_nd", False) if f"{c}_nd" in S.columns else False
            if pd.notna(v) or nd is True:
                vals[SUBST_RU[c]] = "не обн." if nd is True and pd.isna(v) else _clean(v)
        samples.append({"label": str(r.sample_label), "source": SOURCE_RU.get(r.source_id, r.source_id), "year": _clean(r.year),
                        "depth_cm": _clean(r.depth_cm), "coord_source": _clean(r.coord_source), "values": vals,
                        "flags": r.flags if isinstance(r.flags, str) else ""})
    F = d["files"]
    F = F[F.site_id == map_id]
    docs = [{"name": str(x.filename).strip(), "url": x.url} for x in F[F.is_photo != True].itertuples()]  # noqa: E712
    photos = [x.url for x in F[F.is_photo == True].itertuples()][:12]  # noqa: E712
    T = d["facts"]
    T = T[(T.map_id == map_id) & T.field.isin(FACT_RU)]
    facts = [{"field": FACT_RU[x.field], "value": x.value, "snippet": x.snippet} for x in T.itertuples()]
    form = {k: _clean(site.get(k)) for k in ["square", "smell", "pit_dug", "access", "soil_cover", "agriculture_near",
                                              "animals_food", "water", "river_distance", "flood_zone", "groundwater_depth",
                                              "groundwaters_using", "surface_water_usage", "well_distance",
                                              "lived_on_site", "lived_50m", "lived_100m", "lived_500m"]}
    r = d["risk"][d["risk"].map_id == map_id]
    p = d["pred"][d["pred"].map_id == map_id]
    return {"map_id": int(map_id), "tj_code": site.tj_code, "site_name": site.site_name, "region": site.region,
            "status": site.pollution_status, "site_date": str(site.site_date)[:10],
            "problem_description": _clean(site.problem_description), "site_description": _clean(site.site_description),
            "impact": _clean(site.impact), "card_url": f"http://chemicals.tj/api/pollutions/{int(map_id)}",
            "risk": _records(r)[0] if len(r) else None, "screening": _records(p)[0] if len(p) else None,
            "samples": samples, "docs": docs, "photos": photos, "facts": facts, "form": form}


# ------------------------------- модель для новой точки ----------------------------
FORM_FIELDS = ["square", "smell", "pit_dug", "access", "soil_cover", "chemical_storage", "land", "status",
               "agriculture_near", "animals_food", "water", "surface_water", "surface_water_usage", "river_distance",
               "flood_zone", "groundwaters", "groundwaters_using", "groundwater_signs", "groundwater_depth",
               "another_water", "well_distance", "slope_position"]


@lru_cache(maxsize=1)
def form_options():
    s = data()["sites"]
    s = s[s.on_map == True]  # noqa: E712
    out = {}
    for f in FORM_FIELDS:
        vals = s[f].dropna().astype(str)
        vals = vals[~vals.isin(["Не указан", "нет данных"])].value_counts()
        out[f] = list(vals.index)
    return out


@lru_cache(maxsize=1)
def model_bundle():
    with _lock:
        T, L, targets = SM.get_data()
        y = targets[SM.FINAL_TARGET]
        fs, kind, key = SM.FINAL_PARTS[0]
        cats, nums = SM.FEATURE_SETS[fs]
        Ltr = SM.add_lag(L, L)
        model = SM.make_model(kind, cats, nums).fit(Ltr[cats + nums], y)
        p_lab = np.sort(model.predict_proba(Ltr[cats + nums])[:, 1])
        oof = np.array(json.loads((SM.OUT / "runs" / f"{key}.json").read_text())["oof"])
        q1, q2 = np.quantile(oof, [1 / 3, 2 / 3])
        R = data()["risk"]
        h_by_class = R[R.hazard_source == "анализы"].merge(data()["pred"][["map_id", "screen_class"]], on="map_id",
                                                             suffixes=("", "_p")).groupby("screen_class").H.mean().to_dict()
        rank_ref = {"children": np.sort(np.log1p(R.children_1000m.values)),
                    "people": np.sort(np.log1p(R.people_on_site_100m.values))}
        risk_q = np.quantile(R.child_risk, [.5, .8, .95])
        return dict(model=model, cats=cats, nums=nums, L=L, T=T, p_lab=p_lab, q=(q1, q2),
                    h_by_class=h_by_class, rank_ref=rank_ref, risk_q=risk_q)


def _pct(ref, v):
    return float(np.searchsorted(ref, v, side="right") / len(ref))


def predict_new(inp):
    """inp: dict с lat, lon и полями анкеты (строки как в форме chemicals.tj)."""
    B = model_bundle()
    lat, lon = float(inp["lat"]), float(inp["lon"])
    if not (36.5 <= lat <= 41.1 and 67.3 <= lon <= 75.2):
        raise ValueError("Точка вне Таджикистана")
    T = B["T"]
    row = {}
    for f in SM.CAT_FORM:
        v = (inp.get(f) or "").strip()
        row[f] = v if v else "нет данных"
    for k, m in SM.ORD.items():
        row[f"{k}_ord"] = m.get(inp.get(k), np.nan)
    for f in ["depth", "lived_on_site", "lived_50m", "lived_100m", "lived_500m"]:
        v = inp.get(f)
        v = float(v) if v not in (None, "") else np.nan
        row[f] = np.log1p(max(v, 0)) if np.isfinite(v) else np.nan
    d_all = SM.hv_km(lat, lon, T.latitude.values, T.longitude.values)
    nearest = T.iloc[int(np.argmin(d_all))]
    row["region"] = inp.get("region") or nearest.region
    row["latitude"], row["longitude"], row["map_id"] = lat, lon, -1
    cov = COVS.covariates_at(lon, lat)
    row.update(cov)
    row["pop_500m"] = np.log1p(row["pop_500m"])
    row["flowacc_max"] = np.log1p(row["flowacc_max"])
    X = SM.add_lag(B["L"], pd.DataFrame([row]))
    p = float(B["model"].predict_proba(X[B["cats"] + B["nums"]])[:, 1][0])
    score = float(np.searchsorted(B["p_lab"], p) / len(B["p_lab"]))
    q1, q2 = B["q"]
    cls = "низкий" if score <= q1 else ("средний" if score <= q2 else "высокий")
    share = data()["summary"]["share_positive_by_class"].get(cls)

    # индекс риска для детей
    H = B["h_by_class"].get(cls, np.nan)
    children = CR.children_at(lon, lat, 1000, row["region"]) or 0.0   # 0, если растров населения нет
    children500 = CR.children_at(lon, lat, 500, row["region"]) or 0.0
    people = sum(float(inp.get(k) or 0) for k in ("lived_on_site", "lived_50m", "lived_100m"))
    school = 1 if inp.get("school_near") in ("1", "true", True, "on") else 0
    W = CR.W_EXPOSURE
    E = (W["children_1km"] * _pct(B["rank_ref"]["children"], np.log1p(children)) +
         W["people_on_site_100m"] * _pct(B["rank_ref"]["people"], np.log1p(people)) +
         W["school_or_children_mentioned"] * school)
    has = CR.has
    paths = {
        "drinking_water": has(inp.get("groundwaters_using"), "Питьев") or has(inp.get("surface_water_usage"), "Пить")
                          or inp.get("drinking_water") in ("1", "on", True),
        "irrigation": has(inp.get("groundwaters_using"), "Ороше") or has(inp.get("surface_water_usage"), "Ороше"),
        "livestock": has(inp.get("animals_food"), "животн"),
        "crops": has(inp.get("agriculture_near"), "На загрязненной", "10 м"),
        "easy_access": has(inp.get("access"), "Легкий"),
        "flood_or_river": inp.get("flood_zone") == "Да" or inp.get("river_distance") == "от 0 до 300 м",
        "shallow_groundwater": has(inp.get("groundwater_depth"), "Мелководье", "Средняя"),
    }
    V_path = float(np.mean([bool(v) for v in paths.values()]))
    poverty = CR.poverty_at(lat, lon)
    V = V_path if poverty is None else CR.W_V["pathways"] * V_path + CR.W_V["poverty"] * poverty
    f = 0.05
    risk = float(np.cbrt(max(H, f) * max(E, f) * max(V, f)))
    rq = B["risk_q"]
    rcls = "низкий" if risk <= rq[0] else "средний" if risk <= rq[1] else "высокий" if risk <= rq[2] else "очень высокий"
    nb = SM.add_lag(B["L"], pd.DataFrame([row]))
    return {"lat": lat, "lon": lon, "region_used": row["region"],
            "nearest_site": {"tj_code": nearest.tj_code, "site_name": nearest.site_name, "dist_km": round(float(d_all.min()), 2)},
            "screen_probability_raw": round(p, 3), "screen_score": round(score, 3), "screen_class": cls,
            "share_strong_in_class": share,
            "covariates": {k: _clean(v) for k, v in cov.items()},
            "neighbours": {"mean_burden_mgkg": round(float(10 ** nb.nb_mean_log_burden.iloc[0]), 2),
                           "max_burden_mgkg": round(float(10 ** nb.nb_max_log_burden.iloc[0]), 2),
                           "nearest_km": round(float(nb.nb_dist_km.iloc[0]), 2)},
            "child_risk": round(risk, 3), "child_risk_class": rcls, "H": round(float(H), 3), "E": round(float(E), 3),
            "V": round(V, 3), "children_1000m": int(round(children)), "children_500m": int(round(children500)),
            "poverty": None if poverty is None else round(poverty, 3),
            "pathways": [CR.PATHWAYS[k] for k, v in paths.items() if v]}


# ------------------------------- офлайн-подложка -----------------------------------
HILLSHADE_PNG = Path(__file__).resolve().parent / "static" / "pop_hillshade.png"
DEM_TIF = ROOT / "source_data" / "Terrain150_4326.tif"


def hillshade_bounds(step=3):
    """Отмывка рельефа из локального DEM (150 м) - подложка без внешних тайлов.
    PNG строится один раз и кладётся в static/."""
    if not DEM_TIF.exists():
        return None                      # нет растра рельефа - карта без подложки
    import rasterio
    from PIL import Image
    with rasterio.open(DEM_TIF) as r:
        b = r.bounds
        h, w = r.height // step, r.width // step
        if not HILLSHADE_PNG.exists():
            z = r.read(1, out_shape=(h, w)).astype(float)
            z[~np.isfinite(z) | (np.abs(z) > 1e30)] = np.nan
            zf = np.where(np.isnan(z), np.nanmin(z), z)
            cell_y = (b.top - b.bottom) / h * 111320
            cell_x = (b.right - b.left) / w * 111320 * math.cos(math.radians((b.top + b.bottom) / 2))
            gy, gx = np.gradient(zf, cell_y, cell_x)
            slope = np.arctan(np.hypot(gx, gy) * 2.0)
            aspect = np.arctan2(-gx, gy)
            az, alt = math.radians(315), math.radians(45)
            hs = np.sin(alt) * np.cos(slope) + np.cos(alt) * np.sin(slope) * np.cos(az - aspect)
            hs = np.clip(hs, 0, 1)
            elev = np.clip((zf - 300) / 5000, 0, 1)
            # светлая гипсометрия: низины - зеленовато-серые, горы - светло-коричневые
            base = np.stack([200 + 35 * elev, 210 + 15 * elev, 195 - 10 * elev], -1)
            rgb = base * (0.55 + 0.45 * hs[..., None])
            a = np.where(np.isnan(z), 0, 255)
            img = np.dstack([rgb, a])
            # растр в градусах, карта в веб-Меркаторе: пересэмплируем строки, чтобы они шли
            # равномерно по Меркатору (иначе к центру растра сдвиг до нескольких км)
            merc = lambda lat: np.log(np.tan(np.pi / 4 + np.radians(lat) / 2))  # noqa: E731
            y_out = np.linspace(merc(b.top), merc(b.bottom), h)
            lat_out = np.degrees(2 * np.arctan(np.exp(y_out)) - np.pi / 2)
            rows = np.clip(np.round((b.top - lat_out) / (b.top - b.bottom) * (h - 1)).astype(int), 0, h - 1)
            img = img[rows]
            Image.fromarray(img.clip(0, 255).astype("uint8"), "RGBA").save(HILLSHADE_PNG, optimize=True)
    return {"south": b.bottom, "west": b.left, "north": b.top, "east": b.right}
