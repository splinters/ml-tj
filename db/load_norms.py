"""
Загрузка справочника нормативов качества почвы в БД (идемпотентно).

Что делает:
  1. создаёт таблицы и представления из db/schema_site_context.sql и db/schema_norms.sql
     (если их нет; контекст использования нужен для землепользования по складам);
  2. читает три CSV и обновляет справочник (upsert, ничего не удаляется
     из БД, кроме связей профилей, которые пересобираются из norm_profiles.csv):
        db/norm_sources_seed.csv   источники (документы)
        db/norms_seed.csv          значения нормативов
        db/norm_profiles.csv       профили: порядок источников + тип землепользования
  3. проверяет данные до записи (единицы, коды, ссылки на источники, дубликаты);
  4. переключает активный профиль, если указан --profile.

Как добавить норматив, когда он появится:
  - новый документ: строка в norm_sources_seed.csv (source_id, название, орган,
    tier=national|international, реквизиты, ссылка);
  - значения: строки в norms_seed.csv (вещество, значение в мг/кг, форма
    total/mobile, землепользование, тип нормы, статус сверки);
  - включить его в расчёт: добавить source_id в source_order нужного профиля
    (первый в списке имеет приоритет) или создать новый профиль;
  - python db/load_norms.py --profile <profile_id>
  Код модели, скриптов и сайта менять не нужно.

Запуск:
    python db/load_norms.py                       # загрузить/обновить справочник
    python db/load_norms.py --profile customer_stated
    python db/load_norms.py --list                # показать профили и активный
"""
import csv
import os
import sys

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()
DB_URL = os.getenv("DATABASE_URL", "postgresql://soil_ml:soil_ml_dev_password@localhost:5432/soil_ml")

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_CONTEXT = os.path.join(HERE, "schema_site_context.sql")  # нужна представлению v_site_land_use
SCHEMA = os.path.join(HERE, "schema_norms.sql")
SOURCES_CSV = os.path.join(HERE, "norm_sources_seed.csv")
NORMS_CSV = os.path.join(HERE, "norms_seed.csv")
PROFILES_CSV = os.path.join(HERE, "norm_profiles.csv")

BASIS = {"total", "mobile", "acid_extractable"}
LAND_USE = {"any", "agricultural", "residential", "commercial", "industrial"}
PROFILE_LAND_USE = LAND_USE - {"any"}
NORM_TYPE = {"MPC", "ODK", "screening", "guideline", "cleanup"}
VERIFICATION = {"primary", "customer_doc", "secondary", "unverified"}
TIER = {"national", "international"}


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def parse_array(s):
    s = (s or "").strip()
    if not s:
        return None
    return [x.strip() for x in s.strip("{}").replace(",", ";").split(";") if x.strip()]


def validate(sources, norms, profiles):
    errors = []
    source_ids = set()
    for r in sources:
        if not r["source_id"] or not r["title"]:
            errors.append(f"norm_sources: пустой source_id/title: {r}")
        if r["source_id"] in source_ids:
            errors.append(f"norm_sources: дубликат source_id {r['source_id']}")
        source_ids.add(r["source_id"])
        if r["tier"] not in TIER:
            errors.append(f"norm_sources[{r['source_id']}]: tier должен быть {sorted(TIER)}")

    seen = set()
    for i, r in enumerate(norms, start=2):
        where = f"norms_seed.csv, строка {i} ({r.get('source_id')}/{r.get('substance')})"
        if r["source_id"] not in source_ids:
            errors.append(f"{where}: источник не описан в norm_sources_seed.csv")
        try:
            if float(r["value"]) <= 0:
                errors.append(f"{where}: value должен быть > 0")
        except ValueError:
            errors.append(f"{where}: value не число: {r['value']!r}")
        if r.get("unit", "mg/kg") not in ("", "mg/kg"):
            errors.append(f"{where}: поддерживается только mg/kg (нужна конвертация единиц)")
        for field, allowed in (("basis", BASIS), ("land_use", LAND_USE),
                               ("norm_type", NORM_TYPE), ("verification", VERIFICATION)):
            if r[field] not in allowed:
                errors.append(f"{where}: {field}={r[field]!r}, допустимо {sorted(allowed)}")
        key = (r["source_id"], r["substance"], r["basis"], r["land_use"], r["soil_class"], r["norm_type"])
        if key in seen:
            errors.append(f"{where}: дубликат ключа {key}")
        seen.add(key)

    profile_ids = set()
    for r in profiles:
        if r["profile_id"] in profile_ids:
            errors.append(f"norm_profiles: дубликат profile_id {r['profile_id']}")
        profile_ids.add(r["profile_id"])
        if r["land_use"] not in PROFILE_LAND_USE:
            errors.append(f"norm_profiles[{r['profile_id']}]: land_use из {sorted(PROFILE_LAND_USE)}")
        for sid in r["source_order"].split("|"):
            if sid.strip() not in source_ids:
                errors.append(f"norm_profiles[{r['profile_id']}]: источник {sid!r} не описан")
    return errors


