"""
Извлечение лабораторных значений по СОЗ (пестициды) из документов chemicals.tj
в длинный формат. Вход: source_data/chemicals_tj/docs + docs_unique.csv
(результат db/fetch_chemicals_tj.py --download-docs).

Типы документов и разборщики:
  afi_2021   - протокол ФГБНУ АФИ №8 от 12.02.2021 (текстовый PDF)
  ilim_2017  - отчёт ОсОО «НПО ИЛИМ» №42 от 01.03.2017, EPA 8081 (текстовый PDF)
  log        - журналы/реестры проб (Excel->PDF, таблица с колонками загрязнителей)
  skip       - производные таблицы «Доля ПДК» (дублируют ИЛИМ), отчёты без замеров
  manual     - сканы и нестандартные таблицы: протокол АФИ №126 (2019) и др.

Дедупликация: документы сводятся по хешу извлечённого текста - побайтно разные
копии одного протокола (сайт пересохраняет PDF) разбираются один раз, а привязки
к участкам объединяются.

Выход: source_data/chemicals_tj/extracted/
  measurements_long.csv  - 1 строка = проба x вещество, мг/кг
  doc_inventory.csv      - 1 строка = уникальный документ: тип, статус, число значений, замечания

source_rank: 1 - лабораторный протокол; 2 - журнал без лабораторного дубля
(сводная выборка = rank 1 + 2); 0 - дубль (журнал той же кампании, что протокол,
или более ранняя редакция того же журнала).

flags (через «;»):
  zero_reported        - в документе стоит 0 (в протоколе №126 на этом месте «не обн.»)
  order_2020           - реестр 2020 г.: «Загрязнитель 1..5» = ДДТ, ДДЕ, γ-, β-, α-ГХЦГ
                         (проверено: 47 из 48 участков совпадают с протоколом АФИ №8)
  site_by_label        - ИЛИМ 2017: участок определён по подписи пробы (ILIM_SITE_KEYS)
  site_by_row_order    - сводный реестр: участок по порядку строк (3 повторности на код)
  multi_site_block / site_ambiguous - участок не определён, см. tj_candidates
  substance_unknown    - колонка «Загрязнитель N» без названия вещества (журналы 2014-2016 гг.)
  value_from_overlap   - значение вытащено из ячейки, на которую наехал текст соседней колонки
  coords_unaligned / coords_partial / coords_far_from_site (>2 км от точки участка на карте)

Известное расхождение источников: TJ-8207, проба К3 - ДДТ 20,83 (протокол АФИ №8)
против 28,83 (реестр проб).

Требует: pdfplumber, pdftotext (poppler).
"""
import csv
import hashlib
import json
import math
import re
import subprocess
from collections import defaultdict
from pathlib import Path

import pdfplumber

ROOT = Path(__file__).resolve().parent.parent / "source_data" / "chemicals_tj"
DOCS = ROOT / "docs"
OUT = ROOT / "extracted"

FIELDS = ["source_rank", "doc_key", "doc_file", "doc_type", "lab", "report_ref", "tj_code", "tj_candidates",
          "sample_label", "replicate", "lat", "lon", "dist_to_site_m", "sample_date", "depth_cm", "sample_kind",
          "substance", "value_mgkg", "below_lod", "raw_value", "unit_raw", "page", "flags"]

