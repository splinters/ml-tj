-- ============================================================================
-- Схема PostGIS: хранение, версионирование модели, результаты для карты
-- ============================================================================
-- Ключевые архитектурные решения, вытекающие из уже пройденного опыта:
--
-- 1. Буферы 50/100м НЕ хранятся как отдельные загруженные полигоны.
--    Именно раздельное хранение и стало причиной бага "Кушониен = дубликат
--    Шахринава" (кто-то скопировал geometry в отдельный слой и не обновил).
--    Здесь буферы строятся на лету через ST_Buffer(contour, 50) - дубликат
--    геометрии между площадками физически невозможен.
--
-- 2. Замеры хранятся в длинном формате (site -> sample -> measurement),
--    а не в широких колонках Pb/As/Hg/... Так одна схема одинаково
--    обслуживает и тяжёлые металлы (9 элементов сейчас), и пестициды
--    (ДДТ/ДДД/ДДЕ, как на объекте TJ-7486) - без миграции схемы при
--    появлении новой категории загрязнения с сайта chemicals.tj.
--
-- 3. Растровые поверхности (heatmap/uncertainty) НЕ хранятся в БД целиком -
--    только путь к файлу (S3/облако) + метаданные. В БД - только
--    векторизованные risk_zones (быстро рендерится на карте) и точечные
--    predictions (для попапов/таблиц).
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS postgis;

-- ---------------------------------------------------------------------------
-- Площадки (склады/объекты) - единый реестр вне зависимости от типа
-- загрязнения (тяжёлые металлы, СОЗ-пестициды, промышленные и т.д.)
-- ---------------------------------------------------------------------------
CREATE TABLE sites (
    site_id         TEXT PRIMARY KEY,          -- напр. 'TJ-7486', если совпадает с chemicals.tj - используем их ID
    name            TEXT NOT NULL,              -- 'Вахш', 'Кушониен', ...
    pollution_type  TEXT NOT NULL,              -- 'heavy_metals' | 'pops_pesticides' | 'industrial' | ...
    contour_geom    GEOMETRY(Polygon, 4326),    -- ЕДИНСТВЕННЫЙ хранимый полигон границы
    status          TEXT DEFAULT 'active',      -- 'active' | 'remediated' | 'under_review'
    source          TEXT,                       -- откуда пришли данные (организация/проект)
    date_added      TIMESTAMPTZ DEFAULT now(),
    notes           TEXT,
    priority_rank              INT,      -- см. db/rank_sites_priority.py: 1 = наиболее приоритетный
    priority_composite_index   NUMERIC   -- log-превышение над общестрановой медианой (Pb+As+Hg+Zn)
);
CREATE INDEX idx_sites_geom ON sites USING GIST (contour_geom);

-- буфер как VIEW, а не таблица - невозможно "рассинхронизировать" с контуром
CREATE VIEW site_buffers AS
SELECT site_id,
       ST_Buffer(contour_geom::geography, 50)::geometry(Polygon, 4326)  AS buf_50m,
       ST_Buffer(contour_geom::geography, 100)::geometry(Polygon, 4326) AS buf_100m
FROM sites;

