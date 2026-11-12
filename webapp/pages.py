"""
Информационные страницы и центр экспорта демо-стенда.

Маршруты:
    /about              методика и ограничения
    /priority           приоритизация складов (ранг, индекс, зоны)
    /models             журнал версий моделей (таблица model_versions)
    /sources            происхождение данных и внешние сервисы карты
    /export             центр экспорта (список файлов)
    /export/<key>       скачивание одного файла
    /export/all_csv.zip все CSV одним архивом

Все данные читаются из БД проекта при каждом запросе; новых таблиц нет.
Доступ к БД - через current_app.config["ENGINE"] (задаётся в app.py).
Внешних ресурсов (CDN, шрифты) страницы не используют.
"""
import io
import os
import time
import zipfile
from datetime import datetime

import numpy as np
import pandas as pd
from flask import (Blueprint, Response, abort, current_app, render_template,
                   request, send_file)
from sqlalchemy import text

from i18n import render, tr, nm, vl

bp = Blueprint("pages", __name__)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
GPKG_PATH = os.path.join(ROOT, "tajikistan_soil_ml_export.gpkg")
if not os.path.exists(GPKG_PATH):   # открытая версия: синтетический набор (или ML_TJ_DATA_ROOT)
    GPKG_PATH = os.path.join(os.getenv("ML_TJ_DATA_ROOT") or os.path.join(ROOT, "data", "synthetic"),
                             "tajikistan_soil_ml_export.gpkg")

RISK_LEVELS = ["low", "medium", "high", "critical"]
RISK_COLORS = {"low": "#2ecc71", "medium": "#f1c40f", "high": "#e67e22", "critical": "#e74c3c"}

# --------------------------------------------------------------------------
# SQL
# --------------------------------------------------------------------------
LATEST_ZONES_VERSION_SQL = """
    SELECT model_version_id FROM model_versions
    WHERE target_variant = 'measured_composite_within_site_quartiles'
    ORDER BY trained_at DESC LIMIT 1
"""

PRIORITY_SQL = """
SELECT s.site_id, s.name, s.priority_rank, s.priority_composite_index,
       raw.pb, raw.as_ AS "as", raw.hg, raw.zn, COALESCE(raw.n_points, 0) AS n_points
FROM sites s
LEFT JOIN (
    SELECT sam.site_id,
           PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY m.value) FILTER (WHERE m.substance = 'Pb') AS pb,
           PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY m.value) FILTER (WHERE m.substance = 'As') AS as_,
           PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY m.value) FILTER (WHERE m.substance = 'Hg') AS hg,
           PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY m.value) FILTER (WHERE m.substance = 'Zn') AS zn,
           count(DISTINCT sam.sample_id) AS n_points
    FROM samples sam JOIN measurements m ON m.sample_id = sam.sample_id
    WHERE m.below_lod = FALSE
    GROUP BY sam.site_id
) raw ON raw.site_id = s.site_id
ORDER BY s.priority_rank NULLS LAST, s.name
"""

ZONE_AREA_SQL = f"""
SELECT rz.site_id, rz.risk_level, SUM(ST_Area(rz.geom::geography)) AS area_m2
FROM risk_zones rz
WHERE rz.model_version_id = ({LATEST_ZONES_VERSION_SQL})
GROUP BY rz.site_id, rz.risk_level
"""

MODELS_SQL = """
SELECT model_version_id, trained_at, target_substance, target_variant,
       n_sites, n_points, naive_r2, lowo_r2, lowo_rmse,
       array_to_string(features_used, ', ') AS features, trigger_reason
FROM model_versions
ORDER BY trained_at DESC, model_version_id DESC
"""

SUMMARY_SQL = """
SELECT (SELECT count(*) FROM sites) AS n_sites,
       (SELECT count(*) FROM samples) AS n_points,
       (SELECT count(DISTINCT substance) FROM measurements) AS n_substances,
       (SELECT count(*) FROM measurements) AS n_measurements,
       (SELECT lowo_r2 FROM model_versions
         WHERE target_substance = 'Pb' AND lowo_r2 IS NOT NULL
         ORDER BY trained_at DESC LIMIT 1) AS lowo_r2_pb
"""

# выгрузки CSV: ключ -> (название, описание, SQL | None, функция постобработки | None)
SITES_CSV_SQL = """
SELECT s.site_id, s.name, s.pollution_type, s.status, s.source, s.date_added,
       s.priority_rank, s.priority_composite_index,
       ST_Y(ST_Centroid(s.contour_geom)) AS centroid_lat,
       ST_X(ST_Centroid(s.contour_geom)) AS centroid_lon,
       ST_Area(s.contour_geom::geography) / 10000.0 AS area_ha
FROM sites s ORDER BY s.priority_rank NULLS LAST, s.site_id
"""

MEASUREMENTS_LONG_SQL = """
SELECT sam.sample_id, sam.site_id, sam.sample_date, sam.method, sam.depth_cm,
       ST_Y(sam.geom) AS lat, ST_X(sam.geom) AS lon,
       sam.dist_to_water_m, sam.dist_to_building_m, sam.dist_to_road_m,
       m.substance, m.value, m.unit, m.below_lod, m.lod_value
FROM samples sam JOIN measurements m ON m.sample_id = sam.sample_id
ORDER BY sam.site_id, sam.sample_id, m.substance
"""

