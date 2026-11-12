"""
Скрининг опасности участков СОЗ: можно ли по сведениям, доступным ДО лабораторного
анализа (анкета chemicals.tj, текст карточки, местоположение, данные соседних
обследованных участков), отличить сильно загрязнённые участки от остальных.

Вход:  source_data/chemicals_tj/pop_layer/pop_sites.csv, pop_samples.csv
Выход: source_data/chemicals_tj/pop_layer/model/
  metrics.csv           AUC / PR-AUC / Brier по целям, наборам признаков и моделям
  selection_curve.csv   сколько сильно загрязнённых участков найдено при обследовании первых K
  oof_predictions.csv   прогноз «вне выборки» для каждого обследованного участка
  site_predictions.csv  прогноз для всех 192 участков (финальная модель на всех данных)
  feature_importance.csv
  selection_curve.png

Целевая переменная (по сводной выборке проб):
  burden = max по пробам участка из (ДДТ+ДДД+ДДЕ) и (сумма изомеров ГХЦГ), мг/кг.
  Основная цель: burden >= 100 мг/кг (в 1000 раз выше норматива 0,1 мг/кг).
Проверка: пространственная кросс-валидация - участки группируются в 10 кластеров по
координатам, каждый кластер по очереди откладывается целиком (соседи откладываемого
участка не попадают в обучение); 10 повторов с разной разбивкой на кластеры.

Не используются как признаки (утечка результата анализа): всё, что посчитано из проб,
число документов, статус «очищен», дата обследования (совпадает с кампанией и лабораторией).
"""
import os
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

_PROJECT = Path(__file__).resolve().parent.parent
_LAYER = Path("source_data") / "chemicals_tj" / "pop_layer"
# данные: ML_TJ_DATA_ROOT, иначе проект (данные пилота), иначе синтетический набор открытой версии
ROOT = Path(os.getenv("ML_TJ_DATA_ROOT") or (_PROJECT if (_PROJECT / _LAYER / "pop_sites.csv").exists()
                                             else _PROJECT / "data" / "synthetic")) / _LAYER
OUT = ROOT / "model"
N_CLUSTERS, N_REPEATS, K_NEIGHB = 10, 10, 5

ORD = {
    "square": {"<100 м²": 1, "100 - 500 м²": 2, "500 - 1000 м²": 3, "1000 - 5000 м²": 4,
               "5000 - 10000 м² (1 гектар)": 5, "1 гектар - 5 гектар": 6, ">5 гектаров": 7},
    "river_distance": {"от 0 до 300 м": 1, "от 300 м до 1 км": 2, "от 1 км до 5 км": 3, "> 5 км": 4},
    "groundwater_depth": {"Мелководье <2 м": 1, "Средняя 2 - 10 м": 2, "Глубоко 10 - 50 м": 3, "Очень глубоко >50 м": 4},
    "well_distance": {"В пределах 50 м от загрязнения": 1, "В пределах 100 м от загрязнения": 2,
                      "В пределах 500 м от загрязняения": 3, "В пределах 500 м от загряздения": 3,
                      "Поблизости нет колодца": 4},
}
CAT_FORM = ["pit_dug", "smell", "land", "soil_cover", "access", "chemical_storage", "water", "surface_water",
            "flood_zone", "groundwaters", "groundwater_signs", "slope_position", "agriculture_near", "animals_food",
            "status", "another_water"]
NUM_FORM = ["depth", "lived_on_site", "lived_50m", "lived_100m", "lived_500m"] + [f"{k}_ord" for k in ORD]
CAT_TEXT = ["site_kind_text", "building_state_text"]
FLAG_TEXT = ["mentions_bags_containers", "mentions_buried_pesticides", "mentions_stains", "mentions_smell",
             "mentions_livestock_access", "mentions_crops_on_or_near", "mentions_houses_built_on_or_near_site",
             "mentions_remediation", "mentions_irrigation", "mentions_drinking_water", "mentions_health_complaints"]
NUM_TEXT = ["dist_houses_m_text", "dist_water_m_text", "pesticide_quantity_kg_max_text", "count_people_max_text"]
LOC_CAT, LOC_NUM = ["region"], ["latitude", "longitude"]
LAG = ["nb_mean_log_burden", "nb_max_log_burden", "nb_dist_km"]
COV = ["ph", "elev_m", "slope_deg", "pop_500m", "flowacc_max", "bio1_temp_c", "bio12_precip_mm"]  # db/pop_site_covariates.py

