"""Двуязычный интерфейс стенда (RU / EN) без внешних библиотек.

Язык: параметр ?lang=ru|en (запоминается в cookie на год), затем cookie, затем
переменная окружения DEMO_LANG (по умолчанию ru).

В шаблонах:  {{ T('Русский', 'English') }}, {{ lang }}, {{ lang_url('en') }},
             {{ name|nm }} - название (склад, участок, район) на языке интерфейса,
             {{ value|vl }} - известные значения (классы, статусы, регионы...) на языке интерфейса.
В Python:    from i18n import tr, nm, vl, lang
В JS:        TL('рус', 'eng') - функция из _nav.html (имя L занято Leaflet)
"""
import os
import re
import unicodedata

from flask import g, request

LANGS = ("ru", "en")
COOKIE = "demo_lang"


def lang():
    try:
        return g.get("lang") or "ru"
    except RuntimeError:  # вне запроса
        return os.getenv("DEMO_LANG", "ru")


def tr(ru, en=None):
    """tr('рус', 'eng') или tr(('рус', 'eng')) -> строка на языке интерфейса."""
    if en is None and isinstance(ru, (tuple, list)) and len(ru) == 2:
        ru, en = ru
    if en is None:
        return ru
    return en if lang() == "en" else ru


# --------------------------------------------------------------------------
# Названия: словарь известных имён + фразы + транслитерация остального
# --------------------------------------------------------------------------
NAMES = {
    # склады XRF
    "Вахш": "Vakhsh", "Гиссар": "Hisor", "Дж. Балхи": "J. Balkhi", "Джамийский район": "Jami district",
    "Дусти": "Dusti", "Ёвон": "Yovon", "Кушониен": "Kushoniyon", "Кушониён": "Kushoniyon", "Пяндж": "Panj",
    "Турсунзаде": "Tursunzoda", "Шахринав": "Shahrinav",
    # регионы
    "Хатлонская область": "Khatlon region", "Согдийская область": "Sughd region",
    "РРП": "Districts of Republican Subordination", "ГБАО": "GBAO", "Душанбе": "Dushanbe",
}
DISTRICTS = {"Вахш": "Vakhsh", "Кумсангир": "Kumsangir", "Пяндж": "Panj", "Пандж": "Panj", "Канибадам": "Konibodom",
             "Матчин": "Mastchoh", "Гафуров": "Ghafurov", "Спитамен": "Spitamen", "Ашт": "Asht", "Фархор": "Farkhor",
             "Хуросон": "Khuroson", "Джамий": "Jami", "Куляб": "Kulob", "Яван": "Yovon", "Бохтар": "Bokhtar",
             "Шахритус": "Shahritus", "Хамадонийс": "Hamadoni", "Джиликул": "Jilikul", "Дусти": "Dusti"}
TOWNS = {"Хамадони": "Hamadoni", "Яван": "Yovon", "Джами": "Jami", "Турсунзаде": "Tursunzoda", "Куляб": "Kulob",
         "Пяндж": "Panj", "Пандж": "Panj", "Ховалинг": "Hovaling", "Мастчо": "Mastchoh", "Фархор": "Farkhor",
         "Джиликуль": "Jilikul", "Кумсангир": "Kumsangir", "Шахритус": "Shahritus", "Бохтар": "Bokhtar", "Вахдат": "Vahdat"}
