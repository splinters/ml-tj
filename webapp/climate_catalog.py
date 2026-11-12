"""
Каталог климатических слоёв WorldClim 2.1 (обрезка по Таджикистану).

Единый источник названий для двух мест:
  - geoserver/publish_rasters.py записывает из него title/abstract слоёв GeoServer;
  - webapp/climate.py отдаёт его странице «Климат» (фильтры, подписи).

Имя файла -> слой. Допустимые формы стема:
  past:    wc2.1_30s_bio_<1..19>, wc2.1_30s_tavg_<01..12>, wc2.1_30s_prec_<01..12>
  future:  wc2.1_30s_bioc_<GCM>_<sspXXX>_<YYYY-YYYY>_bio<01..19>
Имя слоя в GeoServer - стем с заменой символов вне [A-Za-z0-9_-] на "_"
(wc2.1_... -> wc2_1_...), как в publish_rasters.py.
"""
import re

MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль",
          "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]

# номер BIO -> (название, единицы, тема)
BIO = {
    1: ("Среднегодовая температура", "°C", "temp"),
    2: ("Средний суточный размах температуры", "°C", "temp"),
    3: ("Изотермальность (BIO2/BIO7)", "%", "temp"),
    4: ("Сезонность температуры", "СО × 100", "temp"),
    5: ("Максимальная температура самого тёплого месяца", "°C", "temp"),
    6: ("Минимальная температура самого холодного месяца", "°C", "temp"),
    7: ("Годовой размах температуры (BIO5 − BIO6)", "°C", "temp"),
    8: ("Средняя температура самого влажного квартала", "°C", "temp"),
    9: ("Средняя температура самого сухого квартала", "°C", "temp"),
    10: ("Средняя температура самого тёплого квартала", "°C", "temp"),
    11: ("Средняя температура самого холодного квартала", "°C", "temp"),
    12: ("Годовая сумма осадков", "мм", "precip"),
    13: ("Осадки самого влажного месяца", "мм", "precip"),
    14: ("Осадки самого сухого месяца", "мм", "precip"),
    15: ("Сезонность осадков (коэффициент вариации)", "%", "precip"),
    16: ("Осадки самого влажного квартала", "мм", "precip"),
    17: ("Осадки самого сухого квартала", "мм", "precip"),
    18: ("Осадки самого тёплого квартала", "мм", "precip"),
    19: ("Осадки самого холодного квартала", "мм", "precip"),
}

SCENARIOS = {
    "hist": "норма 1970–2000",
    "ssp245": "SSP2-4.5 (промежуточный сценарий)",
    "ssp585": "SSP5-8.5 (высокие выбросы)",
}
SCENARIO_SHORT = {"hist": "норма", "ssp245": "SSP2-4.5", "ssp585": "SSP5-8.5"}

# английские подписи (интерфейс стенда на английском)
MONTHS_EN = ["January", "February", "March", "April", "May", "June", "July",
             "August", "September", "October", "November", "December"]
BIO_EN = {
    1: "Annual mean temperature", 2: "Mean diurnal temperature range", 3: "Isothermality (BIO2/BIO7)",
    4: "Temperature seasonality", 5: "Max temperature of the warmest month", 6: "Min temperature of the coldest month",
    7: "Annual temperature range (BIO5 − BIO6)", 8: "Mean temperature of the wettest quarter",
    9: "Mean temperature of the driest quarter", 10: "Mean temperature of the warmest quarter",
    11: "Mean temperature of the coldest quarter", 12: "Annual precipitation", 13: "Precipitation of the wettest month",
    14: "Precipitation of the driest month", 15: "Precipitation seasonality (coefficient of variation)",
    16: "Precipitation of the wettest quarter", 17: "Precipitation of the driest quarter",
    18: "Precipitation of the warmest quarter", 19: "Precipitation of the coldest quarter",
}
UNIT_EN = {"мм": "mm", "СО × 100": "SD × 100"}
SCENARIOS_EN = {
    "hist": "1970–2000 normal",
    "ssp245": "SSP2-4.5 (intermediate scenario)",
    "ssp585": "SSP5-8.5 (high emissions)",
}
SCENARIO_SHORT_EN = {"hist": "normal", "ssp245": "SSP2-4.5", "ssp585": "SSP5-8.5"}
SOURCE_EN = ("Source: WorldClim 2.1 (Fick, Hijmans, 2017), 30 arc-second resolution (about 1 km), "
             "clipped to Tajikistan.")