COVARIATES_SQL = "SELECT * FROM site_covariates ORDER BY site_id"

# --- нормативы (db/schema_norms.sql, загрузка: python db/load_norms.py) ---
NORMS_REF_SQL = """
SELECT n.substance, n.value, n.basis, n.land_use, n.soil_class, n.norm_type, n.verification,
       n.note, s.source_id, s.title AS source_title, s.tier, s.version_date, s.url
FROM soil_norms n JOIN norm_sources s ON s.source_id = n.source_id
ORDER BY n.substance, (s.tier = 'national'), s.source_id, n.land_use, n.soil_class
"""

ACTIVE_PROFILE_SQL = "SELECT profile_id, land_use, description FROM norm_profiles WHERE is_active"

ACTIVE_PROFILE_SOURCES_SQL = """
SELECT ps.priority, s.source_id, s.title, s.tier, s.version_date, s.url
FROM norm_profile_sources ps
JOIN norm_sources s ON s.source_id = ps.source_id
JOIN norm_profiles p ON p.profile_id = ps.profile_id AND p.is_active
ORDER BY ps.priority
"""

ACTIVE_NORMS_SQL = """
SELECT substance, value, source_id, norm_type, land_use, verification, applies_to
FROM v_active_norms ORDER BY substance
"""

MEASURED_SUBSTANCES_SQL = """
SELECT substance, count(*) AS n_values,
       count(*) FILTER (WHERE below_lod) AS n_below_lod
FROM measurements GROUP BY substance ORDER BY substance
"""

SITE_EXCEEDANCE_SQL = """
SELECT s.site_id, s.name, e.norm_substance,
       count(*) AS n,
       count(*) FILTER (WHERE e.status = 'exceeds') AS n_exceeds,
       count(*) FILTER (WHERE e.status IN ('below_lod', 'lod_above_norm')) AS n_censored,
       max(e.ratio) AS max_ratio
FROM v_exceedance e JOIN sites s ON s.site_id = e.site_id
GROUP BY s.site_id, s.name, e.norm_substance
ORDER BY s.name, e.norm_substance
"""

EXCEEDANCES_CSV_SQL = """
SELECT e.site_id, s.name AS site_name, e.sample_id, e.norm_substance, e.measured,
       e.norm_value, e.ratio, e.status, e.source_id, e.norm_type, e.verification
FROM v_exceedance e JOIN sites s ON s.site_id = e.site_id
ORDER BY e.site_id, e.sample_id, e.norm_substance
"""

MODELS_CSV_SQL = MODELS_SQL

# --- контекст использования территорий (db/schema_site_context.sql, загрузка: python db/load_site_context.py) ---
CONTEXT_ATTRS_SQL = "SELECT attr_code, label_ru, value_type, unit FROM context_attributes ORDER BY sort_order"
CONTEXT_CURRENT_SQL = """
SELECT c.site_id, s.name, c.attr_code, c.value_text, c.value_num, c.verification,
       c.source, c.source_ref, c.as_of, c.note
FROM v_site_context_current c JOIN sites s ON s.site_id = c.site_id
"""
SITES_NAMES_SQL = "SELECT site_id, name FROM sites ORDER BY name"
SITE_LAND_USE_SQL = "SELECT site_id, land_use, land_use_origin FROM v_site_land_use"
CONTEXT_CSV_SQL = """
SELECT s.site_id, s.name AS site_name, c.attr_code, a.label_ru, c.value_text, c.value_num, a.unit,
       c.verification, c.source, c.source_ref, c.as_of, c.note
FROM site_context c
JOIN sites s ON s.site_id = c.site_id
JOIN context_attributes a ON a.attr_code = c.attr_code
ORDER BY s.name, a.sort_order, c.as_of DESC
"""

# значения признаков -> подписи
CONTEXT_VALUE_RU = {
    "agricultural": "сельскохозяйственное", "residential": "жилое", "commercial": "коммерческое",
    "industrial": "промышленное", "unused": "не используется", "mixed": "смешанное",
    "unknown": "неизвестно", "yes": "да", "no": "нет",
    "current_owner": "нынешнему собственнику", "agri_authority": "Минсельхозу / Комитету по земледелию",
    "by_privatization_status": "по статусу приватизации", "other": "иное",
}
CONTEXT_VALUE_EN = {
    "agricultural": "agricultural", "residential": "residential", "commercial": "commercial",
    "industrial": "industrial", "unused": "unused", "mixed": "mixed",
    "unknown": "unknown", "yes": "yes", "no": "no",
    "current_owner": "to the current owner", "agri_authority": "Ministry of Agriculture / Land Committee",
    "by_privatization_status": "by privatization status", "other": "other",
}
CONTEXT_VERIFICATION = {
    "customer_doc": (("документ заказчика", "client document"), "#27ae60"),
    "customer_stated": (("сообщение заказчика", "client statement"), "#2c3e50"),
    "estimate_imagery": (("оценка по снимкам, не подтверждена", "estimate from imagery, unconfirmed"), "#e67e22"),
    "unverified": (("не проверено", "unverified"), "#7f8c8d"),
}


def _verification():
    return {k: (tr(v[0]), v[1]) for k, v in CONTEXT_VERIFICATION.items()}


def _ctx_values():
    return CONTEXT_VALUE_EN if tr("ru", "en") == "en" else CONTEXT_VALUE_RU

GEOJSON_EXPORTS = {
    "sites_geojson": (
        ("Контуры складов (GeoJSON)", "Warehouse outlines (GeoJSON)"),
        ("10 полигонов с рангом приоритета и индексом, WGS84.", "10 polygons with priority rank and index, WGS84."),
        "SELECT site_id, name, status, priority_rank, priority_composite_index, "
        "contour_geom AS geometry FROM sites ORDER BY site_id",
    ),
    "samples_geojson": (
        ("Точки замеров (GeoJSON)", "Measurement points (GeoJSON)"),
        ("Точки отбора, расстояния до воды/зданий/дорог. Значения веществ - в CSV.", "Sampling points with distances to water/buildings/roads. Substance values are in the CSV files."),
        "SELECT sample_id, site_id, sample_date, method, dist_to_water_m, "
        "dist_to_building_m, dist_to_road_m, geom AS geometry FROM samples ORDER BY sample_id",
    ),
    "risk_zones_geojson": (
        ("Зоны загрязнения (GeoJSON)", "Contamination zones (GeoJSON)"),
        ("Зоны внутри площадок по квартилям композитного индекса (последний прогон).", "Within-site zones by composite index quartiles (latest run)."),
        f"SELECT zone_id, site_id, risk_level, geom AS geometry FROM risk_zones "
        f"WHERE model_version_id = ({LATEST_ZONES_VERSION_SQL}) ORDER BY site_id, zone_id",
    ),
}

CSV_EXPORTS = {
    "sites_csv": (("Склады (CSV)", "Warehouses (CSV)"), ("Реестр складов: статус, ранг приоритета, центроид, площадь, га.", "Warehouse register: status, priority rank, centroid, area in ha."), SITES_CSV_SQL),
    "priority_csv": (("Приоритизация (CSV)", "Prioritization (CSV)"), ("Ранг, композитный индекс, медианы Pb/As/Hg/Zn, число точек.", "Rank, composite index, Pb/As/Hg/Zn medians, number of points."), PRIORITY_SQL),
    "measurements_long_csv": (("Замеры, длинный формат (CSV)", "Measurements, long format (CSV)"), ("Одна строка = вещество в точке; признаки ниже предела обнаружения помечены.", "One row = one substance at one point; values below the detection limit are flagged."), MEASUREMENTS_LONG_SQL),
    "measurements_wide_csv": (("Замеры, широкий формат (CSV)", "Measurements, wide format (CSV)"), ("Одна строка = точка; колонки - элементы, <элемент>_below_lod - флаги LOD.", "One row = one point; columns are elements, <element>_below_lod are LOD flags."), None),
    "covariates_csv": (("Ковариаты складов (CSV)", "Warehouse covariates (CSV)"), ("Почва, рельеф, климат (текущий и проекции), геология, гидрология.", "Soil, terrain, climate (current and projections), geology, hydrology."), COVARIATES_SQL),
    "model_versions_csv": (("Журнал версий моделей (CSV)", "Model version log (CSV)"), ("Все прогоны: метрики LOWO, признаки, причина пересчёта.", "All runs: LOWO metrics, features, reason for the run."), MODELS_CSV_SQL),
    "norms_csv": (("Справочник нормативов (CSV)", "Soil standards reference (CSV)"), ("Все нормативы по источникам: значение, форма, землепользование, статус сверки.", "All standards by source: value, form, land use, verification status."), NORMS_REF_SQL),
    "site_context_csv": (("Контекст использования территорий (CSV)", "Land-use context (CSV)"), ("Землепользование, жильё, поля и каналы рядом со складами: значение, источник, степень проверки, история.", "Land use, housing, fields and canals near the warehouses: value, source, verification level, history."), CONTEXT_CSV_SQL),
    "exceedances_csv": (("Сравнение замеров с нормативом (CSV)", "Measurements vs. standards (CSV)"), ("Каждый замер: норматив активного профиля, кратность, статус (превышение / в норме / ниже предела обнаружения).", "Each measurement: the active-profile standard, ratio, status (exceeds / within / below detection limit)."), EXCEEDANCES_CSV_SQL),
}

# выгрузки, которым нужен справочник нормативов (python db/load_norms.py)
NORMS_EXPORTS = {"norms_csv", "exceedances_csv", "site_context_csv"}