# --- вещества -----------------------------------------------------------------
# порядок важен: сначала более специфичные шаблоны
SUBSTANCE_PATTERNS = [
    ("heptachlor_epoxide", r"heptachlor[\s-]*epox|гептахлор[\s-]*эпокс"),
    ("heptachlor", r"heptachlor|гептахлор"),
    ("endrin_aldehyde", r"endrine?[\s-]*aldehyde|эндрин[\s-]*альдегид"),
    ("endosulfan_sulfate", r"endosul[\w-]*sulf|эндосульфан[\s-]*сульфат"),
    ("endosulfan_2", r"endosulf\w*\s*-?\s*2|endosulf2|эндосульфан[\s-]*(2|ii)"),
    ("endosulfan_1", r"endosulf\w*\s*-?\s*1|эндосульфан[\s-]*(1|i)\b|эндосульфан"),
    ("dieldrin", r"dieldrin|дильдрин"),
    ("endrin", r"endrin|эндрин"),
    ("aldrin", r"aldrin|альдрин"),
    ("methoxychlor", r"metoxychlor|methoxychlor|метоксихлор"),
    ("a_HCH", r"альфа|\balpha|\ba[\s-]*(bhc|hch)|α"),
    ("b_HCH", r"бетт?а|\bbeta|\bb[\s-]*(bhc|hch)|β"),
    ("g_HCH", r"гамма|\bgamma|\b[gy][\s-]*(bhc|hch)|γ|линдан|lindane"),
    ("d_HCH", r"дельта|\bdelta|\bd[\s-]*(bhc|hch)|δ"),
    ("DDD", r"ддд|\bddd"),
    ("DDE", r"дд[еэ]|\bdde"),
    ("DDT", r"ддт|\bddt"),
    ("HCH_sum", r"гхцг|\bhch\b|\bbhc\b"),
    ("pesticides_total", r"хлорорганик|organochl|pesticides?\s*total|сумма\s*пестицид|total\s*pesticide|пестицид\w*\s*\(?общ"),
]
CONTAMINANT_GENERIC = r"(contamin|загрязни)[a-zа-я\s]*?,?\s*(\d)"
KNOWN_CODES = {r["tj_code"] for r in csv.DictReader(open(ROOT / "sites.csv", encoding="utf-8-sig")) if r["tj_code"]}
ORDER_2020 = ["DDT", "DDE", "g_HCH", "b_HCH", "a_HCH"]  # реестры 2020 г.: см. doc_inventory, флаг order_2020


def map_substance(header):
    """Сначала кириллическая часть заголовка (английские строки в шаблонах
    журналов местами устаревшие, напр. «A-BHC» над столбцом «ДДТ»), затем вся строка."""
    # Excel переносит слова внутри ячейки («Эндосуль\nфан»), поэтому проверяем
    # и вариант со склеенными переносами, и вариант с пробелами
    raw = (header or "").lower()
    variants = []
    for h in (raw.replace("\n", ""), raw.replace("\n", " ")):
        cyr = " ".join(w for w in h.split() if re.search(r"[а-я]", w))
        variants += [cyr, h]
    for text in variants:
        for name, pat in SUBSTANCE_PATTERNS:
            if re.search(pat, text):
                return name
    return None


# --- значения -----------------------------------------------------------------
NUM_RE = re.compile(r"^[<≤]?\s*\d+(?:[.,]\d+)?$")
ND_RE = re.compile(r"^(не\s*обн\.?|н\.?\s*о\.?|н/о|nd|n\.d\.|not\s*detected|-|—|–|<\s*lod|ниже.*)$", re.I)


def parse_value(raw, allow_overlap=False):
    """-> (value|None|"BAD", below_lod, raw, from_overlap)"""
    s = (raw or "").strip()
    if not s:
        return None, False, "", False
    s1 = re.sub(r"\s+", "", s)
    if NUM_RE.match(s1):
        below = s1.startswith(("<", "≤"))
        v = float(s1.lstrip("<≤").replace(",", "."))
        return (None if below else v), below, s, False
    if ND_RE.match(s):
        return None, True, s, False
    if allow_overlap:
        # в журналах Excel текст соседней колонки «наезжает» на ячейку значения:
        # «скл7а» = «склада» + «7», «скл3а,д5о» = «склада» + «3,5»
        s2 = re.sub(r"[A-Za-zА-Яа-яЁё\s()\-]", "", s).strip(".,")
        # все цифры должны сидеть в одном «слове» (иначе это число из текста: «o 100 mete2r»)
        digit_tokens = [t for t in s.split() if re.search(r"\d", t)]
        if NUM_RE.match(s2) and len(digit_tokens) == 1:
            return float(s2.replace(",", ".")), False, s, True
        if not re.search(r"\d", s):
            return None, False, s, False
    return "BAD", False, s, False


