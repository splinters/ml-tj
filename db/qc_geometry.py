"""
QC геометрии контуров складов (п.3 ТЗ - "уточнить границы"): валидность,
самопересечения, вложенность буферов, похожие/задвоенные полигоны.

Это ЦИФРОВАЯ проверка того, что уже занесено в БД - системная версия
того, как в своё время вручную нашли дубликат Кушониен=Шахринав (тогда -
сверкой координат центроидов на глаз). Здесь проверяются ВСЕ C(10,2)=45
пар полигонов на пересечение/похожесть, а не только те, что показались
подозрительными визуально.

НЕ подменяет полевое уточнение границ на местности - для этого нужны
либо повторные геодезические замеры, либо анализ фактических проб
(см. build_risk_zones.py, который выводит контур загрязнения из уже
имеющихся точечных замеров).

Запуск: python db/qc_geometry.py
"""
import os
import pandas as pd
import geopandas as gpd
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.getenv("DATABASE_URL", "postgresql://soil_ml:soil_ml_dev_password@localhost:5432/soil_ml")
engine = create_engine(DB_URL)

ISSUES = []


def flag(site_id, issue):
    ISSUES.append((site_id, issue))
    print(f"  [!] {site_id}: {issue}")


def main():
    with engine.connect() as conn:
        sites = gpd.read_postgis(
            "SELECT site_id, name, contour_geom, "
            "ST_IsValid(contour_geom) AS is_valid, "
            "ST_IsValidReason(contour_geom) AS invalid_reason, "
            "ST_SRID(contour_geom) AS srid, "
            "ST_Area(contour_geom::geography) AS area_m2, "
            "ST_NPoints(contour_geom) AS n_vertices "
            "FROM sites ORDER BY site_id",
            conn, geom_col="contour_geom")

    print("=" * 100)
    print("1. ВАЛИДНОСТЬ И БАЗОВЫЕ ХАРАКТЕРИСТИКИ ПОЛИГОНОВ")
    print("=" * 100)
    print(sites[["site_id", "is_valid", "srid", "area_m2", "n_vertices"]]
          .assign(area_m2=lambda d: d.area_m2.round(1)).to_string(index=False))

    for _, row in sites.iterrows():
        if not row.is_valid:
            flag(row.site_id, f"НЕВАЛИДНАЯ геометрия: {row.invalid_reason}")
        if row.srid != 4326:
            flag(row.site_id, f"SRID={row.srid}, ожидался 4326")
        if row.area_m2 < 50:
            flag(row.site_id, f"площадь подозрительно мала: {row.area_m2:.1f} м² (не точка ли это по ошибке?)")
        if row.area_m2 > 200_000:
            flag(row.site_id, f"площадь подозрительно велика: {row.area_m2:.0f} м² (~{row.area_m2/10000:.1f} га)")

    print("\n" + "=" * 100)
    print("2. ПОХОЖИЕ/ЗАДВОЕННЫЕ ПОЛИГОНЫ (IoU по всем парам)")
    print("=" * 100)
    print("(тот же приём, каким в своё время нашли Кушониен=Шахринав, но по всем 45 парам сразу)")
    any_dup = False
    for i in range(len(sites)):
        for j in range(i + 1, len(sites)):
            a, b = sites.iloc[i], sites.iloc[j]
            if not (a.is_valid and b.is_valid):
                continue
            inter = a.contour_geom.intersection(b.contour_geom).area
            union = a.contour_geom.union(b.contour_geom).area
            iou = inter / union if union > 0 else 0
            if iou > 0.05:  # порог с запасом - точный дубликат даст IoU=1.0
                any_dup = True
                flag(f"{a.site_id} / {b.site_id}", f"пересечение полигонов, IoU={iou:.3f}")
    if not any_dup:
        print("  Дубликатов/значимых пересечений не найдено (все IoU < 0.05).")

    print("\n" + "=" * 100)
    print("3. ВЛОЖЕННОСТЬ БУФЕРОВ (contour ⊂ buf_50m ⊂ buf_100m)")
    print("=" * 100)
    with engine.connect() as conn:
        nesting = pd.read_sql("""
            SELECT s.site_id,
                   ST_Contains(sb.buf_50m, s.contour_geom) AS contour_in_50,
                   ST_Contains(sb.buf_100m, sb.buf_50m) AS buf50_in_100
            FROM sites s JOIN site_buffers sb ON sb.site_id = s.site_id
            ORDER BY s.site_id
        """, conn)
    print(nesting.to_string(index=False))
    for _, row in nesting.iterrows():
        if not row.contour_in_50 or not row.buf50_in_100:
            flag(row.site_id, "нарушена вложенность буферов - не должно происходить, т.к. буферы это "
                               "VIEW от contour_geom; сигнал о более глубокой проблеме (см. schema.sql)")

    print("\n" + "=" * 100)
    print(f"ИТОГО: {len(ISSUES)} замечаний" if ISSUES else "ИТОГО: замечаний не найдено, геометрия чистая.")
    print("=" * 100)
    if ISSUES:
        for site_id, issue in ISSUES:
            print(f"  - {site_id}: {issue}")
    print("""
ВАЖНО: это проверка ЦИФРОВОЙ корректности того, что уже занесено в БД
(самопересечения, дубли, вложенность буферов) - НЕ полевое уточнение
границ по факту загрязнения на местности. Для содержательного уточнения
контура загрязнения (не административной границы склада, а того, где
реально сосредоточено загрязнение) - см. db/build_risk_zones.py.
""")


if __name__ == "__main__":
    main()
