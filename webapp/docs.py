"""Раздел «Документы» демо-стенда: методика СОЗ, очистка и подготовка данных, презентация (PDF).

Страницы статические: текст методики СОЗ хранится как готовый HTML-фрагмент
(templates/docs/pop_methodology_body.html), PDF-файлы — в static/docs/. Внешних ссылок нет.
"""
from pathlib import Path

from flask import Blueprint, abort, send_from_directory

from i18n import render, tpl, tr

HERE = Path(__file__).resolve().parent
FILES = HERE / "static" / "docs"

bp = Blueprint("docs", __name__)

DOCS = [
    {"title": ("Методика пилота: скрининг участков СОЗ и риск для детей",
               "Pilot methodology: POPs site screening and risk to children"),
     "href": "/docs/pop-methodology", "pdf": ("pop_methodology.pdf", "pop_methodology_en.pdf"),
     "text": ("Данные, модель скрининга и её проверка, индекс риска для детей, применение, "
              "качество данных chemicals.tj, связь с металлами, ограничения.",
              "Data, the screening model and its validation, the child risk index, application, "
              "chemicals.tj data quality, link with metals, limitations.")},
    {"title": ("Методика: тяжёлые металлы (XRF)", "Methodology: heavy metals (XRF)"), "href": "/about", "pdf": None,
     "text": ("Что моделируется, данные, модель Pb и проверка LOWO, результаты по девяти элементам, как читать карту.",
              "What is modelled, data, the Pb model and LOWO validation, results for nine elements, how to read the map.")},
    {"title": ("Очистка и подготовка данных", "Data cleaning and preparation"), "href": "/docs/data-preparation",
     "pdf": ("data_preparation.pdf", "data_preparation_en.pdf"),
     "text": ("Как из полевых замеров XRF и материалов портала chemicals.tj получены проверяемые таблицы: загрузка, "
              "контроль, извлечение, сведение дублей, координаты, аудит.",
              "How verifiable tables were produced from XRF field measurements and chemicals.tj portal materials: "
              "loading, checks, extraction, duplicate merging, coordinates, audit.")},
    {"title": ("Презентация пилота (PDF)", "Pilot presentation (PDF)"),
     "href": ("/docs/presentation.pdf", "/docs/presentation-en.pdf"), "pdf": ("presentation.pdf", "presentation_en.pdf"),
     "text": ("10 слайдов: задача, слой данных, модель скрининга, индекс риска для детей, аудит, план отбора, "
              "демо-стенд, предложения по продолжению.",
              "10 slides: the task, the data layer, the screening model, the child risk index, the audit, the sampling "
              "plan, the demo stand, proposals for continuation.")},
    {"title": ("Блокноты с расчётами", "Notebooks"), "href": "/notebooks", "pdf": None,
     "text": ("Демонстрационные блокноты по СОЗ и металлам: методы моделей на реальных данных.",
              "Demonstration notebooks on POPs and metals: model methods on real data.")},
]


def _loc(d):
    return {k: (tr(*v) if isinstance(v, tuple) else v) for k, v in d.items()}


@bp.route("/docs")
def docs_index():
    items = [_loc(d) for d in DOCS]
    items = [dict(d, has_pdf=bool(d["pdf"]) and (FILES / d["pdf"]).exists()) for d in items]
    return render("docs/index.html", items=items)


@bp.route("/docs/pop-methodology")
def pop_methodology():
    return render("docs/page.html", title=tr(*DOCS[0]["title"]),
                  body=tpl("docs/pop_methodology_body.html"), pdf=tr(*DOCS[0]["pdf"]))


@bp.route("/docs/data-preparation")
def data_preparation():
    return render("docs/page.html", title=tr(*DOCS[2]["title"]),
                  body=tpl("docs/data_preparation_body.html"), pdf=tr(*DOCS[2]["pdf"]))


@bp.route("/docs/<name>.pdf")
def doc_pdf(name):
    fn = f"{name.replace('-', '_')}.pdf"
    if not (FILES / fn).exists():
        abort(404)
    return send_from_directory(FILES, fn, mimetype="application/pdf")
