"""Build the persistent thermal source mask (flares, industry) for a region.

Reads the last N months of VIIRS detections from NASA FIRMS for the region,
marks places that burn on many different days in several different months,
and writes them with the region outline (role "coverage") to GeoJSON. The
service excludes FIRMS points near these places before looking for burns.

The mask for Krasnodar Krai and Adygea is in the repository
(configs/persistent-sources/); rebuild it about once a year. The service also
builds a missing mask on first start (service/persistent_mask.py).

Run from the repository root:
    python scripts/build_persistent_sources.py --region configs/regions/krasnodar_adygea.geojson \
        --output configs/persistent-sources/krasnodar_adygea.geojson
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from service.config import DEFAULT_SERVICE_CONFIG, PROJECT_ROOT, Settings  # noqa: E402
from service.fire_evidence import load_service_config  # noqa: E402
from service.persistent_mask import build_mask  # noqa: E402


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--region', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--service-config', type=Path, default=DEFAULT_SERVICE_CONFIG)
    parser.add_argument('--end-date', help='последний день истории, по умолчанию вчера (UTC)')
    args = parser.parse_args(argv)
    end = datetime.fromisoformat(args.end_date).date() if args.end_date else None
    summary = build_mask(args.region, args.output, load_service_config(args.service_config),
                         Settings.from_env().firms_map_key, PROJECT_ROOT / 'artifacts' / 'firms-cache', end)
    print(f"история {summary['history_from']}..{summary['history_to']}: {summary['detections']} детекций, "
          f"наборы {summary['sources']}, сбоев запросов {summary['failed_requests']}")
    print(f"постоянных источников: {summary['persistent_sources']} → {args.output}")


if __name__ == '__main__':
    main()
