"""
Тема «СОЗ (пестициды)» демо-стенда: карта риска для детей, скрининг участков, карточки,
прогноз для нового участка. Данные - файлы слоя source_data/chemicals_tj/pop_layer/ (см. pop_engine.py).

Подложка карты: DEMO_BASEMAP=osm (по умолчанию) или none - без внешних тайлов (изолированная сеть);
для совместимости учитывается и CLIMATE_BASEMAP.
"""
import os

from flask import Blueprint, abort, jsonify, request, send_file

import pop_engine as E
import pop_i18n as P
from i18n import render, tr

bp = Blueprint("pop", __name__)
BASEMAP = os.getenv("DEMO_BASEMAP", os.getenv("CLIMATE_BASEMAP", "osm"))


@bp.route("/pop")
def pop_map():
    return render("pop_map.html", basemap=BASEMAP, summary=E.data()["summary"])


@bp.route("/pop/model")
def pop_model():
    d = E.data()
    m = d["metrics"].copy()
    m = m[m.target.str.contains("основная")].sort_values("auc_mean", ascending=False)
    c = d["curve"]
    pts = [{"k": int(r.k_sites_surveyed), "model": int(r.found_model), "random": float(r.found_random_expected),
            "ideal": int(r.found_ideal)} for r in c.itertuples()]
    pred = d["pred"]
    plan = d["risk"][d["risk"].hazard_source != "анализы"].sort_values("child_risk", ascending=False)
    plan = plan.merge(pred[["map_id", "score_used"]], on="map_id", how="left")
    return render("pop_model.html", summary=d["summary"], metrics=P.metric_rows(E._records(m)), curve=pts,
                  plan=P.plan_rows(E._records(plan[["tj_code", "site_name", "region", "screen_class", "score_used",
                                                  "children_1000m", "child_risk_class", "child_risk"]])))


@bp.route("/pop/api/sites")
def api_sites():
    return jsonify(P.sites(E.sites_geojson()))


@bp.route("/pop/api/samples")
def api_samples():
    return jsonify(P.samples(E.samples_geojson()))


@bp.route("/pop/api/site/<int:map_id>")
def api_site(map_id):
    card = E.site_card(map_id)
    if card is None:
        abort(404)
    return jsonify(P.card(card))


@bp.route("/pop/api/hillshade_bounds")
def api_hillshade():
    b = E.hillshade_bounds()
    if b is None:
        return jsonify({"error": "нет растра рельефа source_data/Terrain150_4326.tif"}), 404
    return jsonify(b)


@bp.route("/pop/api/form_options")
def api_form_options():
    return jsonify(P.form_options(E.form_options()))


@bp.route("/pop/api/predict", methods=["POST"])
def api_predict():
    inp = request.get_json(silent=True) or request.form.to_dict()
    try:
        return jsonify(P.prediction(E.predict_new(inp)))
    except (KeyError, ValueError, TypeError) as e:
        msg = str(e)
        return jsonify({"error": tr(msg, P.ERRORS_EN.get(msg, msg))}), 400


@bp.route("/pop/files/<name>")
def files(name):
    allowed = {"pop_layer.gpkg", "pop_sites.csv", "pop_samples.csv", "pop_measurements.csv", "data_dictionary.md"}
    if name in allowed:
        return send_file(E.LAYER / name, as_attachment=True)
    if name in {"child_risk_index.csv", "child_risk_index.gpkg"}:
        return send_file(E.LAYER / "child_risk" / name, as_attachment=True)
    if name in {"site_predictions.csv", "metrics.csv", "selection_curve.png"}:
        return send_file(E.LAYER / "model" / name, as_attachment=name != "selection_curve.png")
    abort(404)