# --------------------------------------------------------------------------
# Источники данных (статический справочник; уровень = по точке / по складу)
# status: model - в продакшн-модели; tested - протестирован, не включён;
#         stored - собран и хранится без теста; base - основа проекта
# --------------------------------------------------------------------------
SOURCES = [
    dict(name="XRF-замеры, 9 элементов (Mo, U, Rb, Th, Pb, As, Hg, Zn, Co)", name_en="XRF measurements, 9 elements (Mo, U, Rb, Th, Pb, As, Hg, Zn, Co)", provider_en="Project warehouse survey (field sites.source)", level_en="per point", note_en="Portable XRF; values below the detection limit are stored with the below_lod flag and LOD/2 in the model.", license_en="project data",
         provider="Обследование складов проекта (поле sites.source)", level="по точке",
         status="base", where="samples, measurements",
         note="Портативный XRF; значения ниже предела обнаружения хранятся с флагом below_lod и LOD/2 в модели.",
         license="данные проекта"),
    dict(name="Контуры складов", name_en="Warehouse outlines", provider_en="Project site register (IDs from chemicals.tj where they match)", level_en="warehouse", note_en="The outline is stored once; 50/100 m buffers are built by a VIEW query.", license_en="project data",
         provider="Реестр площадок проекта (ID по chemicals.tj, где совпадают)", level="склад",
         status="base", where="sites.contour_geom",
         note="Контур хранится один раз; буферы 50/100 м строятся VIEW-запросом.",
         license="данные проекта"),
    dict(name="Расстояние до воды", name_en="Distance to water", level_en="per point", note_en="Feature of the production model.",
         provider="OpenStreetMap (Overpass)", level="по точке", status="model",
         where="samples.dist_to_water_m", note="Признак продакшн-модели.",
         license="ODbL"),
    dict(name="pH почвы", name_en="Soil pH", level_en="warehouse", note_en="Feature of the production model; 250 m raster.",
         provider="ISRIC SoilGrids (phh2o)", level="склад", status="model",
         where="site_covariates.soil_ph", note="Признак продакшн-модели; растр 250 м.",
         license="CC BY 4.0"),
    dict(name="Население в радиусе 500 м", name_en="Population within 500 m", level_en="warehouse", note_en="Covariate of the heavy-metals model. LOWO on its own: below the baseline model. Shown on the heavy-metals map; for children and the POPs map WorldPop R2025A is used (rows below).",
         provider="WorldPop 2020", level="склад", status="tested",
         where="site_covariates.population_500m",
         note="Признак модели металлов. LOWO отдельно: ниже базовой модели. Показан на карте металлов; для детей и карты СОЗ используется WorldPop R2025A (строки ниже).",
         license="CC BY 4.0"),
    dict(name="Землепользование", name_en="Land use", level_en="warehouse", note_en="The class barely varies between warehouses; SHAP contribution near zero.",
         provider="ESA WorldCover", level="склад", status="tested",
         where="site_covariates.worldcover_class",
         note="Класс почти не варьирует между складами; SHAP-вклад около нуля.",
         license="CC BY 4.0"),
    dict(name="Расстояние до зданий", name_en="Distance to buildings", provider_en="Microsoft Building Footprints, OSM as fallback", level_en="per point", note_en="Excluded: floor-value artefact in the first version, uneven completeness of the source.",
         provider="Microsoft Building Footprints, запасной вариант OSM", level="по точке",
         status="tested", where="samples.dist_to_building_m",
         note="Исключён: ошибка floor-значений в первой версии, полнота источника неравномерна.",
         license="ODbL"),
    dict(name="Расстояние до дорог", name_en="Distance to roads", level_en="per point", note_en="Does not improve LOWO in combination with water and pH.",
         provider="OpenStreetMap", level="по точке", status="tested",
         where="samples.dist_to_road_m", note="Не улучшает LOWO в связке с водой и pH.",
         license="ODbL"),
    dict(name="Высота и уклон", name_en="Elevation and slope", level_en="warehouse", note_en="Elevation alone: strongly negative LOWO R² (trees do not extrapolate).", license_en="depends on the DEM used",
         provider="Open-Topo-Data", level="склад", status="tested",
         where="site_covariates.elevation_m, slope_deg",
         note="Высота отдельно: LOWO R² сильно отрицательный (деревья не экстраполируют).",
         license="зависит от выбранного DEM"),
    dict(name="Пути стока (flow accumulation, direction)", name_en="Flow paths (flow accumulation, direction)", provider_en="150 m DEM, processed in ArcGIS Pro", level_en="per point and per warehouse", note_en="Adding it to the water+pH pair lowers LOWO R².", license_en="project data",
         provider="DEM 150 м, обработка в ArcGIS Pro", level="по точке и по складу",
         status="tested", where="samples.flow_*, site_covariates.flow_*",
         note="Добавление к паре вода+pH снижает LOWO R².",
         license="данные проекта"),
    dict(name="Климат, годовые нормы и помесячные", name_en="Climate, annual and monthly normals", provider_en="WorldClim (historical normals)", level_en="warehouse", note_en="Annual normals tested; monthly columns not tested (24 features for 10 warehouses).", license_en="publisher terms (WorldClim)",
         provider="WorldClim (историческая норма)", level="склад", status="tested",
         where="site_covariates.temp_*, precip_*, *_month_*",
         note="Годовые нормы протестированы; помесячные колонки не тестировались (24 признака на 10 складов).",
         license="условия издателя (WorldClim)"),
    dict(name="Климатические проекции 2021-2100", name_en="Climate projections 2021-2100", provider_en="IPSL-CM6A-LR, ssp245 and ssp585 scenarios", level_en="warehouse", note_en="For future-risk scenarios; not used as a predictor of current contamination.", license_en="publisher terms",
         provider="IPSL-CM6A-LR, сценарии ssp245 и ssp585", level="склад", status="stored",
         where="site_covariates.future_*",
         note="Для сценариев будущего риска; не используются как предиктор текущего загрязнения.",
         license="условия издателя"),
    dict(name="Геология (литология)", name_en="Geology (lithology)", provider_en="GLiM, 0.5° grid", level_en="warehouse", note_en="Coarse resolution relative to warehouse size; negative LOWO.", license_en="publisher terms",
         provider="GLiM, сетка 0.5°", level="склад", status="tested",
         where="site_covariates.lithology_class",
         note="Грубое разрешение относительно размера склада; LOWO отрицательный.",
         license="условия издателя"),
    dict(name="Слой гидрографии на карте", name_en="Hydrography layer on the map", level_en="country",
         note_en="Full file (34 MB) is used for distance to water; the maps show a lightweight copy (lines simplified to ~30 m, water bodies under ~1 ha dropped, 1.4 MB gzip), rebuilt automatically when the source changes.",
         provider="OpenStreetMap (Overpass)", level="страна", status="stored",
         where="webapp/static/hydrology.geojson, hydrology_web.geojson.gz",
         note="Полный файл (34 МБ) используется для расстояния до воды; на картах показывается облегчённая копия (линии упрощены до ~30 м, водоёмы меньше ~1 га убраны, 1,4 МБ в gzip), пересобирается автоматически при обновлении исходника.",
         license="ODbL"),
    # --- пилот СОЗ (пестициды) и индекс риска для детей ---
    dict(name="Участки СОЗ, анализы ДДТ и ГХЦГ, анкеты участков", name_en="POPs sites, DDT and HCH analyses, site survey forms",
         provider="chemicals.tj (карта и прикреплённые файлы)", provider_en="chemicals.tj (map and attached files)",
         level="участок", level_en="site", status="base",
         where="source_data/chemicals_tj/pop_layer/",
         note="192 участка, 169 с анализами. Основа модели скрининга и индекса риска для детей.",
         note_en="192 sites, 169 with analyses. Basis of the screening model and the child risk index.",
         license="данные заказчика", license_en="client data"),
    dict(name="Дети до 18 и до 5 лет", name_en="Children under 18 and under 5",
         provider="WorldPop R2025A, оценка 2025 г., сетка 100 м (вариант constrained)", provider_en="WorldPop R2025A, 2025 estimate, 100 m grid (constrained)",
         level="участок (радиус 500 м и 1 км)", level_en="site (500 m and 1 km radius)", status="index",
         where="source_data/worldpop_r2025a/ → child_risk_index.csv (children_*)",
         note="Экспозиция E в индексе риска. Население распределено только по населённым пикселям (по снимкам застройки); вне них стоит служебное значение «нет данных» (−99999), это не ошибка. Слои на карте СОЗ и в разделе «Климат и население».",
         note_en="Exposure E in the risk index. Population is allocated only to settled pixels (from building footprints); outside them the raster holds the no-data value (−99999), which is not an error. Layers on the POPs map and in Climate and population.",
         license="CC BY 4.0"),
    dict(name="Население 2025, дети 5–17 лет, женщины 15–49 лет", name_en="Population 2025, children 5–17, women 15–49",
         provider="WorldPop R2025A, суммы возрастных групп", provider_en="WorldPop R2025A, sums of age groups",
         level="страна, 100 м", level_en="country, 100 m", status="stored",
         where="geoserver_rasters/worldpop_2025/ (GeoServer)",
         note="Собираются скриптом geoserver/build_population_layers.py; публикуются в GeoServer для внешних потребителей (WMS). В модели не используются.",
         note_en="Built by geoserver/build_population_layers.py; published in GeoServer for external users (WMS). Not used in the model.",
         license="CC BY 4.0"),
    dict(name="Индекс относительного благосостояния (RWI)", name_en="Relative Wealth Index (RWI)",
         provider="Meta Data for Good (через HDX), сетка ~2,4 км", provider_en="Meta Data for Good (via HDX), ~2.4 km grid",
         level="участок (радиус 2 км)", level_en="site (2 km radius)", status="index",
         where="source_data/tjk_relative_wealth_index.csv → child_risk_index.csv (rwi, poverty)",
         note="Бедность в уязвимости V (вес 0,3). Ошибка оценки близка к разбросу по ячейкам: годится для сравнения групп участков, не соседних.",
         note_en="Poverty in vulnerability V (weight 0.3). Estimation error is close to the spread across cells: suitable for comparing groups of sites, not neighbours.",
         license="CC BY-NC 4.0"),
    dict(name="Доля детей по областям", name_en="Child share by region",
         provider="UNFPA и Агентство по статистике, 2019 (через HDX)", provider_en="UNFPA and Agency on Statistics, 2019 (via HDX)",
         level="область", level_en="region", status="stored",
         where="source_data/tjk_admpop_adm1_2019.csv",
         note="Запасной вариант, если растров WorldPop R2025A нет: население × доля детей области.",
         note_en="Fallback when WorldPop R2025A rasters are absent: population × regional child share.",
         license="условия издателя (HDX)", license_en="publisher terms (HDX)"),
]

