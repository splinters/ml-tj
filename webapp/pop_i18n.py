"""
Английская версия данных темы «СОЗ»: словари значений и перевод ответов API.

Ключи классов (низкий/средний/высокий/очень высокий) и значения анкеты остаются русскими:
по ним работают модель и раскраска карты. Переводятся только подписи для показа.
Свободный текст карточек chemicals.tj (описания, фрагменты-основания) показывается в оригинале.
"""
import i18n
from i18n import lang, name_en, value_en

SUBST_EN = {"ДДТ": "DDT", "ДДД": "DDD", "ДДЕ": "DDE", "α-ГХЦГ": "α-HCH", "β-ГХЦГ": "β-HCH",
            "γ-ГХЦГ (линдан)": "γ-HCH (lindane)", "δ-ГХЦГ": "δ-HCH", "ГХЦГ α+β": "HCH α+β",
            "ГХЦГ (изомер не указан)": "HCH (isomer not specified)", "гептахлор": "heptachlor",
            "гептахлор-эпоксид": "heptachlor epoxide", "альдрин": "aldrin", "дильдрин": "dieldrin", "эндрин": "endrin",
            "эндрин-альдегид": "endrin aldehyde", "эндосульфан I": "endosulfan I", "эндосульфан II": "endosulfan II",
            "эндосульфан-сульфат": "endosulfan sulfate", "метоксихлор": "methoxychlor",
            "пестициды (сумма)": "pesticides (total)", "загрязнитель 1 (не указан)": "contaminant 1 (not specified)",
            "загрязнитель 2 (не указан)": "contaminant 2 (not specified)"}

FACT_EN = {"Тип объекта": "Site type", "Состояние": "Condition", "Использование": "Use",
           "Пестицид в тексте": "Pesticide named in text", "До жилья, м": "To housing, m", "До воды, м": "To water, m",
           "До школы/детсада, м": "To school/kindergarten, m", "До дороги, м": "To road, m", "До посевов, м": "To crops, m",
           "Людей (упоминание)": "People (mentioned)", "Детей (упоминание)": "Children (mentioned)",
           "Пестицидов, кг (упоминание)": "Pesticides, kg (mentioned)",
           "Дома на участке или рядом": "Houses on or near the site", "Жалобы на здоровье": "Health complaints",
           "Гибель животных": "Animal deaths", "Питьевая вода": "Drinking water", "Захоронение": "Burial",
           "Мешки/тара": "Bags/containers", "Ремедиация": "Remediation"}

