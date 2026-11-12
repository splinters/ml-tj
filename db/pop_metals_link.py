"""
Связка СОЗ (пестициды, chemicals.tj) и тяжёлых металлов (XRF, 10 складов проекта) на общих площадках.

1. Уровень площадки: приоритет по металлам (sites.priority_composite_index из основной базы)
   против максимума загрязнения пестицидами (burden = max(ДДТ+ДДД+ДДЕ, сумма ГХЦГ)).
2. Уровень точки: для каждой пробы СОЗ с собственными координатами - ближайшая точка XRF
   той же площадки (не дальше 50 м) и её Pb, As, Hg, Zn; ранговая корреляция.
Вход: tajikistan_soil_ml_export.gpkg (экспорт основной базы), source_data/chemicals_tj/pop_layer/
Выход: source_data/chemicals_tj/pop_layer/metals_link/site_level.csv, point_pairs.csv, summary.json
"""
import json
import re
import unicodedata
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

ROOT = Path(__file__).resolve().parent.parent
LAYER = ROOT / "source_data" / "chemicals_tj" / "pop_layer"
OUT = LAYER / "metals_link"
GPKG = Path(__import__("os").getenv("SOIL_GPKG", str(ROOT / "tajikistan_soil_ml_export.gpkg")))
METALS = ["Pb_mgkg", "As_mgkg", "Hg_mgkg", "Zn_mgkg"]
MAX_PAIR_M = 50


def norm(s):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", str(s))).strip().lower().replace("ё", "е")


def main():
    OUT.mkdir(exist_ok=True)
    X = gpd.read_file(GPKG, layer="measurements_wide").to_crs(32642)
    st = gpd.read_file(GPKG, layer="sites")
    X["key"] = X.site_id.map(norm)
    st["key"] = st.site_id.map(norm)
    T = pd.read_csv(LAYER / "pop_sites.csv", encoding="utf-8-sig", low_memory=False)
    S = pd.read_csv(LAYER / "pop_samples.csv", encoding="utf-8-sig", low_memory=False)
    S["burden"] = S[["DDT_group_sum", "HCH_sum"]].max(axis=1)
    xs = T[T.xrf_site.notna()][["xrf_site", "xrf_site_dist_m", "tj_code", "map_id", "site_name"]].copy()
    xs["key"] = xs.xrf_site.map(norm)

    site_rows, pairs = [], []
    for r in xs.itertuples():
        s_all = S[S.map_id == r.map_id]
        xx = X[X.key == r.key]
        srow = st[st.key == r.key]
        own = s_all[s_all.lat.notna() & ~s_all.coord_source.fillna("").str.startswith("точка участка")]
        site_rows.append({
            "xrf_site": r.xrf_site, "tj_code": r.tj_code, "pop_site_name": r.site_name, "dist_site_points_m": r.xrf_site_dist_m,
            "metals_priority_rank": int(srow.priority_rank.iloc[0]) if len(srow) else None,
            "metals_composite_index": round(float(srow.priority_composite_index.iloc[0]), 3) if len(srow) else None,
            "xrf_points": len(xx), **{f"xrf_{m.split('_')[0]}_median": round(float(xx[m].median()), 2) if len(xx) else None for m in METALS},
            "pop_samples": len(s_all), "pop_samples_with_coords": len(own),
            "pop_burden_max_mgkg": round(float(s_all.burden.max()), 2) if s_all.burden.notna().any() else None,
            "pop_ddt_max_mgkg": round(float(s_all.DDT.max()), 2) if s_all.DDT.notna().any() else None,
            "pop_sources": "; ".join(sorted(set(s_all.source_id))) if len(s_all) else ""})
        if len(own) and len(xx):
            g = gpd.GeoDataFrame(own, geometry=gpd.points_from_xy(own.lon, own.lat), crs=4326).to_crs(32642)
            for p in g.itertuples():
                d = xx.distance(p.geometry)
                i = d.idxmin()
                if d[i] <= MAX_PAIR_M:
                    pairs.append({"xrf_site": r.xrf_site, "tj_code": r.tj_code, "pop_sample": p.sample_label, "pop_source": p.source_id,
                                  "pop_burden_mgkg": p.burden, "pop_DDT_mgkg": p.DDT, "xrf_sample_id": int(xx.loc[i, "sample_id"]),
                                  "dist_m": round(float(d[i]), 1), **{m: xx.loc[i, m] for m in METALS}})
    SL = pd.DataFrame(site_rows).sort_values("metals_priority_rank")
    PP = pd.DataFrame(pairs)
    SL.to_csv(OUT / "site_level.csv", index=False, encoding="utf-8-sig")
    PP.to_csv(OUT / "point_pairs.csv", index=False, encoding="utf-8-sig")
    out = {"n_shared_sites": int(len(SL)), "n_sites_with_pop_burden": int(SL.pop_burden_max_mgkg.notna().sum())}
    v = SL.dropna(subset=["pop_burden_max_mgkg", "metals_composite_index"])
    if len(v) >= 4:
        rho, p = spearmanr(v.metals_composite_index, v.pop_burden_max_mgkg)
        out["site_level_spearman"] = {"n": int(len(v)), "rho": round(float(rho), 2), "p": round(float(p), 3)}
    pv = PP.dropna(subset=["pop_burden_mgkg"])
    out["point_pairs"] = int(len(pv))
    out["point_level_spearman"] = {}
    for m in METALS:
        if len(pv) >= 6:
            rho, p = spearmanr(pv.pop_burden_mgkg, pv[m])
            out["point_level_spearman"][m.split("_")[0]] = {"rho": round(float(rho), 2), "p": round(float(p), 3)}
    (OUT / "summary.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=1))
    print(SL.to_string())
    print(PP.to_string())


if __name__ == "__main__":
    main()
