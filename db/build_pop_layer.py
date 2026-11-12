"""
Сводный слой СОЗ (стойкие органические загрязнители, пестициды) по данным
chemicals.tj: все участки типа «СОЗ» с карты /map-standalone, все показатели
из прикреплённых документов, привязанные к координатам, и признаки,
извлечённые из текстов карточек участков.

Вход (source_data/chemicals_tj/):
  pollutions_raw.json             ответ /api/pollutions (db/fetch_chemicals_tj.py)
  files.csv, docs_unique.csv,
  docs/, docs/_downloaded.json    вложения (db/fetch_chemicals_tj.py --download-docs)
  extracted/measurements_long.csv значения из текстовых PDF (db/extract_pop_tables.py)
  manual/protocol126_transcribed.csv     протокол АФИ №126 (скан), перенесён вручную
  manual/other_scans_transcribed.csv     отчёт Института химии им. Никитина 2017 (скан)

Выход (source_data/chemicals_tj/pop_layer/):
  pop_layer.gpkg  слои:
    pop_sites         точки участков (192) - анкета, файлы, сводка анализов, признаки из текста
    pop_samples       точки проб - показатели по веществам в широком формате
    pop_measurements  таблица: проба x вещество (длинный формат, все источники и ранги)
    pop_text_facts    таблица: признак из текста + фрагмент-основание
    pop_files         таблица: вложения участков со ссылками
    pop_sources       таблица: источники лабораторных данных
  те же таблицы в CSV (UTF-8 с BOM) и data_dictionary.md

Запуск: python db/build_pop_layer.py   (нужны pandas, geopandas, pyogrio, pdftotext)
"""
import csv
import json
import math
import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point

ROOT = Path(__file__).resolve().parent.parent / "source_data" / "chemicals_tj"
OUT = ROOT / "pop_layer"
BASE = "http://chemicals.tj"
DDT_NORM = 0.1  # мг/кг, норматив, названный заказчиком (перечень № 6229-91)

SUBST_RU = {
    "DDT": "ДДТ", "DDD": "ДДД", "DDE": "ДДЕ", "a_HCH": "α-ГХЦГ", "b_HCH": "β-ГХЦГ", "g_HCH": "γ-ГХЦГ (линдан)",
    "d_HCH": "δ-ГХЦГ", "HCH_ab": "ГХЦГ (α+β)", "HCH_unspecified": "ГХЦГ (изомер не указан)",
    "heptachlor": "гептахлор", "heptachlor_epoxide": "гептахлор-эпоксид", "aldrin": "альдрин",
    "dieldrin": "дильдрин", "endrin": "эндрин", "endrin_aldehyde": "эндрин-альдегид",
    "endosulfan_1": "эндосульфан I (α)", "endosulfan_2": "эндосульфан II (β)",
    "endosulfan_sulfate": "эндосульфан-сульфат", "methoxychlor": "метоксихлор",
    "pesticides_total": "пестициды/хлорорганика (сумма)", "unspecified_1": "загрязнитель 1 (не указан)",
    "unspecified_2": "загрязнитель 2 (не указан)",
}
WIDE_SUBST = ["DDT", "DDD", "DDE", "a_HCH", "b_HCH", "g_HCH", "d_HCH", "HCH_ab", "HCH_unspecified",
              "heptachlor", "heptachlor_epoxide", "aldrin", "dieldrin", "endrin", "endrin_aldehyde",
              "endosulfan_1", "endosulfan_2", "endosulfan_sulfate", "methoxychlor", "pesticides_total",
              "unspecified_1", "unspecified_2"]

from xrf_sites import XRF_SITES  # центры XRF-складов из source_data/xrf_warehouses.csv


