"""
План следующей кампании отбора проб по участкам СОЗ (предложение для обсуждения).

Логика - четыре группы задач, внутри групп участки упорядочены по индексу риска для детей:
  1. Первичная оценка: нет данных о ДДТ/ГХЦГ (анализов нет или вещество в журнале не указано).
     Берутся участки с индексом риска >= «средний» или с классом скрининга «высокий»
     или с >= 1000 детей в радиусе 1 км.
  2. Контроль очистки: статус «Очищенный участок», а на сайте только анализы до очистки
     с превышением норматива.
  3. Оконтуривание: измерено >= 100 мг/кг и индекс риска для детей «высокий»/«очень высокий» -
     сетка проб для границ зоны ремедиации (до 8 участков).
  4. Обновление старых данных: последние анализы 2012-2016 гг., индекс риска >= «средний».
Схема проб на участок (по образцу кампаний 2019-2020 гг.: 2 целевые + 1 сборная):
  группа 1 - 3 пробы + 1 фоновая; группа 2 - 3 + 1 фоновая; группа 3 - 9 (сетка 3x3 вокруг очага)
  + 1 фоновая; группа 4 - 3.
Перечень веществ: ДДТ, ДДД, ДДЕ, изомеры ГХЦГ (α, β, γ, δ); на участках, где в тексте или
анализах встречались альдрин, дильдрин, гептахлор, эндосульфан - полный хлорорганический
скрининг (EPA 8081).

Выход: source_data/chemicals_tj/pop_layer/sampling_plan/sampling_plan.csv, .gpkg, summary.json
"""
import json
import shutil
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

LAYER = Path(__file__).resolve().parent.parent / "source_data" / "chemicals_tj" / "pop_layer"
OUT = LAYER / "sampling_plan"
RISK_ORDER = {"низкий": 0, "средний": 1, "высокий": 2, "очень высокий": 3}
SAMPLES = {1: 4, 2: 4, 3: 10, 4: 3}
GROUP_RU = {1: "Первичная оценка (нет данных о ДДТ/ГХЦГ)", 2: "Контроль очистки",
            3: "Оконтуривание очага для ремедиации", 4: "Обновление данных 2012–2016 гг."}
EXTRA = ["aldrin_max", "dieldrin_max", "heptachlor_max", "endosulfan_max"]


