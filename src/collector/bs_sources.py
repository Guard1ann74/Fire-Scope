"""Open burned-area discovery. CLMS extents are coarse evidence, not 20 m truth."""
from __future__ import annotations

from datetime import date, timedelta
import hashlib
import json
import math
from urllib.request import Request, urlopen

import numpy as np
from rasterio.features import shapes
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds
from scipy.ndimage import label
from shapely.geometry import mapping, shape
from shapely.ops import unary_union

from src.collector.burn_severity import PROCESS_URL, _token

CLMS_DOC = ('https://documentation.dataspace.copernicus.eu/APIs/SentinelHub/Data/clms/'
            'bio-geophysical-parameters/vegetation/burnt-area/ba_global_300m_monthly_v4.html')


def fetch_clms(batch, source):
    bounds = batch['bbox']
    width = max(1, math.ceil((bounds[2]-bounds[0])*360))
    height = max(1, math.ceil((bounds[3]-bounds[1])*360))
    payload = {
        'input': {'bounds': {'bbox': bounds, 'properties': {'crs': 'http://www.opengis.net/def/crs/EPSG/0/4326'}},
                  'data': [{'type': 'byoc-' + source['collection_id'],
                            'dataFilter': {'timeRange': {'from': batch['start']+'T00:00:00Z', 'to': batch['end']+'T23:59:59Z'}},
                            'processing': {'upsampling': 'NEAREST', 'downsampling': 'NEAREST'}}]},
        'output': {'width': width, 'height': height,
                   'responses': [{'identifier': 'default', 'format': {'type': 'image/tiff'}}]},
        'evalscript': "//VERSION=3\nfunction setup(){return {input:[{bands:['DOB','CP','BF','dataMask']}],"
                      "output:{bands:4,sampleType:'FLOAT32'}};}\n"
                      "function evaluatePixel(s){return [s.DOB,s.CP,s.BF,s.dataMask];}",
    }
    request = Request(PROCESS_URL, data=json.dumps(payload).encode(), method='POST',
                      headers={'Authorization': 'Bearer '+_token(), 'Content-Type': 'application/json'})
    with urlopen(request, timeout=60) as response:
        content = response.read()
    with MemoryFile(content) as memory, memory.open() as raster:
        expected = from_bounds(*bounds, width, height)
        if (raster.count != 4 or raster.width != width or raster.height != height
                or raster.crs is None or raster.crs.to_epsg() != 4326
                or not raster.transform.almost_equals(expected)):
            raise ValueError('Unexpected CLMS grid')
        return raster.read(), raster.transform


def clms_events(data, affine, batch, source, aoi, start, end):
    dob, confidence, fraction, observed = data
    # CLMS BYOC returns stored integers; CP and BF have a documented scale 1/1000.
    confidence, fraction = confidence / 1000., fraction / 1000.
    year = date.fromisoformat(batch['start']).year
    origin = date(year, 1, 1)
    month_start = (date.fromisoformat(batch['start'])-origin).days + 1
    month_end = (date.fromisoformat(batch['end'])-origin).days + 1
    valid = ((observed > 0) & (dob >= month_start) & (dob <= month_end)
             & (confidence >= source['min_confidence']) & (fraction >= source['min_burn_fraction']))
    features = []
    window = source['event_window_days']
    for week in sorted(set(((dob[valid].astype(int)-1)//window).tolist())):
        components, count = label(valid & ((dob.astype(int)-1)//window == week))
        for component in range(1, count+1):
            mask = components == component
            if mask.sum() < source['min_component_pixels']:
                continue
            first = origin + timedelta(days=int(dob[mask].min())-1)
            last = origin + timedelta(days=int(dob[mask].max())-1)
            if first < start or last > end:
                continue
            geometry = unary_union([shape(g) for g, value in shapes(mask.astype(np.uint8), mask=mask, transform=affine) if value])
            geometry = geometry.intersection(aoi)
            if geometry.is_empty or geometry.area == 0:
                continue
            key = hashlib.sha256(f'{batch["id"]}:{week}:{component}'.encode()).hexdigest()[:16]
            features.append({'type': 'Feature', 'id': 'clms-'+key, 'geometry': mapping(geometry),
                             'properties': {'event_id': 'clms-'+key, 'start': str(first), 'end': str(last),
                                            'source_url': CLMS_DOC, 'source': 'clms_ba300m_monthly_v4',
                                            'license': 'Copernicus free use with attribution', 'resolution_m': 300,
                                            'confidence_min': float(confidence[mask].min()),
                                            'burn_fraction_min': float(fraction[mask].min()),
                                            'batch': batch['id']}})
    return features
