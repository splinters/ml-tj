"""
Забирает Copernicus DEM (рельеф) и ESA WorldCover (землепользование) для
всей территории Таджикистана через Microsoft Planetary Computer - один
консолидированный, бесплатный, без ключа API источник для обоих слоёв.

ВАЖНО: запускать локально (или в Claude Code с доступом в интернет) -
planetarycomputer.microsoft.com недоступен из этой песочницы.

pip install pystac-client planetary-computer rioxarray rasterio odc-stac
"""
import planetary_computer
import pystac_client
import rioxarray
import odc.stac

BBOX = [67.0, 36.4, 75.3, 41.1]  # min_lon, min_lat, max_lon, max_lat - Таджикистан с запасом

catalog = pystac_client.Client.open(
    "https://planetarycomputer.microsoft.com/api/stac/v1",
    modifier=planetary_computer.sign_inplace,
)

print("=== Copernicus DEM GLO-30 ===")
search = catalog.search(collections=["cop-dem-glo-30"], bbox=BBOX)
items = list(search.items())
print(f"Найдено тайлов DEM: {len(items)}")
dem = odc.stac.load(items, bbox=BBOX, resolution=0.00027)  # ~30м в градусах
dem["data"].rio.to_raster("tajikistan_dem_30m.tif")
print("Сохранено: tajikistan_dem_30m.tif")

print("\n=== ESA WorldCover 10м (землепользование) ===")
search = catalog.search(collections=["esa-worldcover"], bbox=BBOX)
items = list(search.items())
print(f"Найдено тайлов WorldCover: {len(items)}")
wc = odc.stac.load(items, bbox=BBOX, resolution=0.00009)  # ~10м в градусах
wc["map"].rio.to_raster("tajikistan_worldcover_10m.tif")
print("Сохранено: tajikistan_worldcover_10m.tif")

print("""
Готово. Классы ESA WorldCover, которые обычно нужны для модели:
  10 - деревья, 20 - кустарники, 30 - травяной покров,
  40 - сельхозземли (путь через урожай), 50 - застройка,
  60 - голая почва, 80 - вода
""")