-- ---------------------------------------------------------------------------
-- Пробы (точки отбора/замера) - независимо от метода (XRF, лаб.анализ)
-- ---------------------------------------------------------------------------
CREATE TABLE samples (
    sample_id       BIGSERIAL PRIMARY KEY,
    site_id         TEXT REFERENCES sites(site_id),
    geom            GEOMETRY(Point, 4326) NOT NULL,
    sample_date     DATE NOT NULL,
    depth_cm        NUMERIC,
    method          TEXT,                       -- 'XRF' | 'lab_iss' | ...
    duration_sec    NUMERIC,                     -- для XRF - вес наблюдения
    sampler_name    TEXT,
    round_number    INT DEFAULT 1,               -- номер раунда обследования (для повторных замеров)
    dist_to_water_m    NUMERIC,                  -- расстояние до ближайшей реки/канала (OSM), per-point
    dist_to_building_m NUMERIC,                   -- расстояние до ближайшего здания (Microsoft Buildings), per-point
    dist_to_road_m NUMERIC,                        -- расстояние до ближайшей дороги (OSM), per-point - Pb физически связан с этилированным бензином
    flow_accumulation      NUMERIC,                -- сырое значение в пикселе точки (ArcGIS Pro: DEM 150м/UTM42N -> Fill -> FlowDir D8 -> FlowAcc), см. db/fetch_flow_covariate.py
    flow_accumulation_max  NUMERIC,                -- максимум в окне NxN вокруг точки (снимает "промах мимо русла")
    flow_direction          INT                     -- код направления стока D8 (1,2,4,8,16,32,64,128), сырое значение в точке
);
CREATE INDEX idx_samples_geom ON samples USING GIST (geom);
CREATE INDEX idx_samples_site ON samples (site_id);

-- ---------------------------------------------------------------------------
-- Измерения - длинный формат: один элемент/вещество на строку
-- ---------------------------------------------------------------------------
CREATE TABLE measurements (
    measurement_id  BIGSERIAL PRIMARY KEY,
    sample_id       BIGINT REFERENCES samples(sample_id) ON DELETE CASCADE,
    substance       TEXT NOT NULL,               -- 'Pb', 'As', 'Hg', 'DDT', 'DDD', ...
    value           NUMERIC,                     -- NULL, если ниже предела обнаружения
    unit            TEXT DEFAULT 'mg/kg',
    below_lod       BOOLEAN DEFAULT FALSE,
    lod_value       NUMERIC
);
CREATE INDEX idx_measurements_sample ON measurements (sample_id);
CREATE INDEX idx_measurements_substance ON measurements (substance);

-- ---------------------------------------------------------------------------
-- Версии модели - при каждом пересчёте новая строка, старые не удаляются
-- ---------------------------------------------------------------------------
CREATE TABLE model_versions (
    model_version_id  BIGSERIAL PRIMARY KEY,
    trained_at         TIMESTAMPTZ DEFAULT now(),
    target_substance   TEXT,                     -- 'Pb', 'As', ...
    target_variant      TEXT,                     -- 'raw' | 'site_normalized'
    n_sites            INT,
    n_points           INT,
    naive_r2           NUMERIC,
    lowo_r2            NUMERIC,
    lowo_rmse          NUMERIC,
    features_used      TEXT[],
    trigger_reason      TEXT,                     -- что вызвало пересчёт (см. секцию триггеров)
    artifact_path       TEXT                       -- путь к сохранённой модели (joblib/pickle) в облаке
);

-- ---------------------------------------------------------------------------
-- Точечные предсказания (для попапов на карте и риск-таблицы)
-- ---------------------------------------------------------------------------
CREATE TABLE predictions (
    prediction_id    BIGSERIAL PRIMARY KEY,
    model_version_id BIGINT REFERENCES model_versions(model_version_id),
    site_id          TEXT REFERENCES sites(site_id),
    geom             GEOMETRY(Point, 4326),
    predicted_value  NUMERIC,
    uncertainty      NUMERIC,
    risk_score       NUMERIC,                    -- 0-100
    risk_level       TEXT                         -- 'low'|'medium'|'high'|'critical'
);
CREATE INDEX idx_predictions_geom ON predictions USING GIST (geom);

-- ---------------------------------------------------------------------------
-- Векторизованные зоны риска - то, что реально рендерится на карте
-- (полигоны, не растр - лёгкие, быстрые, поддерживают попапы/стили)
-- ---------------------------------------------------------------------------
CREATE TABLE risk_zones (
    zone_id           BIGSERIAL PRIMARY KEY,
    model_version_id  BIGINT REFERENCES model_versions(model_version_id),
    site_id           TEXT REFERENCES sites(site_id),
    risk_level        TEXT,
    geom              GEOMETRY(MultiPolygon, 4326)
);
CREATE INDEX idx_risk_zones_geom ON risk_zones USING GIST (geom);