FEATURE_SETS = {
    "A. Анкета": (CAT_FORM, NUM_FORM),
    "B. Анкета + текст карточки": (CAT_FORM + CAT_TEXT, NUM_FORM + FLAG_TEXT + NUM_TEXT),
    "C. Анкета + текст + регион/координаты": (CAT_FORM + CAT_TEXT + LOC_CAT, NUM_FORM + FLAG_TEXT + NUM_TEXT + LOC_NUM),
    "D. C + соседние обследованные участки": (CAT_FORM + CAT_TEXT + LOC_CAT, NUM_FORM + FLAG_TEXT + NUM_TEXT + LOC_NUM + LAG),
    "E. Только регион/координаты + соседи": (LOC_CAT, LOC_NUM + LAG),
    "F. Открытые ковариаты (pH, рельеф, население, сток, климат)": ([], COV),
    "G. Анкета + регион/координаты + соседи + ковариаты": (CAT_FORM + LOC_CAT, NUM_FORM + LOC_NUM + LAG + COV),
}


def load():
    T = pd.read_csv(ROOT / "pop_sites.csv", encoding="utf-8-sig", low_memory=False)
    T = T[T.on_map == True].copy()  # noqa: E712
    S = pd.read_csv(ROOT / "pop_samples.csv", encoding="utf-8-sig", low_memory=False)
    S = S[S.map_id.notna()].copy()
    S["burden"] = S[["DDT_group_sum", "HCH_sum"]].max(axis=1)
    g = S.groupby("map_id").agg(burden=("burden", "max"), ddt=("DDT", "max"), n_samples=("sample_id", "size"))
    T = T.merge(g, left_on="map_id", right_index=True, how="left")
    cov = pd.read_csv(ROOT / "site_covariates_pop.csv", encoding="utf-8-sig").drop(columns=["tj_code"])
    T = T.merge(cov, on="map_id", how="left")
    T["pop_500m"] = np.log1p(T["pop_500m"])
    T["flowacc_max"] = np.log1p(T["flowacc_max"])
    for k, m in ORD.items():
        T[f"{k}_ord"] = T[k].map(m)
    for c in FLAG_TEXT:
        T[c] = T[c].map(lambda v: 1.0 if v is True or str(v) == "True" else 0.0) if c in T else 0.0
    for c in NUM_TEXT + ["depth"] + NUM_FORM[1:5]:
        T[c] = pd.to_numeric(T[c], errors="coerce")
        T[c] = np.log1p(T[c].clip(lower=0))
    for c in CAT_FORM + CAT_TEXT + LOC_CAT:
        T[c] = T[c].fillna("нет данных").astype(str)
    return T


def hv_km(la1, lo1, la2, lo2):
    r = np.radians
    h = np.sin(r(la2 - la1) / 2) ** 2 + np.cos(r(la1)) * np.cos(r(la2)) * np.sin(r(lo2 - lo1) / 2) ** 2
    return 2 * 6371 * np.arcsin(np.sqrt(h))


def add_lag(train, test):
    """Признаки «соседей»: считаются только по обучающим участкам."""
    out = []
    lb = np.log10(train.burden.clip(lower=0.01).values)
    for _, r in test.iterrows():
        d = hv_km(r.latitude, r.longitude, train.latitude.values, train.longitude.values)
        d = np.where(train.map_id.values == r.map_id, np.inf, d)
        idx = np.argsort(d)[:K_NEIGHB]
        w = 1 / (d[idx] + 1)
        out.append((np.sum(w * lb[idx]) / np.sum(w), lb[idx].max(), d[idx[0]]))
    test = test.copy()
    test[LAG] = out
    return test


def make_model(kind, cats, nums):
    pre = ColumnTransformer([
        ("cat", OneHotEncoder(handle_unknown="ignore", min_frequency=3), cats)] * bool(cats) + [
        ("num", Pipeline([("imp", SimpleImputer(strategy="median", add_indicator=True)), ("sc", StandardScaler())]), nums),
    ])
    if kind == "Логистическая регрессия":
        clf = LogisticRegression(C=0.2, class_weight="balanced", max_iter=2000)
    else:
        clf = RandomForestClassifier(n_estimators=200, min_samples_leaf=3, max_features="sqrt",
                                     class_weight="balanced_subsample", random_state=0, n_jobs=-1)
    return Pipeline([("pre", pre), ("clf", clf)])