PHRASES = [  # порядок важен: длинные раньше коротких
    (r"[Бб]ывший склад устаревших пестицидов", "former obsolete pesticide warehouse"),
    (r"[Сс]клад устаревших пестицидов", "obsolete pesticide warehouse"),
    (r"[Бб]ывший склад пестицидов", "former pesticide warehouse"),
    (r"[Бб]ывший склад ядохимикатов", "former agrochemical warehouse"),
    (r"[Бб]ывший сельскохозяйственный аэропорт", "former agricultural airfield"),
    (r"[Бб]ывший сельхозаэродром", "former agricultural airfield"),
    (r"[Сс]ельскохозяйственн\w+ аэродром\w*", "agricultural airfield"),
    (r"[Сс]клад пестицидов", "pesticide warehouse"),
    (r"[Пп]естицидный склад", "pesticide warehouse"),
    (r"[Пп]олигон по захоронению стойких органических загрязнителей \(СОЗ\)", "POPs disposal site"),
    (r"[Пп]олигон захоронения пестицидов", "pesticide disposal site"),
    (r"[Пп]олигон по захоронению ядохимикатов", "agrochemical disposal site"),
    (r"[Пп]олигон (по )?захоронени[яю]", "disposal site"),
    (r"[Дд]ехканское хозяйство|[Дд]ехканского хозяйства", "dehkan farm"),
    (r"[Мм]есто захоронения пестицидов", "pesticide burial site"),
    (r"\bгараж\b", "garage"),
    (r"[Пп]олевой лагерь бригады", "brigade field camp"),
    (r"\b(\w+)ский район\b", lambda m: DISTRICTS.get(m.group(1), m.group(1)) + " district"),
    (r"\b(\w+)ского района\b", lambda m: DISTRICTS.get(m.group(1), m.group(1)) + " district"),
    (r"\b(\w+)ском районе\b", lambda m: DISTRICTS.get(m.group(1), m.group(1)) + " district"),
    (r"\b(\w+)ский\b(?= jamoat| джамоат)", lambda m: DISTRICTS.get(m.group(1), m.group(1))),
    (r"\(\s*[Бб]ывш\w*\s+", "(formerly "),
    (r"\b[Бб]ывш(ий|его|ая|ее|ей)\b", "former"),
    (r"\b[Кк]ишлак(а|е)?\b", "village"),
    (r"\b[Пп]ос[её]л(ок|ка|ке)\b", "settlement"),
    (r"\b[Сс]ел(о|а|е)\b", "village"),
    (r"\b[Дд]еревн(я|и|е)\b", "village"),
    (r"\b[Дд]жамоат(а|е)?\b", "jamoat"),
    (r"\b[Сс]овхоз(а|е)?\b", "sovkhoz"),
    (r"\b[Кк]олхоз(а|е)?\b", "kolkhoz"),
    (r"\b[Хх]озяйств(о|а|е)\b", "farm"),
    (r"\b[Фф]ерм(а|ы|е)\b", "farm"),
    (r"\b[Уу]лиц(а|ы|е)\b", "street"),
    (r"\b[Уу]часток\b", "site"),
    (r"\b[Цц]ентр\b", "centre"),
    (r"\bручья\b", "stream"),
    (r"\b[Аа]эродрома\b", "airfield"),
    (r"\bсельскохозяйственного\b", "agricultural"),
    (r"№\s*", "No. "),
    (r"\b(Хамадони|Яван|Джами|Турсунзаде|Куляб|Пяндж|Пандж|Ховалинг|Мастчо|Фархор|Джиликуль|Кумсангир|Шахритус|Бохтар|Вахдат)\b",
     lambda m: TOWNS[m.group(1)]),
    (r"Согдийская область|Согдийской области", "Sughd region"),
    (r"Хатлонская область|Хатлонской области", "Khatlon region"),
    (r"20 лет Независимости", "20 Years of Independence"),
    (r"\bООО\b", "LLC"),
    (r"\bобъединения\b", "association"),
    (r"\bдеревня\b|\bдеревни\b", "village"),
    (r"[Сс]клад ядохимикатов", "agrochemical warehouse"),
    (r"[Бб]ывший склад", "former warehouse"),
    (r"[Зз]ахоронение пестицидов", "pesticide burial site"),
    (r"[Зз]ахоронение", "burial site"),
    (r"[Пп]олигон по захоронению ядохимикатов", "agrochemical disposal site"),
    (r"[Пп]олигон СОЗ", "POPs disposal site"),
    (r"[Пп]олигон", "disposal site"),
    (r"[Хх]ранилище", "storage"),
    (r"[Сс]клад", "warehouse"),
    (r"[Сс]ельхозхимия", "Selkhozkhimiya (agrochemical depot)"),
    (r"[Аа]эродром", "airfield"),
    (r"[Аа]эропорт", "airport"),
    (r"\bдехканск\w+ хозяйств\w*", "dehkan farm"),
    (r"\bфермерск\w+ хозяйств\w*", "farm"),
    (r"\bхозяйства\b", "farm"),
    (r"\bфермы\b", "farm"),
    (r"\bсовхоза\b|\bсовхоз\b", "sovkhoz"),
    (r"\bколхоза\b|\bколхоз\b", "kolkhoz"),
    (r"\bимени\b|\bим\.", "named after"),
    (r"\bджамоата\b|\bджамоате\b|\bджамоат\b|\bд/ж\b", "jamoat"),
    (r"\bсела\b|\bселе\b|\bсело\b|\bс\.(?=\s)", "village"),
    (r"\bпоселка\b|\bпосёлка\b|\bпоселок\b|\bпосёлок\b|\bпос\.(?=\s)|\bп\.(?=\s)", "settlement"),
    (r"\bгорода\b|\bгород\b|\bг\.(?=\s)", "town"),
    (r"\bрайона\b|\bрайоне\b|\bрайон\b|\bр-н\b", "district"),
    (r"\bобласти\b|\bобласть\b", "region"),
    (r"\bВахшского\b|\bВахшский\b", "Vakhsh"),
    (r"\bКумсангирского\b|\bКумсангирский\b", "Kumsangir"),
    (r"\bПанджский\b|\bПанджского\b|\bПянджский\b|\bПянджского\b", "Panj"),
    (r"\bКанибадамский\b|\bКанибадамского\b", "Konibodom"),
    (r"\bМатчинский\b|\bМатчинского\b", "Mastchoh"),
    (r"\bвоинская часть\b", "military unit"),
    (r"\bрядом с\b|\bоколо\b", "near"),
    (r"\bи\b", "and"),
    (r"\bв\b", "in"),
    (r"\bна\b", "at"),
    (r"\bпестицидов\b", "pesticides"),
]
_TR = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "zh", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
    # таджикские буквы
    "ҳ": "h", "қ": "q", "ғ": "gh", "ҷ": "j", "ӣ": "i", "ӯ": "u",
}