SOURCE = ("Источник: WorldClim 2.1 (Fick, Hijmans, 2017), разрешение 30 угловых секунд (около 1 км), "
          "обрезано по территории Таджикистана.")

# Население WorldPop R2025A (слои собирает geoserver/build_population_layers.py).
# имя слоя -> (название RU, название EN, описание RU, описание EN)
POP = {
    "children_u18_2025": ("Дети до 18 лет", "Children under 18",
                          "Дети 0–17 лет.", "Children aged 0–17."),
    "children_u5_2025": ("Дети до 5 лет", "Children under 5",
                         "Дети 0–4 года: самая уязвимая к загрязнению почвы группа (контакт рук с землёй и пылью).",
                         "Children aged 0–4: the group most exposed to soil contamination (hand-to-mouth contact with soil and dust)."),
    "children_5_17_2025": ("Дети 5–17 лет", "Children aged 5–17",
                           "Дети школьного возраста: дети до 18 лет минус дети до 5 лет.",
                           "School-age children: children under 18 minus children under 5."),
    "population_2025": ("Всё население", "Total population",
                        "Всё население, женщины и мужчины всех возрастов.", "Total population, women and men of all ages."),
    "women_15_49_2025": ("Женщины 15–49 лет", "Women aged 15–49",
                         "Женщины детородного возраста: воздействие СОЗ при беременности и через грудное молоко.",
                         "Women of reproductive age: POP exposure during pregnancy and through breast milk."),
}
POP_UNIT, POP_UNIT_EN = "чел. на пиксель ~100 м", "people per ~100 m pixel"
POP_SOURCE = ("Источник: WorldPop R2025A, оценка на 2025 год (University of Southampton), сетка 3 угловые секунды "
              "(около 100 м). Модельная оценка, а не перепись.")
POP_SOURCE_EN = ("Source: WorldPop R2025A, 2025 estimate (University of Southampton), 3 arc-second grid (about 100 m). "
                 "A model estimate, not a census count.")


def _parse_pop(stem):
    if stem not in POP:
        return None
    ru, _, desc, _ = POP[stem]
    return dict(dataset="population", scenario="pop", period="2025", gcm=None, group="pop", var=stem,
                var_title=f"{ru}, 2025", unit=POP_UNIT, theme="pop", month=None, short=ru, stem=stem,
                name=layer_name(stem), title=f"{ru}, 2025 · {POP_UNIT}",
                abstract=f"{desc} Единицы: {POP_UNIT}. {POP_SOURCE}", source="WorldPop R2025A")


_PAST = re.compile(r"^wc2[._]1_30s_(bio|tavg|prec)_(\d{1,2})$")
_FUT = re.compile(r"^wc2[._]1_30s_bioc_([A-Za-z0-9\-]+)_(ssp\d{3})_(\d{4})-(\d{4})_bio(\d{1,2})$")


def layer_name(stem):
    n = re.sub(r"[^A-Za-z0-9_\-]", "_", stem)
    return n if re.match(r"[A-Za-z_]", n) else "r_" + n


def _period_label(rec):
    if rec["dataset"] == "past":
        return "норма 1970–2000"
    return f"прогноз {rec['period']}, {SCENARIO_SHORT[rec['scenario']]}"


