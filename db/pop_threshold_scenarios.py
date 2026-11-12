"""
Скрининг участков СОЗ при разных порогах «сильного загрязнения»: 10, 50 (Базельская конвенция,
предел низкого содержания СОЗ для ДДТ и суммы ГХЦГ) и 100 мг/кг (порог пилота).

Для каждого порога тот же набор признаков и та же схема, что у итоговой модели
(db/pop_screening_model.py): логистическая регрессия, 10 пространственных кластеров x 10 повторов.
Классы скрининга - трети оценки вне выборки. Для каждого класса - точность (доля загрязнённых
в классе) и полнота (доля всех загрязнённых, попавших в класс).

Выход: source_data/chemicals_tj/pop_layer/model/threshold_scenarios.csv
       source_data/chemicals_tj/pop_layer/model/threshold_classes.csv
"""
import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, roc_auc_score

import pop_screening_model as M

THRESHOLDS = [(10, "10 мг/кг (100 нормативов)"), (50, "50 мг/кг (Базельская конвенция)"), (100, "100 мг/кг (порог пилота)")]
FS = "G. Анкета + регион/координаты + соседи + ковариаты"
KIND = "Логистическая регрессия"
CLASSES = ["низкий", "средний", "высокий"]


def main():
    T, L, _ = M.get_data()
    cats, nums = M.FEATURE_SETS[FS]
    rows, cls_rows = [], []
    for th, name in THRESHOLDS:
        y = (L.burden >= th).astype(int).values
        oof = M.spatial_oof(L, y, cats, nums, KIND)
        aucs = [roc_auc_score(y, o) for o in oof]
        score = M.rank01(oof.mean(0))
        order = np.argsort(-score, kind="stable")
        found = np.cumsum(y[order])
        cls = pd.cut(score, [-1, 1 / 3, 2 / 3, 2], labels=CLASSES).astype(str)
        rows.append({"threshold_mgkg": th, "threshold": name, "n_sites": len(y), "n_positive": int(y.sum()),
                     "prevalence": round(y.mean(), 3), "auc_mean": round(np.mean(aucs), 3),
                     "auc_p05": round(np.percentile(aucs, 5), 3), "auc_p95": round(np.percentile(aucs, 95), 3),
                     "pr_auc": round(average_precision_score(y, score), 3),
                     "found_top20": int(found[19]), "random_top20": round(20 * y.mean(), 1),
                     "found_top56": int(found[55]), "random_top56": round(56 * y.mean(), 1),
                     "recall_high_or_medium": round(y[cls != "низкий"].sum() / y.sum(), 3)})
        for c in CLASSES[::-1]:
            m = cls == c
            pos = y[m].sum()
            cls_rows.append({"threshold_mgkg": th, "class": c, "n_sites": int(m.sum()), "n_positive": int(pos),
                             "precision": round(pos / m.sum(), 3), "recall": round(pos / y.sum(), 3),
                             "median_burden_of_positives_mgkg": round(float(np.median(L.burden.values[m & (y == 1)])), 1) if pos else None})
        print(name, rows[-1])
    pd.DataFrame(rows).to_csv(M.OUT / "threshold_scenarios.csv", index=False, encoding="utf-8-sig")
    C = pd.DataFrame(cls_rows)
    C.to_csv(M.OUT / "threshold_classes.csv", index=False, encoding="utf-8-sig")
    print(C.to_string())


if __name__ == "__main__":
    main()
