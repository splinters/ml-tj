"""
Выгрузка реестра загрязнённых участков chemicals.tj (карта /map-standalone)
в структурированный вид: участки, файлы, связка с 10 XRF-складами.

Источник: открытый JSON-эндпоинт, который использует сама карта:
  GET http://chemicals.tj/api/pollutions        - все участки (~5 МБ, без gzip, ~1-1.5 мин)
  GET /storage/<filepath>                        - вложения (фото, PDF-протоколы)
Детальный /api/pollutions/{id} новых полей не добавляет (только journalists, is_active).

Запуск локально (нужен интернет):
  python db/fetch_chemicals_tj.py                 # только метаданные (JSON + CSV)
  python db/fetch_chemicals_tj.py --download-docs # + все не-фото вложения СОЗ (PDF/DOCX)

Результат: source_data/chemicals_tj/
  pollutions_raw.json        - ответ API как есть (аудит)
  sites.csv                  - 1 строка = 1 участок (все типы), плоские поля
  sites_pop.geojson          - только СОЗ, точки EPSG:4326
  files.csv                  - 1 строка = 1 вложение (site_id, tj_code, тип, размер, url)
  docs_unique.csv            - уникальные документы (дедуп по sha256) и к каким TJ-кодам привязаны
  xrf_link.csv               - ближайший СОЗ-участок для каждого из 10 XRF-складов
  docs/                      - скачанные документы, имя = <sha256[:12]>.<ext>
"""
import argparse
import csv
import hashlib
import json
import math
import mimetypes
import re
import time
from pathlib import Path

import requests

BASE = "http://chemicals.tj"
OUT = Path(__file__).resolve().parent.parent / "source_data" / "chemicals_tj"
LONG_TEXT = ("problem_description", "site_description", "impact", "notes")

from xrf_sites import XRF_SITES  # центры XRF-складов из source_data/xrf_warehouses.csv


def haversine_m(la1, lo1, la2, lo2):
    r = math.radians
    dla, dlo = r(la2 - la1), r(lo2 - lo1)
    h = math.sin(dla / 2) ** 2 + math.cos(r(la1)) * math.cos(r(la2)) * math.sin(dlo / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def tj_code(name):
    m = re.search(r"TJ-?(\d+)", name or "")
    return f"TJ-{m.group(1)}" if m else None


def fetch_sites():
    OUT.mkdir(parents=True, exist_ok=True)
    raw = OUT / "pollutions_raw.json"
    if raw.exists() and time.time() - raw.stat().st_mtime < 24 * 3600:
        print(f"Использую кэш {raw}")
        return json.loads(raw.read_text(encoding="utf-8"))
    print("Загрузка /api/pollutions (~5 МБ, медленно)...")
    r = requests.get(f"{BASE}/api/pollutions", timeout=300)
    r.raise_for_status()
    raw.write_text(r.text, encoding="utf-8")
    return r.json()


def write_sites(sites):
    skip = {"files", "pollution_type", "pollution_status"}
    cols = [k for k in sites[0].keys() if k not in skip]
    with open(OUT / "sites.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["tj_code", "pollution_type", "pollution_status", "n_files", "n_docs"] + cols)
        for s in sites:
            files = s.get("files") or []
            n_docs = sum(1 for x in files if x.get("file_type") != "Фотография" or x.get("mime_type") == "application/pdf")
            w.writerow([tj_code(s["site_name"]),
                        (s.get("pollution_type") or {}).get("name") or s.get("category"),
                        (s.get("pollution_status") or {}).get("name"),
                        len(files), n_docs] + [s.get(c) for c in cols])

    feats = []
    for s in sites:
        if (s.get("pollution_type") or {}).get("name") != "СОЗ":
            continue
        props = {k: v for k, v in s.items() if k not in ("files", "pollution_type", "pollution_status")}
        props["tj_code"] = tj_code(s["site_name"])
        props["pollution_status"] = (s.get("pollution_status") or {}).get("name")
        props["n_files"] = len(s.get("files") or [])
        feats.append({"type": "Feature", "properties": props,
                      "geometry": {"type": "Point", "coordinates": [float(s["longitude"]), float(s["latitude"])]}})
    (OUT / "sites_pop.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": feats}, ensure_ascii=False), encoding="utf-8")
    print(f"sites.csv: {len(sites)} участков, sites_pop.geojson: {len(feats)} СОЗ")