def parse(stem):
    """Разбор стема/имени слоя -> dict или None, если имя не из этой серии."""
    pop = _parse_pop(stem)
    if pop:
        return pop
    m = _PAST.match(stem)
    if m:
        grp, num = m.group(1), int(m.group(2))
        rec = dict(dataset="past", scenario="hist", period="1970–2000", gcm=None, group=grp)
        if grp == "bio":
            if num not in BIO:
                return None
            name, unit, theme = BIO[num]
            rec.update(var=f"bio{num}", var_title=f"{name} (BIO{num})", unit=unit, theme=theme,
                       month=None, short=name)
        else:
            if not 1 <= num <= 12:
                return None
            mon = MONTHS[num - 1]
            if grp == "tavg":
                name, unit, theme = f"Средняя температура воздуха, {mon}", "°C", "temp"
            else:
                name, unit, theme = f"Осадки, {mon}", "мм", "precip"
            rec.update(var=f"{grp}{num:02d}", var_title=name, unit=unit, theme=theme, month=num, short=name)
    else:
        m = _FUT.match(stem)
        if not m:
            return None
        gcm, ssp, y1, y2, num = m.group(1), m.group(2), m.group(3), m.group(4), int(m.group(5))
        if num not in BIO or ssp not in SCENARIOS:
            return None
        name, unit, theme = BIO[num]
        rec = dict(dataset="future", scenario=ssp, period=f"{y1}–{y2}", gcm=gcm, group="bio",
                   var=f"bio{num}", var_title=f"{name} (BIO{num})", unit=unit, theme=theme,
                   month=None, short=name)
    rec["stem"] = stem
    rec["name"] = layer_name(stem)
    rec["title"] = f"{rec['var_title']}, {rec['unit']} · {_period_label(rec)}"
    rec["abstract"] = abstract(rec)
    rec["source"] = "WorldClim 2.1"
    return rec


def abstract(rec):
    parts = [rec["var_title"] + ".", f"Единицы: {rec['unit']}."]
    if rec["dataset"] == "past":
        parts.append("Климатическая норма за 1970–2000 гг. (среднее по историческим наблюдениям).")
    else:
        parts.append(f"Прогноз на {rec['period']} гг., сценарий {SCENARIOS[rec['scenario']]}, "
                     f"климатическая модель {rec['gcm']} (CMIP6), масштабированная до 30 угловых секунд.")
    parts.append(SOURCE)
    return " ".join(parts)


def all_layers(gcm="IPSL-CM6A-LR"):
    """Полный набор слоёв серии: 19 BIO + 12 tavg + 12 prec (норма) и 2 сценария x 4 периода x 19 BIO."""
    recs = []
    for n in range(1, 20):
        recs.append(parse(f"wc2.1_30s_bio_{n}"))
    for g in ("tavg", "prec"):
        for n in range(1, 13):
            recs.append(parse(f"wc2.1_30s_{g}_{n:02d}"))
    for ssp in ("ssp245", "ssp585"):
        for per in ("2021-2040", "2041-2060", "2061-2080", "2081-2100"):
            for n in range(1, 20):
                recs.append(parse(f"wc2.1_30s_bioc_{gcm}_{ssp}_{per}_bio{n:02d}"))
    recs += [parse(k) for k in POP]
    return recs


def localize_en(rec):
    """Копия записи слоя с английскими подписями (имя слоя и коды не меняются)."""
    r = dict(rec)
    if rec["group"] == "pop":
        _, en, _, desc = POP[rec["var"]]
        r.update(var_title=f"{en}, 2025", short=en, unit=POP_UNIT_EN, title=f"{en}, 2025 · {POP_UNIT_EN}",
                 abstract=f"{desc} Units: {POP_UNIT_EN}. {POP_SOURCE_EN}")
        return r
    unit = UNIT_EN.get(rec["unit"], rec["unit"])
    if rec["group"] == "bio":
        num = int(rec["var"][3:])
        name = BIO_EN[num]
        r["var_title"] = f"{name} (BIO{num})"
    else:
        mon = MONTHS_EN[rec["month"] - 1]
        name = f"Mean air temperature, {mon}" if rec["group"] == "tavg" else f"Precipitation, {mon}"
        r["var_title"] = name
    r["short"], r["unit"] = name, unit
    per = "1970–2000 normal" if rec["dataset"] == "past" else f"projection {rec['period']}, {SCENARIO_SHORT_EN[rec['scenario']]}"
    r["title"] = f"{r['var_title']}, {unit} · {per}"
    parts = [r["var_title"] + ".", f"Units: {unit}."]
    if rec["dataset"] == "past":
        parts.append("Climate normal for 1970–2000 (mean of historical observations).")
    else:
        parts.append(f"Projection for {rec['period']}, scenario {SCENARIOS_EN[rec['scenario']]}, "
                     f"climate model {rec['gcm']} (CMIP6), downscaled to 30 arc-seconds.")
    parts.append(SOURCE_EN)
    r["abstract"] = " ".join(parts)
    return r
