# ML TJ: soil contamination screening and child risk index (Tajikistan)

*Preliminary open-source release of the pilot. Russian version: [README.ru.md](README.ru.md).*

The pilot uses machine learning and open geodata to support the inventory of legacy pesticide and agrochemical sites in Tajikistan:

- **POPs screening.** A model that ranks sites by the probability of strong DDT/HCH contamination, so that sites without laboratory analyses can be surveyed in a sensible order.
- **Child risk index.** Hazard × children nearby × vulnerability, following the CCRI-DRM logic: risk = (H·E·V)^(1/3). Children come from WorldPop R2025A age rasters, poverty from the Meta Relative Wealth Index.
- **Heavy metals (XRF).** A model that ranks points inside a warehouse by expected lead (Pb) contamination; it is validated leave-one-warehouse-out.
- **Demo stand.** A Flask and Leaflet web application in Russian and English. It shows maps, model pages, methodology, data sources, notebooks, and climate and population layers served through GeoServer.

All components run in an isolated network: no CDN and no external calls at runtime, with Leaflet bundled. The OpenStreetMap basemap can be switched off.

## Repository structure

| Folder | Contents |
|---|---|
| `webapp/` | the demo stand: Flask app, templates (RU + EN), bundled Leaflet, methodology documents |
| `db/` | data pipelines and models: database schema, import, open-data covariates, POPs layer, screening model, child risk index, heavy-metals model, norms reference |
| `notebooks/` | Jupyter notebooks that reproduce the calculations without a database (`demo_pop_screening`, `demo_metals_xrf`, `01`–`04`); results are stripped |
| `geoserver/` | building population layers from WorldPop and publishing GeoTIFF + SLD to GeoServer over REST |
| `deploy/` | example nginx / systemd configuration |
| `docs/` | installation guide, metals methodology, POPs layer data dictionary |
| `data/` | synthetic dataset (`data/synthetic/`) and a description of the real inputs, see [data/README.md](data/README.md) |
| `tools/` | the script that builds this public release from the working project |

## Quick start: the demo stand on synthetic data

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
createdb soil_ml && psql -d soil_ml -c "CREATE EXTENSION postgis"     # PostgreSQL 14+ with PostGIS
export DATABASE_URL=postgresql://localhost:5432/soil_ml
python db/load_synthetic.py --reset          # schema, norms, synthetic warehouses, model, zones
python notebooks/build_html.py --out notebooks_html --execute   # optional: Notebooks section of the stand
DEMO_PASSWORD=choose-a-password python webapp/app.py           # http://127.0.0.1:5050 (user: demo)
```

Every section of the stand works on the synthetic data: heavy-metals map and tables, warehouse priority, norms, models, POPs map with the screening model and the child risk index, methodology, notebooks. Layers that need large open rasters are empty until those rasters are downloaded: relief, population, pH, rivers (see [data/README.md](data/README.md)). The Climate and population section needs GeoServer.

For real data, the full setup (PostGIS, data import, covariates, model run) is described in [docs/SETUP.md](docs/SETUP.md) (in Russian). Settings are read from `.env` (see `.env.example`).

Typical POPs pipeline:

Notebooks on the synthetic data:

```bash
pip install -r notebooks/requirements-notebooks.txt
cd notebooks && jupyter lab        # demo_pop_screening, demo_metals_xrf, 01–04
```

```bash
python db/fetch_chemicals_tj.py --download-docs   # public site registry chemicals.tj (needs internet once)
python db/extract_pop_tables.py
python db/build_pop_layer.py
python db/pop_site_covariates.py
python db/pop_screening_model.py
python db/pop_child_risk_index.py                 # needs WorldPop R2025A + RWI, see data/README.md
python db/pop_threshold_scenarios.py              # optional: 10 / 50 / 100 mg/kg scenarios
```

Population layers for GeoServer:

```bash
python geoserver/build_population_layers.py
python geoserver/publish_rasters.py --dir geoserver_rasters/worldpop_2025 --workspace ML --mode upload --dry-run
```

## Data

This repository contains **no national or client data**; `data/synthetic/` is invented data in the same format. That covers survey results, XRF measurements, site coordinates from restricted sources and personal data. Run outputs are excluded as well. The [data/README.md](data/README.md) file lists every input, where it comes from, its licence and where the scripts expect it. Open datasets (WorldPop, Meta RWI, SoilGrids, WorldClim, OpenStreetMap, JRC Global Surface Water) are downloaded by the scripts or by the user.

## Status and limitations

- This is a pilot. The models are trained on 169 analysed POPs sites and 10 surveyed warehouses. They support the ordering of surveys and do not replace laboratory analyses. Validation results and limitations are given in the methodology documents (`webapp/templates/docs/`, PDF in `webapp/static/docs/`).
- The repository includes a **synthetic dataset** (`data/synthetic/`) with the same files and columns as the pilot data. The notebooks and the screening model run on it out of the box, without client data. Names, texts and coordinates are invented, and contamination is re-generated from synthetic features, so the figures differ from the pilot results described in the notebook text and the methodology. To use real data, set `ML_TJ_DATA_ROOT` to a folder with the same structure.
- Flow accumulation was computed in a proprietary GIS. It is being moved to open tools (WhiteboxTools or pysheds).

## Licence

Code: MIT, see [LICENSE](LICENSE). The final licence is confirmed by the data owner before publication. Third-party components are listed in [THIRD_PARTY.md](THIRD_PARTY.md). Data sources keep their own licences, see [data/README.md](data/README.md).
