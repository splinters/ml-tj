#!/usr/bin/env python3
"""
Сборка открытой версии проекта для GitHub из рабочей папки.

Принцип - белый список: в выпуск попадает только перечисленное ниже, всё остальное
(source_data, .env, дампы БД, GeoPackage, кэши, растры, результаты, письма и ТЗ) не копируется.
Затем:
  - у блокнотов удаляются результаты выполнения (в них могут быть таблицы с данными);
  - весь выпуск проверяется на секреты и личные данные (пароли, токены, ключи, IP-адреса,
    пути домашних папок, e-mail); при находке сборка завершается с ошибкой;
  - если установлен gitleaks, дополнительно запускается он.

Запуск из корня рабочей папки:
    python tools/make_public_release.py                 # -> out/public_release/ml-tj
    python tools/make_public_release.py --out /path/to/folder
"""
import argparse
import fnmatch
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTRA = Path(__file__).resolve().parent / "public_release"   # README, LICENSE и т.п. для корня выпуска

# (откуда, куда) - glob относительно корня проекта; куда - папка в выпуске
INCLUDE = [
    ("webapp/*.py", "webapp"),
    ("webapp/POP_STAND.md", "webapp"),
    ("webapp/templates/*.html", "webapp/templates"),
    ("webapp/templates/docs/*.html", "webapp/templates/docs"),
    ("webapp/static/*.css", "webapp/static"),
    ("webapp/static/vendor/**/*", "webapp/static/vendor"),
    ("webapp/static/docs/data_preparation*.pdf", "webapp/static/docs"),
    ("webapp/static/docs/pop_methodology*.pdf", "webapp/static/docs"),
    ("webapp/static/docs/selection_curve.png", "webapp/static/docs"),
    ("db/*.py", "db"),
    ("db/*.sql", "db"),
    ("db/norms_seed.csv", "db"),
    ("db/norm_profiles.csv", "db"),
    ("db/norm_sources_seed.csv", "db"),
    ("db/context_attributes.csv", "db"),
    ("notebooks/*.py", "notebooks"),
    ("notebooks/*.ipynb", "notebooks"),
    ("notebooks/README.md", "notebooks"),
    ("notebooks/requirements-notebooks.txt", "notebooks"),
    ("geoserver/*.py", "geoserver"),
    ("deploy/*", "deploy"),
    ("tools/make_public_release.py", "tools"),
    ("tools/make_synthetic_data.py", "tools"),
    ("requirements.txt", "."),
    ("schema.sql", "."),
    ("SETUP.md", "docs"),
    ("METHODOLOGY_ML_v1.md", "docs"),
    ("source_data/chemicals_tj/pop_layer/data_dictionary.md", "docs/pop_layer"),
    ("source_data/chemicals_tj/pop_layer/model/README.md", "docs/pop_layer/model"),
    ("source_data/chemicals_tj/pop_layer/child_risk/README.md", "docs/pop_layer/child_risk"),
    ("source_data/chemicals_tj/pop_layer/metals_link/README.md", "docs/pop_layer/metals_link"),
]
RENAME = {"env.example": ".env.example"}

# устаревшие версии и черновики - в выпуск не идут
EXCLUDE = [
    "webapp/app_pre_auth.py", "webapp/app_v1.py", "webapp/templates/*_pre_auth.html", "webapp/templates/*_v1.html",
    "db/*_0.py", "db/*_v1.py", "db/*_v2.py", "db/fetch_buildings_5.py",
    "**/__pycache__/**", "**/.ipynb_checkpoints/**", "**/.DS_Store",
]