def hv(la1, lo1, la2, lo2):
    r = math.radians
    h = math.sin(r(la2 - la1) / 2) ** 2 + math.cos(r(la1)) * math.cos(r(la2)) * math.sin(r(lo2 - lo1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def tj(name):
    m = re.search(r"TJ-?(\d+)", name or "")
    return f"TJ-{m.group(1)}" if m else None


def fnum(x):
    try:
        return float(str(x).replace(",", "."))
    except (TypeError, ValueError):
        return None


# ============================ участки ==========================================
def load_sites():
    A = json.loads((ROOT / "pollutions_raw.json").read_text(encoding="utf-8"))
    P = [s for s in A if (s.get("pollution_type") or {}).get("name") == "СОЗ"]
    return P


SITE_FIELDS_DROP = {"files", "pollution_type", "pollution_status", "inn", "legal_address", "actual_address",
                    "employee_count", "trained_employee_count", "equipment_exists", "entrepreneur",
                    "region_id", "district_id", "phone", "journalists", "pollution_type_id"}

# ============================ признаки из текста ==============================
SENT_SPLIT = re.compile(r"(?<=[.!?;])\s+")
DIST_RE = re.compile(r"(\d+(?:[.,]\d+)?)(?:\s*[-–]\s*(\d+(?:[.,]\d+)?))?\s*(км|километр\w*|м\b|метр\w*)", re.I)
TARGETS = {
    "dist_houses_m": r"жил\w*|дом\w*|посел\w*|населен\w*|кишлак\w*|сел[оаеу]\b|махалл\w*|двор\w*|постро",
    "dist_water_m": r"канал\w*|арык\w*|рек[аиеу]\w*|речк\w*|ручь?\w*|пруд\w*|водо[её]м\w*|канав\w*|коллектор\w*|родник\w*|источник\w* вод",
    "dist_school_m": r"школ[аыуеоия]\w*(?<!школьного)(?<!школьник)(?<!школьники)|детск\w* сад\w*|детсад\w*",
    "dist_road_m": r"дорог\w*|трасс\w*|шоссе",
    "dist_fields_m": r"пол[ея]\b|полей|посев\w*|огород\w*|сельхоз\w*|сельскохоз\w*|урожа\w*|хлопк\w*|сад\w*",
    "dist_well_m": r"колод\w*|скважин\w*",
}
PESTICIDES = [("ДДТ", r"ддт|ddt|дихлордифенилтрихлор"), ("ГХЦГ/линдан", r"гхцг|hch|линдан|гексахлор"),
              ("альдрин", r"альдрин"), ("дильдрин", r"ди[еэ]?льдрин"), ("гептахлор", r"гептахлор"),
              ("эндосульфан/тиодан", r"эндосульф|тиодан"), ("бутифос", r"бутифос"), ("ртутные (гранозан)", r"гранозан|ртутьсодерж|ртутн\w* препарат"),
              ("хлорат магния", r"хлорат"), ("2,4-Д", r"2,4-д\b"), ("дефолианты", r"дефолиант"),
              ("гербициды", r"гербицид"), ("фунгициды", r"фунгицид"), ("метоксихлор", r"метоксихлор"),
              ("токсафен/полихлоркамфен", r"токсафен|полихлоркамфен"), ("карбофос/малатион", r"карбофос|малатион"),
              ("минеральные удобрения", r"удобрен")]
KIND = [("полигон захоронения", r"полигон"), ("захоронение", r"захорон"), ("аэродром/авиаплощадка", r"аэродром|аэропорт|авиаплощад|авиаотряд|взл[её]тн"),
        ("склад", r"склад|хранилищ"), ("хлопкопункт", r"хлопкопункт|хлоб\s*пункт"), ("полевой стан", r"полев\w* стан"),
        ("школа (хранение химикатов)", r"химическ\w* препарат\w* в .*школ")]
FLAGS_RE = {
    "mentions_children": r"дет(и|ей|ям|ьми)\b|детск|школьник|ребен|реб[её]нок",
    "mentions_school": r"школ",
    "mentions_mosque_cemetery": r"мечет|кладбищ|мазар",
    "mentions_livestock_access": r"скот|животн|коров|овц|коз[аы]|пастб|выпас",
    "mentions_crops_on_or_near": r"выращива|посев|урожа|огород|хлопок|пшениц|овощ",
    "mentions_smell": r"запах",
    "mentions_stains": r"пятн|окрашен\w* почв|ж[её]лт\w* .*почв|бел\w* налет",
    "mentions_health_complaints": r"жалу|заболева|болезн|головн\w* бол|аллерг|отравлен|рак\b|онколог|смерт\w* (люд|жител)",
    "mentions_animal_deaths": r"гибел\w* (живот|скот|птиц)|пад[её]ж|погиб\w* (живот|скот)",
    "mentions_houses_built_on_or_near_site": r"(на|на месте|на территории)\s+(бывшего\s+)?склад\w*\s+(были\s+)?построен|построены\s+(новые\s+)?(жилые\s+)?дома|на территории .{0,40}построен",
    "mentions_buried_pesticides": r"захорон|закопа|зарыт",
    "mentions_bags_containers": r"мешк|бочк|тар[аыу]\b|упаковк|контейнер",
    "mentions_drinking_water": r"питьев|для питья|пить\b",
    "mentions_irrigation": r"ороше|ирригац|полив",
    "mentions_remediation": r"ремедиац|очистк|очищен|вывез|рекультив",
    "text_has_chatgpt_artifact": r"chatgpt|сказал:",
}
STATE = [("полностью разрушен/не сохранился", r"полностью разруш|не осталось|не сохранил|снесен|снес[её]н|разобран|следов не|никаких следов"),
         ("полуразрушен", r"полуразруш|частично разруш|практически разруш|разрушен|остались только стены|без крыши"),
         ("используется", r"используется|использует|переоборуд|под (склад|сарай|хлев)"),
         ("сохранился", r"сохранил|в хорошем состоян|закрыт\w* и охраня")]
USE = [("жилая застройка", r"построен\w* (жил|дом)|жилые дома .{0,30}(на территории|на месте)"),
       ("хлев/содержание скота", r"скот|хлев|сарай|загон"),
       ("хранение (дрова, уголь, зерно и т.п.)", r"хранени\w* (дров|угл|зерн|сена|кормов)|склад для хранения"),
       ("сельхозугодья", r"(засеян|распахан|посев|огород|сад) .{0,30}(на территории|на месте)"),
       ("воинская часть", r"воинск|военн\w* част")]


class SiteIndex(dict):
    """tj_code -> карточка; на карте один код может встречаться дважды (TJ-7580),
    поэтому храним все карточки и выбираем нужную по координатам."""
    def __init__(self, P):
        super().__init__()
        self.all = defaultdict(list)
        for s in P:
            self.all[tj(s["site_name"])].append(s)
        for k, v in self.all.items():
            dict.__setitem__(self, k, sorted(v, key=lambda x: int(x["id"]))[0])

    def pick(self, code, lat=None, lon=None):
        v = self.all.get(code)
        if not v:
            return None
        if len(v) > 1 and lat not in (None, ""):
            return min(v, key=lambda s: hv(float(lat), float(lon), float(s["latitude"]), float(s["longitude"])))
        return self[code]


def text_of(s):
    return " ".join(str(s.get(k) or "") for k in ("problem_description", "site_description", "impact", "notes"))


def extract_text_facts(code, text, source="карточка участка"):
    facts = []
    t = re.sub(r"\s+", " ", text)
    tl = t.lower()

    def add(field, value, snippet):
        facts.append({"tj_code": code, "field": field, "value": value, "snippet": snippet.strip()[:300], "text_source": source})

    for name, pat in KIND:
        m = re.search(pat, tl)
        if m:
            add("site_kind_text", name, t[max(0, m.start() - 60): m.end() + 60]); break
    for name, pat in PESTICIDES:
        m = re.search(pat, tl)
        if m:
            add("pesticide_mentioned", name, t[max(0, m.start() - 60): m.end() + 60])
    for name, pat in STATE:
        m = re.search(pat, tl)
        if m:
            add("building_state_text", name, t[max(0, m.start() - 80): m.end() + 60]); break
    for name, pat in USE:
        m = re.search(pat, tl)
        if m:
            add("current_use_text", name, t[max(0, m.start() - 80): m.end() + 60])
    for f, pat in FLAGS_RE.items():
        m = re.search(pat, tl)
        if m:
            add(f, True, t[max(0, m.start() - 80): m.end() + 80])
    # расстояния: число+единица и ключевое слово цели в той же фразе
    for sent in SENT_SPLIT.split(t):
        sl = sent.lower()
        for m in DIST_RE.finditer(sl):
            a, b, unit = fnum(m.group(1)), fnum(m.group(2)), m.group(3)
            if a is None:
                continue
            v = (a + b) / 2 if b else a
            meters = v * 1000 if unit.startswith("км") or unit.startswith("кил") else v
            if meters > 50000 or meters <= 0:
                continue
            w0 = max(0, m.start() - 70)
            window = sl[w0: m.end() + 70]
            if re.search(r"глубин|площад|размер|длин|ширин|высот|\bм2|м²|кв\.?\s*м|м3|м³|куб", sl[max(0, m.start() - 30): m.end() + 6]):
                continue
            if re.search(r"\bнет\b|\bне\s+(?:наход|имеет|распол|проход|выращ)", window):
                continue  # «в пределах 1000 м нет жилых домов» - не расстояние до объекта
            # объект расстояния = ближайшее к числу ключевое слово (кроме «школьного возраста» и т.п.)
            npos = m.start() - w0
            best = None
            for f, pat in TARGETS.items():
                for km in re.finditer(pat, window):
                    if re.match(r"школьн", window[km.start():]):
                        continue
                    dd = min(abs(km.start() - npos), abs(km.end() - npos))
                    if best is None or dd < best[0]:
                        best = (dd, f)
            if best:
                add(best[1], round(meters, 1), sent)
    # количества пестицидов
    for m in re.finditer(r"(\d+(?:[.,]\d+)?(?:\s*\d{3})*)\s*(?:[-–]\s*(\d+(?:[.,]\d+)?)\s*)?(тонн\w*|т\b|кг\b|килограмм\w*)", t, re.I):
        n = fnum(m.group(1).replace(" ", ""))
        if n is None:
            continue
        kg = n * 1000 if m.group(3).lower().startswith("т") else n
        add("pesticide_quantity_kg_mention", kg, t[max(0, m.start() - 120): m.end() + 60])
    for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*(?:м3|м³|куб\w*\s*м\w*)", t, re.I):
        add("volume_m3_mention", fnum(m.group(1)), t[max(0, m.start() - 120): m.end() + 60])
    for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*(га\b|гектар\w*)", t, re.I):
        add("area_ha_mention", fnum(m.group(1)), t[max(0, m.start() - 80): m.end() + 40])
    for m in re.finditer(r"(\d+)\s*(человек|жител\w*|семей|семьи|детей|домов|домохозяйств\w*)", tl):
        kinds = {"челов": "people", "жител": "people", "семе": "families", "семь": "families", "дете": "children",
                 "домо": "houses", "домох": "households"}
        k = next(v for kk, v in kinds.items() if m.group(2).startswith(kk))
        add(f"count_{k}_mention", int(m.group(1)), t[max(0, m.start() - 100): m.end() + 60])
    for m in re.finditer(r"\b(19[3-9]\d|20[0-2]\d)\s*(?:[-–]\s*\d{2,4}\s*)?(?:г\.|год\w*|гг)", t):
        add("year_mention", int(m.group(1)), t[max(0, m.start() - 120): m.end() + 40])
    return facts


def summarize_facts(facts):
    """Свёртка фактов в поля участка: расстояния - минимум, количества - максимум,
    флаги - True/пусто, перечни - через «; »."""
    by = defaultdict(list)
    for f in facts:
        by[f["field"]].append(f["value"])
    out = {}
    for k, v in by.items():
        if k.startswith("dist_"):
            out[k + "_text"] = min(v)
        elif k in ("pesticide_quantity_kg_mention", "volume_m3_mention", "area_ha_mention") or k.startswith("count_"):
            out[k.replace("_mention", "_max_text")] = max(v)
        elif k == "year_mention":
            out["years_mentioned_text"] = "; ".join(str(x) for x in sorted(set(v)))
        elif k in ("pesticide_mentioned", "current_use_text"):
            out[k if k.endswith("_text") else "pesticides_mentioned_text"] = "; ".join(dict.fromkeys(v))
        elif k in ("site_kind_text", "building_state_text"):
            out[k] = v[0]
        else:
            out[k] = True
    return out


# ============================ лабораторные данные ==============================
def load_measurements(site_by_code):
    m = pd.read_csv(ROOT / "extracted" / "measurements_long.csv", dtype=str, encoding="utf-8-sig").fillna("")
    m["source_rank"] = m["source_rank"].astype(int)
    m["value_mgkg"] = m["value_mgkg"].map(fnum)
    m["below_lod"] = m["below_lod"].eq("True")
    m["sample_year"] = ""
    m.loc[m.doc_type == "afi_2021", ["lab", "report_ref", "sample_date"]] = ["ФГБНУ АФИ (Санкт-Петербург)", "Протокол №8 от 12.02.2021", ""]
    m.loc[m.doc_type == "afi_2021", "sample_year"] = "2020"
    m.loc[m.doc_type == "ilim_2017", "sample_year"] = "2016"
    m["coord_source"] = m["lat"].map(lambda x: "документ" if x else "")
    m["source_id"] = m["doc_type"].map({"afi_2021": "AFI8_2021", "ilim_2017": "ILIM42_2017", "log": "JOURNAL"})

    # ---- протокол АФИ №126 (скан, перенесён вручную) ----
    p = pd.read_csv(ROOT / "manual" / "protocol126_transcribed.csv", dtype=str).fillna("")
    rows = []
    for _, r in p.iterrows():
        if r["DDT"] == "":
            continue
        for sub in ("DDT", "DDD", "DDE", "g_HCH"):
            v = r[sub]
            nd = v == "НО"
            rows.append({"source_rank": 1, "doc_type": "afi_126_2019", "source_id": "AFI126_2019",
                         "doc_file": "584da0600971.pdf", "lab": "ФГБНУ АФИ (Санкт-Петербург)",
                         "report_ref": "Протокол №126 от 20.09.2019 (скан, перенесён вручную)",
                         "tj_code": "TJ-" + r["site_group"], "sample_label": r["sample"], "replicate": "",
                         "substance": sub, "value_mgkg": None if nd else float(v), "below_lod": nd,
                         "raw_value": "не обн." if nd else v.replace(".", ","), "unit_raw": "мг/кг",
                         "page": r["page"], "flags": "protocol_label_typo" if r["note"] else "",
                         "note": r["note"], "sample_year": "2019", "lat": "", "lon": "", "coord_source": ""})
    p126 = pd.DataFrame(rows)
    # полевые коды tj77xx протокола, которых нет на карте, -> карточки карты по названию села
    linked = pd.read_csv(ROOT / "manual" / "protocol126_map_links.csv", dtype=str).fillna("")
    lk = {r.protocol_code: r.map_code for r in linked.itertuples() if r.match == "уверенно"}
    hit = p126.tj_code.isin(lk)
    p126.loc[hit, "note"] = p126.loc[hit, "tj_code"].map(lambda c: f"в протоколе участок под кодом {c}; карточка карты - по названию села")
    p126.loc[hit, "flags"] = p126.loc[hit, "flags"].map(lambda f: ";".join(x for x in [f, "site_by_place_name"] if x))
    p126.loc[hit, "tj_code"] = p126.loc[hit, "tj_code"].map(lk)
    p126_sites = set(p126.tj_code)
    # журналы 2019 по тем же участкам дублируют протокол №126
    dup = (m.doc_type == "log") & m.tj_code.isin(p126_sites) & m.substance.isin(["DDT", "DDD", "DDE"]) & (m.source_rank == 2)
    m.loc[dup, "source_rank"] = 0
    m.loc[dup, "flags"] = m.loc[dup, "flags"].map(lambda f: ";".join(x for x in [f, "dup_of_protocol126"] if x))
    # координаты для проб протокола №126 - из журналов того же участка по совпадению значения ДДТ
    jr = m[(m.doc_type == "log") & m.substance.eq("DDT") & m.lat.ne("") & m.tj_code.isin(p126_sites)]
    coords = defaultdict(list)
    for _, r in jr.iterrows():
        coords[(r.tj_code, round(r.value_mgkg or 0, 2))].append((r.lat, r.lon, r.sample_date, r.depth_cm, r.sample_kind))
    used = Counter()
    for (code, lab), g in p126.groupby(["tj_code", "sample_label"]):
        d = g[g.substance == "DDT"].iloc[0]
        key = (code, round(d.value_mgkg or 0, 2))
        cand = coords.get(key, [])
        if cand and used[key] < len(cand):
            la, lo, dt, dep, kind = cand[used[key]]
            used[key] += 1
            idx = g.index
            p126.loc[idx, ["lat", "lon", "sample_date", "depth_cm", "sample_kind"]] = [la, lo, dt, dep, kind]
            p126.loc[idx, "coord_source"] = "журнал проб (совпадение ДДТ)"

    # ---- отчёт Куканиева / Бобрицкой, 2012 (TJ-3208, 3240, 3241, 3248) ----
    kk = parse_kukaniev()
    # ---- скан отчёта Института химии им. Никитина, 2017 ----
    o = pd.read_csv(ROOT / "manual" / "other_scans_transcribed.csv", dtype=str).fillna("")
    orows = []
    for _, r in o.iterrows():
        code = r.tj_code or ("TJ-5404" if r.site_hint.startswith("Фархор") else "")
        orows.append({"source_rank": 1, "doc_type": "nikitin_2017", "source_id": "NIKITIN_2017",
                      "doc_file": r.doc_file, "lab": "Институт химии им. В.И. Никитина АН РТ",
                      "report_ref": "Письмо 2008/23-1/281 от 27.10.2017 (скан, перенесён вручную)",
                      "tj_code": code, "tj_candidates": "" if r.tj_code or code else "",
                      "sample_label": r.sample_label, "substance": r.substance, "value_mgkg": float(r.value_mgkg),
                      "below_lod": False, "raw_value": r.value_mgkg, "unit_raw": "mg/kg", "sample_date": r.sample_date,
                      "sample_year": "2017", "note": r.note + ("" if r.tj_code else (" | участок определён по названию района (Фархор, обследование 2017 г.)" if code else " | участок на карте не найден")),
                      "flags": "" if r.tj_code else ("site_by_district_name" if code else "site_unknown"),
                      "lat": "", "lon": "", "coord_source": ""})
    allm = pd.concat([m, p126, kk, pd.DataFrame(orows)], ignore_index=True).fillna("")
    for c in ("value_mgkg",):
        allm[c] = allm[c].map(lambda v: None if v == "" else v)

    # ---- координаты проб АФИ №8 из реестров 2020 г. (по порядку повторностей) ----
    regcoords = register_coords(allm)
    afi = allm[(allm.doc_type == "afi_2021")]
    for code, g in afi.groupby("tj_code"):
        labels = list(dict.fromkeys(g.sample_label))
        pts = regcoords.get(code)
        if pts and len(pts) == len(labels):
            for lab, (la, lo) in zip(labels, pts):
                idx = g[g.sample_label == lab].index
                allm.loc[idx, ["lat", "lon", "coord_source"]] = [la, lo, "реестр проб 2020 (порядок повторностей)"]
    # ---- координаты проб ИЛИМ 2016 из журналов 2016 г. (номер пробы 1/2) ----
    jl = allm[(allm.doc_type == "log") & allm.lat.ne("") & allm.tj_code.isin(set(allm[allm.doc_type == "ilim_2017"].tj_code))]
    jpts = defaultdict(dict)
    for _, r in jl.iterrows():
        n = re.match(r"\s*(\d+)", r.sample_label or "")
        if n:
            jpts[r.tj_code].setdefault(int(n.group(1)), (r.lat, r.lon, r.sample_date, r.depth_cm))
    il = allm[allm.doc_type == "ilim_2017"]
    for (code, lab), g in il.groupby(["tj_code", "sample_label"]):
        n = re.search(r"№?\s*(\d)\b", lab.replace("Проба", ""))
        num = int(n.group(1)) if n else (1 if "целев" in lab.lower() else 2 if re.search("смеш|компози|сбор", lab.lower()) else None)
        if num and num in jpts.get(code, {}):
            la, lo, dt, dep = jpts[code][num]
            allm.loc[g.index, ["lat", "lon", "sample_date", "depth_cm", "coord_source"]] = [la, lo, dt, dep, "журнал 2016 (номер пробы)"]

    # не привязанные к участку значения: если есть координаты - ближайший участок в пределах 1 км
    for i, r in allm[(allm.tj_code == "") & allm.lat.ne("")].iterrows():
        la, lo = float(r.lat), float(r.lon)
        best = min(site_by_code.values(), key=lambda s: hv(la, lo, float(s["latitude"]), float(s["longitude"])))
        d = hv(la, lo, float(best["latitude"]), float(best["longitude"]))
        if d < 1000:
            allm.loc[i, "tj_code"] = tj(best["site_name"])
            allm.loc[i, "flags"] = ";".join(x for x in [r["flags"], f"site_by_nearest_{round(d)}m"] if x)
    # журнальная проба, все значения которой есть в лабораторном протоколе по тому же участку, - дубль
    lab = allm[(allm.source_rank == 1) & allm.tj_code.ne("")]
    labvals = defaultdict(list)
    for _, r in lab.iterrows():
        if r.value_mgkg not in (None, ""):
            labvals[r.tj_code].append(round(float(r.value_mgkg), 2))
    jj = allm[(allm.doc_type == "log") & (allm.source_rank == 2) & allm.tj_code.isin(labvals)]
    for (code, doc, lab_), g in jj.groupby(["tj_code", "doc_key", "sample_label"]):
        # сравниваем только значения: в части журналов заголовки веществ устаревшие (A-BHC над ДДТ и т.п.)
        nz = [round(float(r.value_mgkg), 2) for _, r in g.iterrows() if r.value_mgkg not in (None, "") and float(r.value_mgkg) > 0]
        if nz and all(v in labvals[code] for v in nz):
            allm.loc[g.index, "source_rank"] = 0
            allm.loc[g.index, "flags"] = allm.loc[g.index, "flags"].map(lambda f: ";".join(x for x in [f, "dup_of_lab_protocol"] if x))
    # сводные реестры 2020 г. без привязки к участку - дубли протокола АФИ №8
    mb = (allm.doc_type == "log") & allm["flags"].str.contains("multi_site_block", na=False) & allm["flags"].str.contains("order_2020", na=False)
    allm.loc[mb, "source_rank"] = 0
    # известные ошибки привязки журналов
    bad = (allm.doc_file == "f8dc1cff204d.pdf") & (allm.tj_code == "TJ-7447")
    allm.loc[bad, "source_rank"] = 0
    allm.loc[bad, "flags"] = allm.loc[bad, "flags"].map(
        lambda f: ";".join(x for x in [f, "journal_misattached:values_match_TJ-7545_protocol126"] if x))
    # координаты: заполнить точкой участка там, где у пробы своих нет
    for i, r in allm[(allm.lat == "") & allm.tj_code.ne("")].iterrows():
        s = site_by_code.pick(r.tj_code)
        if s:
            allm.loc[i, ["lat", "lon", "coord_source"]] = [s["latitude"], s["longitude"], "точка участка (координат пробы нет)"]
    # расстояние пробы до точки участка
    def dist(r):
        s = site_by_code.pick(r.tj_code, r.lat, r.lon)
        if not s or r.lat == "" or r.coord_source.startswith("точка участка"):
            return None
        return round(hv(float(r.lat), float(r.lon), float(s["latitude"]), float(s["longitude"])))
    allm["dist_to_site_m"] = allm.apply(dist, axis=1)
    allm["map_id"] = allm.apply(lambda r: (site_by_code.pick(r.tj_code, r.lat, r.lon) or {}).get("id", ""), axis=1)
    allm["year"] = allm.apply(lambda r: r.sample_year or year_of(r.sample_date), axis=1)
    return allm


def year_of(d):
    m = re.search(r"(19|20)\d{2}", d or "")
    if m:
        return m.group(0)
    m = re.search(r"/(\d{2})$", (d or "").strip())
    return "20" + m.group(1) if m else ""


def register_coords(allm):
    RX = re.compile(r"(?:(\d{4})\s+)?([ТTКK]\d\*?|\b\d\b)\s+(\d{2}[.,]\d{3,})\s+(\d{2}[.,]\d{3,})")
    out = {}
    docs = allm[allm["flags"].str.contains("order_2020", na=False)].groupby("doc_file").tj_code.agg(lambda x: sorted(set(x) - {""}))
    for f, codes in docs.items():
        if len(codes) != 1:
            continue
        t = subprocess.run(["pdftotext", "-layout", str(ROOT / "docs" / f), "-"], capture_output=True).stdout.decode("utf-8", "replace")
        pts = []
        for _, rep, a, b in RX.findall(t):
            a, b = fnum(a), fnum(b)
            la, lo = (a, b) if 36 <= a <= 41.5 else (b, a)
            if 36 <= la <= 41.5 and 67 <= lo <= 75.5:
                pts.append((str(la), str(lo)))
        if pts:
            out.setdefault(codes[0], pts)
    return out


def parse_kukaniev():
    """Таблица «Содержание хлорорганических пестицидов в образцах почвы, мг/кг»
    (Бобрицкая Л.С., 27.07.2012; подписи М.А. Куканиев, Х.Х. Авлоев). Одна и та же
    таблица прикреплена к TJ-3208, TJ-3240, TJ-3248 (текстовый PDF и JPG).
    Посёлок -> участок по названию: «Поселок 1» = TJ-3208 (деревня №1, Кумсангир),
    Согдиана = TJ-3248, Багара = TJ-3240, Ойкамар = TJ-3241."""
    cols = ["HCH_unspecified", "b_HCH", "g_HCH", "heptachlor", "aldrin", "dieldrin", "DDT", "methoxychlor",
            "endosulfan_1", "endosulfan_2"]
    sites = {"поселок 1": "TJ-3208", "поселок согдиана": "TJ-3248", "поселок багара": "TJ-3240", "поселок ойкамар": "TJ-3241"}
    t = subprocess.run(["pdftotext", "-layout", str(ROOT / "docs" / "140738356a3f.pdf"), "-"],
                       capture_output=True).stdout.decode("utf-8", "replace")
    rows, cur, seen = [], None, set()
    for line in t.splitlines():
        l = line.strip().lower().replace("ё", "е")
        if l in sites:
            cur = l
            continue
        m = re.match(r"^\s*(проба\s*\d+[^\d]*?)\s+((?:\S+\s+){9}\S+)\s*$", line, re.I)
        if m and cur and (cur, m.group(1).strip()) not in seen:
            seen.add((cur, m.group(1).strip()))
            vals = m.group(2).split()
            for sub, raw in zip(cols, vals):
                raw2 = raw.replace("о;", "0,")
                v = fnum(raw2)
                rows.append({"source_rank": 1, "doc_type": "kukaniev_2012", "source_id": "KUKANIEV_2012",
                             "doc_file": "140738356a3f.pdf", "lab": "Институт химии им. В.И. Никитина АН РТ (М.А. Куканиев)",
                             "report_ref": "Таблица «Содержание хлорорганических пестицидов в образцах почвы», 27.07.2012",
                             "tj_code": sites[cur], "sample_label": f"{cur.title()}: {m.group(1).strip()}",
                             "substance": sub, "value_mgkg": v, "below_lod": False, "raw_value": raw,
                             "unit_raw": "мг/кг", "page": "1", "sample_date": "2012-07-27", "sample_year": "2012",
                             "flags": ";".join(x for x in ["zero_reported" if v == 0 else "",
                                                           "ocr_fix" if raw != raw2 else "",
                                                           "site_by_settlement_name"] if x),
                             "note": "столбец «ГХЦГ» без указания изомера" if sub == "HCH_unspecified" else "",
                             "lat": "", "lon": "", "coord_source": ""})
    return pd.DataFrame(rows)


# ============================ сборка ===========================================
def main():
    OUT.mkdir(exist_ok=True)
    P = load_sites()
    site_by_code = SiteIndex(P)
    files = pd.read_csv(ROOT / "files.csv", dtype=str, encoding="utf-8-sig").fillna("")
    files = files[files.pollution_type == "СОЗ"].copy()
    files["is_photo"] = files.file_type.eq("Фотография") & files.mime_type.str.startswith("image")
    cache = json.loads((ROOT / "docs" / "_downloaded.json").read_text(encoding="utf-8"))
    files["local_doc_file"] = files.url.map(lambda u: cache.get(u, ""))

    allm = load_measurements(site_by_code)
    allm.to_csv(OUT / "pop_measurements.csv", index=False, encoding="utf-8-sig")

    # ---- пробы (широкий формат), только сводная выборка rank 1/2 ----
    use = allm[allm.source_rank.isin([1, 2])].copy()
    use["sample_key"] = use.apply(lambda r: f"{r.source_id}|{r.doc_file}|{r.tj_code}|{r.sample_label}|{r.lat}|{r.lon}", axis=1)
    samples = []
    for key, g in use.groupby("sample_key", sort=False):
        r0 = g.iloc[0]
        d = {"sample_key": key, "tj_code": r0.tj_code, "map_id": r0.map_id, "tj_candidates": r0.get("tj_candidates", ""),
             "sample_label": re.sub(r"\s+", " ", str(r0.sample_label)).strip(), "source_id": r0.source_id,
             "lab": r0.lab, "report_ref": r0.report_ref, "doc_file": r0.doc_file, "year": r0.year,
             "sample_date": r0.sample_date, "depth_cm": r0.depth_cm, "sample_kind": r0.sample_kind,
             "lat": r0.lat, "lon": r0.lon, "coord_source": r0.coord_source, "dist_to_site_m": r0.dist_to_site_m,
             "flags": ";".join(sorted({f for x in g["flags"] for f in str(x).split(";") if f}))}
        for sub in WIDE_SUBST:
            x = g[g.substance == sub]
            if len(x):
                xr = x.iloc[0]
                d[sub] = xr.value_mgkg if xr.value_mgkg is not None and xr.value_mgkg != "" else None
                d[sub + "_nd"] = bool(xr.below_lod)
        # «не обнаружено» (пусто + флаг _nd) входит в сумму как 0; сумма пустая, только если
        # ни одно вещество группы не определялось
        def gsum(keys):
            vals = [d.get(k) for k in keys]
            num = [v for v in vals if isinstance(v, (int, float)) and np.isfinite(v)]
            nd = [k for k in keys if d.get(k + "_nd")]
            return float(sum(num)) if (num or nd) else None
        d["DDT_group_sum"] = gsum(("DDT", "DDD", "DDE"))
        d["HCH_sum"] = gsum(("a_HCH", "b_HCH", "g_HCH", "d_HCH", "HCH_ab", "HCH_unspecified"))
        d["DDT_x_norm"] = round(d["DDT"] / DDT_NORM, 2) if d.get("DDT") is not None else None
        d["DDT_gt_norm"] = (d["DDT"] > DDT_NORM) if d.get("DDT") is not None else None
        samples.append(d)
    S = pd.DataFrame(samples)
    S = S[[c for c in S.columns if not (c.endswith("_nd") and S[c].isna().all())]]
    S.insert(0, "sample_id", range(1, len(S) + 1))

    # ---- сводка по участку ----
    lab_sum = {}
    S["site_key"] = S.apply(lambda r: r.map_id if r.map_id not in ("", None) else ("TJ:" + r.tj_code if r.tj_code else ""), axis=1)
    for code, g in S[S.site_key.ne("")].groupby("site_key"):
        def mx(col):
            return g[col].max() if col in g and g[col].notna().any() else None
        lab_sum[code] = {
            "lab_n_samples": len(g), "lab_sources": "; ".join(sorted(set(g.source_id))),
            "lab_years": "; ".join(sorted({str(y) for y in g.year if y})),
            "lab_n_samples_with_own_coords": int((~g.coord_source.str.startswith("точка участка")).sum()),
            "ddt_max": mx("DDT"), "ddd_max": mx("DDD"), "dde_max": mx("DDE"), "ddt_group_sum_max": mx("DDT_group_sum"),
            "ddt_x_norm_max": round(mx("DDT") / DDT_NORM, 1) if mx("DDT") is not None else None,
            "n_samples_ddt_gt_norm": int(g["DDT_gt_norm"].eq(True).sum()) if "DDT_gt_norm" in g else 0,
            "hch_sum_max": mx("HCH_sum"), "g_hch_max": mx("g_HCH"), "aldrin_max": mx("aldrin"), "dieldrin_max": mx("dieldrin"),
            "heptachlor_max": mx("heptachlor"), "endosulfan_max": max([v for v in (mx("endosulfan_1"), mx("endosulfan_2"), mx("endosulfan_sulfate")) if v is not None], default=None),
            "pesticides_total_max": mx("pesticides_total"),
            "substances_detected": "; ".join(SUBST_RU[s] for s in WIDE_SUBST if s in g and (g[s].fillna(0) > 0).any()),
        }

    # ---- текст ----
    facts = []
    for s in P:
        facts += [dict(f, map_id=s["id"]) for f in extract_text_facts(tj(s["site_name"]), text_of(s))]
    # текст отчёта о ремедиации (TJ-4958) - факты из документа
    rep = ROOT / "docs" / "bc0964938bab.doc"
    if rep.exists():
        txt = subprocess.run(["soffice", "--headless", "--cat", str(rep)], capture_output=True, timeout=120).stdout.decode("utf-8", "replace")
        if txt.strip():
            facts.append({"tj_code": "TJ-4958", "field": "remediation_report", "value": "Pure Earth / UNIDO, май-июнь 2017",
                          "snippet": "FINAL PROJECT REPORT: Obsolete Pesticides Cleanup Project in Saidov, Jami (bc0964938bab.doc)",
                          "text_source": "документ bc0964938bab.doc"})
            for k, pat in [("remediation_removed_soil_m3", r"removed up to ([\d\-–]+) cubic meters of polluted soil"),
                           ("remediation_pesticides_found_kg", r"up to (\d+) kg of pesticides were buried"),
                           ("burial_depth_m", r"burial depth of the pesticides found at Saidov was from ([\d ]+to \d+) meters"),
                           ("groundwater_depth_m", r"Depth of groundwater table is about (\d+) meters"),
                           ("lindane_exceedance_text", r"(Lindane in soil exceeds[^.]+)"),
                           ("families_living", r"(\d+) families lived there"),
                           ("remediation_budget_usd", r"total budget of the project was \$([\d,]+)")]:
                mm = re.search(pat, txt)
                if mm:
                    facts.append({"tj_code": "TJ-4958", "field": k, "value": mm.group(1),
                                  "snippet": txt[max(0, mm.start() - 80): mm.end() + 80].replace("\n", " "),
                                  "text_source": "документ bc0964938bab.doc"})
    F = pd.DataFrame(facts)
    F.to_csv(OUT / "pop_text_facts.csv", index=False, encoding="utf-8-sig")
    fsum = {mid: summarize_facts(g.to_dict("records")) for mid, g in F[F.text_source == "карточка участка"].groupby("map_id")}

    # ---- дубли/близкие участки, точность координат, XRF ----
    near = {}
    for s in P:
        la, lo = float(s["latitude"]), float(s["longitude"])
        near[s["id"]] = min((hv(la, lo, float(o["latitude"]), float(o["longitude"])), tj(o["site_name"]))
                            for o in P if o["id"] != s["id"])
    xrf_of = {}
    for n, (la, lo) in XRF_SITES.items():
        d, mid = min((hv(la, lo, float(s["latitude"]), float(s["longitude"])), s["id"]) for s in P)
        if d < 300:
            xrf_of[mid] = (n, round(d))
    code_count = Counter(tj(s["site_name"]) for s in P)

    rows = []
    for s in P:
        c, mid = tj(s["site_name"]), s["id"]
        d = {"tj_code": c, "site_name": re.sub(r"^Объект\s+TJ-?\d+\.\s*", "", s["site_name"]).strip(),
             "site_name_full": s["site_name"], "map_id": s["id"],
             "pollution_status": (s.get("pollution_status") or {}).get("name"),
             "card_url": f"{BASE}/api/pollutions/{s['id']}"}
        for k, v in s.items():
            if k in SITE_FIELDS_DROP or k in ("id", "site_name"):
                continue
            d[k] = v
        d["site_date"] = (s.get("site_date") or "")[:10]
        d["created_at"] = (s.get("created_at") or "")[:10]
        d["updated_at"] = (s.get("updated_at") or "")[:10]
        lat_s, lon_s = str(s["latitude"]).rstrip("0"), str(s["longitude"]).rstrip("0")
        d["coord_decimals"] = min(len(lat_s.split(".")[1]) if "." in lat_s else 0, len(lon_s.split(".")[1]) if "." in lon_s else 0)
        d["nearest_pop_site"], d["nearest_pop_site_m"] = near[mid][1], round(near[mid][0])
        d["qc_possible_duplicate"] = near[mid][0] < 50
        d["qc_tj_code_repeated_on_map"] = code_count[c] > 1
        d["qc_coords_low_precision"] = d["coord_decimals"] < 4
        d["xrf_site"], d["xrf_site_dist_m"] = xrf_of.get(mid, (None, None))
        f = files[files.site_id == str(mid)]
        ph, dc = f[f.is_photo], f[~f.is_photo]
        d["n_photos"], d["n_docs"] = len(ph), len(dc)
        d["photo_urls"] = " ".join(ph.url.head(10))
        d["doc_names"] = " | ".join(dict.fromkeys(dc.filename.str.strip()))[:500]
        d["doc_urls"] = " ".join(dc.url)
        d.update(lab_sum.get(mid, lab_sum.get(str(mid), {"lab_n_samples": 0})))
        d.update(fsum.get(mid, {}))
        d["text_all"] = re.sub(r"\s+", " ", text_of(s)).strip()
        rows.append(d)
    # участки, которые есть в протоколе №126, но отсутствуют на карте
    off = pd.read_csv(ROOT / "manual" / "protocol126_places_offmap.csv", dtype=str).fillna("")
    linked = pd.read_csv(ROOT / "manual" / "protocol126_map_links.csv", dtype=str).fillna("")
    done = set(linked[linked.match == "уверенно"].protocol_code)
    for _, o in off[~off.tj_code.isin(done)].iterrows():
        d = {"tj_code": o.tj_code, "site_name": o.place_name, "site_name_full": o.place_name, "map_id": None,
             "pollution_status": "нет на карте chemicals.tj", "on_map": False,
             "latitude": None, "longitude": None,
             "notes": f"Участок есть только в протоколе АФИ №126 (строка {o.protocol_row}); координат нет"}
        d.update(lab_sum.get("TJ:" + o.tj_code, {"lab_n_samples": 0}))
        rows.append(d)
    Sites = pd.DataFrame(rows)
    Sites["on_map"] = Sites["on_map"].map(lambda v: v is not False)
    first = ["tj_code", "site_name", "map_id", "on_map", "pollution_status", "region", "site_date", "latitude", "longitude",
             "lab_n_samples", "lab_n_samples_with_own_coords", "lab_sources", "lab_years", "ddt_max", "ddd_max", "dde_max",
             "ddt_group_sum_max", "ddt_x_norm_max", "n_samples_ddt_gt_norm", "hch_sum_max", "g_hch_max", "aldrin_max",
             "dieldrin_max", "heptachlor_max", "endosulfan_max", "pesticides_total_max", "substances_detected"]
    text_cols = sorted([c for c in Sites.columns if c.endswith("_text") or c.startswith("mentions_") or c == "text_has_chatgpt_artifact"])
    form = ["square", "depth", "pit_dug", "smell", "land", "source", "status", "neformal", "iss", "issue_type",
            "lived_on_site", "lived_50m", "lived_100m", "lived_500m", "worked_on_site", "worked_50m", "worked_100m", "worked_500m",
            "visited_on_site", "visited_50m", "visited_100m", "visited_500m", "animals_food", "agriculture_near", "access",
            "soil_cover", "chemical_storage", "water", "surface_water", "surface_water_usage", "river_distance", "flood_zone",
            "groundwaters", "groundwaters_using", "groundwater_signs", "groundwater_depth", "another_water", "well_distance",
            "well_direction", "slope_position", "health_effects", "journalists_count", "males", "females"]
    qc = ["xrf_site", "xrf_site_dist_m", "nearest_pop_site", "nearest_pop_site_m", "qc_possible_duplicate",
          "qc_tj_code_repeated_on_map", "qc_coords_low_precision", "coord_decimals"]
    fl = ["n_photos", "n_docs", "doc_names", "photo_urls", "doc_urls", "card_url"]
    last = ["problem_description", "site_description", "impact", "notes", "site_name_full", "country", "pollution_status_id",
            "created_at", "updated_at", "text_all"]
    order = [c for c in first + text_cols + form + qc + fl + last if c in Sites.columns]
    Sites = Sites[order + [c for c in Sites.columns if c not in order]]

    # ---- файлы и источники ----
    files_out = files[["site_id", "tj_code", "file_id", "filename", "file_type", "mime_type", "file_size", "url", "local_doc_file", "is_photo"]]
    src = (allm.groupby(["source_id", "lab", "report_ref"])
           .agg(n_values=("substance", "size"), n_sites=("tj_code", lambda x: len(set(x) - {""})),
                rank_1_2=("source_rank", lambda x: int((x > 0).sum()))).reset_index())

    # ---- запись ----
    gS = gpd.GeoDataFrame(Sites, geometry=[Point(float(a), float(b)) if a not in (None, "") and not (isinstance(a, float) and math.isnan(a)) else None
                                           for a, b in zip(Sites.longitude, Sites.latitude)], crs=4326)
    geom = [Point(float(r.lon), float(r.lat)) if r.lat not in ("", None) else None for r in S.itertuples()]
    gP = gpd.GeoDataFrame(S, geometry=geom, crs=4326)
    # GeoPackage = SQLite: пишем во временный каталог на локальном диске (на сетевых и
    # смонтированных папках SQLite может не создать служебные таблицы), затем копируем
    import shutil
    import tempfile
    final_gpkg = OUT / "pop_layer.gpkg"
    tmpdir = Path(tempfile.mkdtemp())
    gpkg = tmpdir / "pop_layer.gpkg"
    for col in gS.columns:
        if gS[col].dtype == object:
            gS[col] = gS[col].map(lambda v: None if v is None or (isinstance(v, float) and math.isnan(v)) else (v if isinstance(v, (str, bool, int, float)) else str(v)))
    gS.to_file(gpkg, layer="pop_sites", driver="GPKG", engine="pyogrio")
    gP.to_file(gpkg, layer="pop_samples", driver="GPKG", engine="pyogrio")
    import pyogrio
    for name, df in [("pop_measurements", allm), ("pop_text_facts", F), ("pop_files", files_out), ("pop_sources", src)]:
        df2 = df.copy()
        for col in df2.columns:
            if df2[col].dtype == object:
                df2[col] = df2[col].map(lambda v: None if v is None or (isinstance(v, float) and math.isnan(v)) else str(v))
        pyogrio.write_dataframe(df2, gpkg, layer=name, driver="GPKG", append=False)
    shutil.copyfile(gpkg, final_gpkg)
    Sites.drop(columns=["text_all"]).to_csv(OUT / "pop_sites.csv", index=False, encoding="utf-8-sig")
    S.to_csv(OUT / "pop_samples.csv", index=False, encoding="utf-8-sig")
    files_out.to_csv(OUT / "pop_files.csv", index=False, encoding="utf-8-sig")
    src.to_csv(OUT / "pop_sources.csv", index=False, encoding="utf-8-sig")
    print(f"участков: {len(Sites)}, проб: {len(S)} (с собственными координатами: "
          f"{int((~S.coord_source.str.startswith('точка участка')).sum())}), значений: {len(allm)}, "
          f"фактов из текста: {len(F)}")


if __name__ == "__main__":
    main()