def spatial_oof(L, y, cats, nums, kind):
    oof = np.zeros((N_REPEATS, len(L)))
    for rep in range(N_REPEATS):
        groups = KMeans(N_CLUSTERS, n_init=5, random_state=rep).fit_predict(L[["latitude", "longitude"]].values)
        for gidx in np.unique(groups):
            te, tr = groups == gidx, groups != gidx
            Ltr, Lte = L[tr], L[te]
            if set(LAG) & set(nums):
                Ltr = add_lag(L[tr], L[tr])   # для обучения - соседи из того же обучающего набора (без самого участка)
                Lte = add_lag(L[tr], L[te])
            if len(set(y[tr])) < 2:
                oof[rep, te] = y[tr].mean()
                continue
            m = make_model(kind, cats, nums).fit(Ltr[cats + nums], y[tr])
            oof[rep, te] = m.predict_proba(Lte[cats + nums])[:, 1]
    return oof


def selection(order_scores, y):
    o = np.argsort(-order_scores, kind="stable")
    return np.cumsum(y[o])


def run_combo(T, L, targets, tname, fs, kind):
    y = targets[tname]
    cats, nums = FEATURE_SETS[fs]
    oof = spatial_oof(L, y, cats, nums, kind)
    aucs = [roc_auc_score(y, o) for o in oof]
    p = oof.mean(0)
    row = {"target": tname, "prevalence": round(y.mean(), 3), "n_sites": len(y), "features": fs, "model": kind,
           "auc_mean": round(np.mean(aucs), 3), "auc_p05": round(np.percentile(aucs, 5), 3),
           "auc_p95": round(np.percentile(aucs, 95), 3), "pr_auc": round(average_precision_score(y, p), 3),
           "brier": round(brier_score_loss(y, p), 3),
           "brier_base": round(brier_score_loss(y, np.full(len(y), y.mean())), 3)}
    return row, p


def get_data():
    T = load()
    L = T[T.burden.notna()].reset_index(drop=True)
    targets = {
        "burden>=100 мг/кг (основная)": (L.burden >= 100).astype(int).values,
        "burden>=10 мг/кг": (L.burden >= 10).astype(int).values,
        "ДДТ>=10 мг/кг": (L.ddt >= 10).fillna(False).astype(int).values,
    }
    return T, L, targets


def main(only=None):
    """Каждая комбинация (цель, признаки, модель) считается отдельно и дописывается
    в model/runs/ - прогон можно прерывать и продолжать."""
    import time
    OUT.mkdir(exist_ok=True)
    (OUT / "runs").mkdir(exist_ok=True)
    T, L, targets = get_data()
    t0 = time.time()
    for ti, tname in enumerate(targets):
        for fi, fs in enumerate(FEATURE_SETS):
            for ki, kind in enumerate(("Логистическая регрессия", "Случайный лес")):
                key = f"{ti}_{fi}_{ki}"
                f = OUT / "runs" / f"{key}.json"
                if f.exists() or (only and key not in only):
                    continue
                if time.time() - t0 > 100:
                    print("пауза: время шага исчерпано, запустите снова"); return
                row, p = run_combo(T, L, targets, tname, fs, kind)
                f.write_text(json.dumps({"row": row, "oof": p.tolist()}, ensure_ascii=False))
                print(key, row["auc_mean"], row["features"], row["model"], row["target"])
    rows = [json.loads(x.read_text())["row"] for x in sorted((OUT / "runs").glob("*.json"))]
    pd.DataFrame(rows).to_csv(OUT / "metrics.csv", index=False, encoding="utf-8-sig")
    print("готово:", len(rows))




