"""Снимки события для проверки глазами: Sentinel-2 до/после и контур гари поверх.

Используются те же снимки, по которым сервис считал гарь (сохраняются при
расчёте в annotation-scenes/<ключ>/bs) — ничего не скачивается заново и не
дорисовывается: эксперт видит ровно то, что видела модель.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import numpy as np
from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform

from service.sources import Dataset

# Каналы сохранённой сцены: B2,B3,B4,B5,B6,B7,B8A,B11,B12,SCL.
B2, B3, B4, B8A, B12, SCL = 0, 1, 2, 6, 8, 9
SEVERITY_COLORS = {1: "#ffe14d", 2: "#ff8c1a", 3: "#ff2d2d"}
SEVERITY_NAMES = {1: "слабая", 2: "средняя", 3: "сильная"}
MARGIN_M = 500
HARVEST_RGBA = (0.85, 0.3, 1.0, 0.55)  # пиксели, вырезанные проверкой «убрано или выжжено»


class PreviewError(LookupError):
    """У события нет снимков: картинку построить не из чего."""


def render_event(dataset: Dataset, prediction: dict[str, Any], event_id: str, cache_dir: Path,
                 cloud_classes: list[int]) -> bytes:
    """PNG 2×2: до и после в естественных цветах, после в SWIR, после с контуром и точками огня."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch
    import rasterio

    event = next((e for e in prediction.get("events", []) if e["event_id"] == event_id), None)
    if event is None:
        raise PreviewError(f"Событие {event_id} не найдено в ответе")
    tiles = [s["tile"] for s in prediction.get("scenes", []) if s.get("event_id") == event_id]
    burns = [b for b in dataset.burns if b.extra.get("event_id") == event_id]
    key = prediction.get("result_key") or next((b.extra["prediction_key"] for b in burns), None)
    if not tiles or not key or not event.get("pair"):
        raise PreviewError("Для события нет снимков: " + (event.get("message") or event["status"]))
    root = Path(cache_dir) / "annotation-scenes" / key / "bs"

    # Сборка плиток события в одну картинку: плитки лежат в одной UTM-зоне на сетке 20 м.
    rasters = []
    for tile in tiles:
        pre_path, post_path = root / "sentinel2_pre" / f"{tile}_pre.tif", root / "sentinel2_post" / f"{tile}_post.tif"
        if not pre_path.is_file() or not post_path.is_file():
            raise PreviewError("Снимки события не сохранены на диске; повторите запрос")
        with rasterio.open(pre_path) as pre_src, rasterio.open(post_path) as post_src:
            # Что вырезала проверка «убрано или выжжено» (есть у расчётов после её появления).
            harvest_path = root.parent / "filtered" / f"{tile}_harvest.tif"
            harvest_tile = None
            if harvest_path.is_file():
                with rasterio.open(harvest_path) as harvest_src:
                    harvest_tile = harvest_src.read(1).astype(bool)
            rasters.append((pre_src.bounds, pre_src.crs, pre_src.res[0], pre_src.read(), post_src.read(),
                            harvest_tile))
    crs, gsd = rasters[0][1], rasters[0][2]
    to_utm = Transformer.from_crs(4326, crs, always_xy=True).transform
    buffer = transform(to_utm, shape(event["buffer"]))
    left, bottom, right, top = buffer.bounds
    left, bottom, right, top = left - MARGIN_M, bottom - MARGIN_M, right + MARGIN_M, top + MARGIN_M
    width, height = int(round((right - left) / gsd)), int(round((top - bottom) / gsd))
    pre = np.zeros((10, height, width), np.float32)
    post = np.zeros_like(pre)
    covered = np.zeros((height, width), bool)
    harvested = np.zeros((height, width), bool)
    for bounds, _, _, pre_tile, post_tile, harvest_tile in rasters:
        col, row = int(round((bounds.left - left) / gsd)), int(round((top - bounds.top) / gsd))
        rows, cols = pre_tile.shape[1:]
        r0, c0 = max(row, 0), max(col, 0)
        r1, c1 = min(row + rows, height), min(col + cols, width)
        if r0 >= r1 or c0 >= c1:
            continue
        pre[:, r0:r1, c0:c1] = pre_tile[:, r0 - row:r1 - row, c0 - col:c1 - col]
        post[:, r0:r1, c0:c1] = post_tile[:, r0 - row:r1 - row, c0 - col:c1 - col]
        covered[r0:r1, c0:c1] = True
        if harvest_tile is not None:
            harvested[r0:r1, c0:c1] |= harvest_tile[r0 - row:r1 - row, c0 - col:c1 - col]
    if not covered.any():
        raise PreviewError("Снимки события не пересекают его буфер")
    # Обрезка до снятой части: за краем плиток снимков нет.
    rows, cols = np.flatnonzero(covered.any(axis=1)), np.flatnonzero(covered.any(axis=0))
    pre, post = pre[:, rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1], post[:, rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]
    covered = covered[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]
    harvested = harvested[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]
    left, top = left + cols[0] * gsd, top - rows[0] * gsd
    height, width = covered.shape

    def pixel(x: float, y: float) -> tuple[float, float]:
        return (x - left) / gsd, (top - y) / gsd

    def stretch(bands: list[int]) -> tuple[np.ndarray, np.ndarray]:
        # Одна растяжка для «до» и «после»: изменение яркости остаётся видимым.
        values = np.concatenate([pre[bands][:, covered], post[bands][:, covered]], axis=1)
        low, high = np.percentile(values, 2, axis=1), np.percentile(values, 98, axis=1)
        scale = lambda stack: np.clip(np.moveaxis(
            (stack - low[:, None, None]) / np.maximum(high - low, 1)[:, None, None], 0, -1), 0, 1)
        return scale(pre[bands]), scale(post[bands])

    pre_rgb, post_rgb = stretch([B4, B3, B2])
    _, post_swir = stretch([B12, B8A, B4])
    cloudy = covered & (np.isin(pre[SCL], cloud_classes) | np.isin(post[SCL], cloud_classes))

    pair = event["pair"]
    evidence = event["fire_evidence"]
    ink, paper = "#ececec", "#16181c"
    panel_w = 6.4
    fig, axes = plt.subplots(2, 2, figsize=(2 * panel_w + 0.6, 2 * panel_w * height / width + 2.6),
                             squeeze=False, facecolor=paper,
                             gridspec_kw={"hspace": 0.12, "wspace": 0.03,
                                          "left": 0.01, "right": 0.99, "top": 0.88, "bottom": 0.09})
    panels = [
        (axes[0][0], pre_rgb, f"До: {pair['pre']} · естественные цвета · точки огня"),
        (axes[0][1], post_rgb, f"После: {pair['post']} · естественные цвета"),
        (axes[1][0], post_swir, f"После: {pair['post']} · SWIR (B12-B8A-B4), гарь тёмная"),
        (axes[1][1], post_rgb, "После + контур гари сервиса"),
    ]
    for axis, image, title in panels:
        axis.imshow(np.where(covered[..., None], image, 0.1), interpolation="nearest")
        axis.set_title(title, fontsize=10, color=ink, pad=4)
        axis.set_xlim(0, width)
        axis.set_ylim(height, 0)
        axis.axis("off")
    marked = axes[1][1]
    if cloudy.any():
        overlay = np.zeros((height, width, 4))
        overlay[cloudy] = (0.55, 0.75, 1.0, 0.5)
        marked.imshow(overlay, interpolation="nearest")
    if harvested.any():
        overlay = np.zeros((height, width, 4))
        overlay[harvested] = HARVEST_RGBA
        marked.imshow(overlay, interpolation="nearest")
    for burn in burns:
        polygon = transform(to_utm, burn.geometry)
        for part in getattr(polygon, "geoms", [polygon]):
            for ring in [part.exterior, *part.interiors]:
                xs, ys = zip(*(pixel(x, y) for x, y in ring.coords))
                marked.plot(xs, ys, color=SEVERITY_COLORS[burn.severity], lw=1.0)
    points = [h for h in dataset.hotspots if h.extra.get("event_id") == event_id]
    xy = [pixel(*to_utm(h.geometry.x, h.geometry.y)) for h in points]
    for axis in (axes[0][0], marked):
        if xy:
            axis.scatter(*zip(*xy), s=20, c="#00e5ff", edgecolors="black", linewidths=0.5, zorder=5)
    xs, ys = zip(*(pixel(x, y) for x, y in buffer.exterior.coords))
    marked.plot(xs, ys, color="white", lw=0.8, ls="--")
    for axis in axes.flat:  # масштабная линейка 1 км
        x0, y0 = width * 0.04, height * 0.95
        axis.plot([x0, x0 + 1000 / gsd], [y0, y0], color="white", lw=3, solid_capstyle="butt")
        axis.text(x0, y0 - height * 0.015, "1 км", color="white", fontsize=9)

    handles = [Line2D([], [], color=SEVERITY_COLORS[s], lw=2, label=f"гарь: {SEVERITY_NAMES[s]}") for s in (1, 2, 3)]
    handles += [Line2D([], [], marker="o", ls="", markerfacecolor="#00e5ff", markeredgecolor="black",
                       label="точка огня VIIRS (пиксель 375 м)"),
                Line2D([], [], color="white", ls="--", label="буфер 3 км: где искалась гарь"),
                Patch(color=(0.55, 0.75, 1.0, 0.6), label="облака и тени: не оценивалось")]
    if harvested.any():
        handles.append(Patch(color=HARVEST_RGBA, label="убрано проверкой потемнения: посветлело — уборка или дымка, не гарь"))
    fig.legend(handles=handles, loc="lower center", ncol=3, fontsize=9, frameon=False, labelcolor=ink)
    area = event.get("burn_area_ha")
    reliability = {"reliable": "надёжная оценка", "unreliable": "ненадёжная оценка"}.get(event.get("reliability"), "")
    status = {"ok": f"гарь {area} га ({reliability})", "no_scar": "огонь есть, гари в местах детекций не видно"}
    fig.suptitle(
        f"{evidence['n_points']} {_points_word(evidence['n_points'])} огня NASA FIRMS {evidence['first_detected']} — {evidence['last_detected']} "
        f"(FRP до {evidence.get('max_frp_mw') or '—'} МВт) · {status.get(event['status'], event['status'])}\n"
        f"Sentinel-2 L2A (Copernicus Data Space): {pair['pre']} → {pair['post']}, разрыв {pair.get('gap_days', '—')} дн. "
        f"Те же снимки, по которым сервис считал гарь. Событие {event_id}",
        fontsize=10.5, color=ink)
    output = io.BytesIO()
    fig.savefig(output, format="png", dpi=90, facecolor=paper)
    plt.close(fig)
    return output.getvalue()


def _points_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "точка"
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return "точки"
    return "точек"
