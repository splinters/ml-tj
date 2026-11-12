"""
Центры 10 складов с измерениями XRF (данные заказчика).

Координаты не хранятся в коде: они читаются из source_data/xrf_warehouses.csv
(столбцы name, lat, lon). Если файла нет, возвращается пустой словарь.
"""
import csv
from pathlib import Path

CSV = Path(__file__).resolve().parent.parent / "source_data" / "xrf_warehouses.csv"


def load_xrf_sites(path=CSV):
    """{название склада: (широта, долгота)}"""
    if not Path(path).exists():
        print(f"ВНИМАНИЕ: нет {path} - связка с XRF-складами пропускается")
        return {}
    with open(path, encoding="utf-8") as f:
        return {r["name"]: (float(r["lat"]), float(r["lon"])) for r in csv.DictReader(f)}


XRF_SITES = load_xrf_sites()
