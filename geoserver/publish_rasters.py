#!/usr/bin/env python3
"""
Публикация GeoTIFF со стилями SLD в GeoServer через REST.

Правило: растр X.tif + стиль X.sld (рядом, то же имя) -> слой <workspace>:X
со стилем <workspace>:X по умолчанию.

Пример (на машине, где лежат файлы и работает GeoServer):
    python publish_rasters.py --dir ../tj_clipped --workspace tj \
        --url http://localhost:8080/geoserver --user admin

Пароль: переменная GEOSERVER_PASSWORD или запрос в консоли (в аргументах не передаётся).
Сначала проверка без изменений:  --dry-run

Заголовки слоёв (title/abstract) берутся из webapp/climate_catalog.py; для уже
опубликованных слоёв достаточно:  --retitle

Если GeoServer в Docker: путь к файлам внутри контейнера отличается от пути на
хосте. Задайте --map-prefix "<путь на хосте>=<путь в контейнере>".

Скрипт не меняет проекцию: SRS берётся из самого GeoTIFF. Любая ошибка REST
печатается с кодом и телом ответа GeoServer и пишется в publish.log.
"""
import argparse
import csv
import getpass
import os
import re
import sys
import time
from pathlib import Path
from xml.sax.saxutils import escape

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webapp"))
try:
    import climate_catalog  # человекочитаемые названия серии WorldClim
except ImportError:
    climate_catalog = None

LOG = "publish.log"


def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def safe_name(stem):
    n = re.sub(r"[^A-Za-z0-9_\-]", "_", stem)
    if not re.match(r"[A-Za-z_]", n):
        n = "r_" + n
    return n


def sld_content_type(path):
    head = Path(path).read_text(encoding="utf-8", errors="replace")[:4000]
    m = re.search(r'<[^>]*StyledLayerDescriptor[^>]*\sversion\s*=\s*["\']([\d.]+)["\']', head)
    ver = m.group(1) if m else "1.0.0"
    return ("application/vnd.ogc.se+xml" if ver.startswith("1.1")
            else "application/vnd.ogc.sld+xml"), ver


