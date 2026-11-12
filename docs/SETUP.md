# Локальный тестовый полигон — установка на macOS

Всё для демо: venv с нужными модулями, PostGIS в Docker, Jupyter, веб-карта.

## 0. Что понадобится заранее

- **Docker Desktop для Mac** — [docker.com](https://www.docker.com/products/docker-desktop/), нужен для БД.
- **Homebrew** — если ещё не стоит: `/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"`

Если MacBook на Apple Silicon (M1/M2/M3/M4) — команды ниже те же, отдельных
шагов не нужно, но если что-то из geopandas/rasterio откажется собираться
через pip, самый надёжный обходной путь — поставить `miniforge`
(conda-forge с бинарниками под arm64) и создать env через `conda create`
вместо `venv`. Начните с варианта ниже, к conda переходите только если
упрётесь в ошибку сборки GDAL.

## 1. Системные библиотеки (GDAL и клиент Postgres)

```bash
brew install gdal postgresql
```

`gdal` нужен для geopandas/rasterio/rioxarray, `postgresql` — ради
клиентских библиотек для psycopg2 (сам сервер БД будет в Docker, не через brew).

## 2. Виртуальное окружение Python

```bash
cd local_demo_stand
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Если тут споткнётесь на geopandas/rasterio/fiona — почти всегда помогает:

```bash
pip install --no-binary fiona,rasterio fiona rasterio
```

(объясняет системному пакету, что GDAL уже стоит через brew, вместо того
чтобы тянуть несовместимый бинарник).

## 3. База данных

У вас уже есть локальный Postgres — используем его напрямую, Docker не нужен
(секция для Docker оставлена ниже как запасной вариант, если понадобится
на другой машине или для коллеги без своего Postgres).

### 3a. Вариант: существующий локальный Postgres

Проверьте, стоит ли расширение PostGIS:

```bash
psql -d postgres -c "SELECT * FROM pg_available_extensions WHERE name = 'postgis';"
```

Если пусто — поставьте (команда зависит от того, как у вас установлен
Postgres):

```bash
# если Postgres поставлен через Homebrew:
brew install postgis

# если через Postgres.app - PostGIS обычно уже входит в комплект,
# ничего доустанавливать не нужно
```

Создайте базу и накатите схему:

```bash
createdb soil_ml
psql -d soil_ml -f db/schema.sql
```

`schema.sql` сам делает `CREATE EXTENSION IF NOT EXISTS postgis;` — отдельно
включать не нужно, если расширение доступно в кластере.

Пропишите реальные данные подключения в `.env` (у локального Postgres на
Mac часто нет пароля и имя пользователя = ваш логин macOS — тогда так):

```
DATABASE_URL=postgresql://ваш_логин_macos@localhost:5432/soil_ml
```

Если у вашего Postgres задан пароль или нестандартный порт — подставьте
их сюда же. Дальше всё как в остальной инструкции (`python db/import_data.py`
и т.д.) — скрипты читают `DATABASE_URL`, им всё равно, Docker это или нет.

### 3b. Вариант: Docker (запасной)

<details>
<summary>Развернуть, если понадобится</summary>

```bash
docker compose up -d
docker compose logs db | tail -20
```

Схема (`db/schema.sql`) подключена как init-скрипт — применится
автоматически при первом запуске контейнера.

</details>

Скопируйте `.env.example` в `.env`, если ещё не сделали:

```bash
cp .env.example .env
```


## 4. Загрузка данных в базу

Внутри активного venv:

```bash
python db/import_data.py
```

Загрузит 10 реальных складов (`sites`) и 169 точек XRF-замеров
(`samples`/`measurements`) из `db/source_data/`.

## 5. Открытые данные для модели (pH, уклон, расстояние до воды)

Схема БД уже обновлена (`dist_to_water_m` в `samples`), но если база у вас
создавалась раньше по старой схеме - накатите миграцию вручную:

```bash
psql -d soil_ml -c "ALTER TABLE samples ADD COLUMN IF NOT EXISTS dist_to_water_m NUMERIC;"
```

Затем заберите реальные ковариаты (10 складов по pH/уклону, 169 точек по
расстоянию до воды - см. пояснение в самом скрипте, почему не наоборот):

```bash
python db/fetch_point_covariates.py
```

И оставшиеся три параметра (землепользование и население - по складу;
расстояние до зданий - по точке, как вода):

```bash
psql -d soil_ml -c "ALTER TABLE samples ADD COLUMN IF NOT EXISTS dist_to_building_m NUMERIC;"
python db/fetch_landcover_population.py
python db/fetch_buildings.py
```

`fetch_buildings.py` скачивает контуры зданий Microsoft для всей страны
один раз (кэш - `db/tjk_buildings.geojson`) - может занять время в
зависимости от объёма тайлов, зато дальше работает по кэшу мгновенно.

Результат кэшируется в `db/covariates_cache.json` - при повторном запуске
не будет заново дёргать SoilGrids/Overpass, если уже посчитано.

## 6. Пересчёт модели (связывает ML с картой)

```bash
python db/run_model_and_store.py "первый запуск"
```

Без этого шага карта покажет только сырую статистику (медианы), а не то,
что предсказала и честно проверила модель. Перезапускайте эту команду
после любого добавления/изменения данных — см. раздел про переобучение
ниже.

## 7. Jupyter — для небольших демо

```bash
python -m ipykernel install --user --name soil-ml-demo --display-name "Soil ML demo"
jupyter notebook notebooks/
```

Откройте `00_quickstart.ipynb` — выберите ядро "Soil ML demo", если Jupyter
не выбрал его сам, и прогоните ячейки сверху вниз.

## 8. Веб-приложение — карта в браузере

```bash
python webapp/app.py
```

Откройте [http://127.0.0.1:5050](http://127.0.0.1:5050) — интерактивная
карта складов с попапами и точками замеров, напрямую из PostGIS.

## Проверка, что всё работает целиком

```bash
docker compose ps                     # контейнер db "healthy"/"running"
python db/import_data.py              # без ошибок, "sites: загружено 10 площадок"
python webapp/app.py &                # поднялся на :5050
curl -s localhost:5050/api/sites | head -c 200   # видно geojson
```

## Структура проекта

```
local_demo_stand/
  requirements.txt
  docker-compose.yml
  .env.example
  db/
    schema.sql            # структура БД (PostGIS)
    import_data.py        # загрузка данных в БД
    source_data/           # исходные файлы (10 складов, реальные замеры)
  notebooks/
    00_quickstart.ipynb   # проверка стенда + риск-таблица прямо из БД
  webapp/
    app.py                # Flask: /api/sites, /api/samples
    templates/index.html  # Leaflet-карта
```

## Передача базы заказчику (дамп)

Когда придёт время отдать БД — два формата на выбор:

```bash
# custom-формат (сжатый, гибкий restore, рекомендуется по умолчанию)
pg_dump -Fc --no-owner --no-acl -d soil_ml -f soil_ml_dump.pgdump

# обычный SQL-дамп (человекочитаемый, максимально портируемый между версиями)
pg_dump --no-owner --no-acl -d soil_ml > soil_ml_dump.sql
```

`--no-owner --no-acl` — важно: без этого в дампе останутся ссылки на ваши
локальные роли/права macOS-пользователя, которых не будет на сервере
заказчика, и restore упадёт с ошибками прав доступа.

**Что нужно уточнить у заказчика заранее, а не в момент передачи:**
- на их сервере должно быть доступно расширение PostGIS (`CREATE EXTENSION postgis;` должен отрабатывать) — без этого restore схемы не пройдёт;
- версия PostgreSQL на их стороне — custom-формат (`-Fc`) обычно спокойно переносится на более новую версию сервера, но откат на более старую версию, чем у вас локально, может не сработать; если не уверены в их версии — берите обычный SQL-дамп, он переносится надёжнее в обе стороны.

Restore на их стороне (для custom-формата):

```bash
createdb soil_ml
pg_restore --no-owner --no-acl -d soil_ml soil_ml_dump.pgdump
```

Для SQL-дампа — просто `psql -d soil_ml -f soil_ml_dump.sql`.

## Что дальше


- Скрипты `fix_data.py` / `build_dataset.py` / `model_pipeline.py` из
  предыдущего пакета можно переключить на чтение из этой БД вместо CSV
  (`pd.read_sql` вместо `pd.read_csv`) — тогда весь пайплайн будет работать
  на одном источнике данных.
- Для страновых открытых слоёв (DEM/WorldCover/SoilGrids) — отдельный пакет
  `country_wide_data/`, тоже запускается в этом же venv.
