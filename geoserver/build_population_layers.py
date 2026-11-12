#!/usr/bin/env python3
"""
Сборка слоёв населения для GeoServer из возрастных растров WorldPop R2025A.

Из 65 исходных растров (группы по полу и возрасту) собираются 5 слоёв:
    children_u18_2025   дети 0-17 лет          готовый растр T_Under_18
    children_u5_2025    дети 0-4 года          t_00 + t_01
    children_5_17_2025  дети 5-17 лет          T_Under_18 - (t_00 + t_01)
    population_2025     всё население          T_F + T_M
    women_15_49_2025    женщины 15-49 лет      f_15 + f_20 + ... + f_45

Значение пикселя - число человек на пиксель ~100 м. Для каждого слоя рядом
пишется стиль .sld того же имени: границы цветов - квантили ненулевых пикселей
этого слоя, поэтому одинаковый цвет на разных слоях означает одинаковую долю
самых населённых мест.

Выход - отдельная папка, которую публикует publish_rasters.py:
    python geoserver/build_population_layers.py
    python geoserver/publish_rasters.py --dir geoserver_rasters/worldpop_2025 \
        --workspace tj --mode external --dry-run

Нужно: Python 3.9+, numpy, rasterio (GDAL входит в колёса rasterio).
Интернет не нужен. Расчёт идёт блоками, памяти хватает 1-2 ГБ.
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.windows import Window

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "source_data" / "worldpop_r2025a"
OUT = ROOT / "geoserver_rasters" / "worldpop_2025"
SUFFIX = "_2025_CN_100m_R2025A_v1.tif"
NODATA = -99999.0


def f(code):
    return f"tjk_{code}{SUFFIX}"


# имя слоя: (заголовок RU, заголовок EN, растры со знаком +, растры со знаком -, цвета)
PURD = ["#f1eef6", "#d7b5d8", "#df65b0", "#dd1c77", "#980043"]
LAYERS = {
    "children_u18_2025": ("Дети до 18 лет, 2025", "Children under 18, 2025",
                          [f("T_Under_18")], [], PURD),
    "children_u5_2025": ("Дети до 5 лет, 2025", "Children under 5, 2025",
                         [f("t_00"), f("t_01")], [], ["#feedde", "#fdbe85", "#fd8d3c", "#e6550d", "#a63603"]),
    "children_5_17_2025": ("Дети 5-17 лет, 2025", "Children aged 5-17, 2025",
                           [f("T_Under_18")], [f("t_00"), f("t_01")],
                           ["#edf8fb", "#b3cde3", "#8c96c6", "#8856a7", "#810f7c"]),
    "population_2025": ("Население, 2025", "Total population, 2025",
                        [f("T_F"), f("T_M")], [], ["#ffffcc", "#c2e699", "#78c679", "#31a354", "#006837"]),
    "women_15_49_2025": ("Женщины 15-49 лет, 2025", "Women aged 15-49, 2025",
                         [f(f"f_{a}") for a in (15, 20, 25, 30, 35, 40, 45)], [],
                         ["#fef0d9", "#fdcc8a", "#fc8d59", "#e34a33", "#b30000"]),
}
QUANTILES = (0.50, 0.75, 0.90, 0.97, 0.995)   # пороги цветов по ненулевым пикселям


def read_block(ds, win):
    a = ds.read(1, window=win).astype("float64")
    nd = ds.nodata
    bad = ~np.isfinite(a) | (a < 0) | ((a == nd) if nd is not None else False)
    return a, bad


def build(name, plus, minus, src, out, block=1024):
    paths = [src / p for p in plus + minus]
    missing = [p.name for p in paths if not p.exists()]
    if missing:
        return None, "нет файлов: " + ", ".join(missing)
    dss = [rasterio.open(p) for p in paths]
    ref = dss[0]
    for d in dss[1:]:
        if (d.width, d.height, d.transform, d.crs) != (ref.width, ref.height, ref.transform, ref.crs):
            return None, f"сетка {d.name} не совпадает с {ref.name}"
    prof = ref.profile.copy()
    prof.update(driver="GTiff", dtype="float32", count=1, nodata=NODATA, compress="deflate",
                predictor=3, tiled=True, blockxsize=256, blockysize=256, BIGTIFF="IF_SAFER")
    tif = out / f"{name}.tif"
    rng = np.random.default_rng(0)
    sample, total = [], 0.0
    with rasterio.open(tif, "w", **prof) as dst:
        for r0 in range(0, ref.height, block):
            for c0 in range(0, ref.width, block):
                win = Window(c0, r0, min(block, ref.width - c0), min(block, ref.height - r0))
                acc, bad = read_block(dss[0], win)
                for d in dss[1:len(plus)]:
                    a, b = read_block(d, win); acc += np.where(b, 0, a); bad &= b
                for d in dss[len(plus):]:
                    a, b = read_block(d, win); acc -= np.where(b, 0, a)
                acc = np.clip(acc, 0, None)              # разность может уйти чуть ниже 0
                acc[bad] = NODATA
                dst.write(acc.astype("float32"), 1, window=win)
                v = acc[(~bad) & (acc > 0)]
                total += float(v.sum())
                if v.size:
                    sample.append(rng.choice(v, size=min(v.size, 20000), replace=False))
        dst.build_overviews([2, 4, 8, 16, 32], Resampling.average)
        dst.update_tags(ns="rio_overview", resampling="average")
    for d in dss:
        d.close()
    s = np.concatenate(sample) if sample else np.array([1.0])
    breaks = [float(np.quantile(s, q)) for q in QUANTILES]
    for i in range(1, len(breaks)):                     # строго по возрастанию
        breaks[i] = max(breaks[i], breaks[i - 1] * 1.01 + 1e-6)
    return {"tif": tif, "total": total, "breaks": breaks}, None


def fmt(x):
    return f"{x:.3g}"


def write_sld(name, title_ru, title_en, colors, breaks, out):
    entries = [f'     <ColorMapEntry color="#000000" quantity="{NODATA:g}" opacity="0"/>',
               f'     <ColorMapEntry color="{colors[0]}" quantity="0" opacity="0"/>']
    labels = []
    for i, (c, b) in enumerate(zip(colors, breaks)):
        op = [0.35, 0.6, 0.75, 0.85, 0.95][i]
        lab = f"{fmt(b)} чел./пиксель"
        labels.append(lab)
        entries.append(f'     <ColorMapEntry color="{c}" quantity="{fmt(b)}" opacity="{op}" label="{lab}"/>')
    sld = f"""<?xml version="1.0" encoding="UTF-8"?>
