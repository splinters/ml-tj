"""Раздел «Блокноты» демо-стенда: готовые HTML-страницы блокнотов (notebooks_html/) и исходные .ipynb.

Страницы статические: код на сервере не выполняется. Пересборка - python notebooks/build_html.py --execute
(из каталога notebooks). Внешних ссылок в страницах нет, раздел работает без интернета.
"""
from pathlib import Path

from flask import Blueprint, abort, send_from_directory

from i18n import lang, render, tr

ROOT = Path(__file__).resolve().parent.parent
HTML_DIR = ROOT / "notebooks_html"
NB_DIR = ROOT / "notebooks"

bp = Blueprint("notebooks", __name__)

ITEMS = [
    {"stem": "demo_pop_screening", "demo": True,
     "title": ("СОЗ: скрининг участков и индекс риска для детей", "POPs: site screening and the child risk index"),
     "text": ("Откуда данные, модель скрининга и её проверка без утечки данных, кривая отбора, проверка на случайность, "
              "оценка участка без анализов, индекс риска для детей и его устойчивость, план отбора проб.",
              "Where the data come from, the screening model and its leakage-free validation, the selection curve, "
              "a test against chance, assessing a site without analyses, the child risk index and its robustness, "
              "the sampling plan.")},
    {"stem": "demo_metals_xrf", "demo": True,
     "title": ("Тяжёлые металлы: данные XRF, приоритет складов и модель", "Heavy metals: XRF data, warehouse priority and the model"),
     "text": ("Сравнение с нормативами, приоритет складов, где на складе искать очаг, проверка на складе вне "
              "обучения, сколько данных нужно, связь с участками СОЗ.",
              "Comparison with standards, warehouse priority, where to look for the hotspot within a warehouse, testing "
              "on a warehouse left out of training, how much data is needed, link with POPs sites.")},
    {"stem": "01_data_and_qc", "demo": False, "title": ("Данные и контроль качества (металлы)", "Data and quality control (metals)"), "text": ""},
    {"stem": "02_model_pb_lowo", "demo": False, "title": ("Модель Pb и проверка LOWO", "Pb model and LOWO validation"), "text": ""},
    {"stem": "03_all_elements", "demo": False, "title": ("Результаты по всем девяти элементам", "Results for all nine elements"), "text": ""},
    {"stem": "04_priority_and_risk_score", "demo": False, "title": ("Приоритет складов и индекс 0–100", "Warehouse priority and the 0–100 index"), "text": ""},
]
BY_STEM = {i["stem"]: i for i in ITEMS}


def _variant(stem, folder, ext):
    """Английская версия блокнота (stem_en), если она собрана и выбран английский."""
    if lang() == "en" and (folder / f"{stem}_en{ext}").exists():
        return f"{stem}_en"
    return stem


def _loc(i):
    d = {k: (tr(*v) if isinstance(v, tuple) else v) for k, v in i.items()}
    d["html_stem"], d["ipynb_stem"] = _variant(i["stem"], HTML_DIR, ".html"), _variant(i["stem"], NB_DIR, ".ipynb")
    d["has_html"] = (HTML_DIR / f"{d['html_stem']}.html").exists()
    d["has_ipynb"] = (NB_DIR / f"{d['ipynb_stem']}.ipynb").exists()
    return d


def _available():
    return [_loc(i) for i in ITEMS]


def _known(stem):
    return stem in BY_STEM or (stem.endswith("_en") and stem[:-3] in BY_STEM)


@bp.route("/notebooks")
def notebooks_index():
    return render("notebooks.html", items=_available())


@bp.route("/notebooks/<stem>")
def notebook_view(stem):
    item = BY_STEM.get(stem)
    if not item:
        abort(404)
    item = _loc(item)
    if not item["has_html"]:
        abort(404)
    return render("notebook_view.html", item=item, items=_available())


@bp.route("/notebooks/raw/<stem>.html")
def notebook_raw(stem):
    if not _known(stem):
        abort(404)
    return send_from_directory(HTML_DIR, f"{stem}.html")


@bp.route("/notebooks/ipynb/<stem>.ipynb")
def notebook_ipynb(stem):
    if not _known(stem):
        abort(404)
    return send_from_directory(NB_DIR, f"{stem}.ipynb", as_attachment=True)