STATUS_LABELS = {
    "base": (("Основа", "Core data"), "#2c3e50"),
    "model": (("В модели", "In the model"), "#27ae60"),
    "tested": (("Проверен, не включён", "Tested, not included"), "#e67e22"),
    "stored": (("Хранится, без теста", "Stored, not tested"), "#7f8c8d"),
    "index": (("В индексе риска для детей", "In the child risk index"), "#8e44ad"),
}

# внешние сервисы, которые карта запрашивает из браузера при каждом открытии
EXTERNAL_MAP_SERVICES = [
    (("Подложка OSM", "OSM basemap"), "tile.openstreetmap.org"),
    (("Космоснимки", "Satellite imagery"), "server.arcgisonline.com (Esri World Imagery)"),
    (("Слой pH (WMS)", "pH layer (WMS)"), "maps.isric.org"),
    (("Поверхностные воды (JRC GSW)", "Surface water (JRC GSW)"), "jeodpp.jrc.ec.europa.eu"),
]


# --------------------------------------------------------------------------
# Доступ к данным (отдельные функции - чтобы их можно было подменить в тестах)
# --------------------------------------------------------------------------
def _engine():
    return current_app.config["ENGINE"]


def fetch_df(sql):
    with _engine().connect() as conn:
        return pd.read_sql(text(sql), conn)