<StyledLayerDescriptor version="1.0.0" xmlns="http://www.opengis.net/sld" xmlns:ogc="http://www.opengis.net/ogc">
 <NamedLayer><Name>{name}</Name>
  <UserStyle><Name>{name}</Name><Title>{title_ru} / {title_en}</Title>
   <Abstract>WorldPop R2025A, человек на пиксель ~100 м; границы - квантили {", ".join(f"{q:g}" for q in QUANTILES)} ненулевых пикселей</Abstract>
   <FeatureTypeStyle><Rule><RasterSymbolizer>
    <ColorMap type="ramp">
{chr(10).join(entries)}
    </ColorMap>
   </RasterSymbolizer></Rule></FeatureTypeStyle>
  </UserStyle></NamedLayer>
</StyledLayerDescriptor>
"""
    (out / f"{name}.sld").write_text(sld, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", type=Path, default=SRC, help="папка с растрами WorldPop R2025A")
    ap.add_argument("--out", type=Path, default=OUT, help="куда положить слои и стили")
    ap.add_argument("--only", nargs="*", choices=list(LAYERS), help="собрать только эти слои")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    rows, failed = [], 0
    for name, (tru, ten, plus, minus, colors) in LAYERS.items():
        if a.only and name not in a.only:
            continue
        print(f"{name}: сборка из {len(plus) + len(minus)} растров...", flush=True)
        try:
            res, err = build(name, plus, minus, a.src, a.out)
        except Exception as e:                          # один слой не должен останавливать остальные
            res, err = None, f"{type(e).__name__}: {e}"
        if err:
            print(f"   пропущен: {err}"); failed += 1; continue
        write_sld(name, tru, ten, colors, res["breaks"], a.out)
        mb = res["tif"].stat().st_size / 1e6
        print(f"   всего {res['total']:,.0f} чел.; границы {', '.join(fmt(b) for b in res['breaks'])}; {mb:.0f} МБ")
        rows.append(f"{name}\t{tru}\t{ten}\t{res['total']:.0f}\t" + "\t".join(fmt(b) for b in res["breaks"]))
    tsv = a.out / "layers.tsv"                          # при --only строки других слоёв сохраняются
    keep = {}
    if tsv.exists():
        for line in tsv.read_text(encoding="utf-8").splitlines()[1:]:
            if line.strip():
                keep[line.split("\t", 1)[0]] = line
    keep.update({r.split("\t", 1)[0]: r for r in rows})
    order = [n for n in LAYERS if n in keep]
    tsv.write_text("layer\ttitle_ru\ttitle_en\ttotal\t" + "\t".join(f"q{q:g}" for q in QUANTILES) + "\n" +
                   "\n".join(keep[n] for n in order) + "\n", encoding="utf-8")
    print(f"Готово: {a.out}  (собрано {len(rows)}, пропущено {failed})")
    sys.exit(1 if failed and not rows else 0)


if __name__ == "__main__":
    main()
