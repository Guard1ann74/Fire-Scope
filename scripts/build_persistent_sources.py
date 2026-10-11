"""Build the persistent thermal source mask (flares, industry) for a region.

Reads the last N months of VIIRS detections from NASA FIRMS for the region,
marks places that burn on many different days in several different months,
and writes them with the region outline (role "coverage") to GeoJSON. The
service excludes FIRMS points near these places before looking for burns.

Run from the repository root:
    python scripts/build_persistent_sources.py --region configs/regions/krasnodar_krai.geojson \
        --output artifacts/persistent-sources/krasnodar_krai.geojson
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

from shapely.geometry import Point, mapping, shape

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from service.config import DEFAULT_SERVICE_CONFIG, PROJECT_ROOT, Settings  # noqa: E402
from service.fire_evidence import FirmsArchive, load_service_config, persistent_cells  # noqa: E402


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--region', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--service-config', type=Path, default=DEFAULT_SERVICE_CONFIG)
    parser.add_argument('--end-date', help='последний день истории, по умолчанию вчера (UTC)')
    args = parser.parse_args(argv)

    config = load_service_config(args.service_config)
    params = config['persistent_sources']
    region = shape(json.loads(args.region.read_text(encoding='utf-8'))['features'][0]['geometry'])
    end = (datetime.fromisoformat(args.end_date).date() if args.end_date
           else datetime.now(timezone.utc).date() - timedelta(days=1))
    start = end - timedelta(days=round(30.44 * params['history_months']))
    archive = FirmsArchive(Settings.from_env().firms_map_key, PROJECT_ROOT / 'artifacts' / 'firms-cache',
                           config['evidence']['platforms'], config['evidence']['window_days'])
    points, sources, failures = archive.points(region.bounds, start, end)
    points = [p for p in points if region.contains(Point(p.lon, p.lat))]
    cells = persistent_cells(points, params['cell_m'], params['min_days'], params['min_months'],
                             params.get('require_winter', True))
    features = [{'type': 'Feature', 'geometry': mapping(region),
                 'properties': {'role': 'coverage', 'history_from': start.isoformat(),
                                'history_to': end.isoformat(), 'sources': sources,
                                'detections': len(points), 'failed_requests': len(failures),
                                'params': params}}]
    features += [{'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [round(c['lon'], 5), round(c['lat'], 5)]},
                  'properties': {'role': 'persistent_source', 'days': c['days'], 'months': c['months'],
                                 'detections': c['detections']}} for c in cells]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({'type': 'FeatureCollection', 'features': features}, ensure_ascii=False),
                           encoding='utf-8')
    print(f'история {start}..{end}: {len(points)} детекций, наборы {sources}, сбоев запросов {len(failures)}')
    print(f'постоянных источников: {len(cells)} → {args.output}')


if __name__ == '__main__':
    main()