# ============================ итоговая модель ==================================
# Выбор по результатам прогона (metrics.csv, цель burden>=100 мг/кг):
#   «Анкета» + логистическая регрессия  - AUC 0,647 (перестановочный тест p=0,011)
#   «Регион/координаты + соседи» + случайный лес - AUC 0,648 (p<=0,03)
#   Текст карточки качество не улучшил (переобучение на 30+ флагах при 151 участке) - не используется.
#   Открытые ковариаты (pH, рельеф, население, сток, климат) дали прирост: отдельно AUC 0,683.
# Итог: одна логистическая регрессия на наборе G (анкета + регион/координаты + соседи +
# ковариаты): AUC 0,701, перестановочный тест p=0,004. Перебиралось 7 наборов признаков,
# поэтому лучшая оценка немного оптимистична - это указано в отчёте.
FINAL_TARGET = "burden>=100 мг/кг (основная)"
FINAL_PARTS = [("G. Анкета + регион/координаты + соседи + ковариаты", "Логистическая регрессия", "0_6_0")]
# Участки протокола №126 с полевыми кодами tj77xx, которых нет на карте, и участки карты с
# теми же населёнными пунктами (по названию; на карте анализов у них нет). Используются как
# дополнительная проверка: модель обучалась без этих значений.
PROTOCOL_TO_MAP = {"TJ-7721": ("TJ-7765", "уверенно"), "TJ-7723": ("TJ-7775", "уверенно"),
                   "TJ-7725": ("TJ-7771", "уверенно"), "TJ-7726": ("TJ-7768", "уверенно"),
                   "TJ-7727": ("TJ-7773", "уверенно"), "TJ-7728": ("TJ-7769", "уверенно"),
                   "TJ-7730": ("TJ-7772", "уверенно"), "TJ-7732": ("TJ-7523", "уверенно"),
                   "TJ-7724": ("TJ-7770 или TJ-7776", "неоднозначно"), "TJ-7722": ("TJ-7770 / TJ-7774 / TJ-7776", "неоднозначно"),
                   "TJ-7729": ("TJ-7777", "предположительно"), "TJ-7731": ("TJ-7522", "уверенно")}


def rank01(p):
    return (np.argsort(np.argsort(p)) + 0.5) / len(p)