COORD_RE = re.compile(r"\d{2}[.,]\d{3,}")


def coords_from(text):
    lat = lon = None
    for tok in COORD_RE.findall(text or ""):
        v = float(tok.replace(",", "."))
        if 36.0 <= v <= 41.5 and lat is None:
            lat = v
        elif 67.0 <= v <= 75.5 and lon is None:
            lon = v
    return lat, lon


def pdftext(path):
    return subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True).stdout.decode("utf-8", "replace")


# --- разборщик: протокол АФИ №8 (2021) -----------------------------------------
def parse_afi_2021(path, text, ctx):
    """Таблица протокола: № п/п | номер пробы (= код TJ, объединённая ячейка на
    группу повторностей) | повторность | ДДТ | ДДЕ | γ | β | α-ГХЦГ.
    pdfplumber отдаёт объединённую ячейку значением в первой строке и None далее,
    группа может продолжаться на следующей странице (там первая ячейка пустая)."""
    rows, issues = [], []
    cols = ["DDT", "DDE", "g_HCH", "b_HCH", "a_HCH"]
    code = None
    with pdfplumber.open(path) as pdf:
        for pno, p in enumerate(pdf.pages, 1):
            for t in p.extract_tables():
                for r in t:
                    if len(r) < 8:
                        continue
                    if r[1] and re.fullmatch(r"\d{4}", r[1].strip()):
                        code = r[1].strip()
                    rep = (r[2] or "").strip().replace("T", "Т").replace("K", "К")
                    vals = [(x or "").strip() for x in r[3:8]]
                    if not rep or not all(NUM_RE.match(v.replace(" ", "")) for v in vals):
                        continue
                    for sub, raw in zip(cols, vals):
                        v, below, rawv, _ = parse_value(raw)
                        rows.append(dict(ctx, tj_code=f"TJ-{code}" if code else None,
                                         sample_label=f"{code}-{rep}", replicate=rep, substance=sub,
                                         value_mgkg=v, below_lod=below, raw_value=rawv, unit_raw="мг/кг",
                                         page=pno, flags="zero_reported" if v == 0 else ""))
    return rows, issues


# --- разборщик: отчёт ИЛИМ 2017 -----------------------------------------------
ILIM_SUBST = {"A-BHC": "a_HCH", "B-BHC": "b_HCH", "G-BHC": "g_HCH", "D-BHC": "d_HCH",
              "Heptachlor": "heptachlor", "Aldrin": "aldrin", "Heptachlor-epox": "heptachlor_epoxide",
              "Endosulfan-1": "endosulfan_1", "4.4 DDE": "DDE", "Dieldrin": "dieldrin", "Endrin": "endrin",
              "4.4 DDD": "DDD", "Endosulf2": "endosulfan_2", "Endrine-Aldehyde": "endrin_aldehyde",
              "Endosul-sulf": "endosulfan_sulfate", "4.4 DDT": "DDT", "Metoxychlor": "methoxychlor"}


ILIM_SITE_KEYS = [  # подпись пробы -> участок (по названиям участков на chemicals.tj)
    (r"50 лет ссср", "TJ-4987"), (r"энгельс|зарм|завмес", "TJ-4956"), (r"ф\.\s*са[ин]\w*ев|ф\.\s*саидов|чонин", "TJ-4958"),
    (r"саидов", "TJ-4990"), (r"турсун\s*заде", "TJ-4963"), (r"ленин|ленак", "TJ-4971"),
    (r"совхоз 6|совхоз №6", "TJ-4972"), (r"давлат", "TJ-4973"), (r"в[иа]хдат", "TJ-4981"),
    (r"турд[ыи]ев", "TJ-4982"), (r"совхоз 5", "TJ-4984"), (r"дфх 4|дфх №4", "TJ-4985"),
    (r"имм?омбердиев", "TJ-4986"), (r"[иеэ][ас]т[иі]?к[оа]л|истиклол", "TJ-4989"),
    (r"хамидов", "TJ-4991"), (r"итт?\w*\s*иф", "TJ-4992"), (r"сарх", "TJ-4993"),
]


