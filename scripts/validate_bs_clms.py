"""Compare the service burn prediction with Copernicus CLMS Burnt Area (300 m).

CLMS is an independent algorithmic product, coarser than our 20 m output. It
checks detection and the order of magnitude of burned area, not exact edges:
small fires below ~10 ha are invisible to it, and its edges are 300 m blocks.

Two stages, both resumable:
    python scripts/validate_bs_clms.py discover --region configs/regions/krasnodar_krai.geojson \
        --months 2024-03:2024-10 2025-03:2025-10 2026-03:2026-04 --work artifacts/validation-clms
    python scripts/validate_bs_clms.py evaluate --work artifacts/validation-clms \
        --model-config configs/burn_model.yaml --max-events 20 \
        --output experiments/validation/clms20-after.json
"""

from __future__ import annotations

import argparse
import calendar
from dataclasses import replace
from datetime import date, timedelta
import json
import math
import os
from pathlib import Path
import sys
import time

from pyproj import Transformer
from shapely.geometry import box, shape
from shapely.ops import transform, unary_union
from shapely.validation import make_valid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from service.config import Settings  # noqa: E402

CLMS = {'collection_id': 'b8b617c6-182f-427e-a86c-23fc36ac6098', 'min_confidence': 0.8,
        'min_burn_fraction': 0.6, 'min_component_pixels': 3, 'event_window_days': 7}
TILE_DEGREES = 0.5
MAX_SIDE_M = 18_000  # сервис принимает участок до ~20 км по стороне
DETECTED_SHARE = 0.10