class GS:
    def __init__(self, url, ws, user, pwd, timeout=60):
        self.url = url.rstrip("/")
        self.ws = ws
        self.s = requests.Session()
        self.s.auth = (user, pwd)
        self.timeout = timeout

    def req(self, method, path, **kw):
        kw.setdefault("timeout", self.timeout)
        return self.s.request(method, f"{self.url}/rest{path}", **kw)

    def exists(self, path):
        return self.req("GET", path).status_code == 200

    # --- preflight ---
    def preflight(self):
        try:
            r = self.req("GET", "/about/version.json")
        except requests.RequestException as e:
            log(f"ОШИБКА: GeoServer недоступен по {self.url}: {e}")
            return False
        if r.status_code == 401:
            log("ОШИБКА: 401 - неверный логин/пароль GeoServer")
            return False
        if r.status_code != 200:
            log(f"ОШИБКА: {r.status_code} от /rest/about/version.json: {r.text[:300]}")
            return False
        try:
            ver = r.json()["about"]["resource"][0]["Version"]
        except Exception:
            ver = "?"
        log(f"GeoServer доступен, версия {ver}")
        if not self.exists(f"/workspaces/{self.ws}.json"):
            r = self.req("POST", "/workspaces", json={"workspace": {"name": self.ws}})
            if r.status_code not in (200, 201):
                log(f"ОШИБКА: не удалось создать workspace '{self.ws}': {r.status_code} {r.text[:300]}")
                return False
            log(f"Создан workspace '{self.ws}'")
        else:
            log(f"Workspace '{self.ws}' существует")
        return True

    # --- styles ---
    def put_style(self, name, sld_path, update):
        ctype, ver = sld_content_type(sld_path)
        body = Path(sld_path).read_bytes()
        have = self.exists(f"/workspaces/{self.ws}/styles/{name}.json")
        if have and not update:
            return True, f"стиль есть (SLD {ver} не загружался)"
        if have:
            r = self.req("PUT", f"/workspaces/{self.ws}/styles/{name}",
                         headers={"Content-Type": ctype}, data=body)
        else:
            r = self.req("POST", f"/workspaces/{self.ws}/styles", params={"name": name},
                         headers={"Content-Type": ctype}, data=body)
        if r.status_code in (200, 201):
            return True, f"стиль {'обновлён' if have else 'создан'} (SLD {ver})"
        return False, f"стиль: {r.status_code} {r.text[:500]}"

    # --- coverage ---
    def layer_exists(self, name):
        return self.exists(f"/layers/{self.ws}:{name}.json")

    def delete_store(self, name):
        r = self.req("DELETE", f"/workspaces/{self.ws}/coveragestores/{name}",
                     params={"recurse": "true"})
        return r.status_code in (200, 404), f"{r.status_code} {r.text[:200]}"

    @staticmethod
    def _err(r):
        body = (r.text or "").strip().replace("\n", " ")[:600] or "<пустое тело ответа>"
        return f"HTTP {r.status_code} {r.reason}; тело: {body}"

    def _pub_external(self, name, server_path, stem, tif):
        r = self.req("PUT", f"/workspaces/{self.ws}/coveragestores/{name}/external.geotiff",
                     params={"configure": "first", "coverageName": name},
                     headers={"Content-Type": "text/plain"},
                     data=("file://" if server_path.startswith("/") else "file:///") + server_path)
        return r.status_code in (200, 201), self._err(r)

    def _pub_xml(self, name, server_path, stem, tif):
        url = ("file://" if server_path.startswith("/") else "file:///") + server_path
        body = (f"<coverageStore><name>{escape(name)}</name><type>GeoTIFF</type><enabled>true</enabled>"
                f"<workspace><name>{escape(self.ws)}</name></workspace><url>{escape(url)}</url></coverageStore>")
        r = self.req("POST", f"/workspaces/{self.ws}/coveragestores",
                     headers={"Content-Type": "text/xml"}, data=body.encode())
        if r.status_code not in (200, 201):
            return False, "хранилище: " + self._err(r)
        body = (f"<coverage><name>{escape(name)}</name><nativeName>{escape(stem)}</nativeName>"
                f"<title>{escape(name)}</title><enabled>true</enabled></coverage>")
        r = self.req("POST", f"/workspaces/{self.ws}/coveragestores/{name}/coverages",
                     headers={"Content-Type": "text/xml"}, data=body.encode())
        return r.status_code in (200, 201), "покрытие: " + self._err(r)

    def _pub_upload(self, name, server_path, stem, tif):
        with open(tif, "rb") as f:
            r = self.req("PUT", f"/workspaces/{self.ws}/coveragestores/{name}/file.geotiff",
                         params={"configure": "first", "coverageName": name},
                         headers={"Content-Type": "image/tiff"}, data=f, timeout=600)
        return r.status_code in (200, 201), self._err(r)

    def publish_tif(self, name, server_path, stem, tif, mode):
        """mode: external | xml | upload | auto (external -> xml -> upload)."""
        order = ["external", "xml", "upload"] if mode == "auto" else [mode]
        last = ""
        for m in order:
            ok, msg = getattr(self, "_pub_" + m)(name, server_path, stem, tif)
            if ok:
                return True, f"слой создан (способ: {m})"
            last += f"[{m}] {msg} | "
            log(f"   способ {m} не удался: {msg}")
            self.delete_store(name)  # убрать недосозданное хранилище
        return False, last

    def set_title(self, name, title, abstract):
        body = (f"<coverage><title>{escape(title)}</title><abstract>{escape(abstract)}</abstract></coverage>")
        r = self.req("PUT", f"/workspaces/{self.ws}/coveragestores/{name}/coverages/{name}",
                     headers={"Content-Type": "text/xml; charset=utf-8"}, data=body.encode("utf-8"))
        if r.status_code in (200, 201):
            return True, "заголовок задан"
        return False, "заголовок: " + self._err(r)

    def set_default_style(self, name):
        body = (f"<layer><defaultStyle><name>{escape(self.ws)}:{escape(name)}</name>"
                f"</defaultStyle></layer>")
        r = self.req("PUT", f"/layers/{self.ws}:{name}",
                     headers={"Content-Type": "text/xml"}, data=body.encode())
        if r.status_code in (200, 201):
            return True, "стиль назначен"
        return False, f"{r.status_code} {r.text[:500]}"

    def check_style(self, name):
        r = self.req("GET", f"/layers/{self.ws}:{name}.json")
        if r.status_code != 200:
            return False
        try:
            return r.json()["layer"]["defaultStyle"]["name"].endswith(name)
        except Exception:
            return False

    def getmap_ok(self, name):
        """Пробный WMS GetMap по границам слоя."""
        r = self.req("GET", f"/workspaces/{self.ws}/coveragestores/{name}/coverages/{name}.json")
        try:
            bb = r.json()["coverage"]["latLonBoundingBox"]
            bbox = f"{bb['minx']},{bb['miny']},{bb['maxx']},{bb['maxy']}"
        except Exception:
            return False, "нет latLonBoundingBox"
        g = self.s.get(f"{self.url}/{self.ws}/wms", timeout=self.timeout, params={
            "service": "WMS", "version": "1.1.1", "request": "GetMap",
            "layers": f"{self.ws}:{name}", "bbox": bbox, "width": 256, "height": 256,
            "srs": "EPSG:4326", "format": "image/png"})
        ok = g.status_code == 200 and g.headers.get("Content-Type", "").startswith("image/")
        return ok, "" if ok else f"{g.status_code} {g.text[:200]}"