def translit(s):
    out = []
    for i, ch in enumerate(s):
        lo = ch.lower()
        if lo in _TR:
            t = _TR[lo]
            # «е» в начале слова и после гласной -> ye
            if lo == "е" and (i == 0 or not s[i - 1].isalpha() or s[i - 1].lower() in "аеёиоуыэюяъь"):
                t = "ye"
            if ch.isupper() and t:
                t = t[0].upper() + t[1:]
            out.append(t)
        else:
            out.append(ch)
    return "".join(out)


_CYR = re.compile(r"[А-Яа-яЁёҲҳҚқҒғҶҷӢӣӮӯ]")


def name_en(s):
    """Название участка/склада/района на английском: словарь, затем фразы, затем транслитерация."""
    if s is None:
        return s
    s = unicodedata.normalize("NFC", str(s))  # имена из GeoPackage бывают в NFD (й = и + бреве)
    if s in NAMES:
        return NAMES[s]
    if not _CYR.search(s):
        return s
    t = s
    for pat, rep in PHRASES:
        t = re.sub(pat, rep, t)
    t = translit(t)
    t = t.replace("«", "“").replace("»", "”")
    t = re.sub(r"\s+", " ", t).strip()
    return t[:1].upper() + t[1:]


def nm(s):
    return name_en(s) if lang() == "en" else s