def main():
    args = sys.argv[1:]
    engine = create_engine(DB_URL)

    if "--list" in args:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT profile_id, land_use, is_active FROM norm_profiles ORDER BY profile_id")).all()
        for pid, lu, act in rows:
            print(f"{'*' if act else ' '} {pid:20s} {lu}")
        return

    want_profile = args[args.index("--profile") + 1] if "--profile" in args else None

    sources, norms, profiles = read_csv(SOURCES_CSV), read_csv(NORMS_CSV), read_csv(PROFILES_CSV)
    errors = validate(sources, norms, profiles)
    if errors:
        print("Справочник не загружен, найдены ошибки:")
        for e in errors:
            print("  -", e)
        sys.exit(1)

    ddl = ""
    for path in (SCHEMA_CONTEXT, SCHEMA):
        with open(path, encoding="utf-8") as f:
            ddl += f.read() + "\n"

    with engine.begin() as conn:
        conn.exec_driver_sql(ddl)

        for r in sources:
            conn.execute(text("""
                INSERT INTO norm_sources (source_id, title, issuer, jurisdiction, tier, doc_ref, version_date, url, notes)
                VALUES (:source_id, :title, :issuer, :jurisdiction, :tier, :doc_ref, :version_date, :url, :notes)
                ON CONFLICT (source_id) DO UPDATE SET
                    title = EXCLUDED.title, issuer = EXCLUDED.issuer, jurisdiction = EXCLUDED.jurisdiction,
                    tier = EXCLUDED.tier, doc_ref = EXCLUDED.doc_ref, version_date = EXCLUDED.version_date,
                    url = EXCLUDED.url, notes = EXCLUDED.notes
            """), r)

        for r in norms:
            conn.execute(text("""
                INSERT INTO soil_norms (source_id, substance, applies_to, value, basis, land_use,
                                        soil_class, norm_type, verification, note)
                VALUES (:source_id, :substance, :applies_to, :value, :basis, :land_use,
                        :soil_class, :norm_type, :verification, :note)
                ON CONFLICT (source_id, substance, basis, land_use, soil_class, norm_type) DO UPDATE SET
                    applies_to = EXCLUDED.applies_to, value = EXCLUDED.value,
                    verification = EXCLUDED.verification, note = EXCLUDED.note
            """), {**r, "applies_to": parse_array(r.get("applies_to")), "value": float(r["value"])})

        for r in profiles:
            conn.execute(text("""
                INSERT INTO norm_profiles (profile_id, land_use, description)
                VALUES (:profile_id, :land_use, :description)
                ON CONFLICT (profile_id) DO UPDATE SET
                    land_use = EXCLUDED.land_use, description = EXCLUDED.description
            """), r)
            conn.execute(text("DELETE FROM norm_profile_sources WHERE profile_id = :p"), {"p": r["profile_id"]})
            for prio, sid in enumerate(r["source_order"].split("|"), start=1):
                conn.execute(text("""
                    INSERT INTO norm_profile_sources (profile_id, source_id, priority)
                    VALUES (:p, :s, :prio)
                """), {"p": r["profile_id"], "s": sid.strip(), "prio": prio})

        active = conn.execute(text("SELECT profile_id FROM norm_profiles WHERE is_active")).scalar()
        target = want_profile or active or profiles[0]["profile_id"]
        if target not in {r["profile_id"] for r in profiles}:
            raise SystemExit(f"Профиль {target!r} не найден в norm_profiles.csv")
        if target != active:
            conn.execute(text("UPDATE norm_profiles SET is_active = FALSE WHERE is_active"))
            conn.execute(text("UPDATE norm_profiles SET is_active = TRUE WHERE profile_id = :p"), {"p": target})

    print(f"Источников: {len(sources)}, нормативов: {len(norms)}, профилей: {len(profiles)}")
    print(f"Активный профиль: {target}")
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT substance, value, source_id, norm_type, verification FROM v_active_norms ORDER BY substance")).all()
    print("\nНормативы активного профиля (валовое содержание, мг/кг):")
    for s, v, src, t, ver in rows:
        flag = "" if ver in ("primary", "customer_doc") else "   [требует сверки: " + ver + "]"
        print(f"  {s:8s} {float(v):>8g}  {src} ({t}){flag}")


if __name__ == "__main__":
    main()