POP_VALUES = {
    # регионы и статусы
    "РРП": "Districts of Republican Subordination", "Согдийская область": "Sughd region",
    "Хатлонская область": "Khatlon region",
    # источники лабораторных данных
    "ФГБНУ АФИ, протокол №126 (2019)": "Agrophysical Research Institute (AFI), protocol No. 126 (2019)",
    "ФГБНУ АФИ, протокол №8 (отбор 2020)": "Agrophysical Research Institute (AFI), protocol No. 8 (sampled 2020)",
    "НПО ИЛИМ, отчёт №42 (отбор 2016)": "ILIM research association, report No. 42 (sampled 2016)",
    "Институт химии им. Никитина (2012)": "Nikitin Institute of Chemistry (2012)",
    "Институт химии им. Никитина (2017)": "Nikitin Institute of Chemistry (2017)",
    "журнал отбора проб": "sampling log",
    # откуда координаты пробы
    "журнал 2016 (номер пробы)": "2016 log (sample number)",
    "точка участка (координат пробы нет)": "site point (no sample coordinates)",
    "реестр проб 2020 (порядок повторностей)": "2020 sample register (replicate order)",
    "документ": "document", "журнал проб (совпадение ДДТ)": "sample log (DDT value match)",
    # сведения из текста карточек
    "аэродром/авиаплощадка": "airfield/airstrip", "захоронение": "burial site", "полигон захоронения": "disposal site",
    "склад": "warehouse", "используется": "in use", "полностью разрушен/не сохранился": "completely destroyed / gone",
    "полуразрушен": "partly destroyed", "сохранился": "intact", "воинская часть": "military unit",
    "жилая застройка": "housing", "хлев/содержание скота": "barn / livestock",
    "хранение (дрова, уголь, зерно и т.п.)": "storage (firewood, coal, grain etc.)",
    "ГХЦГ/линдан": "HCH/lindane", "бутифос": "butifos", "гептахлор": "heptachlor", "гербициды": "herbicides",
    "дефолианты": "defoliants", "дильдрин": "dieldrin", "минеральные удобрения": "mineral fertilisers",
    "эндосульфан/тиодан": "endosulfan/thiodan", "альдрин": "aldrin", "ДДТ": "DDT",
    # анкета chemicals.tj
    "1 гектар - 5 гектар": "1–5 ha", ">5 гектаров": ">5 ha", "500 - 1000 м²": "500–1000 m²", "100 - 500 м²": "100–500 m²",
    "<100 м²": "<100 m²", "1000 - 5000 м²": "1000–5000 m²", "5000 - 10000 м² (1 гектар)": "5000–10,000 m² (1 ha)",
    "Контролируемый доступ; вход затруднен": "Controlled access; entry difficult",
    "Легкий доступ; малое расстояние для входа": "Easy access; short distance to enter",
    "Удаленные места; менее доступен": "Remote location; less accessible",
    "Умеренный доступ; вход более затруднен": "Moderate access; entry more difficult",
    "Площадка покрыта бетонной плиткой или другим видом инженерного покрытия":
        "Site covered with concrete slabs or other engineered surface",
    "Загрязненная территория голая": "Contaminated area is bare",
    "Имеется полный травяной покров и другая растительность": "Full grass cover and other vegetation",
    "Имеется редкий травяной чехол": "Sparse grass cover",
    "Участок покрыт бетоном или имеет другой вид инженерного сооружения в очень плохом состоянии":
        "Covered with concrete or another engineered structure in very poor condition",
    "Сельское хозяйство": "Agriculture", "Другое": "Other", "Промышленная зона": "Industrial zone",
    "Жилой район": "Residential area", "Рекреационная зона": "Recreational area",
    "Устаревший": "Obsolete", "Активный": "Active",
    "В пределах 10 м от загрязненной зоны": "Within 10 m of the contaminated area",
    "В пределах 100 м от загрязненной зоны": "Within 100 m of the contaminated area",
    "На загрязненной территории выращиваются сельскохозяйственные культуры": "Crops are grown on the contaminated area",
    "В радиусе 100 м не выращивается ни один урожай": "No crops grown within 100 m",
    "Доступно для случайных животных, употребляющих пищу": "Accessible to stray food animals",
    "Продукты питания и продукты, пригодные в пищу, в радиусе 100 м": "Food and edible produce within 100 m",
    "Пищевые животные/рыбы на территории": "Food animals / fish on the site",
    "Малая река / Ручей": "Small river / stream", "Пруд": "Pond", "Большая река": "Large river", "Эстуарии": "Estuary",
    "Водно-болотные угодья": "Wetland", "Малое озеро": "Small lake",
    "Орошение": "Irrigation", "Питье": "Drinking", "Рыбалка": "Fishing", "Не используется": "Not used",
    "от 0 до 300 м": "0–300 m", "> 5 км": "> 5 km", "от 1 км до 5 км": "1–5 km", "от 300 м до 1 км": "300 m – 1 km",
    "Неизвестно": "Unknown", "Питьевой": "Drinking", "Домашний скот": "Livestock",
    "Очень глубоко >50 м": "Very deep >50 m", "Средняя 2 - 10 м": "Medium 2–10 m", "Мелководье <2 м": "Shallow <2 m",
    "Глубоко 10 - 50 м": "Deep 10–50 m",
    "Поблизости нет колодца": "No well nearby", "В пределах 500 м от загряздения": "Within 500 m of the contamination",
    "В пределах 100 м от загрязнения": "Within 100 m of the contamination",
    "В пределах 50 м от загрязнения": "Within 50 m of the contamination",
    "На уровне земли или ниже, плоский склон": "At or below ground level, flat slope",
    "Над уровне земли и с ровным уклоном": "Above ground level, even slope",
    "Над уровнем земли и средний уклон": "Above ground level, moderate slope",
    "На уровне земли или ниже, средний уклон": "At or below ground level, moderate slope",
    "Над уровнем земли и крутой склон": "Above ground level, steep slope",
    "На уровне земли и ниже, крутой склон": "At or below ground level, steep slope",
    # варианты моделей (метрики)
    "A. Анкета": "A. Survey form", "B. Анкета + текст карточки": "B. Survey form + card text",
    "C. Анкета + текст + регион/координаты": "C. Survey form + text + region/coordinates",
    "D. C + соседние обследованные участки": "D. C + neighbouring sampled sites",
    "E. Только регион/координаты + соседи": "E. Region/coordinates + neighbours only",
    "F. Открытые ковариаты (pH, рельеф, население, сток, климат)":
        "F. Open covariates (pH, terrain, population, flow, climate)",
    "G. Анкета + регион/координаты + соседи + ковариаты": "G. Survey form + region/coordinates + neighbours + covariates",
    "Логистическая регрессия": "Logistic regression", "Случайный лес": "Random forest",
    "уверенно": "confident", "неоднозначно": "ambiguous", "предположительно": "probable",
}
i18n.VALUES.update(POP_VALUES)