# --------------------------------------------------------------------------
# Известные значения (классы, статусы, пути воздействия, ответы анкеты...)
# --------------------------------------------------------------------------
VALUES = {
    # классы
    "низкий": "low", "средний": "medium", "высокий": "high", "очень высокий": "very high",
    "Низкий": "Low", "Средний": "Medium", "Высокий": "High", "Очень высокий": "Very high",
    # статусы участков
    "Загрязненный участок": "Contaminated site", "Загрязнённый участок": "Contaminated site",
    "Очищенный участок": "Cleaned-up site", "Очищенный": "Cleaned-up", "Загрязненный": "Contaminated",
    # источник опасности
    "анализы": "laboratory data", "прогноз скрининга (класс низкий)": "screening forecast (low class)",
    "прогноз скрининга (класс средний)": "screening forecast (medium class)",
    "прогноз скрининга (класс высокий)": "screening forecast (high class)",
    "проверка вне выборки (участок обследован)": "out-of-sample check (site sampled)",
    "прогноз (анализов нет)": "forecast (no laboratory data)",
    "≥100 мг/кг": "≥100 mg/kg", "<100 мг/кг": "<100 mg/kg",
    # пути воздействия
    "вода с участка или рядом используется для питья": "water on or near the site is used for drinking",
    "орошение из источников рядом": "irrigation from nearby sources",
    "доступ скота / пищевые животные на участке": "livestock / food animals on the site",
    "посевы на участке или в 10 м": "crops on the site or within 10 m",
    "лёгкий доступ людей": "easy access for people",
    "зона затопления или река ближе 300 м": "flood zone or river within 300 m",
    "грунтовые воды ближе 10 м": "groundwater shallower than 10 m",
    # группы плана отбора
    "Первичная оценка (нет данных о ДДТ/ГХЦГ)": "Initial assessment (no DDT/HCH data)",
    "Контроль очистки": "Clean-up verification",
    "Оконтуривание очага для ремедиации": "Delineating the hotspot for remediation",
    "Обновление данных 2012–2016 гг.": "Updating 2012–2016 data",
    # да/нет
    "да": "yes", "нет": "no", "Да": "Yes", "Нет": "No", "нет данных": "no data", "Не указан": "Not specified",
    # источники лабораторных данных
    "журнал/реестр проб": "sampling log / register",
    # журнал моделей металлов
    "первый запуск": "first run", "реальные pH/уклон/вода": "real pH/slope/water",
    "здания добавлены, floor вместо NULL": "buildings added, floor instead of NULL",
    "здания добавлены + osm, floor вместо NULL": "buildings added + OSM, floor instead of NULL",
    "здания убраны насовсем": "buildings removed for good", "v3: вода + только pH": "v3: water + pH only",
    "ТЗ п.3 - уточнение контура загрязнения по факту замеров (не ML-предсказание)":
        "ToR item 3: contamination outline refined from measurements (not an ML prediction)",
    "склад-обследование (XRF)": "warehouse survey (XRF)",
    # контекст использования территорий
    "Текущее землепользование": "Current land use", "Землепользование после рекультивации": "Land use after remediation",
    "Земля приватизирована": "Land privatized", "Кому передаётся после рекультивации": "Transferred after remediation to",
    "Жилая застройка в радиусе 500 м": "Housing within 500 m", "Расстояние до ближайшего жилья": "Distance to nearest housing",
    "Сельхозугодья в радиусе 500 м": "Farmland within 500 m", "Расстояние до ближайших сельхозугодий": "Distance to nearest farmland",
    "Расстояние до ближайшего канала или арыка": "Distance to nearest canal or ditch",
    "Промышленные объекты в радиусе 500 м": "Industrial facilities within 500 m", "м": "m",
    "Письмо заказчика о землепользовании": "Client letter on land use",
    # профили и источники нормативов
    "Те же международные источники для промышленного землепользования. Для Pb в справочнике нет значения для industrial, будет отмечено как пробел.":
        "The same international sources for industrial land use. The reference has no industrial value for Pb; it is marked as a gap.",
    "Международные ориентиры (по умолчанию): для каждого вещества берётся первый по порядку источник. Тип землепользования residential - предположение, пока заказчик не подтвердил назначение земель.":
        "International guideline values (default): for each substance the first source in order is used. Residential land use is an assumption until the client confirms the intended land use.",
    "Нормативы, названные заказчиком (ДДТ 0,1 мг/кг по N 6229-91), затем международные. Значения металлов из N 6229-91 пока со статусом secondary.":
        "Standards named by the client (DDT 0.1 mg/kg under No. 6229-91), then international ones. Metal values from No. 6229-91 still have secondary status.",
    "Перечень ПДК и ОДК химических веществ в почве N 6229-91": "List of MPC and APC of chemicals in soil, No. 6229-91 (USSR)",
    "ГН 2.1.7.020-94 ОДК тяжёлых металлов и мышьяка в почвах (Дополнение N 1 к перечню N 6229-91)":
        "GN 2.1.7.020-94: APC of heavy metals and arsenic in soils (Supplement 1 to list No. 6229-91)",
    "Неканцерогенный SL (THQ=1), industrial soil.": "Non-carcinogenic SL (THQ=1), industrial soil.",
    "ПДК для подвижной формы: с валовым XRF не сопоставим.": "MPC for the mobile form: not comparable with total-content XRF.",
    "Неорганическая ртуть, CCME 1999.": "Inorganic mercury, CCME 1999.",
    "ОДК валовое, pH(KCl) < 5,5.": "APC, total content, pH(KCl) < 5.5.",
    "p,p'-DDD, канцерогенный SL.": "p,p'-DDD, carcinogenic SL.",
    "ОДК валовое, суглинистые и глинистые, pH(KCl) > 5,5.": "APC, total content, loamy and clay soils, pH(KCl) > 5.5.",
    "p,p'-DDT, канцерогенный SL.": "p,p'-DDT, carcinogenic SL.",
    "Неканцерогенный SL (THQ=1), resident soil.": "Non-carcinogenic SL (THQ=1), resident soil.",
    "ОДК валовое, pH(KCl) > 5,5.": "APC, total content, pH(KCl) > 5.5.",
    "Неорганический мышьяк, CCME 1997; лимитирует здоровье человека (проглатывание почвы).":
        "Inorganic arsenic, CCME 1997; limited by human health (soil ingestion).",
    "ПДК суммарного содержания ДДТ и метаболитов; п. 38 раздела I перечня N 6229-91; то же значение в Национальном плане по Стокгольмской конвенции (2007, разд. 2.3.5).":
        "MPC for the total of DDT and metabolites; item 38 of section I of list No. 6229-91; the same value appears in the National Implementation Plan for the Stockholm Convention (2007, section 2.3.5).",
    "ПДК валовое; значение из вторичного источника, сверить с оригиналом.": "MPC, total content; value from a secondary source, to be checked against the original.",
    "Неорганическая ртуть, CCME 1999 (residential/parkland).": "Inorganic mercury, CCME 1999 (residential/parkland).",
    "Канцерогенный SL (TR=1E-06), industrial soil.": "Carcinogenic SL (TR=1E-06), industrial soil.",
    "ОДК валовое, песчаные и супесчаные почвы.": "APC, total content, sandy and sandy-loam soils.",
    "Канцерогенный SL (TR=1E-06), resident soil. Ниже природного фона многих почв: использовать как уровень внимания, не как критерий превышения.":
        "Carcinogenic SL (TR=1E-06), resident soil. Below natural background of many soils: use as an attention level, not as an exceedance criterion.",
    "Неорганический мышьяк, CCME 1997.": "Inorganic arsenic, CCME 1997.",
    "ОДК валовое, суглинистые и глинистые, pH(KCl) < 5,5.": "APC, total content, loamy and clay soils, pH(KCl) < 5.5.",
    "CCME 2018, общий норматив (контакт с почвой, экосистемы).": "CCME 2018, general guideline (soil contact, ecosystems).",
    "p,p'-DDE, канцерогенный SL.": "p,p'-DDE, carcinogenic SL.",
    "Скрининговый уровень для жилых почв (директива OLEM от 16.10.2025).": "Screening level for residential soils (OLEM directive of 16.10.2025).",
}


