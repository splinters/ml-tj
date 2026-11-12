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
    notes           TEXT
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
    dist_to_water_m NUMERIC                      -- расстояние до ближайшей реки/канала (OSM), per-point ковариат
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
    population_density     NUMERIC,
    dist_to_building_m     NUMERIC,
    dist_to_river_m        NUMERIC,
    soil_ph                NUMERIC,
    computed_at            TIMESTAMPTZ DEFAULT now()
);