def _months(spans: list[str]) -> list[tuple[int, int]]:
    out = []
    for span in spans:
        first, last = span.split(':')
        y, m = map(int, first.split('-'))
        y2, m2 = map(int, last.split('-'))
        while (y, m) <= (y2, m2):
            out.append((y, m))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _metric(geometry):
    lon, lat = geometry.centroid.x, geometry.centroid.y
    epsg = 32600 + int((lon + 180) // 6) + 1 if lat >= 0 else 32700 + int((lon + 180) // 6) + 1
    forward = Transformer.from_crs(4326, epsg, always_xy=True).transform
    back = Transformer.from_crs(epsg, 4326, always_xy=True).transform
    return forward, back


def discover(args) -> None:
    from src.collector.bs_sources import clms_events, fetch_clms

    region = shape(json.loads(args.region.read_text(encoding='utf-8'))['features'][0]['geometry'])
    cache = args.work / 'clms'
    cache.mkdir(parents=True, exist_ok=True)
    west, south, east, north = region.bounds
    batches = []
    for year, month in _months(args.months):
        last = calendar.monthrange(year, month)[1]
        for x in range(math.floor(west / TILE_DEGREES), math.ceil(east / TILE_DEGREES)):
            for y in range(math.floor(south / TILE_DEGREES), math.ceil(north / TILE_DEGREES)):
                bounds = (x * TILE_DEGREES, y * TILE_DEGREES, (x + 1) * TILE_DEGREES, (y + 1) * TILE_DEGREES)
                if region.intersects(box(*bounds)):
                    batches.append({'id': f'{year}-{month:02d}_{x}_{y}', 'bbox': bounds,
                                    'start': f'{year}-{month:02d}-01', 'end': f'{year}-{month:02d}-{last}'})
    done = 0
    for batch in batches:
        path = cache / f"{batch['id']}.json"
        if path.is_file():
            done += 1
            continue
        for attempt in range(4):
            try:
                data, affine = fetch_clms(batch, CLMS)
                break
            except OSError as error:  # сетевые сбои временные: блок повторяется
                if attempt == 3:
                    raise
                print(f"{batch['id']}: {error}; повтор", flush=True)
                time.sleep(5 * (attempt + 1))
        features = clms_events(data, affine, batch, CLMS, region,
                               date.fromisoformat(batch['start']), date.fromisoformat(batch['end']))
        path.write_text(json.dumps(features, ensure_ascii=False), encoding='utf-8')
        done += 1
        if done % 25 == 0:
            print(f'{done}/{len(batches)} блоков', flush=True)
    events = []
    for path in sorted(cache.glob('*.json')):
        events.extend(json.loads(path.read_text(encoding='utf-8')))
    for event in events:
        forward, _ = _metric(shape(event['geometry']))
        event['properties']['area_ha'] = round(transform(forward, shape(event['geometry'])).area / 1e4, 1)
    (args.work / 'events.geojson').write_text(json.dumps(
        {'type': 'FeatureCollection', 'features': events}, ensure_ascii=False), encoding='utf-8')
    print(f'событий CLMS в регионе: {len(events)}; блоков: {len(batches)}')


def evaluate(args) -> None:
    from service.query import QueryError, QuerySpec
    from service.satellite import SatellitePredictor
    from service.sources import DataSourceError

    events = json.loads((args.work / 'events.geojson').read_text(encoding='utf-8'))['features']
    events = [e for e in events if e['properties']['area_ha'] >= args.min_area_ha]
    events.sort(key=lambda e: (-e['properties']['area_ha'], e['id']))
    settings = replace(Settings.from_env(), model_config=args.model_config, query_mode='satellite')
    if args.mode == 'fire-first':
        from service.fire_first import FireFirstPredictor

        predictor = FireFirstPredictor(settings)
        suffix = f'-{args.tag}' if args.tag else ''
        results_path = args.work / f'results-{args.model_config.stem}-fire-first{suffix}.json'
    else:
        predictor = SatellitePredictor(settings)
        results_path = args.work / f'results-{args.model_config.stem}.json'
    results = json.loads(results_path.read_text(encoding='utf-8')) if results_path.is_file() else {}
    attempted = 0
    for event in events:
        if attempted >= args.max_events:
            break
        key = event['id']
        if key in results and 'error' not in results[key]:
            attempted += 1
            continue
        reference = shape(event['geometry'])
        forward, back = _metric(reference)
        projected = transform(forward, reference)
        minx, miny, maxx, maxy = projected.buffer(1000).bounds
        if maxx - minx > MAX_SIDE_M or maxy - miny > MAX_SIDE_M:
            results[key] = {'skipped': 'событие больше участка, который сервис обрабатывает за раз',
                            'reference_ha': event['properties']['area_ha']}
            results_path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding='utf-8')
            continue
        attempted += 1
        area = transform(back, box(minx, miny, maxx, maxy))
        start = date.fromisoformat(event['properties']['start'])
        end = date.fromisoformat(event['properties']['end'])
        if args.mode == 'fire-first':
            # Дата горения в CLMS — оценка продукта: термоточки ищем с запасом в 3 дня.
            start, end = start - timedelta(days=3), end + timedelta(days=3)
        spec = QuerySpec(area=area, date_from=start, date_to=end, source='bbox')
        started = time.time()
        try:
            dataset, prediction = predictor.predict(spec)
        except (QueryError, DataSourceError) as error:
            results[key] = {'error': str(error), 'reference_ha': event['properties']['area_ha']}
            results_path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding='utf-8')
            print(key, 'ошибка:', error, flush=True)
            continue
        # Контуры с разных плиток и степеней могут касаться: make_valid снимает конфликты топологии.
        # В режиме fire-first сравнивается главная цифра сервиса — надёжная площадь;
        # ненадёжная считается отдельно.
        chosen = [burn for burn in dataset.burns if burn.extra.get('reliable', True)]
        unreliable_ha = sum(transform(forward, make_valid(burn.geometry)).area for burn in dataset.burns
                            if not burn.extra.get('reliable', True)) / 1e4
        ours = make_valid(unary_union([make_valid(burn.geometry) for burn in chosen])) \
            .intersection(area) if chosen else None
        ours_m = make_valid(transform(forward, ours)) if ours is not None and not ours.is_empty else None
        ref_m = make_valid(projected)
        inter = ours_m.intersection(ref_m).area / 1e4 if ours_m else 0.0
        union = ours_m.union(ref_m).area / 1e4 if ours_m else ref_m.area / 1e4
        ours_ha = ours_m.area / 1e4 if ours_m else 0.0
        # Вне контура CLMS с запасом в один его пиксель (300 м): кандидаты в ложную гарь.
        outside = ours_m.difference(ref_m.buffer(300)).area / 1e4 if ours_m else 0.0
        # Вся гарь, включая ненадёжную: показывает, что делают фильтры контура, отдельно от
        # пометок надёжности (иначе понижение до «ненадёжно» выглядело бы как потеря гари).
        every = make_valid(unary_union([make_valid(burn.geometry) for burn in dataset.burns]))             .intersection(area) if dataset.burns else None
        every_m = make_valid(transform(forward, every)) if every is not None and not every.is_empty else None
        inter_all = every_m.intersection(ref_m).area / 1e4 if every_m else 0.0
        union_all = every_m.union(ref_m).area / 1e4 if every_m else ref_m.area / 1e4
        all_ha = every_m.area / 1e4 if every_m else 0.0
        results[key] = {
            'reference_ha': round(ref_m.area / 1e4, 1), 'predicted_ha': round(ours_ha, 1),
            'intersection_ha': round(inter, 1), 'iou': round(inter / union, 4) if union else None,
            'reference_covered': round(inter / (ref_m.area / 1e4), 4),
            'detected': inter / (ref_m.area / 1e4) >= DETECTED_SHARE,
            'predicted_outside_reference_ha': round(outside, 1),
            'area_ratio': round(ours_ha / (ref_m.area / 1e4), 3),
            'all_predicted_ha': round(all_ha, 1), 'all_intersection_ha': round(inter_all, 1),
            'all_iou': round(inter_all / union_all, 4) if union_all else None,
            'all_reference_covered': round(inter_all / (ref_m.area / 1e4), 4),
            'all_area_ratio': round(all_ha / (ref_m.area / 1e4), 3),
            'observed_fraction': prediction.get('observed_fraction'),
            'status': prediction.get('status'), 'unreliable_ha': round(unreliable_ha, 1),
            'events': [{k: e.get(k) for k in ('event_id', 'status', 'reason', 'reliability', 'reliability_reasons',
                                              'burn_area_ha', 'fire_evidence', 'pair', 'harvest_check',
                                              'buffer_edge', 'area_per_point_ha')}
                       for e in prediction.get('events', [])],
            'query_area_ha': round(transform(forward, area).area / 1e4, 1),
            'event': {'start': str(start), 'end': str(end),
                      'centre': [round(reference.centroid.x, 4), round(reference.centroid.y, 4)]},
            'scenes': prediction['scenes'], 'seconds': round(time.time() - started, 1),
        }
        results_path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding='utf-8')
        r = results[key]
        print(f"{key}: CLMS {r['reference_ha']} га, наша {r['predicted_ha']} га, "
              f"покрыто {r['reference_covered']:.0%}, вне CLMS {r['predicted_outside_reference_ha']} га, "
              f"видно {r['observed_fraction']}", flush=True)

    rows = [r for r in results.values() if 'iou' in r]
    def total(name):
        return sum(r.get(name) or 0 for r in rows)

    def median(name):
        import statistics
        values = [r[name] for r in rows if r.get(name) is not None]
        return round(statistics.median(values), 4) if values else None
    summary = {
        'model_config': args.model_config.as_posix(), 'mode': args.mode,
        'compared': 'reliable burns only' if args.mode == 'fire-first' else 'all burns',
        'reference': 'Copernicus CLMS Burnt Area 300 m monthly v4 (CP>=0.8, BF>=0.6)',
        'events_evaluated': len(rows),
        'events_skipped_or_failed': len(results) - len(rows),
        'detected': sum(r['detected'] for r in rows),
        'reference_ha': round(total('reference_ha'), 1),
        'predicted_ha': round(total('predicted_ha'), 1),
        'intersection_ha': round(total('intersection_ha'), 1),
        'pooled_iou': round(total('intersection_ha') / (total('reference_ha') + total('predicted_ha')
                                                        - total('intersection_ha')), 4) if rows else None,
        'pooled_reference_covered': round(total('intersection_ha') / total('reference_ha'), 4) if rows else None,
        'predicted_outside_reference_ha': round(total('predicted_outside_reference_ha'), 1),
        'median_area_ratio': median('area_ratio'), 'median_iou': median('iou'),
        'all_burns': {
            'predicted_ha': round(total('all_predicted_ha'), 1),
            'intersection_ha': round(total('all_intersection_ha'), 1),
            'pooled_iou': round(total('all_intersection_ha') / (total('reference_ha') + total('all_predicted_ha')
                                                                - total('all_intersection_ha')), 4) if rows else None,
            'pooled_reference_covered': round(total('all_intersection_ha') / total('reference_ha'), 4) if rows else None,
            'median_area_ratio': median('all_area_ratio'), 'median_iou': median('all_iou'),
        },
        'caveat': ('CLMS is 300 m and misses small fires; area outside it is not proven false. '
                   'IoU is limited by the 300 m reference edges.'),
        'events': results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in summary.items() if k != 'events'}, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='stage', required=True)
    first = sub.add_parser('discover')
    first.add_argument('--region', type=Path, required=True)
    first.add_argument('--months', nargs='+', required=True, help='YYYY-MM:YYYY-MM')
    first.add_argument('--work', type=Path, required=True)
    second = sub.add_parser('evaluate')
    second.add_argument('--work', type=Path, required=True)
    second.add_argument('--model-config', type=Path, required=True)
    second.add_argument('--max-events', type=int, default=20)
    second.add_argument('--min-area-ha', type=float, default=100.0)
    second.add_argument('--output', type=Path, required=True)
    second.add_argument('--tag', default='', help='метка прогона: свой файл результатов (например, before/after)')
    second.add_argument('--mode', choices=['whole-area', 'fire-first'], default='fire-first',
                        help='fire-first — как сервис сейчас; whole-area — модель по всему участку (прежний режим)')
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    os.environ.setdefault('CDSE_CLIENT_ID', settings.cdse_client_id)
    os.environ.setdefault('CDSE_CLIENT_SECRET', settings.cdse_client_secret)
    discover(args) if args.stage == 'discover' else evaluate(args)


if __name__ == '__main__':
    main()