EN = lambda: lang() == "en"  # noqa: E731


def _list(s, sep="; "):
    if not s:
        return s
    return sep.join(value_en(x.strip()) for x in str(s).split(sep.strip()) if x.strip())


def _subst(s):
    return ", ".join(SUBST_EN.get(x.strip(), x.strip()) for x in str(s).split(",")) if s else s


def sites(d):
    """Подписи для показа: label, region_label, *_label; ключи остаются прежними."""
    for f in d["features"]:
        p = f["properties"]
        p["label"] = name_en(p.get("site_name")) if EN() else p.get("site_name")
        p["region_label"] = value_en(p.get("region")) if EN() else p.get("region")
        if EN():
            for k in ("pollution_status", "site_kind_text", "building_state_text", "score_type"):
                if p.get(k):
                    p[k] = value_en(p[k])
            p["pathways_list"] = _list(p.get("pathways_list"))
            p["hazard_label"] = value_en(p.get("hazard_source"))
            p["substances_detected"] = _subst(p.get("substances_detected"))
    if EN():
        for r in d.get("off_map", []):
            r["site_name"] = name_en(r.get("site_name"))
    return d


def samples(d):
    if EN():
        for f in d["features"]:
            p = f["properties"]
            p["source"], p["coord_source"] = value_en(p.get("source")), value_en(p.get("coord_source"))
    return d


def card(c):
    c["site_name_ru"] = c["site_name"]
    if not EN():
        return c
    c["site_name"] = name_en(c["site_name"])
    c["region"], c["status"] = value_en(c["region"]), value_en(c["status"])
    r = c.get("risk")
    if r:
        r["pathways_list"] = _list(r.get("pathways_list"))
        r["hazard_label"] = value_en(r.get("hazard_source"))
    s = c.get("screening")
    if s and s.get("score_type"):
        s["score_type"] = value_en(s["score_type"])
    for x in c["samples"]:
        x["source"], x["coord_source"] = value_en(x["source"]), value_en(x["coord_source"])
        x["values"] = {SUBST_EN.get(k, k): ("n.d." if v == "не обн." else v) for k, v in x["values"].items()}
    for f in c["facts"]:
        f["field"] = FACT_EN.get(f["field"], f["field"])
        f["value"] = value_en(f["value"])
    c["form"] = {k: value_en(v) if isinstance(v, str) else v for k, v in c["form"].items()}
    return c


def form_options(opts):
    """[[значение, подпись], ...]: значение уходит в модель как есть, подпись - на языке страницы."""
    return {k: [[v, value_en(v) if EN() else v] for v in vals] for k, vals in opts.items()}


def prediction(j):
    if not EN():
        return j
    j["pathways"] = [value_en(x) for x in j["pathways"]]
    j["region_used"] = value_en(j["region_used"])
    j["nearest_site"]["site_name"] = name_en(j["nearest_site"]["site_name"])
    return j


ERRORS_EN = {"Точка вне Таджикистана": "The point is outside Tajikistan"}


def plan_rows(rows):
    if EN():
        for r in rows:
            r["site_name"], r["region"] = name_en(r["site_name"]), value_en(r["region"])
    return rows


def metric_rows(rows):
    if EN():
        for r in rows:
            r["features"], r["model"] = value_en(r["features"]), value_en(r["model"])
    return rows