def write_files(sites):
    rows = []
    for s in sites:
        for f in s.get("files") or []:
            rows.append({
                "site_id": s["id"], "tj_code": tj_code(s["site_name"]),
                "pollution_type": (s.get("pollution_type") or {}).get("name"),
                "file_id": f["id"], "filename": f.get("filename"), "file_type": f.get("file_type"),
                "mime_type": f.get("mime_type"), "file_size": f.get("file_size"),
                "url": f"{BASE}/storage/{f['filepath']}",
            })
    with open(OUT / "files.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"files.csv: {len(rows)} вложений")
    return rows


def download_docs(files):
    """Не-фото вложения СОЗ. Скачивается КАЖДОЕ вложение (без группировки по размеру
    или имени: одинаковый размер не гарантирует одинаковое содержимое), затем
    дедупликация по sha256. Побайтно разные копии одного документа (сайт пересохраняет
    PDF при загрузке) остаются отдельными строками - сводить их по тексту нужно на этапе
    извлечения таблиц.
    Скачанные URL кэшируются в docs/_downloaded.json - повторный запуск докачивает только новое."""
    docs_dir = OUT / "docs"
    docs_dir.mkdir(exist_ok=True)
    cache_path = docs_dir / "_downloaded.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}


    docs = [r for r in files if r["pollution_type"] == "СОЗ"
            and (r["file_type"] != "Фотография" or r["mime_type"] == "application/pdf")]
    groups = {}
    for r in docs:
        key = (r["file_size"], r["url"])
        groups.setdefault(key, []).append(r)
    print(f"Документов СОЗ: {len(docs)}, уникальных URL: {len(groups)}, уже в кэше: {sum(1 for k in groups if k[1] in cache)}")

    unique = {}
    for i, ((size, _), group) in enumerate(groups.items(), 1):
        r0 = group[0]
        path = None
        if r0["url"] in cache:
            path = docs_dir / cache[r0["url"]]

        if path is not None and path.exists():
            content = path.read_bytes()
        else:
            try:
                resp = requests.get(r0["url"], timeout=300)
                resp.raise_for_status()
            except Exception as e:
                print(f"  [{i}/{len(groups)}] ошибка {r0['url']}: {e}")
                continue
            content = resp.content
            print(f"  [{i}/{len(groups)}] скачан {r0['filename']!r}")
        sha = hashlib.sha256(content).hexdigest()
        ext = mimetypes.guess_extension(r0["mime_type"] or "") or ".bin"
        target = docs_dir / f"{sha[:12]}{ext}"
        if not target.exists():
            target.write_bytes(content)
        cache[r0["url"]] = target.name

        u = unique.setdefault(sha, {"sha256": sha, "local_file": target.name, "mime_type": r0["mime_type"],
                                    "file_size": size, "filenames": set(), "tj_codes": set()})
        for r in group:
            u["filenames"].add(r["filename"] or "")
            u["tj_codes"].add(r["tj_code"] or "")

    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    with open(OUT / "docs_unique.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["sha256", "local_file", "mime_type", "file_size", "n_sites", "filenames", "tj_codes"])
        for u in sorted(unique.values(), key=lambda x: -len(x["tj_codes"])):
            w.writerow([u["sha256"], u["local_file"], u["mime_type"], u["file_size"], len(u["tj_codes"]),
                        " | ".join(sorted(u["filenames"])), ",".join(sorted(u["tj_codes"]))])
    print(f"docs_unique.csv: {len(unique)} уникальных документов")


def link_xrf(sites):
    pop = [s for s in sites if (s.get("pollution_type") or {}).get("name") == "СОЗ"]
    with open(OUT / "xrf_link.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["xrf_site", "tj_code", "pop_site_id", "pop_site_name", "dist_m", "match"])
        for name, (la, lo) in XRF_SITES.items():
            best = min(pop, key=lambda s: haversine_m(la, lo, float(s["latitude"]), float(s["longitude"])))
            d = haversine_m(la, lo, float(best["latitude"]), float(best["longitude"]))
            w.writerow([name, tj_code(best["site_name"]), best["id"], best["site_name"], round(d),
                        "да" if d < 300 else "нет"])
    print("xrf_link.csv записан")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--download-docs", action="store_true")
    a = ap.parse_args()
    sites = fetch_sites()
    write_sites(sites)
    files = write_files(sites)
    link_xrf(sites)
    if a.download_docs:
        download_docs(files)
