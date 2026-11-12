"""Общий код демонстрационных блокнотов: оформление графиков, подложка рельефа, форматирование чисел.

Всё работает локально: подложка строится из растра рельефа проекта
(source_data/Terrain150_4326.tif), обращений в интернет нет.
"""
import os
from pathlib import Path

import matplotlib
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
TERRAIN = ROOT / "source_data" / "Terrain150_4326.tif"
# Папка с данными: ML_TJ_DATA_ROOT, иначе корень проекта (если там есть данные пилота),
# иначе синтетический набор data/synthetic из открытой версии.
_REAL = ROOT / "source_data" / "chemicals_tj" / "pop_layer" / "pop_sites.csv"
DATA = Path(os.getenv("ML_TJ_DATA_ROOT") or (ROOT if _REAL.exists() else ROOT / "data" / "synthetic"))
SYNTHETIC = (DATA / "README.md").exists() and "synthetic" in DATA.as_posix()

# палитра стенда: один акцент, серые для фона, тёплый для предупреждений
BLUE, ORANGE, RED, GREEN, GREY, DARK = "#1f6fb2", "#d7782a", "#a3261f", "#3c8d5a", "#9aa3ae", "#16253d"
CLASS_COLORS = {"низкий": "#3c8d5a", "средний": "#e3b23c", "высокий": "#d7782a", "очень высокий": "#a3261f"}
CLASS_EN = {"низкий": "low", "средний": "medium", "высокий": "high", "очень высокий": "very high"}
LANG = "ru"  # язык подписей и чисел; английские блокноты вызывают setup("en")


def setup(lang="ru"):
    global LANG
    LANG = lang
    matplotlib.rcParams.update({
        "figure.dpi": 110, "savefig.dpi": 110, "font.size": 10, "axes.titlesize": 11, "axes.titleweight": "bold",
        "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True, "grid.alpha": .25,
        "axes.edgecolor": "#888", "axes.labelcolor": "#333", "xtick.color": "#555", "ytick.color": "#555",
        "legend.frameon": False, "figure.autolayout": True,
    })
    import warnings
    warnings.filterwarnings("ignore")
    if SYNTHETIC:
        note("Синтетические данные (data/synthetic): структура как у данных пилота, значения придуманы. "
             "Числа и выводы в тексте блокнота относятся к данным пилота и с результатами ниже не совпадают."
             if lang != "en" else
             "Synthetic data (data/synthetic): same structure as the pilot data, invented values. "
             "Figures and conclusions in the text refer to the pilot data and will not match the results below.")


def f(x, nd=1):
    """RU: «12 345,6» (запятая, пробел между разрядами); EN: «12,345.6»."""
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "—"
    if LANG == "en":
        return f"{x:,.{nd}f}"
    return f"{x:,.{nd}f}".replace(",", " ").replace(".", ",")


def pct(x, nd=0):
    return f(100 * x, nd) + ("%" if LANG == "en" else " %")


def comma_axis(ax, axis="y", nd=1):
    fmt = matplotlib.ticker.FuncFormatter(lambda v, _: f(v, nd))
    (ax.yaxis if axis == "y" else ax.xaxis).set_major_formatter(fmt)


def hillshade(ax, bounds, max_px=900, alpha=.55):
    """Подложка рельефа (теневая отмывка) из локального растра; bounds = (lon_min, lat_min, lon_max, lat_max)."""
    if not TERRAIN.exists():
        return
    import rasterio
    from rasterio.windows import from_bounds
    with rasterio.open(TERRAIN) as r:
        w = from_bounds(*bounds, transform=r.transform).round_offsets().round_lengths()
        scale = max(w.width, w.height) / max_px
        shape = (max(int(w.height / max(scale, 1)), 2), max(int(w.width / max(scale, 1)), 2))
        z = r.read(1, window=w, out_shape=shape, boundless=True, fill_value=np.nan).astype(float)
        wb = rasterio.windows.bounds(w, r.transform)
    z[z < -100] = np.nan
    gy, gx = np.gradient(np.nan_to_num(z, nan=np.nanmean(z)))
    az, alt = np.radians(315), np.radians(45)
    slope = np.pi / 2 - np.arctan(np.hypot(gx, gy) * 0.08)
    aspect = np.arctan2(-gx, gy)
    hs = np.sin(alt) * np.sin(slope) + np.cos(alt) * np.cos(slope) * np.cos(az - aspect)
    ax.imshow(hs, extent=(wb[0], wb[2], wb[1], wb[3]), cmap="Greys_r", alpha=alpha, origin="upper",
              vmin=-.2, vmax=1.1, zorder=0, interpolation="bilinear")
    ax.set_xlim(bounds[0], bounds[2]); ax.set_ylim(bounds[1], bounds[3])
    ax.set_aspect(1 / np.cos(np.radians((bounds[1] + bounds[3]) / 2)))


def map_axes(ax):
    ax.set_xlabel("longitude" if LANG == "en" else "долгота"); ax.set_ylabel("latitude" if LANG == "en" else "широта")
    ax.grid(alpha=.15)


def note(text):
    """Короткий вывод под расчётом, оформленный как выделенный абзац."""
    from IPython.display import Markdown, display
    display(Markdown(f"> {text}"))


def table(df, **kw):
    """Таблица без индекса, с русскими числами: текст слева, числа справа."""
    from IPython.display import HTML, display
    df = df.copy()
    num = [c for c in df.columns if df[c].dtype.kind in "fiu"]
    for c in df.columns:
        if df[c].dtype.kind == "f":
            nd = kw.get("nd", {}).get(c, 1) if isinstance(kw.get("nd"), dict) else kw.get("nd", 1)
            df[c] = df[c].map(lambda v: f(v, nd))
        elif df[c].dtype.kind in "iu":
            df[c] = df[c].map(lambda v: f(v, 0))
    head = "".join(f'<th style="text-align:{"right" if c in num else "left"}">{_esc(c)}</th>' for c in df.columns)
    rows = "".join("<tr>" + "".join(f'<td style="text-align:{"right" if c in num else "left"}">{_esc(v)}</td>'
                                    for c, v in zip(df.columns, r)) + "</tr>" for r in df.itertuples(index=False))
    display(HTML(f'<table class="demo-table"><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>'))


def _esc(v):
    import html
    return html.escape(str(v))


def cls(k):
    """Подпись класса риска на языке блокнота (ключи данных остаются русскими)."""
    return CLASS_EN.get(k, k) if LANG == "en" else k


def short(name, n=34):
    """Укорачивает название по границе слова."""
    name = str(name)
    if len(name) <= n:
        return name
    cut = name[:n].rsplit(" ", 1)[0].rstrip(",;")
    return cut + "…"
