-- ---------------------------------------------------------------------------
-- Справочник нормативов качества почвы: вещество - норматив - единица -
-- тип землепользования - источник. Применяется как отдельный слой над
-- measurements: модель, ранжирование и карта не меняются, а сравнение с
-- нормативом строится представлением v_exceedance. Новый норматив
-- добавляется строкой в справочник (db/load_norms.py), без правок кода.
--
-- Запуск (идемпотентно): python db/load_norms.py
-- Требует db/schema_site_context.sql (load_norms.py применяет оба файла).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS norm_sources (
    source_id     TEXT PRIMARY KEY,           -- короткий код: 'EPA_RSL_2023'
    title         TEXT NOT NULL,
    issuer        TEXT,                        -- орган/организация
    jurisdiction  TEXT,                        -- 'TJ' | 'SU' | 'US' | 'CA' | 'INT' ...
    tier          TEXT NOT NULL CHECK (tier IN ('national', 'international')),
    doc_ref       TEXT,                        -- номер/реквизиты документа
    version_date  TEXT,                        -- редакция/дата выпуска (текстом: '2023-05')
    url           TEXT,
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS soil_norms (
    norm_id       BIGSERIAL PRIMARY KEY,
    source_id     TEXT NOT NULL REFERENCES norm_sources(source_id) ON DELETE CASCADE,
    substance     TEXT NOT NULL,               -- 'Pb', 'As', 'Hg', 'Zn', 'Co', 'DDT_sum', ...
    applies_to    TEXT[],                      -- для суммарных нормативов: состав суммы {'DDT','DDD','DDE'}
    value         NUMERIC NOT NULL CHECK (value > 0),
    unit          TEXT NOT NULL DEFAULT 'mg/kg' CHECK (unit = 'mg/kg'),
    basis         TEXT NOT NULL DEFAULT 'total'
                  CHECK (basis IN ('total', 'mobile', 'acid_extractable')),
                                              -- форма: XRF даёт валовое содержание ('total');
                                              -- нормативы по подвижным формам с XRF не сопоставимы
    land_use      TEXT NOT NULL DEFAULT 'any'
                  CHECK (land_use IN ('any', 'agricultural', 'residential', 'commercial', 'industrial')),
    soil_class    TEXT NOT NULL DEFAULT 'any',  -- 'any' | 'sand' | 'acid' | 'neutral' (ОДК по группам почв)
    norm_type     TEXT NOT NULL
                  CHECK (norm_type IN ('MPC', 'ODK', 'screening', 'guideline', 'cleanup')),
    verification  TEXT NOT NULL DEFAULT 'unverified'
                  CHECK (verification IN ('primary', 'customer_doc', 'secondary', 'unverified')),
                                              -- primary: сверено с первоисточником;
                                              -- customer_doc: значение приведено в документе заказчика;
                                              -- secondary: вторичный источник, нужна сверка;
                                              -- unverified: не сверено
    note          TEXT,
    valid_from    DATE,
    valid_to      DATE,
    UNIQUE (source_id, substance, basis, land_use, soil_class, norm_type)
);
CREATE INDEX IF NOT EXISTS idx_soil_norms_substance ON soil_norms (substance);

-- профиль = какие источники и в каком порядке применять + тип землепользования
CREATE TABLE IF NOT EXISTS norm_profiles (
    profile_id    TEXT PRIMARY KEY,
    land_use      TEXT NOT NULL
                  CHECK (land_use IN ('agricultural', 'residential', 'commercial', 'industrial')),
    description   TEXT,
    is_active     BOOLEAN NOT NULL DEFAULT FALSE
);
-- одновременно активен только один профиль
CREATE UNIQUE INDEX IF NOT EXISTS uq_norm_profiles_active ON norm_profiles (is_active) WHERE is_active;

CREATE TABLE IF NOT EXISTS norm_profile_sources (
    profile_id    TEXT NOT NULL REFERENCES norm_profiles(profile_id) ON DELETE CASCADE,
    source_id     TEXT NOT NULL REFERENCES norm_sources(source_id) ON DELETE CASCADE,
    priority      INT NOT NULL,                -- 1 = применяется первым
    PRIMARY KEY (profile_id, source_id)
);

-- Нормативы активного профиля: для каждого вещества берётся первый по
-- приоритету источник, у которого есть норматив для валового содержания
-- и подходящего землепользования. Если у норматива несколько групп почв
-- (ОДК), берётся наименьшее значение (консервативно: группа почвы склада
-- в БД не хранится).
CREATE OR REPLACE VIEW v_active_norms AS
WITH prof AS (
    SELECT profile_id, land_use FROM norm_profiles WHERE is_active
),
cand AS (
    SELECT n.substance, n.applies_to, n.source_id, ps.priority, n.norm_type, n.land_use,
           MIN(n.value) AS value, n.unit, MAX(n.verification) AS verification
    FROM prof
    JOIN norm_profile_sources ps ON ps.profile_id = prof.profile_id
    JOIN soil_norms n ON n.source_id = ps.source_id
    WHERE n.basis = 'total'
      AND (n.land_use = prof.land_use OR n.land_use = 'any')
      AND (n.valid_from IS NULL OR n.valid_from <= CURRENT_DATE)
      AND (n.valid_to   IS NULL OR n.valid_to   >= CURRENT_DATE)
    GROUP BY n.substance, n.applies_to, n.source_id, ps.priority, n.norm_type, n.land_use, n.unit
)
SELECT DISTINCT ON (substance)
       substance, applies_to, source_id, norm_type, land_use, value, unit, verification, priority
FROM cand
ORDER BY substance, priority, (land_use = 'any');

-- Землепользование для сравнения с нормативом. По умолчанию - тип активного
-- профиля. Для склада, у которого заказчик (документ или сообщение) указал
-- целевое землепользование после рекультивации (target_land_use), берётся оно.
-- Оценки по снимкам и непроверенные значения норматив не меняют.
CREATE OR REPLACE VIEW v_site_land_use AS
SELECT s.site_id,
       COALESCE(ctx.value_text, prof.land_use) AS land_use,
       CASE WHEN ctx.value_text IS NULL THEN 'profile' ELSE 'site_context' END AS land_use_origin
FROM sites s
CROSS JOIN (SELECT land_use FROM norm_profiles WHERE is_active) prof
LEFT JOIN (
    SELECT site_id, value_text FROM v_site_context_current
    WHERE attr_code = 'target_land_use'
      AND verification IN ('customer_doc', 'customer_stated')
      AND value_text IN ('agricultural', 'residential', 'commercial', 'industrial')
) ctx ON ctx.site_id = s.site_id;

-- Нормативы для каждого склада: та же логика, что в v_active_norms, но с
-- землепользованием склада из v_site_land_use.
CREATE OR REPLACE VIEW v_site_norms AS
WITH cand AS (
    SELECT sl.site_id, n.substance, n.applies_to, n.source_id, ps.priority, n.norm_type, n.land_use,
           MIN(n.value) AS value, n.unit, MAX(n.verification) AS verification
    FROM v_site_land_use sl
    JOIN norm_profiles p ON p.is_active
    JOIN norm_profile_sources ps ON ps.profile_id = p.profile_id
    JOIN soil_norms n ON n.source_id = ps.source_id
    WHERE n.basis = 'total'
      AND (n.land_use = sl.land_use OR n.land_use = 'any')
      AND (n.valid_from IS NULL OR n.valid_from <= CURRENT_DATE)
      AND (n.valid_to   IS NULL OR n.valid_to   >= CURRENT_DATE)
    GROUP BY sl.site_id, n.substance, n.applies_to, n.source_id, ps.priority, n.norm_type, n.land_use, n.unit
)
SELECT DISTINCT ON (site_id, substance)
       site_id, substance, applies_to, source_id, norm_type, land_use, value, unit, verification, priority
FROM cand
ORDER BY site_id, substance, priority, (land_use = 'any');

-- Сравнение каждого замера с нормативом (с учётом землепользования склада).
-- status: exceeds | within | below_lod | lod_above_norm
--   below_lod       - значение ниже предела обнаружения, предел не выше норматива
--   lod_above_norm  - предел обнаружения выше норматива: прибор не позволяет судить
CREATE OR REPLACE VIEW v_exceedance AS
SELECT sam.site_id, sam.sample_id, an.substance AS norm_substance, an.source_id, an.norm_type,
       an.verification, an.value AS norm_value, m.value AS measured,
       CASE WHEN m.value IS NULL THEN NULL ELSE m.value / an.value END AS ratio,
       CASE
           WHEN m.value IS NOT NULL AND m.value > an.value THEN 'exceeds'
           WHEN m.value IS NOT NULL THEN 'within'
           WHEN m.lod_value IS NOT NULL AND m.lod_value > an.value THEN 'lod_above_norm'
           ELSE 'below_lod'
       END AS status,
       an.land_use AS norm_land_use
FROM v_site_norms an
JOIN samples sam ON sam.site_id = an.site_id
JOIN measurements m ON m.sample_id = sam.sample_id AND m.substance = an.substance
WHERE an.applies_to IS NULL
UNION ALL
SELECT sam.site_id, sam.sample_id, an.substance, an.source_id, an.norm_type,
       an.verification, an.value, agg.total,
       CASE WHEN agg.total IS NULL THEN NULL ELSE agg.total / an.value END,
       CASE
           WHEN agg.total IS NOT NULL AND agg.total > an.value THEN 'exceeds'
           WHEN agg.total IS NOT NULL THEN 'within'
           ELSE 'below_lod'
       END,
       an.land_use
FROM v_site_norms an
JOIN samples sam ON sam.site_id = an.site_id
JOIN LATERAL (
    SELECT SUM(m.value) AS total
    FROM measurements m
    WHERE m.sample_id = sam.sample_id AND m.substance = ANY (an.applies_to)
    HAVING COUNT(*) > 0
) agg ON TRUE
WHERE an.applies_to IS NOT NULL;