def main():
    OUT.mkdir(exist_ok=True)
    R = pd.read_csv(LAYER / "child_risk" / "child_risk_index.csv", encoding="utf-8-sig")
    T = pd.read_csv(LAYER / "pop_sites.csv", encoding="utf-8-sig", low_memory=False)
    P = pd.read_csv(LAYER / "model" / "site_predictions.csv", encoding="utf-8-sig")[["map_id", "score_used"]]
    summ = json.loads((LAYER / "model" / "summary.json").read_text(encoding="utf-8"))
    cols = ["map_id", "lab_years", "lab_sources", "xrf_site", "site_kind_text", "pesticides_mentioned_text"] + EXTRA
    R = R.drop(columns=["pollution_status"]).merge(T[cols + ["pollution_status"]], on="map_id", how="left").merge(P, on="map_id", how="left")
    R["risk_ord"] = R.child_risk_class.map(RISK_ORDER)
    R["last_year"] = R.lab_years.astype(str).str.findall(r"\d{4}").map(lambda x: max(map(int, x)) if x else np.nan)
    R["group"] = np.nan
    R["reason"] = ""

    g1 = R.burden.isna() & ((R.risk_ord >= 1) | (R.screen_class == "высокий") | (R.children_1000m >= 1000))
    R.loc[g1, "group"] = 1
    R.loc[g1, "reason"] = R[g1].apply(lambda r: "; ".join(x for x in [
        f"индекс риска для детей «{r.child_risk_class}»" if r.risk_ord >= 1 else "",
        f"скрининг: класс «{r.screen_class}»" if r.screen_class == "высокий" else "",
        f"{int(r.children_1000m)} детей в 1 км" if r.children_1000m >= 1000 else ""] if x), axis=1)
    g2 = R.group.isna() & (R.pollution_status == "Очищенный участок") & (R.burden >= 0.1)
    R.loc[g2, "group"] = 2
    R.loc[g2, "reason"] = R[g2].apply(lambda r: f"статус «очищен», но последние анализы ({r.lab_years}) — {r.burden:.1f} мг/кг", axis=1)
    g3c = R.group.isna() & (R.burden >= 100) & (R.risk_ord >= 2)
    top3 = R[g3c].sort_values("child_risk", ascending=False).head(8).index
    R.loc[top3, "group"] = 3
    R.loc[top3, "reason"] = R.loc[top3].apply(lambda r: f"{r.burden:.0f} мг/кг, риск для детей «{r.child_risk_class}», {int(r.children_1000m)} детей в 1 км", axis=1)
    g4 = R.group.isna() & (R.last_year <= 2016) & (R.risk_ord >= 1)
    R.loc[g4, "group"] = 4
    R.loc[g4, "reason"] = R[g4].apply(lambda r: f"последние анализы {int(r.last_year)} г., риск для детей «{r.child_risk_class}»", axis=1)

    plan = R[R.group.notna()].copy()
    plan["group"] = plan.group.astype(int)
    plan["group_name"] = plan.group.map(GROUP_RU)
    plan["n_samples"] = plan.group.map(SAMPLES)
    plan["full_ocp_screen"] = plan[EXTRA].fillna(0).gt(0).any(axis=1) | plan.pesticides_mentioned_text.fillna("").str.contains(
        "альдрин|дильдрин|гептахлор|эндосульфан")
    plan["analytes"] = np.where(plan.full_ocp_screen, "полный хлорорганический скрининг (EPA 8081)",
                                "ДДТ, ДДД, ДДЕ, α-, β-, γ-, δ-ГХЦГ")
    plan["expected_strong_share"] = np.where(plan.group == 1, plan.screen_class.map(summ["share_positive_by_class"]), np.nan)
    plan = plan.sort_values(["group", "child_risk"], ascending=[True, False])
    plan.insert(0, "priority", range(1, len(plan) + 1))
    keep = ["priority", "group", "group_name", "tj_code", "site_name", "region", "latitude", "longitude", "reason",
            "child_risk_class", "child_risk", "children_1000m", "hazard_source", "burden", "screen_class",
            "expected_strong_share", "lab_years", "pollution_status", "xrf_site", "n_samples", "analytes", "pathways_list"]
    aud = LAYER / "audit" / "data_quality_audit.csv"
    if aud.exists():  # замечания аудита данных по участку (db/pop_data_audit.py)
        A = pd.read_csv(aud, encoding="utf-8-sig")
        A = A[A.severity.isin(["высокая", "средняя"]) & A.tj_codes.notna()]
        notes = {}
        for r in A.itertuples():
            for c in str(r.tj_codes).split(", "):
                notes.setdefault(c, []).append(f"[{r.issue_id}] {r.description}")
        plan["data_issues"] = plan.tj_code.map(lambda c: " | ".join(notes.get(c, [])))
        keep.append("data_issues")
    plan = plan[keep]
    plan.to_csv(OUT / "sampling_plan.csv", index=False, encoding="utf-8-sig")
    import geopandas as gpd
    g = gpd.GeoDataFrame(plan, geometry=gpd.points_from_xy(plan.longitude, plan.latitude), crs=4326)
    tmp = Path(tempfile.mkdtemp()) / "sampling_plan.gpkg"
    g.to_file(tmp, layer="sampling_plan", driver="GPKG", engine="pyogrio")
    shutil.copyfile(tmp, OUT / "sampling_plan.gpkg")
    s = {"n_sites": int(len(plan)), "n_samples": int(plan.n_samples.sum()),
         "by_group": {GROUP_RU[k]: {"sites": int((plan.group == k).sum()), "samples": int(plan[plan.group == k].n_samples.sum())} for k in GROUP_RU},
         "children_1000m_total": int(plan.children_1000m.sum()),
         "group1_expected_strong": round(float(plan[plan.group == 1].expected_strong_share.sum()), 1)}
    (OUT / "summary.json").write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(s, ensure_ascii=False, indent=1))
    print(plan[["priority", "group", "tj_code", "site_name", "reason", "n_samples"]].to_string()[:5000])


if __name__ == "__main__":
    main()
