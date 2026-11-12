r"""
Обрезает локальные глобальные растры WorldClim (past-trends И future-trends)
по границе Таджикистана - запускать НА ВАШЕЙ машине, где лежат исходные TIF.

Открытый стек (rasterio/GDAL), без ArcGIS - соответствует предпочтению не
привязываться к проприетарному инструменту, хотя то же самое можно сделать
и в ArcGIS Pro (Clip Raster / Extract by Mask), если так удобнее.

Использование (структура папки определяется автоматически):
    python clip_climate_rasters.py "D:\Projects\gca-portal\data\past-trends" "D:\...\tajikistan_clipped\past"
    python clip_climate_rasters.py "D:\Projects\gca-portal\data\future-trends" "D:\...\tajikistan_clipped\future"

past-trends: bio_1..19, prec_01..12, tavg_01..12 - плоская структура по
подпапке на переменную (wc2.1_30s_bio / wc2.1_30s_prec / wc2.1_30s_tavg).

future-trends: подпапка на каждую комбинацию сценарий+период
(wc2.1_30s_bioc_IPSL-CM6A-LR_sspXXX_YYYY-YYYY), внутри bio01..19.tif -
оба сценария (ssp245, ssp585), все 4 периода (2021-2040 ... 2081-2100).

Дальше переносите обе папки-результата (уже небольшие) туда, где работает
основной проект - остальное подключается локально, без сети.
"""
import os
import sys
import glob
import rasterio
from rasterio.windows import from_bounds
from rasterio.transform import Affine

# те же границы, что используются во всём проекте local_demo_stand
TJ_BBOX = dict(west=67.0, south=36.4, east=75.3, north=41.1)


def clip_one(src_path, dst_path, bbox):
    with rasterio.open(src_path) as src:
        window = from_bounds(bbox["west"], bbox["south"], bbox["east"], bbox["north"],
                              transform=src.transform)
        window = window.round_offsets().round_lengths()
        transform = src.window_transform(window)
        data = src.read(1, window=window)

        profile = src.profile.copy()
        profile.update({
            "height": data.shape[0],
            "width": data.shape[1],
            "transform": transform,
        })

        with rasterio.open(dst_path, "w", **profile) as dst:
            dst.write(data, 1)

    src_mb = os.path.getsize(src_path) / 1e6
    dst_mb = os.path.getsize(dst_path) / 1e6
    print(f"  {os.path.basename(src_path)}: {src_mb:.1f}МБ -> {dst_mb:.1f}МБ")


def clip_past_trends(src_root, dst_root):
    """past-trends: плоская структура по подпапке на переменную
    (wc2.1_30s_bio/prec/tavg), см. clip_one выше."""
    subfolders = ["wc2.1_30s_bio", "wc2.1_30s_prec", "wc2.1_30s_tavg"]

    total = 0
    for sub in subfolders:
        src_dir = os.path.join(src_root, sub)
        if not os.path.isdir(src_dir):
            print(f"Пропускаю (нет папки): {src_dir}")
            continue
        dst_dir = os.path.join(dst_root, sub)
        os.makedirs(dst_dir, exist_ok=True)

        tifs = sorted(glob.glob(os.path.join(src_dir, "*.tif")))
        print(f"\n{sub}: найдено {len(tifs)} файлов")
        for tif in tifs:
            dst_path = os.path.join(dst_dir, os.path.basename(tif))
            clip_one(tif, dst_path, TJ_BBOX)
            total += 1

    print(f"\nГотово: обрезано {total} файлов (историческая часть) в {dst_root}")


def clip_future_trends(src_root, dst_root):
    """future-trends: по подпапке на каждую комбинацию сценарий+период
    (wc2.1_30s_bioc_IPSL-CM6A-LR_sspXXX_YYYY-YYYY/...bio01..19.tif) -
    структура найдена по реальному списку файлов пользователя, не угадана."""
    total = 0
    subdirs = sorted(glob.glob(os.path.join(src_root, "wc2.1_30s_bioc_*")))
    print(f"Найдено папок сценарий+период: {len(subdirs)}")
    for subdir in subdirs:
        name = os.path.basename(subdir)
        dst_dir = os.path.join(dst_root, name)
        os.makedirs(dst_dir, exist_ok=True)
        tifs = sorted(glob.glob(os.path.join(subdir, "*.tif")))
        print(f"\n{name}: {len(tifs)} файлов")
        for tif in tifs:
            dst_path = os.path.join(dst_dir, os.path.basename(tif))
            clip_one(tif, dst_path, TJ_BBOX)
            total += 1
    print(f"\nГотово: обрезано {total} файлов (будущие проекции) в {dst_root}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    src_root, dst_root = sys.argv[1], sys.argv[2]
    # автоопределение по имени папок внутри: past-trends содержит
    # wc2.1_30s_bio/prec/tavg, future-trends - wc2.1_30s_bioc_* по сценарию+периоду
    if glob.glob(os.path.join(src_root, "wc2.1_30s_bioc_*")):
        clip_future_trends(src_root, dst_root)
    else:
        clip_past_trends(src_root, dst_root)
    print("\nПеренесите папку-результат туда, где работает основной проект, "
          "и используйте extract_climate_from_local.py для загрузки в БД.")
