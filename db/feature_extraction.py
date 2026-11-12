"""
Универсальный экстрактор признаков "дай координаты - получи ковариаты" -
работает для ЛЮБОЙ точки в Таджикистане, не только для 10 уже известных
складов. Именно это нужно, чтобы новый склад "где угодно на территории"
сразу можно было прогнать через модель без ручной досборки данных.

Предполагает, что страновые слои уже скачаны локально скриптами из
fetch_planetary_computer.py и README_open_data_tajikistan.md:
  tajikistan_dem_30m.tif
  tajikistan_worldcover_10m.tif
  microsoft_buildings_tjk.geojson   (после фильтрации dataset-links.csv)
  hydrorivers_tjk.gpkg              (обрезанный HydroRIVERS по границе TJK)
  worldpop_tjk.tif

Запускать локально - требует geopandas/rioxarray с реальными файлами на диске.
"""
import numpy as np
import requests
import rioxarray
import geopandas as gpd
from shapely.geometry import Point

UTM = "EPSG:32642"  # UTM 42N - метры, для расстояний


class TajikistanFeatureExtractor:
    def __init__(self, data_dir="."):
        self.dem = rioxarray.open_rasterio(f"{data_dir}/tajikistan_dem_30m.tif").squeeze()
        self.worldcover = rioxarray.open_rasterio(f"{data_dir}/tajikistan_worldcover_10m.tif").squeeze()
        self.worldpop = rioxarray.open_rasterio(f"{data_dir}/worldpop_tjk.tif").squeeze()

        self.buildings = gpd.read_file(f"{data_dir}/microsoft_buildings_tjk.geojson").to_crs(UTM)
        self.rivers = gpd.read_file(f"{data_dir}/hydrorivers_tjk.gpkg").to_crs(UTM)

        # пространственный индекс для быстрого поиска ближайших объектов
        self._buildings_sindex = self.buildings.sindex
        self._rivers_sindex = self.rivers.sindex

    def _elevation_and_slope(self, lat, lon):
        elev_center = float(self.dem.sel(x=lon, y=lat, method="nearest").values)
        d = 0.001  # ~100м в градусах, для приближённого градиента
        e_n = float(self.dem.sel(x=lon, y=lat + d, method="nearest").values)
        e_e = float(self.dem.sel(x=lon + d, y=lat, method="nearest").values)
        dz_dy = (e_n - elev_center) / (d * 111_320)
        dz_dx = (e_e - elev_center) / (d * 111_320 * np.cos(np.radians(lat)))
        slope_deg = np.degrees(np.arctan(np.hypot(dz_dx, dz_dy)))
        return elev_center, float(slope_deg)

    def _worldcover_class(self, lat, lon):
        return int(self.worldcover.sel(x=lon, y=lat, method="nearest").values)

    def _population_density(self, lat, lon):
        val = float(self.worldpop.sel(x=lon, y=lat, method="nearest").values)
        return max(val, 0.0)

    def _nearest_building_dist_m(self, point_utm, radius_m=3000):
        buf = point_utm.buffer(radius_m)
        idx = list(self._buildings_sindex.intersection(buf.bounds))
        if not idx:
            return np.nan, 0
        cand = self.buildings.iloc[idx]
        dists = cand.distance(point_utm)
        return float(dists.min()), int((dists < 200).sum())  # ближайшее здание + сколько в радиусе 200м

    def _nearest_river_dist_m(self, point_utm, radius_m=20000):
        buf = point_utm.buffer(radius_m)
        idx = list(self._rivers_sindex.intersection(buf.bounds))
        if not idx:
            return np.nan
        cand = self.rivers.iloc[idx]
        return float(cand.distance(point_utm).min())

    @staticmethod
    def _soil_ph(lat, lon):
        try:
            r = requests.get(
                "https://rest.isric.org/soilgrids/v2.0/properties/query",
                params={"lon": lon, "lat": lat, "property": "phh2o",
                        "depth": "0-5cm", "value": "mean"},
                timeout=15,
            )
            r.raise_for_status()
            layers = r.json()["properties"]["layers"]
            ph_x10 = layers[0]["depths"][0]["values"]["mean"]
            return ph_x10 / 10.0  # SoilGrids отдаёт pH*10
        except Exception as e:
            print(f"  SoilGrids недоступен ({e}), pH = NaN")
            return np.nan

    def extract(self, lat, lon):
        point_utm = gpd.GeoSeries([Point(lon, lat)], crs=4326).to_crs(UTM).iloc[0]
        elevation_m, slope_deg = self._elevation_and_slope(lat, lon)
        wc_class = self._worldcover_class(lat, lon)
        pop_density = self._population_density(lat, lon)
        dist_building_m, n_buildings_200m = self._nearest_building_dist_m(point_utm)
        dist_river_m = self._nearest_river_dist_m(point_utm)
        soil_ph = self._soil_ph(lat, lon)

        return {
            "elevation_m": elevation_m,
            "slope_deg": slope_deg,
            "worldcover_class": wc_class,
            "is_agricultural": wc_class == 40,
            "is_builtup": wc_class == 50,
            "population_density": pop_density,
            "dist_to_nearest_building_m": dist_building_m,
            "n_buildings_within_200m": n_buildings_200m,
            "dist_to_river_m": dist_river_m,
            "soil_ph": soil_ph,
        }


if __name__ == "__main__":
    # пример: те же 10 реальных складов, теперь через универсальный экстрактор
    from xrf_sites import XRF_SITES as WAREHOUSES  # source_data/xrf_warehouses.csv
    fx = TajikistanFeatureExtractor(data_dir=".")
    for name, (lat, lon) in WAREHOUSES.items():
        feats = fx.extract(lat, lon)
        print(name, feats)
