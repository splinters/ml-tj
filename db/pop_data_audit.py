"""
Аудит качества данных chemicals.tj по участкам СОЗ: что на карте и в прикреплённых
файлах противоречит само себе или мешает использовать данные.

Вход: source_data/chemicals_tj/ (pollutions_raw.json, docs/, docs_unique.csv, extracted/,
      manual/, pop_layer/)
Выход: source_data/chemicals_tj/pop_layer/audit/
  data_quality_audit.csv  - 1 строка = 1 замечание (категория, важность, участки, описание,
                            основание, что сделать на стороне сайта)
  audit_summary.csv       - число замечаний по категориям
"""
import json
import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent / "source_data" / "chemicals_tj"
LAYER = ROOT / "pop_layer"
OUT = LAYER / "audit"
P126_SIZE = 1760837  # размер файла протокола №126 на сайте


def tj(name):
    m = re.search(r"TJ-?(\d+)", name or "")
    return f"TJ-{m.group(1)}" if m else None


def main():
    OUT.mkdir(exist_ok=True)
    A = json.loads((ROOT / "pollutions_raw.json").read_text(encoding="utf-8"))
    P = [s for s in A if (s.get("pollution_type") or {}).get("name") == "СОЗ"]
    T = pd.read_csv(LAYER / "pop_sites.csv", encoding="utf-8-sig", low_memory=False)
    Tm = T[T.on_map == True]  # noqa: E712
    S = pd.read_csv(LAYER / "pop_samples.csv", encoding="utf-8-sig", low_memory=False)
    M = pd.read_csv(LAYER / "pop_measurements.csv", encoding="utf-8-sig", low_memory=False)
    du = pd.read_csv(ROOT / "docs_unique.csv", encoding="utf-8-sig")
    inv = pd.read_csv(ROOT / "extracted" / "doc_inventory.csv", encoding="utf-8-sig")
    links = pd.read_csv(ROOT / "manual" / "protocol126_map_links.csv", dtype=str).fillna("")
    p126 = pd.read_csv(ROOT / "manual" / "protocol126_transcribed.csv", dtype=str).fillna("")
    files = pd.read_csv(ROOT / "files.csv", encoding="utf-8-sig")
    codes = set(Tm.tj_code)
    issues = []

    def add(cat, sev, sites, desc, evidence, action):
        issues.append({"category": cat, "severity": sev, "tj_codes": ", ".join(sorted(set(sites))) if sites else "",
                       "description": desc, "evidence": evidence, "action_on_site": action})

    # 1. повтор кода TJ
    cnt = Counter(tj(s["site_name"]) for s in P)
    for c, n in cnt.items():
        if n > 1:
            ss = [s for s in P if tj(s["site_name"]) == c]
            add("Идентификация", "высокая", [c], f"Код {c} используется на карте {n} раза для разных карточек",
                "; ".join(f"id {s['id']}: {s['latitude']}, {s['longitude']}, статус «{(s.get('pollution_status') or {}).get('name')}»" for s in ss),
                "Присвоить второй карточке собственный код или объединить карточки")
    # 2. вероятные дубли по координатам
    seen = set()
    for r in Tm[Tm.qc_possible_duplicate == True].itertuples():  # noqa: E712
        pair = tuple(sorted([r.tj_code, r.nearest_pop_site]))
        if pair in seen:
            continue
        seen.add(pair)
        add("Идентификация", "средняя", list(pair), f"Участки {pair[0]} и {pair[1]} ближе 50 м друг к другу — возможно, один объект",
            f"расстояние {int(r.nearest_pop_site_m)} м", "Проверить, один ли это объект; при совпадении объединить карточки и вложения")
    # 3. точность координат
    for r in Tm[Tm.qc_coords_low_precision == True].itertuples():  # noqa: E712
        add("Координаты", "средняя", [r.tj_code], "Координаты участка округлены (меньше 4 знаков после запятой, точность ~1 км)",
            f"{r.latitude}, {r.longitude}", "Уточнить координаты по GPS или снимку")
    # 4. участки протокола №126 под кодами, которых нет на карте
    for r in links.itertuples():
        sev = "средняя" if r.match == "уверенно" else "высокая"
        add("Идентификация", sev, [r.protocol_code] + ([r.map_code] if r.map_code.startswith("TJ-") and " " not in r.map_code else []),
            f"В протоколе №126 участок записан как {r.protocol_code} («{r.protocol_place}»), на карте такого кода нет",
            f"соответствие карточке: {r.map_code or 'не найдено'} ({r.match})",
            "Указать в карточке карты полевой код протокола; прикрепить протокол к карточке" if r.match == "уверенно"
            else "Определить, к какой карточке относятся пробы")
    # 5. протокол №126 прикреплён не ко всем своим участкам
    att126 = set(du.loc[du.file_size == P126_SIZE, "tj_codes"].iloc[0].split(","))
    in126 = {"TJ-" + g for g in p126.site_group}
    lk = {r.protocol_code: r.map_code for r in links.itertuples() if r.match == "уверенно"}
    in126_map = {lk.get(c, c) for c in in126} & codes
    miss = sorted(in126_map - att126)
    add("Документы", "средняя", miss, f"Протокол №126 содержит результаты по {len(miss)} участкам карты, к карточкам которых он не прикреплён",
        f"протокол прикреплён к {len(att126)} карточкам, участков в протоколе {len(in126)}", "Прикрепить протокол к этим карточкам")
    extra = sorted(att126 - in126_map)
    if extra:
        add("Документы", "низкая", extra, "Протокол №126 прикреплён к карточкам, проб которых в протоколе нет",
            "", "Проверить привязку вложения")
    # 6. журналы и реестры, прикреплённые к чужим участкам
    for r in du.itertuples():
        if not r.local_file.endswith(".pdf") or r.file_size == P126_SIZE or not re.search(r"журнал|реестр|протокол|проб", str(r.filenames).lower()):
            continue
        txt = subprocess.run(["pdftotext", "-layout", str(ROOT / "docs" / r.local_file), "-"], capture_output=True).stdout.decode("utf-8", "replace")
        found = {"TJ-" + x for x in re.findall(r"(?<![\d.,])(?:tj|TJ)?-?(7[3-9]\d\d|8[12]\d\d)(?![\d.,])", txt)} & codes
        att = set(r.tj_codes.split(","))
        if found and not (found & att) and len(found) <= 5:
            add("Документы", "высокая", sorted(att | found), f"Документ «{r.filenames.split(' | ')[0].strip()}» прикреплён к {', '.join(sorted(att))}, но содержит пробы только других участков: {', '.join(sorted(found))}",
                f"файл {r.local_file}", "Перенести вложение в карточки указанных участков")
    # 7. журнал TJ-7447 = данные TJ-7545 по протоколу
    add("Документы", "высокая", ["TJ-7447", "TJ-7545"], "Журнал отбора проб в карточке TJ-7447 содержит значения, совпадающие с TJ-7545 по протоколу №126",
        "файл f8dc1cff204d.pdf", "Проверить, к какому участку относится журнал")
    # 8. зачёркнутая проба
    add("Анализы", "средняя", ["TJ-7599"], "В протоколе №126 строка участка зачёркнута с пометкой «Нет пробы», при этом в карточке есть вложения «реестр проб» и «результаты анализов»",
        "протокол №126, лист 5, строка 68", "Указать в карточке, что анализа нет, или приложить фактические результаты")
    # 9. расхождения значений
    for codes_, d in [(["TJ-8207"], "проба К3: ДДТ 20,83 мг/кг (протокол АФИ №8) и 28,83 мг/кг (реестр проб)"),
                      (["TJ-7442"], "проба 7442-3: ДДЕ «не обн.» (протокол №126) и 0,03 мг/кг (журнал)"),
                      (["TJ-7635"], "проба 7635-1: ДДЕ 0,18 мг/кг (протокол №126) и 0,13 мг/кг (журнал)")]:
        add("Анализы", "средняя", codes_, "Значения в журнале и лабораторном протоколе расходятся", d, "Исправить журнал по протоколу")
    # 10. опечатки номеров проб в протоколе
    for r in p126[p126.note.str.contains("вероятно")].itertuples():
        add("Анализы", "низкая", ["TJ-" + r.site_group], "В протоколе №126 номер пробы напечатан с ошибкой", r.note, "Учитывать при переносе данных")
    # 11. «очищенный» участок с высоким загрязнением
    cl = Tm[Tm.pollution_status == "Очищенный участок"]
    for r in cl.itertuples():
        b = np.nanmax([r.ddt_group_sum_max, r.hch_sum_max]) if r.lab_n_samples else np.nan
        if np.isfinite(b) and b >= 1:
            add("Статус", "высокая" if b >= 100 else "средняя", [r.tj_code],
                "Участок отмечен как «Очищенный», но единственные анализы на сайте показывают загрязнение выше норматива",
                f"максимум {b:.1f} мг/кг; годы анализов: {r.lab_years}",
                "Приложить анализы после очистки (контрольный отбор) или скорректировать статус")
    # 12. пробы далеко от участка
    far = S[S.dist_to_site_m > 2000].groupby("tj_code").dist_to_site_m.agg(["size", "max"])
    for c, r in far.iterrows():
        add("Координаты", "высокая" if r["max"] > 20000 else "средняя", [c], f"Координаты {int(r['size'])} проб удалены от точки участка более чем на 2 км",
            f"максимум {r['max']/1000:.1f} км", "Сверить координаты проб и участка")
    # 13. перепутанные колонки координат в шаблоне журнала
    n_swapped = 0
    for r in du.itertuples():
        if r.local_file.endswith(".pdf"):
            t = subprocess.run(["pdftotext", "-layout", "-l", "1", str(ROOT / "docs" / r.local_file), "-"], capture_output=True).stdout.decode("utf-8", "replace")
            if re.search(r"Долгота\s+Широта", t) and re.search(r"\b3[6-9][.,]\d{4,}\s+[67]\d[.,]\d{4,}", t):
                n_swapped += 1
    add("Шаблон журнала", "низкая", [], f"В {n_swapped} журналах колонки подписаны «Долгота / Широта», а заполнены в порядке «широта / долгота»",
        "значения 36–41 (широта) в колонке «Долгота»", "Исправить заголовки в шаблоне журнала")
    # 14. безымянные колонки загрязнителей
    un = sorted(set(M[M.substance.astype(str).str.startswith("unspecified")].tj_code.dropna()))
    add("Шаблон журнала", "средняя", un, f"В журналах {len(un)} участков колонки «Загрязнитель 1/2» без названия вещества",
        "журналы 2014–2016 гг.", "Указать вещество в заголовке колонки")
    # 15. документы без значений
    nv = inv[inv.status == "no_values"]
    add("Документы", "низкая", sorted({c for x in nv.tj_codes.dropna() for c in str(x).split(",")}),
        f"{len(nv)} журналов отбора проб не содержат значений концентраций (только описание проб)", "", "Дополнить результатами анализа или пометить как журнал без анализа")
    # 16. сканы вместо таблиц
    add("Документы", "средняя", [], "Ключевые лабораторные протоколы опубликованы как сканы (протокол АФИ №126 на 61 участок, отчёт Института химии 2017 г.)",
        "текстового слоя нет, перенос вручную", "Публиковать исходные таблицы (xlsx) рядом со сканом")
    # 17. копии одного файла
    dup = du.groupby("file_size").size()
    add("Документы", "низкая", [], f"Один и тот же документ загружен многократно: 373 вложения документов дают 245 разных по содержанию",
        "протокол №126 — 61 копия, «Доля ПДК» — 18 копий", "Хранить документ один раз и связывать с несколькими участками")
    # 18. артефакты ChatGPT
    ch = Tm[Tm.text_has_chatgpt_artifact == True].tj_code.tolist()  # noqa: E712
    add("Тексты", "низкая", ch, f"В описаниях {len(ch)} участков остался служебный текст «ChatGPT сказал:»", "", "Удалить служебный текст")
    # 19. неинформативные и пустые поля анкеты
    he = Tm.health_effects.value_counts().to_dict()
    add("Анкета", "низкая", [], "Поле «Последствия для здоровья» заполнено только значением «Нет»", str(he),
        "Заполнять по результатам опроса или убрать поле")
    fill = {c: round(Tm[c].notna().mean() * 100) for c in ["lived_50m", "lived_100m", "lived_500m", "worked_on_site", "visited_on_site"]}
    add("Анкета", "средняя", [], "Поля численности населения в кольцах 50/100/500 м заполнены у меньшинства участков",
        "заполненность, %: " + ", ".join(f"{k} {v}" for k, v in fill.items()), "Сделать поля обязательными при обследовании")
    # 20. участки без анализов
    no_lab = Tm[Tm.lab_n_samples.fillna(0) == 0].tj_code.tolist()
    add("Анализы", "информация", no_lab, f"{len(no_lab)} участков карты не имеют лабораторных данных", "", "Кандидаты на отбор проб (см. план)")
    S["burden"] = S[["DDT_group_sum", "HCH_sum"]].max(axis=1)
    has_b = set(S[S.burden.notna()].tj_code)
    partial = sorted(set(Tm[Tm.lab_n_samples.fillna(0) > 0].tj_code) - has_b)
    add("Анализы", "информация", partial, f"У {len(partial)} участков есть только значения без названия вещества или сумма пестицидов — уровень ДДТ и ГХЦГ по ним неизвестен",
        "журналы 2014–2016 гг.", "Кандидаты на повторный отбор проб")

    D = pd.DataFrame(issues)
    D.insert(0, "issue_id", range(1, len(D) + 1))
    D["n_sites"] = D.tj_codes.map(lambda s: len([x for x in s.split(", ") if x]))
    D.to_csv(OUT / "data_quality_audit.csv", index=False, encoding="utf-8-sig")
    summ = D.groupby(["category", "severity"]).agg(n_issues=("issue_id", "size"), n_sites=("n_sites", "sum")).reset_index()
    summ.to_csv(OUT / "audit_summary.csv", index=False, encoding="utf-8-sig")
    print(len(D)); print(summ.to_string())


if __name__ == "__main__":
    main()
