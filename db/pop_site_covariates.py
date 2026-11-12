"""
Открытые ковариаты для точек участков СОЗ (локальные растры, без сети):
  ph       - pH почвы (SoilGrids, db/tjk_ph.tif; в растре pH*10)
  elev_m   - высота (source_data/Terrain150_4326.tif, 150 м)
  slope_deg- уклон по окну 3x3 того же растра
  pop_500m - население в радиусе ~500 м (WorldPop 2020, db/tjk_worldpop_2020.tif, сумма окна 13x13 пикселей ~1,1 км)
  flowacc_max - максимум накопления стока в окне 5x5 (source_data/flow/FlowAcc4326.tif)
  bio1_temp_c, bio12_precip_mm - WorldClim 2.1 (source_data/tj_clipped/past-trends/wc2.1_30s_bio)
Выход: source_data/chemicals_tj/pop_layer/site_covariates_pop.csv
"""
import glob
from pathlib import Path

import numpy as np
import pandas as pd
import rasterio
from rasterio.windows import Window

ROOT = Path(__file__).resolve().parent.parent
SITES = ROOT / "source_data/chemicals_tj/pop_layer/pop_sites.csv"


def window_vals(r, lon, lat, half):
    row, col = r.index(lon, lat)
    w = Window(col - half, row - half, 2 * half + 1, 2 * half + 1)
    a = r.read(1, window=w, boundless=True, fill_value=r.nodata if r.nodata is not None else np.nan).astype(float)
    if r.nodata is not None:
        a[a == r.nodata] = np.nan
    a[np.abs(a) > 1e30] = np.nan
    return a


_RASTERS = {}


def open_rasters():
    """Открывает растры один раз (кэш на процесс) - нужно стенду для расчёта по новой точке."""
    if not _RASTERS:
        bio = sorted(glob.glob(str(ROOT / "source_data/tj_clipped/past-trends/wc2.1_30s_bio/*.tif")))
        pick = lambda n: next((f for f in bio if f.endswith(f"_{n}.tif")), None)  # noqa: E731
        opn = lambda p: rasterio.open(p) if p and Path(p).exists() else None  # noqa: E731  нет растра - None
        _RASTERS.update({
            "ph": opn(ROOT / "db/tjk_ph.tif"),
            "dem": opn(ROOT / "source_data/Terrain150_4326.tif"),
            "pop": opn(ROOT / "db/tjk_worldpop_2020.tif"),
            "fa": opn(ROOT / "source_data/flow/FlowAcc4326.tif"),
            "b1": opn(pick(1)),
            "b12": opn(pick(12)),
        })
    return _RASTERS


def covariates_at(lon, lat):
    r = open_rasters()
    d = {}
    nan = lambda k: np.full((2 * k + 1, 2 * k + 1), np.nan)  # noqa: E731
    win = lambda key, k: window_vals(r[key], lon, lat, k) if r[key] is not None else nan(k)  # noqa: E731
    v = win("ph", 0)[0, 0]
    d["ph"] = v / 10 if np.isfinite(v) else np.nan
    z = win("dem", 1)
    d["elev_m"] = z[1, 1]
    gy = ((z[2, :] - z[0, :]).mean()) / (2 * 150)
    gx = ((z[:, 2] - z[:, 0]).mean()) / (2 * 150)
    d["slope_deg"] = float(np.degrees(np.arctan(np.hypot(gx, gy)))) if np.all(np.isfinite(z)) else np.nan
    d["pop_500m"] = float(np.nansum(win("pop", 6))) if r["pop"] is not None else np.nan
    d["flowacc_max"] = float(np.nanmax(win("fa", 2))) if r["fa"] is not None else np.nan
    d["bio1_temp_c"] = win("b1", 0)[0, 0]
    d["bio12_precip_mm"] = win("b12", 0)[0, 0]
    return d


def main():
    T = pd.read_csv(SITES, encoding="utf-8-sig", low_memory=False)
    T = T[T.latitude.notna()]
    out = [{"map_id": s.map_id, "tj_code": s.tj_code, **covariates_at(float(s.longitude), float(s.latitude))}
           for _, s in T.iterrows()]
    df = pd.DataFrame(out)
    df.to_csv(ROOT / "source_data/chemicals_tj/pop_layer/site_covariates_pop.csv", index=False, encoding="utf-8-sig")
    print(df.describe().T[["count", "mean", "min", "max"]])


if __name__ == "__main__":
    main()