def ilim_site(label):
    l = label.lower()
    for pat, code in ILIM_SITE_KEYS:
        if re.search(pat, l):
            return code
    return None


def parse_ilim_2017(path, text, ctx):
    rows, issues = [], []
    label = None
    page = 1
    names = sorted(ILIM_SUBST, key=len, reverse=True)
    for line in text.splitlines():
        page += line.count("\f")
        m = re.match(r"^\s*(\d+)\s+(Проба.*?)\s{2,}(\S.*)$", line)
        rest = line
        if m:
            label = re.sub(r"\s+", " ", m.group(2)).strip()
            rest = m.group(3)
        for nm in names:
            mm = re.match(r"^\s*" + re.escape(nm) + r"\s+(\S+)\s*$", rest)
            if mm and label:
                v, below, rawv, _ = parse_value(mm.group(1))
                if v == "BAD":
                    issues.append(f"не число: {label} {nm} {rawv}")
                    break
                rows.append(dict(ctx, tj_code=ilim_site(label), sample_label=label, substance=ILIM_SUBST[nm],
                                 value_mgkg=v, below_lod=below, raw_value=rawv, unit_raw="mg/kg",
                                 page=page, flags="site_by_label"))
                break
    return rows, issues


# --- разборщик: журналы/реестры проб ------------------------------------------
HEADER_HINT = re.compile(r"ppm|мг/кг|mg/kg|latitude|широта|долгота|longt|sampler|отбиравш|номер|depth|глубин|дата|date", re.I)


def page_tables(pdf):
    out = []
    for i, p in enumerate(pdf.pages):
        for t in p.extract_tables():
            if t and len(t) >= 2:
                out.append((i + 1, [[(c or "").strip() for c in r] for r in t]))
    return out


def stitch(tables):
    """Excel печатает широкую таблицу частями по страницам: части с одинаковым
    числом строк склеиваем по горизонтали."""
    blocks = []
    for page, t in tables:
        if blocks and len(blocks[-1]["rows"]) == len(t):
            for r, add in zip(blocks[-1]["rows"], t):
                r.extend(add)
            blocks[-1]["pages"].append(page)
        else:
            blocks.append({"rows": [list(r) for r in t], "pages": [page]})
    return blocks


def is_data_cell(s):
    if not s:
        return False
    v = parse_value(s, allow_overlap=True)[0]
    return v not in (None, "BAD") or bool(ND_RE.match(s))