def finalize():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    T, L, targets = get_data()
    y = targets[FINAL_TARGET]
    oofs = [np.array(json.loads((OUT / "runs" / f"{k}.json").read_text())["oof"]) for _, _, k in FINAL_PARTS]
    oof = np.mean([rank01(p) for p in oofs], axis=0)
    auc = roc_auc_score(y, oof)
    # кривая отбора
    order = np.argsort(-oof, kind="stable")
    found = np.cumsum(y[order])
    k = np.arange(1, len(y) + 1)
    curve = pd.DataFrame({"k_sites_surveyed": k, "found_model": found, "found_random_expected": np.round(k * y.mean(), 2),
                          "found_ideal": np.minimum(k, y.sum())})
    curve.to_csv(OUT / "selection_curve.csv", index=False, encoding="utf-8-sig")
    fig, ax = plt.subplots(figsize=(7.5, 4.8), dpi=150)
    ax.plot(k, curve.found_ideal, color="#9aa5b1", lw=1, ls=":", label="Идеальный отбор")
    ax.plot(k, found, color="#1f6fb2", lw=2.2, label="Отбор по модели (проверка вне выборки)")
    ax.plot(k, curve.found_random_expected, color="#c0392b", lw=1.6, ls="--", label="Случайный отбор (ожидание)")
    for kk in (20, 30):
        ax.annotate(f"{kk} участков: {found[kk-1]} против {k[kk-1]*y.mean():.0f}", xy=(kk, found[kk - 1]),
                    xytext=(kk + 12, found[kk - 1] - 6), fontsize=8, arrowprops=dict(arrowstyle="-", color="#555"))
    ax.set_xlabel("Сколько участков обследовано (по убыванию оценки модели)")
    ax.set_ylabel("Найдено сильно загрязнённых\n(ДДТ-группа или ГХЦГ ≥ 100 мг/кг)")
    ax.set_title(f"Скрининг участков СОЗ: кривая отбора (AUC={auc:.2f}, {len(y)} участков, {int(y.sum())} сильно загрязнённых)", fontsize=9)
    ax.grid(alpha=.3); ax.legend(fontsize=8, loc="lower right"); fig.tight_layout()
    fig.savefig(OUT / "selection_curve.png"); plt.close(fig)

    # финальные модели на всех обследованных участках -> прогноз для всех 192
    preds, imp = [], []
    for fs, kind, _ in FINAL_PARTS:
        cats, nums = FEATURE_SETS[fs]
        Ltr = add_lag(L, L) if set(LAG) & set(nums) else L
        Tall = add_lag(L, T) if set(LAG) & set(nums) else T
        m = make_model(kind, cats, nums).fit(Ltr[cats + nums], y)
        preds.append(m.predict_proba(Tall[cats + nums])[:, 1])
        names = m.named_steps["pre"].get_feature_names_out()
        if kind == "Логистическая регрессия":
            w = m.named_steps["clf"].coef_[0]
        else:
            w = m.named_steps["clf"].feature_importances_
        imp += [{"model": f"{fs} / {kind}", "feature": n, "weight": round(float(v), 4)} for n, v in zip(names, w)]
    pd.DataFrame(imp).sort_values(["model", "weight"], key=lambda s: s.abs() if pd.api.types.is_numeric_dtype(s) else s,
                                  ascending=[True, False]).to_csv(OUT / "feature_importance.csv", index=False, encoding="utf-8-sig")
    # шкала: ранг прогноза среди обследованных участков (0..1), классы по третям
    ref = np.mean([rank01(p[T.burden.notna().values]) for p in preds], axis=0)
    raw = np.mean([np.searchsorted(np.sort(p[T.burden.notna().values]), p) / T.burden.notna().sum() for p in preds], axis=0)
    out = T[["map_id", "tj_code", "site_name", "region", "latitude", "longitude", "burden"]].copy()
    out["screen_score"] = np.round(raw, 3)
    lab = T.burden.notna().values
    out.loc[lab, "screen_score_oof"] = np.round(oof, 3)
    out["score_used"] = np.where(lab, out.screen_score_oof, out.screen_score)
    q1, q2 = np.quantile(oof, [1 / 3, 2 / 3])
    out["screen_class"] = pd.cut(out.score_used, [-1, q1, q2, 2], labels=["низкий", "средний", "высокий"]).astype(str)
    out["score_type"] = np.where(lab, "проверка вне выборки (участок обследован)", "прогноз (анализов нет)")
    out["measured_class"] = np.where(lab, np.where(T.burden >= 100, "≥100 мг/кг", "<100 мг/кг"), "")
    out.to_csv(OUT / "site_predictions.csv", index=False, encoding="utf-8-sig")
    rate = out[lab].groupby("screen_class").apply(lambda d: (d.burden >= 100).mean(), include_groups=False)
    summary = {"target": FINAL_TARGET, "n_sites_labelled": int(lab.sum()), "n_positive": int(y.sum()),
               "auc_oof": round(auc, 3), "found_top20": int(found[19]), "random_top20": round(20 * y.mean(), 1),
               "found_top30": int(found[29]), "random_top30": round(30 * y.mean(), 1),
               "share_positive_by_class": {k: round(v, 3) for k, v in rate.items()},
               "n_unlabelled_predicted": int((~lab).sum()),
               "unlabelled_by_class": out[~lab].screen_class.value_counts().to_dict()}
    # дополнительная проверка на участках, связанных по названию
    allT = pd.read_csv(ROOT / "pop_sites.csv", encoding="utf-8-sig", low_memory=False)
    chk = []
    for pc, (mc, conf) in PROTOCOL_TO_MAP.items():
        a = allT[allT.tj_code == pc]
        b = out[out.tj_code == mc] if conf == "уверенно" else out.iloc[0:0]
        burden = np.nanmax([a.ddt_group_sum_max.iloc[0], a.hch_sum_max.iloc[0]]) if len(a) else np.nan
        chk.append({"protocol_code": pc, "protocol_place": a.site_name.iloc[0] if len(a) else "", "map_code": mc,
                    "match": conf, "measured_burden_mgkg": burden,
                    "measured_class": "≥100 мг/кг" if burden >= 100 else "<100 мг/кг",
                    "predicted_class": b.screen_class.iloc[0] if len(b) else "", "predicted_score": b.score_used.iloc[0] if len(b) else None})
    pd.DataFrame(chk).to_csv(OUT / "check_linked_sites.csv", index=False, encoding="utf-8-sig")
    summary["linked_sites_check"] = chk
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["final"]:
        finalize()
    else:
        main(sys.argv[1:] or None)
