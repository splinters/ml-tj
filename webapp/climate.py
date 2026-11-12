"""
Страница «Климат»: слои WorldClim 2.1 из GeoServer с фильтрами и легендой.

Браузер GeoServer напрямую не видит: запросы идут через прокси /climate/wms
этого же приложения. Поэтому работает за любым подпутём nginx, закрыта тем же
Basic Auth, а порт GeoServer наружу открывать не нужно.

Переменные окружения:
    GEOSERVER_URL        по умолчанию http://127.0.0.1:8080/geoserver
    GEOSERVER_WORKSPACE  по умолчанию ML
    GEOSERVER_USER, GEOSERVER_PASSWORD   если чтение слоёв требует авторизации
    CLIMATE_BASEMAP      osm (по умолчанию) или none - без внешней подложки (изолированная сеть)
"""
import os
import re
import time

import requests
from flask import Blueprint, Response, jsonify, render_template, request

import climate_catalog
from i18n import render, tr

bp = Blueprint("climate", __name__)

GS_URL = os.getenv("GEOSERVER_URL", "http://127.0.0.1:8080/geoserver").rstrip("/")
GS_WS = os.getenv("GEOSERVER_WORKSPACE", "ML")
GS_AUTH = (os.getenv("GEOSERVER_USER"), os.getenv("GEOSERVER_PASSWORD")) \
    if os.getenv("GEOSERVER_USER") else None

ALLOWED_PARAMS = {"service", "version", "request", "layers", "styles", "format", "transparent",
                  "srs", "crs", "bbox", "width", "height", "query_layers", "info_format",
                  "x", "y", "i", "j", "layer", "legend_options", "feature_count", "exceptions"}
ALLOWED_REQUESTS = {"getmap", "getlegendgraphic", "getfeatureinfo"}
_LAYER_RE = re.compile(r"^[A-Za-z0-9_\-]+:[A-Za-z0-9_\-]+$")

_cap_cache = {"t": 0.0, "names": None, "error": None}


def _published_names():
    """Имена слоёв workspace из GetCapabilities (кэш 60 с). -> (set|None, error|None)"""
    now = time.time()
    if now - _cap_cache["t"] < 60 and (_cap_cache["names"] is not None or _cap_cache["error"]):
        return _cap_cache["names"], _cap_cache["error"]
    try:
        r = requests.get(f"{GS_URL}/{GS_WS}/wms", timeout=8, auth=GS_AUTH,
                         params={"service": "WMS", "version": "1.1.1", "request": "GetCapabilities"})
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        # в workspace-сервисе (/ML/wms) GeoServer отдаёт имена слоёв БЕЗ префикса workspace,
        # в глобальном - с префиксом; принимаем оба варианта
        names = {m.split(":", 1)[-1] for m in re.findall(r"<Name>([^<]+)</Name>", r.text)}
        _cap_cache.update(t=now, names=names, error=None)
    except Exception as e:  # GeoServer недоступен - страница всё равно открывается
        _cap_cache.update(t=now, names=None, error=str(e)[:200])
    return _cap_cache["names"], _cap_cache["error"]


@bp.route("/climate")
def climate_page():
    return render("climate.html", workspace=GS_WS,
                           basemap=os.getenv("CLIMATE_BASEMAP", "osm").lower())


@bp.route("/api/climate/catalog")
def catalog():
    names, err = _published_names()
    layers = []
    en = tr("ru", "en") == "en"
    for rec in climate_catalog.all_layers():
        if en:
            rec = climate_catalog.localize_en(rec)
        layers.append({k: rec[k] for k in ("name", "dataset", "scenario", "period", "var", "var_title",
                                           "short", "unit", "theme", "group", "month", "title", "abstract", "source")}
                      | {"published": (names is None) or rec["name"] in names})
    return jsonify(workspace=GS_WS, geoserver_ok=names is not None, geoserver_error=err,
                   published=0 if names is None else sum(1 for l in layers if l["published"]),
                   scenarios=climate_catalog.SCENARIOS_EN if en else climate_catalog.SCENARIOS, layers=layers)


@bp.route("/climate/wms")
def wms_proxy():
    args = {k.lower(): v for k, v in request.args.items() if k.lower() in ALLOWED_PARAMS}
    if args.get("request", "").lower() not in ALLOWED_REQUESTS:
        return Response("Недопустимый запрос.\n", 400)
    for key in ("layers", "query_layers", "layer"):
        if key in args and not (_LAYER_RE.match(args[key]) and args[key].split(":")[0] == GS_WS):
            return Response("Недопустимый слой.\n", 400)
    try:
        r = requests.get(f"{GS_URL}/{GS_WS}/wms", params=args, timeout=40, auth=GS_AUTH)
    except requests.RequestException as e:
        return Response(f"GeoServer недоступен: {str(e)[:200]}\n", 502)
    resp = Response(r.content, r.status_code, {"Content-Type": r.headers.get("Content-Type", "application/octet-stream")})
    if r.status_code == 200:
        resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp
