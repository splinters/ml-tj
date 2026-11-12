"""
Забирает НАСТОЯЩИЙ растр pH почвы для всего Таджикистана из SoilGrids WCS
(не 10 точечных значений по складам, как раньше - полноценная поверхность).

Разрешение намеренно снижено (350x200 точек на всю страну) - нативные 250м
SoilGrids дали бы ~2800x2000 точек, это медленный WCS-запрос и тяжёлый
файл ради превью-слоя на карте. Если понадобится точный анализ - берите
полное разрешение через тот же soilgrids.get_coverage_data() с бОльшими
width/height.

pip install soilgrids
Запуск: python db/fetch_ph_raster.py
"""
import os
from soilgrids import SoilGrids

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_TIF = os.path.join(HERE, "tjk_ph.tif")

BBOX = dict(west=67.0, south=36.4, east=75.3, north=41.1)  # Таджикистан с запасом


def main():
    if os.path.exists(OUT_TIF):
        print(f"pH-растр уже скачан: {OUT_TIF} (удалите файл, чтобы перекачать заново)")
        return

    print("Запрашиваю pH (0-5см, среднее) из SoilGrids WCS для всего Таджикистана...")
    print("Это может занять минуту-две - сервис ISRIC не самый быстрый.")
    sg = SoilGrids()
    try:
        sg.get_coverage_data(
            service_id="phh2o",
            coverage_id="phh2o_0-5cm_mean",
            west=BBOX["west"], south=BBOX["south"], east=BBOX["east"], north=BBOX["north"],
            crs="urn:ogc:def:crs:EPSG::4326",
            width=350, height=200,
            output=OUT_TIF,
        )
    except Exception as e:
        raise SystemExit(
            f"Не удалось получить растр из SoilGrids WCS ({e}).\n"
            f"Проверьте вручную: https://maps.isric.org/ - сервис иногда лежит под нагрузкой,\n"
            f"попробуйте позже, или уменьшите width/height в этом скрипте."
        )

    print(f"Сохранено: {OUT_TIF}")
    print("Значения в файле - pH*10 (так отдаёт SoilGrids) - учтено при отрисовке слоя в app.py.")


if __name__ == "__main__":
    main()