-- ---------------------------------------------------------------------------
-- Кэш страновых ковариат по площадке - чтобы не дёргать SoilGrids/DEM
-- заново при каждом пересчёте модели
-- ---------------------------------------------------------------------------
CREATE TABLE site_covariates (
    site_id                TEXT PRIMARY KEY REFERENCES sites(site_id),
    elevation_m            NUMERIC,
    slope_deg              NUMERIC,
    worldcover_class       INT,
    population_500m        NUMERIC,                -- сумма населения WorldPop в радиусе 500м (не плотность в пикселе)
    dist_to_building_m     NUMERIC,                 -- устарело: перенесено на samples (по точке, не по складу) - см. ниже
    dist_to_river_m        NUMERIC,
    soil_ph                NUMERIC,
    lithology_class        INT,                     -- GLiM 0.5° grid, категориальный код породы
    temp_annual_c           NUMERIC,                -- среднегодовая температура, WorldClim BIO1 (историческая)
    precip_annual_mm        NUMERIC,                -- годовое кол-во осадков, WorldClim BIO12 (историческая)

    -- помесячные исторические нормы (WorldClim) - 12 месяцев x 2 переменные
    prec_month_01_mm NUMERIC, prec_month_02_mm NUMERIC, prec_month_03_mm NUMERIC,
    prec_month_04_mm NUMERIC, prec_month_05_mm NUMERIC, prec_month_06_mm NUMERIC,
    prec_month_07_mm NUMERIC, prec_month_08_mm NUMERIC, prec_month_09_mm NUMERIC,
    prec_month_10_mm NUMERIC, prec_month_11_mm NUMERIC, prec_month_12_mm NUMERIC,
    tavg_month_01_c NUMERIC, tavg_month_02_c NUMERIC, tavg_month_03_c NUMERIC,
    tavg_month_04_c NUMERIC, tavg_month_05_c NUMERIC, tavg_month_06_c NUMERIC,
    tavg_month_07_c NUMERIC, tavg_month_08_c NUMERIC, tavg_month_09_c NUMERIC,
    tavg_month_10_c NUMERIC, tavg_month_11_c NUMERIC, tavg_month_12_c NUMERIC,

    -- будущие климатические проекции: модель IPSL-CM6A-LR, 2 сценария
    -- (ssp245 умеренный, ssp585 высокий) x 4 периода x 2 переменные
    -- (источник: GCA GeoServer / локальные растры future-trends)
    future_ssp245_2021_2040_temp_c NUMERIC, future_ssp245_2021_2040_precip_mm NUMERIC,
    future_ssp245_2041_2060_temp_c NUMERIC, future_ssp245_2041_2060_precip_mm NUMERIC,
    future_ssp245_2061_2080_temp_c NUMERIC, future_ssp245_2061_2080_precip_mm NUMERIC,
    future_ssp245_2081_2100_temp_c NUMERIC, future_ssp245_2081_2100_precip_mm NUMERIC,
    future_ssp585_2021_2040_temp_c NUMERIC, future_ssp585_2021_2040_precip_mm NUMERIC,
    future_ssp585_2041_2060_temp_c NUMERIC, future_ssp585_2041_2060_precip_mm NUMERIC,
    future_ssp585_2061_2080_temp_c NUMERIC, future_ssp585_2061_2080_precip_mm NUMERIC,
    future_ssp585_2081_2100_temp_c NUMERIC, future_ssp585_2081_2100_precip_mm NUMERIC,

    -- гидрология (ArcGIS Pro, см. db/fetch_flow_covariate.py) - те же признаки,
    -- что и на samples, но по центроиду склада, для параллели с другими
    -- site-level ковариатами и тестов "глобальной" модели
    flow_accumulation       NUMERIC,
    flow_accumulation_max   NUMERIC,
    flow_direction           INT,

    computed_at            TIMESTAMPTZ DEFAULT now()
);
