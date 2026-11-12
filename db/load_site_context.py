"""
Контекст использования территорий вокруг складов: загрузка и просмотр.

Что хранится: для каждого склада значения признаков (db/context_attributes.csv):
текущее и целевое землепользование, жильё/поля/каналы рядом и т.д. У каждого
значения есть происхождение: verification, source, source_ref, as_of.
Старые значения не затираются - действующим считается более надёжное и более
новое (документ > сообщение заказчика > оценка по снимкам > не установлено).

Запуск:
    python db/load_site_context.py                     # признаки + db/site_context_seed.csv
    python db/load_site_context.py ФАЙЛ.csv            # длинный CSV (см. ниже)
    python db/load_site_context.py ФАЙЛ.xlsx --verification customer_doc
    python db/load_site_context.py ФАЙЛ --dry-run      # проверка без записи
    python db/load_site_context.py --list              # действующие значения
    python db/load_site_context.py --delete-source "Название источника"   # убрать ошибочно загруженное

Параметры загрузки (для CSV/XLSX; колонки verification, source, as_of в самом файле имеют приоритет над ними):
    --verification customer_doc|customer_stated|estimate_imagery|unverified   (по умолчанию unverified)
    --source "Письмо заказчика 2026-11-02"                                    (по умолчанию имя файла)
    --as-of 2026-11-02                                                        (по умолчанию сегодня)

Форматы файла:
  CSV, длинный: колонки site, attr_code, value [, verification, source, source_ref, as_of, note].
      site - название склада или site_id.
  XLSX, широкий (шаблон db/../addfiles/site_context_request.xlsx): лист "Контекст", строка 1 -
      подписи, строка 2 - коды признаков (не менять), данные со строки 3, по строке на склад.
      Служебные колонки: site, verification, source, source_ref, as_of, note.
      Пустые ячейки пропускаются.

Новый признак: строка в db/context_attributes.csv и повторный запуск. Схему менять не нужно.
Изменить значение: загрузить новое значение с новым source или as_of (старое останется в истории)
или с теми же source и as_of (запись обновится на месте).
"""
import csv
import unicodedata
import datetime as dt
import os
import sys

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()
DB_URL = os.getenv("DATABASE_URL", "postgresql://soil_ml:soil_ml_dev_password@localhost:5432/soil_ml")
HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA = os.path.join(HERE, "schema_site_context.sql")
ATTR_CSV = os.path.join(HERE, "context_attributes.csv")
SEED_CSV = os.path.join(HERE, "site_context_seed.csv")
VERIFICATION = ["customer_doc", "customer_stated", "estimate_imagery", "unverified"]
# русские значения из шаблона Excel -> коды
VALUE_RU = {
    "сельскохозяйственное": "agricultural", "жилое": "residential", "коммерческое": "commercial",
    "промышленное": "industrial", "не используется": "unused", "смешанное": "mixed",
    "неизвестно": "unknown", "да": "yes", "нет": "no",
    "нынешнему собственнику": "current_owner",
    "минсельхозу / комитету по земледелию": "agri_authority",
    "по статусу приватизации": "by_privatization_status", "иное": "other",
}
SERVICE_COLS = {"site", "verification", "source", "source_ref", "as_of", "note"}


def norm(s):
    # В БД часть названий хранится в разложенной форме Unicode (Ё = Е + U+0308,
    # Й = И + U+0306), в CSV/Excel - в составной. Сравниваем без диакритики.
    s = unicodedata.normalize("NFKD", (s or "").strip().casefold())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return " ".join(s.replace("ё", "е").split())


