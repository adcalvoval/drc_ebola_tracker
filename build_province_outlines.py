#!/usr/bin/env python3
"""
Dissolve drc_affected_health_zones.geojson by province to generate province
outlines that are guaranteed to align with the health zone layer.

Run whenever drc_affected_health_zones.geojson changes:
    python build_province_outlines.py
"""

import collections
import json
import os

from shapely.geometry import mapping, shape
from shapely.ops import unary_union

_DIR    = os.path.dirname(os.path.abspath(__file__))
HZ_PATH = os.path.join(_DIR, 'data', 'drc_affected_health_zones.geojson')
OUT     = os.path.join(_DIR, 'data', 'drc_affected_province_outlines.geojson')

COORD_PRECISION = 4

def _round_coords(obj):
    """Recursively round all coordinate values in a GeoJSON geometry."""
    if isinstance(obj, (int, float)):
        return round(obj, COORD_PRECISION)
    if isinstance(obj, list):
        return [_round_coords(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _round_coords(v) for k, v in obj.items()}
    return obj

def main():
    with open(HZ_PATH, encoding='utf-8') as f:
        hz = json.load(f)

    by_province = collections.defaultdict(list)
    for feat in hz['features']:
        pname = feat['properties'].get('province', '')
        by_province[pname].append(shape(feat['geometry']))

    features = []
    for pname, geoms in sorted(by_province.items()):
        dissolved = unary_union(geoms)
        features.append({
            'type':       'Feature',
            'geometry':   _round_coords(mapping(dissolved)),
            'properties': {'province': pname},
        })
        print(f'  {pname}: {len(geoms)} zones -> 1 polygon')

    out = {
        'type':     'FeatureCollection',
        'features': features,
        '_source':  'Dissolved from drc_affected_health_zones.geojson (same boundary source)',
    }
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(out, f, separators=(',', ':'), ensure_ascii=False)

    size_kb = os.path.getsize(OUT) / 1024
    print(f'Written: {OUT}  ({size_kb:.0f} KB, {len(features)} provinces)')

if __name__ == '__main__':
    main()
