"""Сравнить прогоны CLMS на одном и том же наборе пожаров.

Прогоны `validate_bs_clms.py evaluate --tag …` могут отличаться составом: лимит событий
и пропуски слишком больших пожаров меняют, какие пожары попали в выборку. Здесь метрики
считаются заново только по пожарам, оценённым во всех переданных прогонах, и только если
ни в одном из них не было сбоя Copernicus («источник недоступен»).

    python scripts/compare_clms_runs.py experiments/validation/clms20-before.json \
        experiments/validation/clms20-after.json experiments/validation/clms20-rel.json
"""

from __future__ import annotations

import json
from pathlib import Path
import statistics
import sys


def failed(row: dict) -> bool:
    return any(event.get("status") == "source_unavailable" for event in row.get("events", []))


def metrics(rows: list[dict], prefix: str) -> dict:
    reference = sum(r["reference_ha"] for r in rows)
    predicted = sum(r[f"{prefix}predicted_ha"] for r in rows)
    intersection = sum(r[f"{prefix}intersection_ha"] for r in rows)
    ratios = [r[f"{prefix}predicted_ha"] / r["reference_ha"] for r in rows]
    return {"iou": round(intersection / (reference + predicted - intersection), 4),
            "covered": round(intersection / reference, 4),
            "median_ratio": round(statistics.median(ratios), 3),
            "predicted_ha": round(predicted, 1)}


def main(paths: list[str]) -> None:
    runs = {Path(p).stem: json.loads(Path(p).read_text(encoding="utf-8"))["events"] for p in paths}
    common = set.intersection(*({k for k, r in events.items() if "iou" in r} for events in runs.values()))
    bad = {k for events in runs.values() for k in common if failed(events[k])}
    keys = sorted(common - bad)
    print(f"пожаров в сравнении: {len(keys)}" + (f"; исключены из-за сбоев сети: {sorted(bad)}" if bad else ""))
    for name, events in runs.items():
        rows = []
        for key in keys:
            row = dict(events[key])
            row["intersection_ha"] = row["intersection_ha"]
            row["all_intersection_ha"] = row.get("all_intersection_ha", row["intersection_ha"])
            row["all_predicted_ha"] = row.get("all_predicted_ha", row["predicted_ha"])
            rows.append(row)
        reliable, every = metrics(rows, ""), metrics(rows, "all_")
        print(f"{name}: надёжная IoU {reliable['iou']}, охват {reliable['covered']:.1%}, "
              f"площадь/CLMS {reliable['median_ratio']}, {reliable['predicted_ha']} га | "
              f"вся IoU {every['iou']}, охват {every['covered']:.1%}, площадь/CLMS {every['median_ratio']}, "
              f"{every['predicted_ha']} га")


if __name__ == "__main__":
    main(sys.argv[1:])
