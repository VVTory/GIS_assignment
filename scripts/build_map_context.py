"""Clip cached Natural Earth vectors for the local study-area basemap."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from shapely.geometry import Point, box, mapping, shape
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
VERSION = 'v5.1.2'


def build(cache: Path, output: Path):
    study_document = json.loads((ROOT / 'data/sample/aoi.geojson').read_text(encoding='utf-8'))
    study = unary_union([shape(f['geometry']) for f in study_document['features']])
    west, south, east, north = study.bounds
    extent = box(west - 4, south - 4, east + 4, north + 4)
    features, sources = [], []

    def read(name):
        path = cache / (name + '.geojson')
        payload = path.read_bytes()
        doc = json.loads(payload)
        sources.append({
            'dataset': name, 'source_version': VERSION,
            'upstream': 'https://github.com/nvkelso/natural-earth-vector/blob/' + VERSION + '/geojson/' + path.name,
            'sha256': hashlib.sha256(payload).hexdigest(),
        })
        return doc['features']

    def feature(geometry, kind, **properties):
        if geometry.is_empty:
            return
        features.append({'type': 'Feature', 'properties': {'kind': kind, **properties}, 'geometry': mapping(geometry)})

    for kind, suffix in [('river', 'rivers_lake_centerlines'), ('lake', 'lakes')]:
        detailed = 'ne_10m_' + suffix
        dataset = detailed if (cache / (detailed + '.geojson')).is_file() else 'ne_50m_' + suffix
        for item in read(dataset):
            geometry = shape(item['geometry'])
            if not geometry.intersects(extent):
                continue
            if not geometry.is_valid:
                geometry = geometry.buffer(0)
            clipped = geometry.intersection(extent).simplify(.001, preserve_topology=True)
            feature(clipped, kind, name=item['properties'].get('name_en') or item['properties'].get('name') or '')

    places = [item for item in read('ne_10m_populated_places_simple')
              if extent.covers(shape(item['geometry']))
              and (item['properties'].get('pop_max', 0) >= 20000 or item['properties'].get('featurecla') == 'Admin-1 capital')]
    city_positions = [shape(item['geometry']) for item in places]
    boundaries = []
    for item in read('ne_50m_admin_1_states_provinces'):
        properties = item['properties']
        if properties.get('admin') != 'United States of America':
            continue
        geometry = shape(item['geometry'])
        if not geometry.intersects(extent):
            continue
        boundaries.append(geometry.boundary.intersection(extent))
        anchor_area = geometry.intersection(study)
        if anchor_area.is_empty or anchor_area.area < .05:
            anchor_area = geometry.intersection(extent)
        anchor = anchor_area.representative_point()
        if geometry.intersects(study) and anchor_area.bounds[3] - anchor_area.bounds[1] < .75:
            # A narrow strip, such as Kansas, reads best near its center and
            # stays clear of the date badge on a phone.
            centroid = anchor_area.centroid
            if anchor_area.covers(centroid):
                anchor = centroid
        elif geometry.intersects(study) and city_positions:
            left, bottom, right, top = anchor_area.bounds
            candidates = [Point(left + (right - left) * x, bottom + (top - bottom) * y)
                          for x in [.25, .5, .75] for y in [.25, .5, .75]]
            candidates = [point for point in candidates if anchor_area.covers(point)]
            if candidates:
                # Leave room around city names instead of letting a city hide
                # the state label. The anchor remains inside the actual state.
                anchor = max(candidates, key=lambda point: min(.65 * (point.x - city.x) ** 2 + (point.y - city.y) ** 2 for city in city_positions))
        english = properties.get('name_en') or properties['name']
        local = properties.get('name_zh')
        label = local + chr(10) + english.upper() if local else english.upper()
        feature(anchor, 'state-label', name=english, label=label, rank=0 if geometry.intersects(study) else 1)
    feature(unary_union(boundaries).simplify(.001), 'administration')

    for item in places:
        geometry = shape(item['geometry'])
        p = item['properties']
        if not extent.covers(geometry) or (p.get('pop_max', 0) < 20000 and p.get('featurecla') != 'Admin-1 capital'):
            continue
        feature(geometry, 'place', name=p['name'], label=p['name'], rank=p.get('labelrank', 10), population=p.get('pop_max', 0))

    document = {'type': 'FeatureCollection', 'bbox': list(extent.bounds), 'features': features}
    # Stable coordinate precision keeps the regional file compact.
    def rounded(value):
        if isinstance(value, float):
            return round(value, 5)
        if isinstance(value, (tuple, list)):
            return [rounded(v) for v in value]
        if isinstance(value, dict):
            return {k: rounded(v) for k, v in value.items()}
        return value

    output.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(rounded(document), ensure_ascii=False, separators=(',', ':'))
    (output / 'sgp-context.geojson').write_text(payload, encoding='utf-8')
    report = {
        'created_at_utc': datetime.now(timezone.utc).isoformat(),
        'extent': list(extent.bounds), 'study_bounds': list(study.bounds),
        'source': 'Natural Earth', 'version': VERSION, 'sources': sources,
        'download_mirrors_used': ['cdn.jsdelivr.net', 'fastly.jsdelivr.net'],
        'features_by_kind': dict(Counter(f['properties']['kind'] for f in features)),
        'bytes': len(payload.encode('utf-8')),
        'processing': 'WGS84 regional clipping; topology-preserving simplification at .001 degrees; coordinates rounded to 5 decimals. State label anchors use the intersection with the study area where available.',
        'scope': 'Generalized geographic context for orientation; never an analytical boundary, pixel mask or scientific data source.',
    }
    (output / 'sources.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k != 'sources'}, ensure_ascii=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache', type=Path, default=ROOT / 'tmp/basemap-downloads')
    parser.add_argument('--output', type=Path, default=ROOT / 'frontend/public/basemap')
    args = parser.parse_args()
    build(args.cache, args.output)
