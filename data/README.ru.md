# Входные данные

*English version: [README.md](README.md).*

Настоящих данных в репозитории нет. В папке `synthetic/` лежит придуманный набор в том же формате (создаётся `tools/make_synthetic_data.py`); блокноты и модель скрининга используют его, если настоящих данных нет, или папку из переменной `ML_TJ_DATA_ROOT`. В таблицах ниже перечислено, что ожидают скрипты, откуда эти данные берутся и на каких условиях. Пути указаны от корня репозитория. Лицензии открытых наборов перед распространением проверяйте на страницах издателей.

## Данные заказчика и национальные данные (не публикуются)

| Данные | Путь | Как получаются |
|---|---|---|
| Реестр загрязнённых участков, карточки и прикреплённые документы | `source_data/chemicals_tj/` | `db/fetch_chemicals_tj.py` с открытой карты chemicals.tj; условия владельца сайта |
| Значения, перенесённые вручную из сканов протоколов | `source_data/chemicals_tj/manual/*.csv` | вводятся вручную; данные заказчика |
| Контуры складов и измерения XRF | `source_data/warehouses.geojson`, `source_data/warehouse_points_enriched.csv` и др. | данные заказчика; загружаются в PostgreSQL скриптом `db/import_data.py` |
| Выгрузка для блокнотов | `data/tajikistan_soil_ml_export.gpkg` (или переменная `ML_TJ_GPKG`) | `db/export_geopackage.py` |

## Открытые данные

| Данные | Путь | Источник | Лицензия |
|---|---|---|---|
| Население по полу и возрасту, 2025, 100 м, вариант constrained (WorldPop R2025A). Для индекса детей нужны `T_Under_18`, `t_00`, `t_01`, `T_F`, `T_M`; для слоёв GeoServer ещё `f_15` … `f_45` | `source_data/worldpop_r2025a/tjk_*_2025_CN_100m_R2025A_v1.tif` | data.worldpop.org, Global2 R2025A | CC BY 4.0 |
| Население 2020, constrained (признак модели металлов) | `db/tjk_worldpop_2020.tif` | `db/fetch_landcover_population.py` | CC BY 4.0 |
| Индекс относительного благосостояния (RWI) | `source_data/tjk_relative_wealth_index.csv` | Meta Data for Good, через HDX | некоммерческая (CC BY-NC 4.0), сверить на HDX |
| Население по областям и возрасту, 2019 (запасной вариант для доли детей) | `source_data/tjk_admpop_adm1_2019.csv` | UNFPA и Агентство по статистике, через HDX | условия издателя |
| pH почвы | `db/tjk_ph.tif` | ISRIC SoilGrids, `db/fetch_ph_raster.py` | CC BY 4.0 |
| Реки, каналы, водоёмы; здания; дороги | `webapp/static/hydrology.geojson`, `db/buildings_cache/`, `db/roads_cache/` | OpenStreetMap, `db/fetch_hydrology_layer.py`, `db/fetch_buildings.py`, `db/fetch_roads_covariate.py` | ODbL |
| Климат (WorldClim 2.1, современный и CMIP6) | `source_data/tj_clipped/` | worldclim.org, `db/clip_climate_rasters.py` | условия издателя (worldclim.org) |
| Земной покров и другие ковариаты | таблицы БД | Microsoft Planetary Computer, `db/fetch_planetary_computer.py` | по каждому набору |
| Геология (GLiM) | `db/glim_data/` | `db/fetch_geology_layer.py` | условия издателя |
| Цифровая модель рельефа для отмывки | `source_data/Terrain150_4326.tif` | любая открытая ЦМР в EPSG:4326 | по источнику |
| Накопление стока | `source_data/flow/` | расчёт по ЦМР; переносится на WhiteboxTools / pysheds | — |

Во время работы стенда скрипты не обращаются к внешним сервисам. Интернет нужен только один раз, чтобы скачать открытые данные.
