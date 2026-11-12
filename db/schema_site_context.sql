-- ---------------------------------------------------------------------------
-- Контекст использования территорий вокруг складов: что находится рядом
-- (жильё, поля, каналы), текущее и целевое землепользование.
--
-- Длинный формат "склад - признак - значение": новый признак добавляется
-- строкой в db/context_attributes.csv, без правки схемы. Каждое значение
-- хранит происхождение (verification, source, as_of): факты заказчика и
-- оценки по снимкам различаются, история не затирается.
--
-- Загрузка (идемпотентно): python db/load_site_context.py
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS context_attributes (
    attr_code    TEXT PRIMARY KEY,                -- 'target_land_use', 'nearest_residential_m', ...
    label_ru     TEXT NOT NULL,
    value_type   TEXT NOT NULL CHECK (value_type IN ('enum', 'number', 'text')),
    allowed      TEXT[],                          -- допустимые значения для enum
    unit         TEXT,                            -- для number
    description  TEXT,
    sort_order   INT NOT NULL DEFAULT 100
);

CREATE TABLE IF NOT EXISTS site_context (
    context_id    BIGSERIAL PRIMARY KEY,
    site_id       TEXT NOT NULL REFERENCES sites(site_id) ON DELETE CASCADE,
    attr_code     TEXT NOT NULL REFERENCES context_attributes(attr_code) ON UPDATE CASCADE,
    value_text    TEXT,
    value_num     NUMERIC,
    verification  TEXT NOT NULL DEFAULT 'unverified'
                  CHECK (verification IN ('customer_doc', 'customer_stated', 'estimate_imagery', 'unverified')),
                                                -- customer_doc: в документе заказчика;
                                                -- customer_stated: заказчик сообщил письмом/устно;
                                                -- estimate_imagery: оценка по снимкам, не подтверждена;
                                                -- unverified: источник не установлен
    source        TEXT NOT NULL,                  -- кто/откуда ("Письмо заказчика 2026-10-16")
    source_ref    TEXT,                           -- файл, лист, страница
    as_of         DATE NOT NULL DEFAULT CURRENT_DATE,
    note          TEXT,
    loaded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (value_text IS NOT NULL OR value_num IS NOT NULL),
    UNIQUE (site_id, attr_code, source, as_of)
);
CREATE INDEX IF NOT EXISTS idx_site_context_site ON site_context (site_id, attr_code);

-- Действующее значение признака: сначала по надёжности источника
-- (документ > сообщение заказчика > оценка по снимкам > не установлен),
-- внутри уровня - более новая дата. Старые записи остаются в site_context.
CREATE OR REPLACE VIEW v_site_context_current AS
SELECT DISTINCT ON (c.site_id, c.attr_code)
       c.site_id, c.attr_code, c.value_text, c.value_num, c.verification,
       c.source, c.source_ref, c.as_of, c.note
FROM site_context c
ORDER BY c.site_id, c.attr_code,
         CASE c.verification WHEN 'customer_doc' THEN 1 WHEN 'customer_stated' THEN 2
                             WHEN 'estimate_imagery' THEN 3 ELSE 4 END,
         c.as_of DESC, c.context_id DESC;