def parse_log(path, text, ctx):
    rows, issues = [], []
    with pdfplumber.open(path) as pdf:
        tables = page_tables(pdf)
    if not tables:
        return [], ["таблицы не найдены"]
    order2020 = bool(re.search(r"ant\s*5|Contaminant 5", text)) and bool(
        re.search(r"Альфа|A-HCH", text)) and bool(re.search(r"ДДЕ|DDE", text))
    for b in stitch(tables):
        grid = b["rows"]
        ncol = max(len(r) for r in grid)
        grid = [r + [""] * (ncol - len(r)) for r in grid]
        # заголовок: строки до первой строки с числом в колонке загрязнителя
        hdr_n = 0
        for i, r in enumerate(grid[:6]):
            if any(HEADER_HINT.search(c) for c in r):
                hdr_n = i + 1
        headers = [" ".join(grid[i][j] for i in range(hdr_n)) for j in range(ncol)]
        subst_cols = {}
        generic = {}
        for j, h in enumerate(headers):
            g = re.search(CONTAMINANT_GENERIC, h, re.I) or re.search(CONTAMINANT_GENERIC, h.replace("\n", ""), re.I)
            s = map_substance(h)
            if order2020 and g:
                subst_cols[j] = ORDER_2020[int(g.group(2)) - 1] if int(g.group(2)) <= 5 else None
                continue
            if s and (re.search(r"ppm|мг|mg|ddt|ддт|hch|гхцг|bhc|пестицид|pesticide|хлорорг|organochl", h.replace("\n", ""), re.I)):
                subst_cols[j] = s
            elif g:
                generic[j] = f"unspecified_{g.group(2)}"
        subst_cols = {j: s for j, s in subst_cols.items() if s}
        allcols = {**generic, **subst_cols}
        if not allcols:
            continue
        data = grid[hdr_n:]
        # строки данных = строки, где есть значение хотя бы в одной колонке загрязнителя
        drows = [r for r in data if any(is_data_cell(r[j]) for j in allcols)]
        if not drows:
            continue
        # метаданные проб: если ячейки объединены, раскладываем токены по порядку
        meta_cols = [j for j in range(ncol) if j not in allcols]
        lat_lon = []
        coord_tokens = []
        for r in data:
            for j in meta_cols:
                coord_tokens += COORD_RE.findall(r[j])
        lats = [float(t.replace(",", ".")) for t in coord_tokens if 36 <= float(t.replace(",", ".")) <= 41.5]
        lons = [float(t.replace(",", ".")) for t in coord_tokens if 67 <= float(t.replace(",", ".")) <= 75.5]
        aligned = len(lats) == len(drows) and len(lons) == len(drows)
        id_col = next((j for j, h in enumerate(headers) if re.search(r"номер|^\s*(#|№)", h, re.I)), 0)
        rep_col = next((j for j, h in enumerate(headers) if re.search(r"gps|сектор", h, re.I) and j != id_col), None)
        date_col = next((j for j, h in enumerate(headers) if re.search(r"date|дата", h, re.I)), None)
        depth_col = next((j for j, h in enumerate(headers) if re.search(r"depth|глубин", h, re.I)), None)
        kind_col = next((j for j, h in enumerate(headers) if re.search(r"target|целев", h, re.I)), None)
        # сводные реестры: в одном блоке пробы нескольких участков (код = номер пробы,
        # объединённая ячейка на 3 повторности Т1/Т2/К3) -> относим строки по порядку
        block_codes = list(dict.fromkeys(
            c for r in data for j in meta_cols[:3] for c in re.findall(r"\b(\d{4})\b", r[j])
            if f"TJ-{c}" in KNOWN_CODES))
        by_order = len(block_codes) > 1 and 3 * (len(block_codes) - 1) < len(drows) <= 3 * len(block_codes)
        last_code = None
        for k, r in enumerate(drows):
            if aligned:
                lat, lon = lats[k], lons[k]
                fl = []
            else:
                lat, lon = coords_from(" ".join(r[j] for j in meta_cols))
                fl = ["coords_unaligned"] if (lats or lons) and lat is None else []
            label = " ".join(x for x in [r[id_col], r[rep_col] if rep_col is not None else ""] if x).strip()
            m4 = re.search(r"\b(\d{4})\b", " ".join(r[j] for j in meta_cols[:3]))
            code = m4.group(1) if m4 else last_code
            last_code = code
            if by_order:
                code = block_codes[k // 3]
                fl = fl + ["site_by_row_order"]
            elif len(block_codes) > 1:
                code = None
                fl = fl + ["multi_site_block"]
            for j, sub in allcols.items():
                v, below, rawv, overlap = parse_value(r[j], allow_overlap=True)
                if v is None and not below:
                    continue
                if v == "BAD":
                    issues.append(f"не число: стр.{b['pages']} {headers[j][:30]!r}={rawv!r}")
                    continue
                f = list(fl)
                if overlap:
                    f.append("value_from_overlap")
                if order2020 and j in subst_cols:
                    f.append("order_2020")
                if sub.startswith("unspecified"):
                    f.append("substance_unknown")
                if v == 0:
                    f.append("zero_reported")
                rows.append(dict(ctx, tj_code=None, sample_label=label or str(k + 1),
                                 replicate=r[rep_col] if rep_col is not None else "",
                                 lat=lat, lon=lon,
                                 sample_date=r[date_col] if date_col is not None else "",
                                 depth_cm=r[depth_col] if depth_col is not None else "",
                                 sample_kind=r[kind_col] if kind_col is not None else "",
                                 substance=sub, value_mgkg=v, below_lod=below, raw_value=rawv,
                                 unit_raw=headers[j][-25:], page=",".join(map(str, b["pages"])),
                                 flags=";".join(f), _code4=code))
    return rows, issues


# --- классификация ------------------------------------------------------------
def classify(text, names):
    t = text.lower()
    n = " ".join(names).lower()
    if "агрофизическ" in t and "№8" in t.replace(" ", ""):
        return "afi_2021"
    if "ilim" in t and "8081" in t:
        return "ilim_2017"
    if "доля пдк" in t:
        return "skip"
    if "биомониторинг" in n or "biomonitoring" in t[:3000]:
        return "skip"
    if "куканиев" in t or "содержаниехлорорг" in re.sub(r"\s+", "", t):
        return "manual"
    return "log"


def assign_site(r, tj_codes):
    code4 = r.pop("_code4", None)
    if r.get("tj_code"):
        return r
    if "site_by_row_order" in (r.get("flags") or "") and code4:
        r["tj_code"] = f"TJ-{code4}"
        return r
    if "multi_site_block" in (r.get("flags") or ""):
        r["tj_candidates"] = ",".join(tj_codes)
        return r
    if len(tj_codes) == 1:
        r["tj_code"] = tj_codes[0]
    elif code4 and f"TJ-{code4}" in tj_codes:
        r["tj_code"] = f"TJ-{code4}"
    else:
        r["tj_candidates"] = ",".join(tj_codes)
        r["flags"] = ";".join(x for x in [r.get("flags"), "site_ambiguous"] if x)
    return r


def check_coords(rows):
    sites = {r["tj_code"]: r for r in csv.DictReader(open(ROOT / "sites.csv", encoding="utf-8-sig")) if r["tj_code"]}
    for r in rows:
        if bool(r.get("lat")) != bool(r.get("lon")):
            r["lat"] = r["lon"] = ""
            r["flags"] = ";".join(x for x in [r["flags"], "coords_partial"] if x)
        s = sites.get(r.get("tj_code"))
        if r.get("lat") and s and s["latitude"]:
            la1, lo1, la2, lo2 = map(math.radians, (float(r["lat"]), float(r["lon"]), float(s["latitude"]), float(s["longitude"])))
            d = 2 * 6371000 * math.asin(math.sqrt(math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2))
            r["dist_to_site_m"] = round(d)
            if d > 2000:
                r["flags"] = ";".join(x for x in [r["flags"], "coords_far_from_site"] if x)


def consolidate(rows):
    """source_rank: 1 - лабораторный протокол; 2 - журнал/реестр без лабораторного дубля;
    0 - журнал, дублирующий протокол той же кампании (в сводную выборку не идёт).
    Кампании: реестры 2020 г. (флаг order_2020) дублируют протокол АФИ №8;
    журналы 2016 г. по участкам из отчёта ИЛИМ дублируют ИЛИМ."""
    afi_sites = {r["tj_code"] for r in rows if r["doc_type"] == "afi_2021" and r["tj_code"]}
    ilim_sites = {r["tj_code"] for r in rows if r["doc_type"] == "ilim_2017" and r["tj_code"]}
    for r in rows:
        if r["doc_type"] in ("afi_2021", "ilim_2017"):
            r["source_rank"] = 1
        elif r["tj_code"] in afi_sites and ("order_2020" in r["flags"] or r["substance"] in ("DDT", "DDE")
                                             and re.search(r"2020|/20\b", r.get("sample_date") or "")):
            r["source_rank"] = 0
        elif r["tj_code"] in ilim_sites:
            r["source_rank"] = 0
        else:
            r["source_rank"] = 2
    # одни и те же журналы загружены на сайт в нескольких редакциях («реестр проб» /
    # «реестр проб финальный»): если набор (вещество, значение) документа по участку
    # целиком входит в набор другого документа - это дубль, оставляем более полный
    by_doc = defaultdict(list)
    for r in rows:
        if r["source_rank"] == 2 and r["tj_code"]:
            by_doc[(r["tj_code"], r["doc_key"])].append(r)
    sig = {k: sorted((x["substance"], x["value_mgkg"], x["below_lod"]) for x in v) for k, v in by_doc.items()}

    def contains(big, small):
        b = list(big)
        for x in small:
            if x in b:
                b.remove(x)
            else:
                return False
        return True
    for k, v in by_doc.items():
        for k2 in by_doc:
            if k2 != k and k2[0] == k[0] and (len(sig[k2]) > len(sig[k]) or (len(sig[k2]) == len(sig[k]) and k2[1] < k[1])) \
                    and contains(sig[k2], sig[k]):
                for r in v:
                    r["source_rank"] = 0
                    r["flags"] = ";".join(x for x in [r["flags"], "dup_of_other_log"] if x)
                break


def main():
    OUT.mkdir(exist_ok=True)
    docs = list(csv.DictReader(open(ROOT / "docs_unique.csv", encoding="utf-8-sig")))
    groups = {}
    for d in docs:
        fn = d["local_file"]
        if fn.endswith(".pdf"):
            text = pdftext(DOCS / fn)
            key = "txt:" + hashlib.sha256(text.encode()).hexdigest()[:12] if text.strip() else "scan:" + fn
        else:
            text, key = "", "bin:" + fn
        g = groups.setdefault(key, {"files": [], "tj": set(), "names": set(), "text": text})
        g["files"].append(fn)
        g["tj"].update(x for x in d["tj_codes"].split(",") if x)
        g["names"].update(d["filenames"].split(" | "))

    all_rows, inv = [], []
    for key, g in groups.items():
        fn = g["files"][0]
        tj = sorted(g["tj"])
        if not key.startswith("txt:"):
            dtype = "manual"
        else:
            dtype = classify(g["text"], g["names"])
        ctx = {f: "" for f in FIELDS}
        ctx.update(doc_key=key, doc_file=fn, doc_type=dtype)
        rows, issues = [], []
        try:
            if dtype == "afi_2021":
                ctx.update(lab="ФГБНУ АФИ (Санкт-Петербург)", report_ref="Протокол №8 от 12.02.2021")
                rows, issues = parse_afi_2021(DOCS / fn, g["text"], ctx)
            elif dtype == "ilim_2017":
                ctx.update(lab="ОсОО НПО ИЛИМ (Бишкек)", report_ref="Отчёт №42 от 01.03.2017, EPA 8081")
                rows, issues = parse_ilim_2017(DOCS / fn, g["text"], ctx)
            elif dtype == "log":
                ctx.update(lab="", report_ref="журнал/реестр проб")
                rows, issues = parse_log(DOCS / fn, g["text"], ctx)
        except Exception as e:
            issues.append(f"ошибка разбора: {e!r}")
        rows = [assign_site(r, tj) for r in rows]
        all_rows += rows
        subs = sorted({r["substance"] for r in rows})
        inv.append({"doc_key": key, "doc_type": dtype, "doc_file": fn, "n_copies": len(g["files"]),
                    "tj_codes": ",".join(tj), "names": " | ".join(sorted(g["names"]))[:200],
                    "n_values": len(rows), "n_samples": len({(r['tj_code'], r['sample_label'], r['lat'], r['lon']) for r in rows}),
                    "substances": ",".join(subs),
                    "status": ("ok" if rows and not issues else "issues" if rows or any("ошибка" in i for i in issues) else
                               "no_values" if dtype == "log" else dtype),
                    "issues": " || ".join(issues)[:500]})

    check_coords(all_rows)
    consolidate(all_rows)
    with open(OUT / "measurements_long.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(all_rows)
    with open(OUT / "doc_inventory.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(inv[0].keys()))
        w.writeheader()
        w.writerows(sorted(inv, key=lambda x: (x["doc_type"], x["status"])))
    print(f"документов (уникальных по тексту): {len(inv)}, значений: {len(all_rows)}")


if __name__ == "__main__":
    main()