def value_en(s):
    if s is None:
        return s
    k = unicodedata.normalize("NFC", str(s))
    return VALUES.get(k, VALUES.get(k.strip(), s))


def vl(s):
    return value_en(s) if lang() == "en" else s


def vl_list(s, sep="; "):
    """Список значений через разделитель (например, пути воздействия)."""
    if s is None or lang() != "en":
        return s
    return sep.join(value_en(x.strip()) for x in str(s).split(sep.strip()) if x.strip())


# --------------------------------------------------------------------------
# Подключение к Flask
# --------------------------------------------------------------------------
def init_i18n(app):
    @app.before_request
    def _pick_lang():
        q = request.args.get("lang")
        if q in LANGS:
            g.lang, g.lang_set = q, True
        else:
            c = request.cookies.get(COOKIE)
            g.lang = c if c in LANGS else os.getenv("DEMO_LANG", "ru")

    @app.after_request
    def _remember_lang(resp):
        if g.get("lang_set"):
            resp.set_cookie(COOKIE, g.lang, max_age=365 * 24 * 3600, samesite="Lax")
        resp.headers.setdefault("Content-Language", g.get("lang", "ru"))
        return resp

    def lang_url(code):
        args = request.args.to_dict()
        args["lang"] = code
        from urllib.parse import urlencode
        return request.script_root + request.path + "?" + urlencode(args)

    @app.context_processor
    def _ctx():
        return {"lang": lang(), "T": tr, "lang_url": lang_url, "tpl": tpl}

    app.add_template_filter(nm, "nm")
    app.add_template_filter(vl, "vl")
    app.add_template_filter(vl_list, "vl_list")


def tpl(name):
    """Имя шаблона на языке интерфейса: для EN берётся name.en.html, если такой файл есть."""
    if lang() == "en" and name.endswith(".html"):
        from flask import current_app
        cand = name[:-5] + ".en.html"
        try:
            current_app.jinja_env.get_template(cand)
            return cand
        except Exception:
            pass
    return name


def render(name, **kw):
    from flask import render_template
    return render_template(tpl(name), **kw)