def find_items(base, require_style):
    items, no_style = [], []
    for root, _, files in os.walk(base):
        for f in sorted(files):
            stem, ext = os.path.splitext(f)
            if ext.lower() not in (".tif", ".tiff"):
                continue
            tif = Path(root) / f
            sld = next((Path(root) / (stem + e) for e in (".sld", ".SLD")
                        if (Path(root) / (stem + e)).exists()), None)
            if sld is None and require_style:
                no_style.append(tif)
                continue
            items.append({"tif": tif.resolve(), "sld": sld.resolve() if sld else None,
                          "name": safe_name(stem), "stem": stem})
    return items, no_style


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default="../tj_clipped", help="папка с .tif/.sld (рекурсивно)")
    ap.add_argument("--url", default=os.getenv("GEOSERVER_URL", "http://localhost:8080/geoserver"))
    ap.add_argument("--workspace", default=os.getenv("GEOSERVER_WORKSPACE", "tj"))
    ap.add_argument("--user", default=os.getenv("GEOSERVER_USER", "admin"))
    ap.add_argument("--map-prefix", help="ХОСТ=КОНТЕЙНЕР, подмена префикса пути (Docker)")
    ap.add_argument("--allow-no-style", action="store_true",
                    help="публиковать и растры без .sld (стиль GeoServer по умолчанию)")
    ap.add_argument("--update-styles", action="store_true", help="перезаписать уже существующие стили")
    ap.add_argument("--force", action="store_true", help="пересоздать слои, которые уже есть")
    ap.add_argument("--only", help="подстрока в имени файла (публиковать часть растров)")
    ap.add_argument("--mode", choices=["auto", "external", "xml", "upload"], default="auto",
                    help="способ публикации: external (путь на сервере), xml (хранилище+покрытие), "
                         "upload (файл загружается в data dir GeoServer); auto перебирает по порядку")
    ap.add_argument("--retitle", action="store_true",
                    help="только обновить заголовки и описания уже опубликованных слоёв")
    ap.add_argument("--dry-run", action="store_true", help="только проверка, без изменений")
    ap.add_argument("--no-getmap", action="store_true", help="не делать пробный GetMap")
    a = ap.parse_args()

    base = Path(a.dir).expanduser().resolve()
    log(f"== запуск: dir={base} workspace={a.workspace} url={a.url}")
    if not base.is_dir():
        log(f"ОШИБКА: папка не найдена: {base}")
        sys.exit(2)
    items, no_style = find_items(base, not a.allow_no_style)
    if a.only:
        items = [i for i in items if a.only in i["stem"]]
    log(f"Найдено растров для публикации: {len(items)}; без .sld пропущено: {len(no_style)}")
    for t in no_style[:20]:
        log(f"  без .sld: {t.name}")
    if not items:
        log("Нечего публиковать: проверьте --dir и что рядом с X.tif лежит X.sld")
        sys.exit(1)
    names = [i["name"] for i in items]
    dup = {n for n in names if names.count(n) > 1}
    if dup:
        log(f"ОШИБКА: совпадают имена слоёв (файлы в разных подпапках): {sorted(dup)}")
        sys.exit(2)

    pwd = os.getenv("GEOSERVER_PASSWORD") or getpass.getpass(f"Пароль GeoServer ({a.user}): ")
    gs = GS(a.url, a.workspace, a.user, pwd)
    if not gs.preflight():
        sys.exit(3)

    hostp, contp = (a.map_prefix.split("=", 1) + [""])[:2] if a.map_prefix else (None, None)
    def meta(it):
        rec = climate_catalog.parse(it["stem"]) if climate_catalog else None
        if rec:
            return rec["title"], rec["abstract"]
        tsv = Path(it["tif"]).parent / "layers.tsv"   # заголовки от build_population_layers.py
        if tsv.exists():
            for r in csv.DictReader(tsv.open(encoding="utf-8"), delimiter="\t"):
                if r.get("layer") == it["stem"]:
                    return (f"{r['title_ru']} / {r['title_en']}",
                            "WorldPop R2025A, человек на пиксель ~100 м / people per ~100 m pixel")
        return it["stem"], ""

    rows, fail = [], 0
    if a.retitle:
        for n, it in enumerate(items, 1):
            title, abstract = meta(it)
            if a.dry_run:
                log(f"[{n}/{len(items)}] {it['name']}: {title}")
                continue
            if not gs.layer_exists(it["name"]):
                log(f"[{n}/{len(items)}] {it['name']}: слоя нет, пропуск")
                fail += 1
                continue
            ok, msg = gs.set_title(it["name"], title, abstract)
            log(f"[{n}/{len(items)}] {it['name']}: {msg if ok else 'ОШИБКА ' + msg}")
            fail += 0 if ok else 1
        log(f"== заголовки: ошибок {fail}")
        sys.exit(1 if fail else 0)
    for n, it in enumerate(items, 1):
        name = it["name"]
        spath = it["tif"].as_posix()
        if hostp and spath.startswith(Path(hostp).as_posix()):
            spath = contp + spath[len(Path(hostp).as_posix()):]
        st = {"layer": f"{a.workspace}:{name}", "tif": it["tif"].name, "status": "", "detail": ""}
        log(f"[{n}/{len(items)}] {it['tif'].name} -> {st['layer']}")
        if a.dry_run:
            st["status"], st["detail"] = "dry-run", f"путь для сервера: {spath}"
            log(f"   dry-run: {spath}")
            rows.append(st)
            continue
        try:
            if it["sld"]:
                ok, msg = gs.put_style(name, it["sld"], a.update_styles)
                log(f"   {msg}")
                if not ok:
                    st["status"], st["detail"] = "ОШИБКА стиль", msg
                    fail += 1
                    rows.append(st)
                    continue
            if gs.layer_exists(name):
                if not a.force:
                    log("   слой уже есть (пропуск; --force пересоздаст)")
                    if it["sld"]:
                        gs.set_default_style(name)
                    gs.set_title(name, *meta(it))
                    st["status"] = "уже был"
                    rows.append(st)
                    continue
                ok, msg = gs.delete_store(name)
                log(f"   удаление старого хранилища: {msg}")
            else:
                gs.delete_store(name)  # хранилище без слоя от прошлой неудачной попытки
            ok, msg = gs.publish_tif(name, spath, it["stem"], it["tif"], a.mode)
            log(f"   публикация: {msg}")
            if not ok:
                st["status"], st["detail"] = "ОШИБКА публикация", msg
                fail += 1
                rows.append(st)
                continue
            ok, msg = gs.set_title(name, *meta(it))
            log(f"   {msg}")
            if it["sld"]:
                ok, msg = gs.set_default_style(name)
                log(f"   {msg}")
                if not ok or not gs.check_style(name):
                    st["status"], st["detail"] = "ОШИБКА назначение стиля", msg
                    fail += 1
                    rows.append(st)
                    continue
            if not a.no_getmap:
                ok, msg = gs.getmap_ok(name)
                log(f"   проверка GetMap: {'OK' if ok else 'ОШИБКА ' + msg}")
                if not ok:
                    st["status"], st["detail"] = "ОШИБКА GetMap", msg
                    fail += 1
                    rows.append(st)
                    continue
            st["status"] = "OK"
        except Exception as e:
            st["status"], st["detail"] = "ОШИБКА", repr(e)
            log(f"   ОШИБКА: {e!r}")
            fail += 1
        rows.append(st)

    with open("publish_report.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["layer", "tif", "status", "detail"])
        w.writeheader()
        w.writerows(rows)
    ok_n = sum(r["status"] in ("OK", "уже был", "dry-run") for r in rows)
    log(f"== итог: успешно {ok_n} из {len(rows)}, ошибок {fail}. Отчёт: publish_report.csv, лог: {LOG}")
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