def fetch_gdf(sql):
    import geopandas as gpd
    return gpd.read_postgis(sql, _engine(), geom_col="geometry")


def _num(v, fmt="{:.2f}", dash="—"):
    try:
        if v is None or (isinstance(v, float) and np.isnan(v)) or pd.isna(v):
            return dash
        return fmt.format(float(v))
    except (TypeError, ValueError):
        return dash


@bp.app_template_filter("num")
def num_filter(v, fmt="{:.2f}"):
    return _num(v, fmt)


@bp.app_context_processor
def _ctx():
    return {"nav_active": request.endpoint}


# --------------------------------------------------------------------------
# Страницы
# --------------------------------------------------------------------------
@bp.route("/about")
def about():
    try:
        summary = fetch_df(SUMMARY_SQL).iloc[0].to_dict()
    except Exception as exc:  # БД недоступна - страница остаётся читаемой
        current_app.logger.warning("about: summary недоступна: %s", exc)
        summary = None
    return render("about.html", s=summary)


@bp.route("/priority")
def priority():
    df = fetch_df(PRIORITY_SQL)
    zones = fetch_df(ZONE_AREA_SQL)
    shares = {}
    if not zones.empty:
        for site_id, g in zones.groupby("site_id"):
            total = float(g["area_m2"].sum())
            shares[site_id] = {r.risk_level: (float(r.area_m2) / total * 100 if total else 0.0)
                               for r in g.itertuples()}
    rows = df.to_dict(orient="records")
    max_idx = max([abs(r["priority_composite_index"]) for r in rows
                   if r["priority_composite_index"] is not None and not pd.isna(r["priority_composite_index"])] or [1.0])
    for r in rows:
        r["zone_share"] = shares.get(r["site_id"], {})
        idx = r["priority_composite_index"]
        r["idx_pct"] = 0 if idx is None or pd.isna(idx) else min(abs(float(idx)) / max_idx * 100, 100)
    return render("priority.html", rows=rows, levels=RISK_LEVELS, colors=RISK_COLORS,
                           has_zones=bool(shares))


@bp.route("/models")
def models():
    df = fetch_df(MODELS_SQL)
    rows = df.to_dict(orient="records")
    prod_id = None
    for r in rows:  # строки отсортированы от новых к старым
        if (r["target_substance"] == "Pb" and r["target_variant"] == "site_normalized"
                and r["lowo_r2"] is not None and not pd.isna(r["lowo_r2"])):
            prod_id = r["model_version_id"]
            break
    for r in rows:
        has_ml = r["lowo_r2"] is not None and not pd.isna(r["lowo_r2"])
        r["kind"] = "ML (LOWO)" if has_ml else tr("Описательный расчёт", "Descriptive calculation")
        r["trigger_reason"] = vl(r["trigger_reason"])
        r["is_prod"] = r["model_version_id"] == prod_id
        r["trained_at_s"] = str(r["trained_at"])[:16] if r["trained_at"] is not None else "—"
    return render("models.html", rows=rows)


@bp.route("/sources")
def sources():
    en = tr("ru", "en") == "en"
    src = [{k: (s_.get(k + "_en", v) if en else v) for k, v in s_.items() if not k.endswith("_en")} for s_ in SOURCES]
    labels = {k: (tr(v[0]), v[1]) for k, v in STATUS_LABELS.items()}
    external = [(tr(n), h) for n, h in EXTERNAL_MAP_SERVICES]
    return render("sources.html", sources=src, status_labels=labels, external=external)


def _norms_missing(exc):
    """Справочник нормативов не создан или не загружен: в БД нет таблиц/представления."""
    return "does not exist" in str(exc)


CORE_SUBSTANCES = ["Pb", "As", "Hg", "Zn", "Co"]