# секреты и личные данные: (название, регулярное выражение)
SECRET_PATTERNS = [
    ("пароль в коде", r"""(?i)(password|passwd|pwd)\s*[=:]\s*['"][^'"\s{}$<]{4,}['"]"""),
    ("строка подключения с паролем", r"[a-z+]+://[^/\s:@]+:[^/\s:@{}$<]{3,}@"),
    ("токен/ключ", r"(?i)(api[_-]?key|secret|token)\s*[=:]\s*['\"][A-Za-z0-9_\-]{12,}['\"]"),
    ("закрытый ключ", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("ключ AWS", r"AKIA[0-9A-Z]{16}"),
    ("токен GitHub", r"gh[pousr]_[A-Za-z0-9]{30,}"),
    ("IP-адрес (не локальный)", r"\b(?!127\.0\.0\.1\b)(?!0\.0\.0\.0\b)(?:\d{1,3}\.){3}\d{1,3}\b"),
    ("домашняя папка", r"/(Users|home)/[A-Za-z][\w.-]+"),
    ("e-mail", r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
]
# Дополнительные шаблоны (имена, внутренние адреса и т.п.) - по одному регулярному выражению в строке
# в tools/private_patterns.txt. Этот файл в выпуск не копируется.
PRIVATE = Path(__file__).resolve().parent / "private_patterns.txt"
if PRIVATE.exists():
    for _line in PRIVATE.read_text(encoding="utf-8").splitlines():
        if _line.strip() and not _line.startswith("#"):
            SECRET_PATTERNS.append(("частный шаблон", _line.strip()))
# допустимые совпадения (версии, примеры, публичные адреса источников)
ALLOW = [
    r"\b\d+\.\d+\.\d+\.\d+\b(?=.*(version|версия|ver))",
    r"noreply@", r"2\.1\.7\.\d+",                             # номер СанПиН, не IP r"example\.(com|org)", r"user@host", r"/home/user\b",
    r"postgresql://soil_ml:soil_ml_dev_password@localhost",   # пароль-заглушка локальной разработки (env.example)
]
TEXT_EXT = {".py", ".md", ".html", ".css", ".js", ".sql", ".csv", ".txt", ".ipynb", ".json", ".sh",
            ".service", ".conf", ".example", ".cfg", ".ini", ".yml", ".yaml", ""}


def excluded(rel):
    return any(fnmatch.fnmatch(rel, p) for p in EXCLUDE)


def collect():
    items = []
    for pattern, dest in INCLUDE:
        base = pattern.split("*")[0].rsplit("/", 1)[0] if "*" in pattern else str(Path(pattern).parent)
        for src in sorted(ROOT.glob(pattern)):
            if not src.is_file():
                continue
            rel = src.relative_to(ROOT).as_posix()
            if excluded(rel):
                continue
            sub = src.relative_to(ROOT / base) if "**" in pattern else Path(src.name)
            items.append((src, Path(dest) / sub))
    for name, new in RENAME.items():
        if (ROOT / name).exists():
            items.append((ROOT / name, Path(new)))
    if EXTRA.exists():
        for src in sorted(EXTRA.rglob("*")):
            if src.is_file():
                items.append((src, src.relative_to(EXTRA)))
    return items


def strip_notebook(path):
    nb = json.loads(path.read_text(encoding="utf-8"))
    for c in nb.get("cells", []):
        if c.get("cell_type") == "code":
            c["outputs"] = []
            c["execution_count"] = None
        c.get("metadata", {}).pop("execution", None)
    nb.get("metadata", {}).pop("widgets", None)
    path.write_text(json.dumps(nb, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def scan(out):
    hits = []
    for f in sorted(out.rglob("*")):
        if not f.is_file() or (f.suffix.lower() not in TEXT_EXT and f.name not in (".env.example", ".gitignore")):
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for name, rx in SECRET_PATTERNS:
                for m in re.finditer(rx, line):
                    if any(re.search(a, line) for a in ALLOW):
                        continue
                    hits.append(f"{f.relative_to(out)}:{n}: {name}: {m.group(0)[:60]}")
    return hits


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=ROOT / "out" / "public_release" / "ml-tj")
    a = ap.parse_args()
    out = a.out.resolve()
    if out.exists():
        shutil.rmtree(out)
    items = collect()
    for src, rel in items:
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        if dst.suffix == ".ipynb":
            strip_notebook(dst)
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"Скопировано файлов: {len(items)}, объём {size / 1e6:.1f} МБ -> {out}")
    big = [f for f in out.rglob("*") if f.is_file() and f.stat().st_size > 5e6]
    for f in big:
        print(f"ВНИМАНИЕ: крупный файл {f.relative_to(out)} ({f.stat().st_size / 1e6:.1f} МБ)")
    hits = scan(out)
    if shutil.which("gitleaks"):
        r = subprocess.run(["gitleaks", "detect", "--no-git", "--source", str(out), "--redact", "-v"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            hits.append("gitleaks:\n" + r.stdout[-3000:])
        else:
            print("gitleaks: находок нет")
    else:
        print("gitleaks не установлен - выполнена только встроенная проверка")
    if hits:
        print(f"\nНАЙДЕНО {len(hits)} подозрительных строк - выпуск НЕ готов к публикации:")
        print("\n".join(hits))
        sys.exit(1)
    print("Встроенная проверка: секретов и личных данных не найдено")


if __name__ == "__main__":
    main()
