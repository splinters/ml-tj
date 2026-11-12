"""
Геологический слой (упрощённая литология) - GLiM v1.0, грубая версия
0.5 градуса (Hartmann & Moosdorf, 2012, PANGAEA). Единственный из трёх
новых слоёв, чей внутренний формат я не смог проверить вживую - сайт
epic.awi.de блокирует автоматический доступ из песочницы (антибот-система
Anubis). У вас на реальной машине, скорее всего, отработает - тот же
паттерн, что был с Overpass/зданиями раньше в этом проекте.

Если скрипт упадёт на разборе архива - результат print(os.listdir(...))
покажет, что реально внутри, дальше поправим за один шаг.

Запуск: python db/fetch_geology_layer.py
"""
import os
import io
import zipfile
import requests
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
EXTRACT_DIR = os.path.join(HERE, "glim_data")
GLIM_URL = "https://epic.awi.de/31092/1/hartmann-moosdorf_2012.zip"


def download_and_extract():
    if os.path.exists(EXTRACT_DIR) and os.listdir(EXTRACT_DIR):
        print(f"Уже скачано: {EXTRACT_DIR}")
        return

    print(f"Скачиваю {GLIM_URL} ...")
    r = requests.get(GLIM_URL, timeout=60, headers={"User-Agent": "Mozilla/5.0 (research script)"})
    r.raise_for_status()
    os.makedirs(EXTRACT_DIR, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        z.extractall(EXTRACT_DIR)
    print("Содержимое архива:")
    for root, _, files in os.walk(EXTRACT_DIR):
        for f in files:
            print(" ", os.path.join(root, f))


def find_files(ext):
    out = []
    for root, _, files in os.walk(EXTRACT_DIR):
        for f in files:
            if f.lower().endswith(ext):
                out.append(os.path.join(root, f))
    return out


def main():
    download_and_extract()

    # пробуем по очереди самые вероятные форматы для такого маленького архива
    tifs = find_files(".tif")
    ascs = find_files(".asc")
    shps = find_files(".shp")
    txts = find_files(".txt") + find_files(".csv")

    print(f"\nНайдено: .tif={len(tifs)}, .asc={len(ascs)}, .shp={len(shps)}, .txt/.csv={len(txts)}")

    if tifs or ascs:
        import rasterio
        path = (tifs or ascs)[0]
        print(f"Пробую открыть как растр: {path}")
        with rasterio.open(path) as src:
            print("  размер:", src.width, "x", src.height, " CRS:", src.crs, " bounds:", src.bounds)
            print("  -> похоже, сработает как растровый слой, аналогично населению (PNG-подложка)")
        return

    if shps:
        import geopandas as gpd
        path = shps[0]
        print(f"Пробую открыть как векторный слой: {path}")
        gdf = gpd.read_file(path)
        print("  колонки:", list(gdf.columns))
        print("  геометрия:", gdf.geom_type.unique())
        print("  -> похоже, сработает как векторный слой, аналогично гидрологии")
        return

    if txts:
        print(f"Похоже на текстовый grid: {txts[0]}")
        print("  первые строки:")
        with open(txts[0]) as f:
            for _ in range(5):
                print("   ", f.readline().rstrip())
        print("  -> формат нестандартный, нужно будет написать парсер под конкретную структуру - пришлите вывод")
        return

    print("Не нашёл ни одного узнаваемого формата - пришлите список файлов выше, разберёмся.")


if __name__ == "__main__":
    main()