@bp.route("/context")
def context_page():
    try:
        attrs = fetch_df(CONTEXT_ATTRS_SQL)
        cur = fetch_df(CONTEXT_CURRENT_SQL)
        sites = fetch_df(SITES_NAMES_SQL)
    except Exception as exc:
        if _norms_missing(exc):
            current_app.logger.warning("context: таблицы не созданы: %s", exc)
            return render("context.html", missing=True)
        raise
    try:  # землепользование для норматива: нужен v_site_land_use (db/load_norms.py)
        lu = {r["site_id"]: r for r in fetch_df(SITE_LAND_USE_SQL).to_dict(orient="records")}
    except Exception:
        lu = {}

    cells = {}
    for r in cur.to_dict(orient="records"):
        if r["value_text"] is not None and not pd.isna(r["value_text"]):
            val = _ctx_values().get(r["value_text"], r["value_text"])
        else:
            val = _num(r["value_num"], "{:g}")
        cells[(r["site_id"], r["attr_code"])] = {**r, "val": val}
    attr_rows = attrs.to_dict(orient="records")
    for a in attr_rows:
        if a["unit"] is None or pd.isna(a["unit"]):
            a["unit"] = None
    rows = []
    for s_ in sites.to_dict(orient="records"):
        rows.append({"site_id": s_["site_id"], "name": nm(s_["name"]),
                     "cells": [cells.get((s_["site_id"], a["attr_code"])) for a in attr_rows],
                     "land_use": lu.get(s_["site_id"])})
    n_vals = len(cur)
    return render("context.html", missing=False, attrs=attr_rows, rows=rows, n_vals=n_vals,
                  verification=_verification(), land_use_ru=_ctx_values(), has_lu=bool(lu))


@bp.route("/norms")
def norms():
    try:
        profile = fetch_df(ACTIVE_PROFILE_SQL)
        ref = fetch_df(NORMS_REF_SQL)
        active = fetch_df(ACTIVE_NORMS_SQL)
        profile_sources = fetch_df(ACTIVE_PROFILE_SOURCES_SQL)
        measured = fetch_df(MEASURED_SUBSTANCES_SQL)
        site_exc = fetch_df(SITE_EXCEEDANCE_SQL)
    except Exception as exc:
        if _norms_missing(exc):
            current_app.logger.warning("norms: справочник не создан: %s", exc)
            return render("norms.html", missing=True)
        raise

    prof = profile.iloc[0].to_dict() if not profile.empty else None
    active_rows = active.to_dict(orient="records")
    by_sub = {r["substance"]: r for r in active_rows}

    # сводка по веществам: сколько замеров, сколько превышений
    totals = {}
    if not site_exc.empty:
        g = site_exc.groupby("norm_substance").agg(
            n=("n", "sum"), n_exceeds=("n_exceeds", "sum"),
            n_censored=("n_censored", "sum"), max_ratio=("max_ratio", "max"))
        totals = g.to_dict(orient="index")
    summary = []
    for r in active_rows:
        t = totals.get(r["substance"], {})
        summary.append({**r, "n": int(t.get("n", 0)), "n_exceeds": int(t.get("n_exceeds", 0)),
                        "n_censored": int(t.get("n_censored", 0)),
                        "max_ratio": t.get("max_ratio")})

    # матрица склад x вещество
    matrix_subs = [s_ for s_ in CORE_SUBSTANCES if s_ in by_sub]
    matrix = {}
    for r in site_exc.itertuples():
        if r.norm_substance in matrix_subs:
            matrix.setdefault((r.site_id, r.name), {})[r.norm_substance] = {
                "n": int(r.n), "exc": int(r.n_exceeds), "cens": int(r.n_censored),
                "max": None if pd.isna(r.max_ratio) else float(r.max_ratio)}
    matrix_rows = [{"site_id": k[0], "name": nm(k[1]), "cells": v} for k, v in sorted(matrix.items(), key=lambda x: x[0][1])]

    # вещества проекта без норматива в активном профиле
    covered = set(by_sub)
    for r in active_rows:
        if r["applies_to"]:
            covered.update(r["applies_to"])
    gaps = [m for m in measured.to_dict(orient="records") if m["substance"] not in covered]

    ref_groups = {}
    for r in ref.to_dict(orient="records"):
        ref_groups.setdefault(r["substance"], []).append(r)

    if prof:
        prof["description"] = vl(prof["description"])
    return render("norms.html", missing=False, prof=prof, profile_sources=profile_sources.to_dict(orient="records"),
                           summary=summary, matrix_subs=matrix_subs, matrix_rows=matrix_rows,
                           gaps=gaps, ref_groups=ref_groups, n_norms=len(ref))


# --------------------------------------------------------------------------
# Центр экспорта
# --------------------------------------------------------------------------
def _stamp():
    return datetime.now().strftime("%Y%m%d")


def _csv_bytes(df):
    for col in df.columns:  # массивы/списки -> строка
        if df[col].map(lambda v: isinstance(v, (list, tuple, np.ndarray))).any():
            df[col] = df[col].map(lambda v: ", ".join(map(str, v)) if isinstance(v, (list, tuple, np.ndarray)) else v)
    # utf-8-sig: Excel корректно открывает кириллицу
    return df.to_csv(index=False).encode("utf-8-sig")


