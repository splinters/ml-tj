"""
Ранжирование 10 складов по приоритету (п.3 ТЗ): какие площадки хуже
других и должны разбираться в первую очередь.

Композитный индекс, не одно вещество (Pb): берём "доверенные" элементы -
устоявшийся набор приоритетных загрязнителей для складов ядохимикатов:
Pb, As, Hg, Zn. Rb и Co НЕ включены - по данным проекта они сильно
различают склады (см. scan_all_substances.py), но без заключения
геохимика неясно, это загрязнение или природный геохимический фон
(см. methodology-and-learnings.md) - включать их в приоритизацию
"что устранять в первую очередь" преждевременно.

Индекс на точку = среднее по log(value) - log(общестрановая медиана по
этому веществу среди ВСЕХ 10 складов), т.е. насколько точка выше/ниже
типичного уровня по всей выборке (не по своему складу - здесь специально
нужна абсолютная, межскладовая шкала, а не site-normalized target модели).
Индекс на склад = медиана по точкам склада.

ВАЖНО: это ОТНОСИТЕЛЬНОЕ ранжирование внутри уже известных 10 складов
(кто хуже кого в этой выборке), НЕ абсолютная оценка риска для здоровья
против национальных ПДК/нормативов Таджикистана - те в проект не
загружены. Если у заказчика есть официальные нормативы почв, сравнение
надо делать против них, а не только друг против друга - стоит уточнить
у заказчика, есть ли такие нормативы.

Запуск: python db/rank_sites_priority.py
"""
import os
import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.getenv("DATABASE_URL", "postgresql://soil_ml:soil_ml_dev_password@localhost:5432/soil_ml")
engine = create_engine(DB_URL)

TRUSTED_SUBSTANCES = ["Pb", "As", "Hg", "Zn"]


def main():
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT sam.sample_id, sam.site_id, m.substance, m.value, m.below_lod
            FROM measurements m JOIN samples sam ON sam.sample_id = m.sample_id
            WHERE m.substance = ANY(:subs)
        """), conn, params={"subs": TRUSTED_SUBSTANCES})

    if df.empty:
        raise SystemExit(f"Нет данных по веществам {TRUSTED_SUBSTANCES} - проверьте названия в measurements.substance.")

    parts = []
    for subst in TRUSTED_SUBSTANCES:
        sub = df[df.substance == subst].copy()
        if sub.empty:
            print(f"[!] Нет данных по {subst} - пропускаю")
            continue
        lod_fallback = sub.loc[sub.value.notna(), "value"].min() / 2 if sub.value.notna().any() else 0.01
        sub["value_for_model"] = sub["value"].fillna(lod_fallback)
        sub["log_value"] = np.log(sub["value_for_model"])
        global_median_log = sub["log_value"].median()
        sub["log_excess"] = sub["log_value"] - global_median_log
        parts.append(sub[["sample_id", "site_id", "log_excess"]].assign(substance=subst))

    combined = pd.concat(parts, ignore_index=True)

    point_index = (combined.groupby(["sample_id", "site_id"])["log_excess"]
                   .mean().reset_index(name="composite_index"))

    print("=" * 100)
    print(f"КОМПОЗИТНЫЙ ИНДЕКС ПО ВЕЩЕСТВАМ: {', '.join(TRUSTED_SUBSTANCES)}")
    print("(log-превышение над общестрановой медианой; >0 = хуже среднего по выборке, <0 = лучше)")
    print("=" * 100)

    site_rank = (point_index.groupby("site_id")["composite_index"]
                 .agg(["median", "mean", "max", "count"])
                 .sort_values("median", ascending=False)
                 .reset_index())
    site_rank.columns = ["site_id", "median_index", "mean_index", "max_index", "n_points"]
    site_rank.insert(0, "priority_rank", range(1, len(site_rank) + 1))

    print(site_rank.round(3).to_string(index=False))

    print("""
КАК ЧИТАТЬ: priority_rank=1 - склад с самым высоким медианным композитным
индексом, то есть в среднем сильнее других превышает общестрановой
уровень по доверенным веществам. max_index показывает наличие отдельных
"горячих" точек даже там, где медиана невысокая - смотрите оба столбца,
не только ранг по медиане.
""")

    print("=" * 100)
    print("РАЗБИВКА ПО ВЕЩЕСТВАМ (медиана log_excess по складу)")
    print("=" * 100)
    breakdown = combined.groupby(["site_id", "substance"])["log_excess"].median().unstack()
    breakdown = breakdown.reindex(site_rank.site_id)
    print(breakdown.round(2).to_string())

    with engine.begin() as conn:
        conn.execute(text("""
            ALTER TABLE sites
                ADD COLUMN IF NOT EXISTS priority_rank INT,
                ADD COLUMN IF NOT EXISTS priority_composite_index NUMERIC
        """))
        for _, row in site_rank.iterrows():
            conn.execute(text("""
                UPDATE sites SET priority_rank = :rank, priority_composite_index = :idx
                WHERE site_id = :site_id
            """), {"site_id": row.site_id, "rank": int(row.priority_rank), "idx": float(row.median_index)})

    print("\nРанг и индекс записаны в sites.priority_rank / sites.priority_composite_index.")
    print("Следующий шаг: build_risk_zones.py - тем же композитным индексом, но нормированным "
          "ВНУТРИ каждого склада, для выделения зон концентрации загрязнения на карте.")


if __name__ == "__main__":
    main()
