#!/usr/bin/env python3
"""
Загрузка синтетического набора (data/synthetic) в PostgreSQL/PostGIS, чтобы демо-стенд
работал целиком без данных заказчика: карта и таблицы металлов, приоритет, нормативы,
модель и зоны загрязнения.

Что делает:
  1. создаёт таблицы (db/schema.sql); с --reset сначала удаляет таблицы и представления проекта;
  2. загружает справочник нормативов (db/load_norms.py, открытые CSV из db/);
  3. переносит из data/synthetic/tajikistan_soil_ml_export.gpkg склады, точки, замеры
     (длинный формат) и ковариаты складов;
  4. заполняет контекст использования территорий значениями «не установлено»;
  5. пересчитывает приоритет складов, модель и зоны загрязнения обычными скриптами
     (db/rank_sites_priority.py, db/run_model_and_store.py, db/build_risk_zones.py).

Запуск (база должна существовать, расширение PostGIS доступно):
    createdb soil_ml && psql -d soil_ml -c "CREATE EXTENSION postgis"
    DATABASE_URL=postgresql://localhost:5432/soil_ml python db/load_synthetic.py --reset
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent.parent
DB_URL = os.getenv("DATABASE_URL", "postgresql://localhost:5432/soil_ml")
ELEMENTS = ["As", "Co", "Hg", "Mo", "Pb", "Rb", "Th", "U", "Zn"]
DROP = """
DROP VIEW IF EXISTS v_exceedance, v_site_norms, v_site_land_use, v_active_norms, v_site_context_current,
                    site_buffers CASCADE;
DROP TABLE IF EXISTS risk_zones, predictions, model_versions, measurements, samples, site_covariates,
                     site_context, context_attributes, norm_profile_sources, norm_profiles, soil_norms,
                     norm_sources, sites CASCADE;
"""


def run(script, *args):
    print(f"-> python db/{script} {' '.join(args)}", flush=True)
    subprocess.run([sys.executable, str(ROOT / "db" / script), *args], check=True,
                   env={**os.environ, "DATABASE_URL": DB_URL})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=ROOT / "data" / "synthetic")
    ap.add_argument("--reset", action="store_true", help="удалить таблицы проекта перед загрузкой")
    a = ap.parse_args()
    gpkg = a.data / "tajikistan_soil_ml_export.gpkg"
    engine = create_engine(DB_URL)

    with engine.begin() as c:
        if a.reset:
            c.execute(text(DROP))
        exists = c.execute(text("SELECT to_regclass('public.sites') IS NOT NULL")).scalar()
        if exists and not a.reset:
            n = c.execute(text("SELECT count(*) FROM sites")).scalar()
            if n:
                sys.exit(f"В базе уже есть {n} складов. Запустите с --reset, чтобы заменить их синтетикой.")
        if not exists:
            c.exec_driver_sql((ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))
    run("load_norms.py")

    sites = gpd.read_file(gpkg, layer="sites")
    pts = gpd.read_file(gpkg, layer="measurements_wide")
    cov = gpd.read_file(gpkg, layer="site_covariates")
    with engine.begin() as c:
        for r in sites.itertuples():
            c.execute(text("""INSERT INTO sites (site_id, name, pollution_type, contour_geom, status, source, notes,
                                                 priority_rank, priority_composite_index)
                              VALUES (:id, :name, :pt, ST_SetSRID(ST_GeomFromText(:g), 4326), :st, :src, :notes, :rk, :ix)"""),
                      dict(id=r.site_id, name=r.name, pt=r.pollution_type, g=r.geometry.wkt, st=r.status,
                           src="синтетические данные", notes="синтетический склад (data/synthetic)",
                           rk=int(r.priority_rank), ix=float(r.priority_composite_index)))
        for r in pts.itertuples():
            sid = c.execute(text("""INSERT INTO samples (site_id, geom, sample_date, depth_cm, method, duration_sec,
                                       round_number, dist_to_water_m, dist_to_building_m, dist_to_road_m,
                                       flow_accumulation, flow_accumulation_max, flow_direction)
                                    VALUES (:site, ST_SetSRID(ST_MakePoint(:x, :y), 4326), :d, NULL, :m, :dur, :rn,
                                            :w, :b, :road, :fa, :fam, :fd) RETURNING sample_id"""),
                            dict(site=r.site_id, x=r.geometry.x, y=r.geometry.y, d=r.sample_date, m=r.method,
                                 dur=_f(r.duration_sec), rn=int(r.round_number), w=_f(r.dist_to_water_m),
                                 b=_f(r.dist_to_building_m), road=_f(r.dist_to_road_m), fa=_f(r.flow_accumulation),
                                 fam=_f(r.flow_accumulation_max), fd=_i(r.flow_direction))).scalar()
            c.execute(text("INSERT INTO measurements (sample_id, substance, value) VALUES (:s, :e, :v)"),
                      [dict(s=sid, e=e, v=_f(getattr(r, f"{e}_mgkg"))) for e in ELEMENTS])
        cols = [x for x in cov.columns if x not in ("geometry", "computed_at")]
        have = set(c.execute(text("SELECT column_name FROM information_schema.columns "
                                  "WHERE table_name = 'site_covariates'")).scalars())
        cols = [x for x in cols if x in have]
        c.execute(text(f"INSERT INTO site_covariates ({', '.join(cols)}) VALUES ({', '.join(':' + x for x in cols)})"),
                  [{x: (None if pd.isna(v) else (v.item() if hasattr(v, "item") else v)) for x, v in row.items()}
                   for row in cov[cols].to_dict("records")])
    print(f"Загружено: складов {len(sites)}, точек {len(pts)}, замеров {len(pts) * len(ELEMENTS)}")

    ctx = a.data / "site_context_synthetic.csv"
    pd.DataFrame([{"site": s, "attr_code": "current_land_use", "value": "unknown", "verification": "unverified",
                   "source": "синтетические данные", "as_of": "2026-01-01", "note": ""} for s in sites.site_id]) \
        .to_csv(ctx, index=False, encoding="utf-8")
    try:
        run("load_site_context.py", str(ctx))
    finally:
        ctx.unlink(missing_ok=True)
    run("rank_sites_priority.py")
    run("run_model_and_store.py", "synthetic data")
    run("build_risk_zones.py")
    print("Готово: стенд можно запускать (python webapp/app.py)")


def _f(v):
    return None if v is None or pd.isna(v) else float(v)


def _i(v):
    return None if v is None or pd.isna(v) else int(v)


if __name__ == "__main__":
    main()
