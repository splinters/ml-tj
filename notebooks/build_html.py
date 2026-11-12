"""Выполняет блокноты (по флагу --execute) и экспортирует их в автономный HTML.

Результат - статические страницы для демо-стенда и публикации через nginx: зрители ничего
не запускают, код на сервере не выполняется. Из страниц удаляются обращения к внешним CDN
(require.js, MathJax), поэтому они открываются и без интернета.

Демонстрационные блокноты (demo_*) экспортируются со скрытым кодом и кнопкой «Показать код».

Запуск (из каталога notebooks):
    python build_html.py --out ../notebooks_html [--execute] [--only demo_pop_screening ...]
"""
import argparse
import re
import sys
from pathlib import Path

import nbformat
from nbconvert import HTMLExporter

# (имя файла, название, демонстрационный: код скрыт по умолчанию)
NOTEBOOKS = [
    ("demo_pop_screening", "СОЗ: скрининг участков и индекс риска для детей", True),
    ("demo_metals_xrf", "Тяжёлые металлы: данные XRF, приоритет складов и модель", True),
    ("demo_pop_screening_en", "POPs: site screening and the child risk index", True),
    ("demo_metals_xrf_en", "Heavy metals: XRF data, warehouse priority and the model", True),
    ("01_data_and_qc", "Данные и контроль качества (металлы)", False),
    ("02_model_pb_lowo", "Модель Pb и проверка LOWO", False),
    ("03_all_elements", "Результаты по всем девяти элементам", False),
    ("04_priority_and_risk_score", "Приоритет складов и индекс 0-100", False),
]
# встроенный модуль mermaid подгружается с CDN (в блокнотах диаграмм нет): блок удаляется целиком
MERMAID = re.compile(r"<script\b[^>]*>(?:(?!</script>).)*?cdnjs\.cloudflare\.com(?:(?!</script>).)*?</script>", re.I | re.S)
EXTERNAL = re.compile(r"""<(script|link)\b[^>]*(src|href)=["']https?://[^"']*["'][^>]*>(\s*</script>)?""", re.I)

DEMO_HEAD = """
<style>
body.demo { background:#fbfaf7; }
body.demo main, body.demo .jp-Notebook { max-width: 1040px; margin: 0 auto; }
body.demo .jp-CodeCell .jp-Cell-inputWrapper, body.demo .jp-InputPrompt, body.demo .jp-OutputPrompt { display: none; }
body.demo.show-code .jp-CodeCell .jp-Cell-inputWrapper { display: block; }
body.demo .jp-RenderedMarkdown { font-size: 15px; line-height: 1.55; color: #16253d; }
body.demo .jp-RenderedMarkdown h1 { font-size: 28px; line-height: 1.25; border-bottom: 3px solid #1f6fb2; padding-bottom: 8px; }
body.demo .jp-RenderedMarkdown h2 { font-size: 21px; margin-top: 28px; color: #16253d; }
body.demo .jp-RenderedMarkdown blockquote { border-left: 4px solid #1f6fb2; background: #eef4fa; margin: 6px 0;
  padding: 8px 14px; color: #16253d; font-size: 15px; }
body.demo table.demo-table { border-collapse: collapse; font-size: 13px; margin: 6px 0 10px; }
body.demo table.demo-table th { background: #16253d; color: #fff; padding: 6px 10px; text-align: left; font-weight: 600; }
body.demo table.demo-table td { padding: 5px 10px; border-bottom: 1px solid #e3e0d8; }
body.demo table.demo-table tr:nth-child(even) td { background: #f3f1eb; }
#codeToggle { position: fixed; top: 12px; right: 16px; z-index: 1000; background: #16253d; color: #fff; border: 0;
  border-radius: 6px; padding: 8px 14px; font: 600 13px -apple-system, "Segoe UI", Roboto, Arial, sans-serif; cursor: pointer; }
#codeToggle:hover { background: #1f6fb2; }
</style>
"""
DEMO_BODY = """
<button id="codeToggle" type="button">Показать код</button>
<script>
(function () {
  document.body.classList.add('demo');
  var b = document.getElementById('codeToggle');
  b.addEventListener('click', function () {
    var on = document.body.classList.toggle('show-code');
    b.textContent = on ? 'Скрыть код' : 'Показать код';
  });
})();
</script>
"""

DEMO_BODY_EN = (DEMO_BODY.replace("Показать код", "Show code").replace("Скрыть код", "Hide code"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="../notebooks_html")
    ap.add_argument("--execute", action="store_true", help="выполнить блокноты перед экспортом")
    ap.add_argument("--only", nargs="*", help="обработать только перечисленные блокноты (имена без .ipynb)")
    a = ap.parse_args()
    here = Path(__file__).parent
    out = (here / a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    exporter = HTMLExporter(template_name="lab")
    exporter.exclude_input_prompt = True
    exporter.exclude_output_prompt = True
    for stem, title, demo in NOTEBOOKS:
        if a.only and stem not in a.only:
            continue
        nb = nbformat.read(here / f"{stem}.ipynb", as_version=4)
        if a.execute:
            from nbclient import NotebookClient
            NotebookClient(nb, timeout=1800, kernel_name="python3",
                           resources={"metadata": {"path": str(here)}}).execute()
            nbformat.write(nb, here / f"{stem}.ipynb")
        nb.metadata["title"] = title
        body, _ = exporter.from_notebook_node(nb)
        body = MERMAID.sub("", EXTERNAL.sub("", body))
        if demo:
            demo_body = DEMO_BODY_EN if stem.endswith("_en") else DEMO_BODY
            body = body.replace("</head>", DEMO_HEAD + "</head>", 1).replace("</body>", demo_body + "</body>", 1)
            if stem.endswith("_en"):
                body = re.sub(r"<html([^>]*)\blang=\"[^\"]*\"", r'<html\1lang="en"', body, count=1)
        (out / f"{stem}.html").write_text(body, encoding="utf-8")
        print("ok", stem)
    links = []
    for stem, title, demo in NOTEBOOKS:
        if (out / f"{stem}.html").exists():
            links.append((demo, f'<li><a href="{stem}.html">{title}</a></li>'))
    index = ("<!doctype html><html lang='ru'><head><meta charset='utf-8'><title>ML TJ: блокноты</title>"
             "<style>body{font-family:sans-serif;max-width:760px;margin:40px auto;padding:0 16px;color:#16253d}"
             "li{margin:8px 0}.n{color:#555;font-size:13px}h2{font-size:18px;margin-top:28px}</style></head><body>"
             "<h1>ML TJ: блокноты с расчётами</h1>"
             "<p>Статические страницы с готовыми результатами: код и данные на сервере не запускаются. "
             "Расчёты воспроизводятся из файлов проекта без базы данных и без интернета.</p>"
             "<h2>Демонстрационные</h2><ul>" + "".join(l for d, l in links if d) + "</ul>"
             "<h2>Технические (металлы)</h2><ul>" + "".join(l for d, l in links if not d) + "</ul>"
             "<p class='n'>Исходные блокноты (.ipynb) и общий код (mltj.py, demo_utils.py) лежат в каталоге notebooks проекта.</p>"
             "</body></html>")
    (out / "index.html").write_text(index, encoding="utf-8")
    bad = [p.name for p in out.glob("*.html")
           if re.search(r'(src|href)=["\']https?://|cdnjs|unpkg|jsdelivr', p.read_text(encoding="utf-8"))]
    if bad:
        print("ВНИМАНИЕ: внешние ссылки остались в", bad, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
