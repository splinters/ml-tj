# Input data

*Russian version: [README.ru.md](README.ru.md).*

Real data files are not included. The folder `synthetic/` holds an invented dataset in the same format (made by `tools/make_synthetic_data.py`); the notebooks and the screening model use it when no real data are present, or the folder set in `ML_TJ_DATA_ROOT`. The tables list what the scripts expect, where it comes from and under which terms. Paths are relative to the repository root. Check the licences of open datasets on the publishers' pages before redistributing.

## Client and national data (not published)

| Data | Expected path | How it is produced |
|---|---|---|
| Registry of contaminated sites, site cards and attached documents | `source_data/chemicals_tj/` | `db/fetch_chemicals_tj.py` from the public map of chemicals.tj; terms of the site owner |
| Values transcribed from scanned protocols | `source_data/chemicals_tj/manual/*.csv` | entered manually; client data |
| Warehouse outlines and XRF measurements | `source_data/warehouses.geojson`, `source_data/warehouse_points_enriched.csv` and others | client data; loaded into PostgreSQL by `db/import_data.py` |
| Data export for the notebooks | `data/tajikistan_soil_ml_export.gpkg` (or `ML_TJ_GPKG`) | `db/export_geopackage.py` |

## Open data

| Data | Expected path | Source | Licence |
|---|---|---|---|
| Population by age and sex, 2025, 100 m, constrained (WorldPop R2025A). Child index: `T_Under_18`, `t_00`, `t_01`, `T_F`, `T_M`; GeoServer layers also `f_15` … `f_45` | `source_data/worldpop_r2025a/tjk_*_2025_CN_100m_R2025A_v1.tif` | data.worldpop.org, Global2 R2025A | CC BY 4.0 |
| Population 2020, constrained (covariate of the metals model) | `db/tjk_worldpop_2020.tif` | `db/fetch_landcover_population.py` | CC BY 4.0 |
| Relative Wealth Index | `source_data/tjk_relative_wealth_index.csv` | Meta Data for Good, via HDX | non-commercial (CC BY-NC 4.0), check on HDX |
| Population by region and age, 2019 (fallback for child shares) | `source_data/tjk_admpop_adm1_2019.csv` | UNFPA / Agency on Statistics, via HDX | publisher terms |
| Soil pH | `db/tjk_ph.tif` | ISRIC SoilGrids, `db/fetch_ph_raster.py` | CC BY 4.0 |
| Rivers, canals, water bodies; buildings; roads | `webapp/static/hydrology.geojson`, `db/buildings_cache/`, `db/roads_cache/` | OpenStreetMap, `db/fetch_hydrology_layer.py`, `db/fetch_buildings.py`, `db/fetch_roads_covariate.py` | ODbL |
| Climate (WorldClim 2.1, current and CMIP6) | `source_data/tj_clipped/` | worldclim.org, `db/clip_climate_rasters.py` | publisher terms (worldclim.org) |
| Land cover and other covariates | database tables | Microsoft Planetary Computer, `db/fetch_planetary_computer.py` | per dataset |
| Geology (GLiM) | `db/glim_data/` | `db/fetch_geology_layer.py` | publisher terms |
| Digital elevation model for hillshade | `source_data/Terrain150_4326.tif` | any open DEM reprojected to EPSG:4326 | per source |
| Flow accumulation | `source_data/flow/` | computed from a DEM; being moved to WhiteboxTools / pysheds | — |

The scripts do not call external services when the stand is running. Internet access is only needed once, to download the open data.