def read_attrs():
    out = {}
    with open(ATTR_CSV, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            r["allowed_list"] = [x for x in (r["allowed"] or "").split("|") if x]
            out[r["attr_code"]] = r
    return out


def read_rows(path, defaults):
    """-> список dict(site, attr_code, value, verification, source, source_ref, as_of, note)"""
    rows = []
    if path.lower().endswith(".xlsx"):
        import openpyxl
        wb = openpyxl.load_workbook(path, data_only=True)
        ws = wb["Контекст"] if "Контекст" in wb.sheetnames else wb.worksheets[0]
        codes = [(str(c.value).strip() if c.value is not None else "") for c in ws[2]]
        for r in ws.iter_rows(min_row=3, values_only=True):
            rec = {c: v for c, v in zip(codes, r) if c}
            site = rec.get("site")
            if site is None or not str(site).strip():
                continue
            common = {k: rec.get(k) for k in ("verification", "source", "source_ref", "as_of", "note")}
            for c, v in rec.items():
                if c in SERVICE_COLS or v is None or str(v).strip() == "":
                    continue
                rows.append({"site": str(site).strip(), "attr_code": c, "value": v, **common})
    else:
        with open(path, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                if (r.get("site") or "").strip() and (r.get("value") or "").strip() != "":
                    rows.append(r)
    for r in rows:
        for k in ("verification", "source", "as_of"):
            if r.get(k) in (None, ""):
                r[k] = defaults[k]
        if isinstance(r["as_of"], (dt.datetime, dt.date)):
            r["as_of"] = r["as_of"].strftime("%Y-%m-%d")
    return rows


def validate(rows, attrs, sites):
    errors, out = [], []
    by_name = {norm(n): sid for sid, n in sites}
    by_id = {norm(sid): sid for sid, _ in sites}
    for i, r in enumerate(rows, start=1):
        where = f"запись {i} ({r.get('site')}/{r.get('attr_code')})"
        sid = by_id.get(norm(r["site"])) or by_name.get(norm(r["site"]))
        if not sid:
            errors.append(f"{where}: склад не найден в таблице sites")
            continue
        a = attrs.get(r["attr_code"])
        if not a:
            errors.append(f"{where}: признак не описан в context_attributes.csv")
            continue
        if r["verification"] not in VERIFICATION:
            errors.append(f"{where}: verification={r['verification']!r}, допустимо {VERIFICATION}")
        try:
            dt.date.fromisoformat(str(r["as_of"]))
        except ValueError:
            errors.append(f"{where}: as_of={r['as_of']!r}, нужен формат ГГГГ-ММ-ДД")
        v = str(r["value"]).strip()
        vt, vn = None, None
        if a["value_type"] == "number":
            try:
                vn = float(v.replace(",", ".").replace(" ", ""))
                if vn < 0:
                    errors.append(f"{where}: отрицательное значение {v!r}")
            except ValueError:
                errors.append(f"{where}: значение {v!r} не число")
        elif a["value_type"] == "enum":
            v = VALUE_RU.get(norm(v), v)
            if v not in a["allowed_list"]:
                errors.append(f"{where}: {v!r} вне списка {a['allowed_list']}")
            vt = v
        else:
            vt = v
        out.append(dict(site_id=sid, attr_code=r["attr_code"], value_text=vt, value_num=vn,
                        verification=r["verification"], source=r["source"],
                        source_ref=(r.get("source_ref") or None), as_of=str(r["as_of"]),
                        note=(r.get("note") or None)))
    return errors, out


def show_current(engine):
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT s.name, a.sort_order, a.label_ru, c.value_text, c.value_num, a.unit, c.verification, c.source
            FROM v_site_context_current c
            JOIN sites s ON s.site_id = c.site_id
            JOIN context_attributes a ON a.attr_code = c.attr_code
            ORDER BY s.name, a.sort_order""")).all()
    if not rows:
        print("Значений нет.")
        return
    cur = None
    for name, _, label, vt, vn, unit, ver, src in rows:
        if name != cur:
            print(f"\n{name}")
            cur = name
        val = vt if vt is not None else f"{float(vn):g} {unit or ''}".strip()
        print(f"  {label:44s} {val:28s} [{ver}] {src}")


def main():
    args = sys.argv[1:]
    opts, pos, i = {}, [], 0
    flags = {"--dry-run", "--list"}
    while i < len(args):
        a = args[i]
        if a in flags:
            opts[a] = True
        elif a in ("--verification", "--source", "--as-of", "--delete-source"):
            opts[a] = args[i + 1]
            i += 1
        else:
            pos.append(a)
        i += 1

    engine = create_engine(DB_URL)
    with open(SCHEMA, encoding="utf-8") as f:
        ddl = f.read()
    with engine.begin() as conn:
        conn.exec_driver_sql(ddl)

    if "--list" in opts:
        show_current(engine)
        return
    if "--delete-source" in opts:
        with engine.begin() as conn:
            n = conn.execute(text("DELETE FROM site_context WHERE source = :s"), {"s": opts["--delete-source"]}).rowcount
        print(f"Удалено записей: {n}")
        return

    attrs = read_attrs()
    path = pos[0] if pos else SEED_CSV
    explicit = bool(pos)
    defaults = {
        "verification": opts.get("--verification", "unverified" if explicit else "customer_stated"),
        "source": opts.get("--source", os.path.basename(path)),
        "as_of": opts.get("--as-of", dt.date.today().isoformat()),
    }
    rows = read_rows(path, defaults)
    with engine.connect() as conn:
        sites = conn.execute(text("SELECT site_id, name FROM sites")).all()
    errors, items = validate(rows, attrs, sites)
    if errors:
        print("Данные не загружены, найдены ошибки:")
        for e in errors:
            print("  -", e)
        sys.exit(1)

    ins = upd = same = 0
    with engine.begin() as conn:
        for code, a in attrs.items():
            conn.execute(text("""
                INSERT INTO context_attributes (attr_code, label_ru, value_type, allowed, unit, description, sort_order)
                VALUES (:c, :l, :t, :al, :u, :d, :o)
                ON CONFLICT (attr_code) DO UPDATE SET label_ru = EXCLUDED.label_ru, value_type = EXCLUDED.value_type,
                    allowed = EXCLUDED.allowed, unit = EXCLUDED.unit, description = EXCLUDED.description,
                    sort_order = EXCLUDED.sort_order
            """), dict(c=code, l=a["label_ru"], t=a["value_type"], al=a["allowed_list"] or None,
                       u=a["unit"] or None, d=a["description"] or None, o=int(a["sort_order"] or 100)))
        for it in items:
            old = conn.execute(text("""
                SELECT value_text, value_num, verification, source_ref, note FROM site_context
                WHERE site_id = :site_id AND attr_code = :attr_code AND source = :source AND as_of = :as_of
            """), {**it, "as_of": dt.date.fromisoformat(it["as_of"])}).first()
            if old is None:
                ins += 1
            elif (old[0], None if old[1] is None else float(old[1]), old[2], old[3], old[4]) == \
                 (it["value_text"], it["value_num"], it["verification"], it["source_ref"], it["note"]):
                same += 1
                continue
            else:
                upd += 1
            if "--dry-run" in opts:
                continue
            conn.execute(text("""
                INSERT INTO site_context (site_id, attr_code, value_text, value_num, verification, source, source_ref, as_of, note)
                VALUES (:site_id, :attr_code, :value_text, :value_num, :verification, :source, :source_ref, :as_of, :note)
                ON CONFLICT (site_id, attr_code, source, as_of) DO UPDATE SET
                    value_text = EXCLUDED.value_text, value_num = EXCLUDED.value_num,
                    verification = EXCLUDED.verification, source_ref = EXCLUDED.source_ref,
                    note = EXCLUDED.note, loaded_at = now()
            """), {**it, "as_of": dt.date.fromisoformat(it["as_of"])})
    mode = "ПРОВЕРКА (запись не выполнялась)" if "--dry-run" in opts else "Загружено"
    print(f"{mode}: файл {os.path.basename(path)}, verification по умолчанию {defaults['verification']}")
    print(f"  новых: {ins}, обновлено: {upd}, без изменений: {same}")


if __name__ == "__main__":
    main()
