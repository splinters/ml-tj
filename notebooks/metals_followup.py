"""
Дополнительные расчёты по модели Pb (ответы на вопросы):
 1. ранжирование точек внутри склада без измерений на этом складе;
 2. абсолютный уровень по k быстрым замерам XRF ("якорь" медианы склада);
 3. доля различий между складами (ICC) и пространственная связь между складами;
 4. оценка числа складов: разброс качества по складам, расчёт мощности, кривая обучения.
Данные - экспорт GeoPackage (mltj.load), без БД. Выход: печать + metals_followup.json рядом.
"""
import json
import random
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import r2_score

import mltj

SEEDS = range(5)
rng = np.random.default_rng(0)
sites, pts, cov = mltj.load()
y = mltj.target(pts, "Pb")                       # log(Pb) - медиана log(Pb) склада
logpb = np.log(pts.Pb_mgkg.values)
sid = pts.site_id.values
ids = list(pd.unique(sid))
P = np.mean([mltj.lowo_predict(pts, y, mltj.FEATURES, seed=s) for s in SEEDS], axis=0)
out = {"n_warehouses": len(ids), "n_points": len(pts), "lowo_r2_relative": round(r2_score(y, P), 3)}

# 1. ранжирование внутри склада: прогноз строится без единого измерения этого склада
rho, cap = [], []
for s in ids:
    m = sid == s
    rho.append(stats.spearmanr(P[m], logpb[m]).statistic)
    k = int(np.ceil(m.sum() * .25))
    cap.append(len(set(np.argsort(-P[m])[:k]) & set(np.argsort(-logpb[m])[:k])) / k)
out["within_spearman_mean"] = round(float(np.mean(rho)), 3)
out["within_spearman_by_wh"] = [round(float(r), 2) for r in rho]
out["within_spearman_positive_share"] = round(float(np.mean(np.array(rho) > 0)), 2)
out["top_quarter_capture_mean"] = round(float(np.mean(cap)), 3)

# 2. абсолютный уровень: якорь = медиана k случайных замеров (k=0: медиана других складов)
abs_r2 = {}
for k in range(0, 6):
    vals = []
    for rep in range(200):
        pred, obs = [], []
        for s in ids:
            m = np.where(sid == s)[0]
            anchor_idx = rng.choice(m, size=k, replace=False) if k else []
            anchor = np.median(logpb[anchor_idx]) if k else np.median([np.median(logpb[sid == o]) for o in ids if o != s])
            rest = np.setdiff1d(m, anchor_idx)
            pred += list(anchor + P[rest] - (np.median(P[anchor_idx]) if k else 0))
            obs += list(logpb[rest])
        vals.append(r2_score(obs, pred))
    abs_r2[k] = round(float(np.mean(vals)), 3)
out["absolute_r2_by_anchor_points"] = abs_r2

# 3. ICC log(Pb) и связь между складами
g = pd.DataFrame({"s": sid, "v": logpb})
n_i = g.groupby("s").size(); m0 = (len(g) - (n_i ** 2).sum() / len(g)) / (len(ids) - 1)
msb = (n_i * (g.groupby("s").v.mean() - g.v.mean()) ** 2).sum() / (len(ids) - 1)
msw = ((g.v - g.groupby("s").v.transform("mean")) ** 2).sum() / (len(g) - len(ids))
s2b = max((msb - msw) / m0, 0)
out["icc_logpb"] = round(float(s2b / (s2b + msw)), 3)
c = sites.to_crs(32642).geometry.centroid
cx = dict(zip(sites.index if "site_id" not in sites else sites.site_id, zip(c.x, c.y)))
med = g.groupby("s").v.median()
pairs = [(a, b) for i, a in enumerate(ids) for b in ids[i + 1:]]
dist = np.array([np.hypot(cx[a][0] - cx[b][0], cx[a][1] - cx[b][1]) / 1000 for a, b in pairs])
diff = np.array([abs(med[a] - med[b]) for a, b in pairs])
r_obs = stats.spearmanr(dist, diff).statistic
perm = []
for _ in range(2000):
    sh = dict(zip(ids, rng.permutation(med.loc[ids].values)))
    perm.append(stats.spearmanr(dist, [abs(sh[a] - sh[b]) for a, b in pairs]).statistic)
out["between_wh_distance_km_min_median_max"] = [round(float(x), 1) for x in (dist.min(), np.median(dist), dist.max())]
out["mantel_rho_dist_vs_median_diff"] = round(float(r_obs), 3)
out["mantel_p"] = round(float(np.mean(np.array(perm) >= r_obs)), 3)

# 4. число складов: качество по каждому складу как единица наблюдения
r2_wh = []
for s in ids:
    m = sid == s
    r2_wh.append(1 - np.sum((y[m] - P[m]) ** 2) / np.sum(y[m] ** 2))   # относительно медианы склада (=0)
r2_wh = np.array(r2_wh)
sd = float(r2_wh.std(ddof=1)); mu = float(r2_wh.mean())
out["per_wh_r2_mean_sd"] = [round(mu, 3), round(sd, 3)]
z_a, z_b = 1.96, 0.84
out["n_wh_detect_r2_gt0_power80"] = int(np.ceil(((z_a + z_b) * sd / mu) ** 2)) if mu > 0 else None
out["n_wh_ci_halfwidth"] = {h: int(np.ceil((1.96 * sd / h) ** 2)) for h in (0.10, 0.05)}
# кривая обучения (больше повторов)
F = mltj.FEATURES; lc = {}
for k in (3, 5, 7, 9):
    vals = []
    for rep in range(20):
        rnd = random.Random(rep * 100 + k); pred = np.zeros(len(y))
        for te_s in ids:
            tr_s = rnd.sample([s for s in ids if s != te_s], k)
            tr, te = np.isin(sid, tr_s), sid == te_s
            pred[te] = RandomForestRegressor(n_estimators=150, min_samples_leaf=3, random_state=rep, n_jobs=-1).fit(
                pts.loc[tr, F].values, y[tr]).predict(pts.loc[te, F].values)
        vals.append(r2_score(y, pred))
    lc[k] = [round(float(np.mean(vals)), 3), round(float(np.std(vals)), 3)]
out["learning_curve_k_mean_sd"] = lc
ks = np.array(list(lc)); ms = np.array([v[0] for v in lc.values()])
b, a = np.polyfit(1 / ks, ms, 1)        # R2(k) ~ a + b/k
out["learning_curve_fit_a_b"] = [round(float(a), 3), round(float(b), 3)]
out["extrapolated_r2"] = {n: round(float(a + b / n), 3) for n in (9, 20, 30, 50)}
print(json.dumps(out, ensure_ascii=False, indent=1))
Path(__file__).with_name("metals_followup.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