def build_csv(key):
    if key == "measurements_wide_csv":
        long_df = fetch_df(MEASUREMENTS_LONG_SQL)
        meta_cols = ["sample_id", "site_id", "sample_date", "method", "depth_cm", "lat", "lon",
                     "dist_to_water_m", "dist_to_building_m", "dist_to_road_m"]
        meta = long_df[meta_cols].drop_duplicates("sample_id").set_index("sample_id")
        grp = long_df.groupby(["sample_id", "substance"])
        vals = grp["value"].first().unstack("substance")  # колонка остаётся, даже если все значения < LOD
        lod = (grp["below_lod"].first().unstack("substance")
               .map(lambda v: "" if pd.isna(v) else ("true" if bool(v) else "false"))
               .add_suffix("_below_lod"))
        wide = meta.join(vals).join(lod).reset_index()
        return _csv_bytes(wide)
    spec = CSV_EXPORTS.get(key)
    if spec is None:
        raise KeyError(key)
    return _csv_bytes(fetch_df(spec[2]))


def build_geojson(key):
    sql = GEOJSON_EXPORTS[key][2]
    gdf = fetch_gdf(sql)
    for col in gdf.columns:
        if str(gdf[col].dtype).startswith("datetime") or col.endswith("_date"):
            gdf[col] = gdf[col].astype(str)
    return gdf.to_json().encode("utf-8")


def _log_download(what):
    auth = request.authorization
    current_app.logger.info("export: %s user=%s ip=%s", what,
                            auth.username if auth else "-", request.remote_addr)


def _gpkg_info():
    if not os.path.exists(GPKG_PATH):
        return None
    st = os.stat(GPKG_PATH)
    return {"size_mb": st.st_size / 1024 / 1024,
            "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "age_days": (time.time() - st.st_mtime) / 86400}


@bp.route("/export")
def export_center():
    csv_items = [(k, tr(v[0]), tr(v[1])) for k, v in CSV_EXPORTS.items()]
    geo_items = [(k, tr(v[0]), tr(v[1])) for k, v in GEOJSON_EXPORTS.items()]
    return render("export.html", gpkg=_gpkg_info(), csv_items=csv_items,
                           geo_items=geo_items,
                           gpkg_name=os.path.basename(GPKG_PATH))


@bp.route("/export/gpkg")
def export_gpkg():
    if not os.path.exists(GPKG_PATH):
        abort(404, "GeoPackage не найден. Запустите: python db/export_geopackage.py tajikistan_soil_ml_export.gpkg")
    _log_download("gpkg")
    return send_file(GPKG_PATH, as_attachment=True,
                     download_name=f"tajikistan_soil_ml_{_stamp()}.gpkg",
                     mimetype="application/geopackage+sqlite3")


@bp.route("/export/all_csv.zip")
def export_all_csv():
    _log_download("all_csv.zip")
    buf = io.BytesIO()
    skipped = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for key in list(CSV_EXPORTS):
            try:
                zf.writestr(f"{key.replace('_csv', '')}.csv", build_csv(key))
            except Exception as exc:
                if key in NORMS_EXPORTS and _norms_missing(exc):
                    skipped.append(key)  # справочник нормативов ещё не загружен
                    continue
                raise
        zf.writestr("README.txt", _readme_text() + (
            "\nПропущено (справочник нормативов не загружен, python db/load_norms.py): "
            + ", ".join(skipped) + "\n" if skipped else ""))
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"tajikistan_soil_ml_csv_{_stamp()}.zip",
                     mimetype="application/zip")


@bp.route("/export/<key>")
def export_one(key):
    if key in CSV_EXPORTS or key == "measurements_wide_csv":
        try:
            data = build_csv(key)
        except Exception as exc:
            if key in NORMS_EXPORTS and _norms_missing(exc):
                abort(503, "Контекст использования не загружен. Запустите: python db/load_site_context.py"
                      if key == "site_context_csv" else
                      "Справочник нормативов не загружен. Запустите: python db/load_norms.py")
            raise
        mime, ext = "text/csv; charset=utf-8", "csv"
    elif key in GEOJSON_EXPORTS:
        data, mime, ext = build_geojson(key), "application/geo+json", "geojson"
    else:
        abort(404)
    _log_download(key)
    name = f"{key.rsplit('_', 1)[0]}_{_stamp()}.{ext}"
    return Response(data, mimetype=mime,
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _readme_text():
    return (
        "Выгрузка демо-стенда ML TJ\n"
        f"Дата: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        "Кодировка CSV: UTF-8 с BOM (корректно открывается в Excel).\n"
        "Единицы замеров: mg/kg (XRF). Значения ниже предела обнаружения: value пусто, below_lod=true.\n"
        "Координаты: WGS84 (EPSG:4326).\n"
        "Ранг приоритета - относительный между 10 складами выборки; не оценка против нормативов ПДК.\n"
        "Сравнение с нормативами (norms.csv, exceedances.csv): скрининговые уровни и ориентиры не являются\n"
        "нормативами очистки; статус сверки каждого значения указан в колонке verification.\n"
    )
