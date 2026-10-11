"""Compute the real demo cases once so the service answers them instantly.

The service caches fire-first results on disk; this script runs the same code
for every case in configs/demo_cases.yaml and writes a short summary.

    python scripts/warm_demo_cases.py --output experiments/validation/demo-cases.json
"""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys
import time

import yaml
from shapely.geometry import box

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from service.config import Settings  # noqa: E402
from service.fire_first import FireFirstPredictor  # noqa: E402
from service.query import QuerySpec  # noqa: E402


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    cases = yaml.safe_load((settings.service_config.parent / 'demo_cases.yaml').read_text(encoding='utf-8'))['cases']
    predictor = FireFirstPredictor(settings)
    summary = {}
    for case in cases:
        started = time.time()
        spec = QuerySpec(box(*case['bbox']), date.fromisoformat(case['from']), date.fromisoformat(case['to']), 'bbox')
        try:
            dataset, prediction = predictor.predict(spec)
        except Exception as error:  # один сбой сети не должен останавливать остальные случаи
            summary[case['id']] = {'error': f'{type(error).__name__}: {error}'}
            print(case['id'], 'ОШИБКА', error, flush=True)
            continue
        events = prediction.get('events', [])
        summary[case['id']] = {
            'status': prediction['status'], 'message': prediction['message'],
            'events': len(events), 'events_by_status': {s: sum(e['status'] == s for e in events)
                                                         for s in sorted({e['status'] for e in events})},
            'reliable_ha': prediction.get('burn_area_reliable_ha'),
            'unreliable_ha': prediction.get('burn_area_unreliable_ha'),
            'landcover_reliable_ha': (prediction.get('burn_by_landcover_ha') or {}).get('reliable'),
            'seconds': round(time.time() - started, 1), 'cached': 'cached_at' in prediction,
            'event_details': [{
                'event_id': e['event_id'], 'status': e['status'], 'reliability': e.get('reliability'),
                'burn_area_ha': e.get('burn_area_ha'), 'gap_days': (e.get('pair') or {}).get('gap_days'),
                'pre_fire_check': e.get('pre_fire_check'), 'post_fire_check': e.get('post_fire_check'),
                'reliability_reasons': e.get('reliability_reasons'), 'harvest_check': e.get('harvest_check'),
                'buffer_edge': e.get('buffer_edge'), 'area_per_point_ha': e.get('area_per_point_ha'),
                'n_points': (e.get('fire_evidence') or {}).get('n_points'),
                'weather': {k: (e.get('weather') or {}).get(k) for k in
                            ('at', 'temperature_c', 'humidity_pct', 'wind_from', 'wind_speed_ms',
                             'danger_class', 'nesterov', 'source', 'error')},
            } for e in events if e['status'] != 'not_processed'],
        }
        print(case['id'], json.dumps(summary[case['id']], ensure_ascii=False), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
