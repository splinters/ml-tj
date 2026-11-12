"""Общий код блокнотов ML TJ: загрузка выгрузки GPKG и построение признаков.

Блокноты работают без БД и без интернета: единственный вход - файл
tajikistan_soil_ml_export.gpkg (создаётся db/export_geopackage.py).
Путь можно задать переменной окружения ML_TJ_GPKG.
"""
import os
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import r2_score, mean_squared_error

_DEFAULT = Path(__file__).parent / "data" / "tajikistan_soil_ml_export.gpkg"
if not _DEFAULT.exists():  # выгрузка в корне проекта стенда
    _DEFAULT = Path(__file__).resolve().parent.parent / "tajikistan_soil_ml_export.gpkg"
if not _DEFAULT.exists():  # открытая версия: синтетический набор (или ML_TJ_DATA_ROOT)
    _DEFAULT = Path(os.getenv("ML_TJ_DATA_ROOT") or Path(__file__).resolve().parent.parent / "data" / "synthetic") \
        / "tajikistan_soil_ml_export.gpkg"
GPKG = Path(os.getenv("ML_TJ_GPKG", _DEFAULT))
ELEMENTS = ["As", "Co", "Hg", "Mo", "Pb", "Rb", "Th", "U", "Zn"]
BASE = ["signed_dist_boundary_m", "dist_centroid_m", "zone_code"]
FEATURES = BASE + ["dist_to_water_m", "soil_ph"]
UTM = 32642  # UTM 42N, метры; CRS выгрузки - EPSG:4326


def load():
    """Возвращает sites, points (169 точек: геометрия + признаки + значения), covariates."""
    if "synthetic" in GPKG.as_posix():
        print("Синтетические данные (data/synthetic): значения придуманы и не совпадают с результатами пилота. "
              "/ Synthetic data: invented values, results differ from the pilot.")
    sites = gpd.read_file(GPKG, layer="sites").to_crs(UTM).set_index("site_id")
    pts = gpd.read_file(GPKG, layer="measurements_wide").to_crs(UTM)
    cov = gpd.read_file(GPKG, layer="site_covariates").set_index("site_id")
    rows = []
    for r in pts.itertuples():
        poly, pt = sites.geometry[r.site_id], r.geometry
        inside, d = poly.contains(pt), poly.distance(pt)
        zone = 3 if inside else (2 if d <= 50 else (1 if d <= 100 else 0))
        rows.append((-d if inside else d, poly.centroid.distance(pt), zone, inside))
    f = pd.DataFrame(rows, columns=BASE + ["inside"], index=pts.index)
    pts = pts.join(f)
    pts["soil_ph_raw"] = pts.site_id.map(cov.soil_ph)
    pts["soil_ph"] = pts.soil_ph_raw.fillna(pts.soil_ph_raw.mean())  # как в продакшн-коде: среднее
    pts["site_name"] = pts.site_id.map(sites.name)
    return sites, pts, cov


def target(pts, element):
    """log(концентрация) минус медиана log по складу."""
    l = np.log(pts[f"{element}_mgkg"].values)
    return l - pd.Series(l, index=pts.index).groupby(pts.site_id).transform("median").values


def rf(seed=0):
    return RandomForestRegressor(n_estimators=300, min_samples_leaf=3, random_state=seed)


def lowo_predict(pts, y, features, seed=0):
    """Leave-One-Warehouse-Out: обучение на 9 складах, предсказание на десятом."""
    pred = np.zeros(len(y))
    for site in pts.site_id.unique():
        te = (pts.site_id == site).values
        pred[te] = rf(seed).fit(pts.loc[~te, features].values, y[~te]).predict(pts.loc[te, features].values)
    return pred


def lowo_r2(pts, y, features, seed=0):
    p = lowo_predict(pts, y, features, seed)
    return r2_score(y, p), float(np.sqrt(mean_squared_error(y, p)))
