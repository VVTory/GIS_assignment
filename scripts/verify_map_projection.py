"""Compare the installed GeoServer tiles to the local display projection."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import warnings

import httpx
import numpy as np
import rasterio
from rasterio.errors import NotGeoreferencedWarning
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds
from rasterio.warp import reproject, transform, Resampling

from backend.app.main import DATA_ROOT, GEOSERVER_URL
from backend.app.styles import rgba, style_for

warnings.filterwarnings('ignore', category=NotGeoreferencedWarning)


def decode_png(blob):
    with MemoryFile(blob) as memory:
        with memory.open() as image:
            pixels = image.read()
            if image.count == 1:
                colors = image.colormap(1)
                # Indexed PNGs may contain fewer than 256 palette entries.
                lookup = np.zeros((256, 4), dtype=np.uint8)
                assert all(int(index) in colors for index in np.unique(pixels[0]))
                for index, color in colors.items():
                    lookup[index] = color
                return lookup[pixels[0]]
            if image.count == 4:
                return pixels.transpose(1, 2, 0)
            raise ValueError('Expected indexed or RGBA PNG')


def main():
    catalog = json.loads((DATA_ROOT / 'catalog.json').read_text(encoding='utf-8'))
    rasters = [a for a in catalog['assets'] if a.get('raster_path')]
    specs = {a['variable']: style_for(a['variable'], catalog['assets']) for a in rasters}
    checks = []
    with httpx.Client(timeout=45, trust_env=False, auth=('admin', 'geoserver')) as client:
        def check(asset):
            with rasterio.open(DATA_ROOT / asset['raster_path']) as native:
                values = native.read(1, masked=True)
                rows, cols = np.where(~np.ma.getmaskarray(values))
                row, col = int(rows[len(rows) // 2]), int(cols[len(cols) // 2])
                x, y = native.xy(row, col)
                xs, ys = transform(native.crs, 'EPSG:3857', [x], [y])
            z, half = 8, 20037508.342789244
            span = 2 * half / 2**z
            tx, ty = int((xs[0] + half) / span), int((half - ys[0]) / span)
            west, north = -half + tx * span, half - ty * span
            destination = np.full((256, 256), np.nan, dtype=np.float32)
            with rasterio.open(DATA_ROOT / asset['map_raster_path']) as source:
                assert source.crs.to_epsg() == 3857
                reproject(source.read(1, masked=True).filled(np.nan), destination,
                          src_transform=source.transform, src_crs=source.crs, src_nodata=np.nan,
                          dst_transform=from_bounds(west, north-span, west+span, north, 256, 256),
                          dst_crs='EPSG:3857', dst_nodata=np.nan, resampling=Resampling.nearest)
            expected = rgba(destination, np.isfinite(destination), specs[asset['variable']])
            tile = f"/gwc/service/tms/1.0.0/webgis:{asset['id']}@EPSG:900913@png/{z}/{tx}/{2**z-1-ty}.png"
            response = client.get(GEOSERVER_URL + tile)
            response.raise_for_status()
            actual = decode_png(response.content)
            a, b = actual[:, :, 3] > 0, expected[:, :, 3] > 0
            both, union = a & b, a | b
            overlap = float(both.sum() / union.sum()) if union.any() else 0
            error = float(np.abs(actual[:, :, :3].astype(float) - expected[:, :, :3]).mean(axis=2)[both].mean()) if both.any() else None
            metadata = client.get(GEOSERVER_URL + f"/rest/workspaces/webgis/coveragestores/{asset['id']}/coverages/{asset['id']}.json")
            metadata.raise_for_status()
            declared = metadata.json()['coverage'].get('srs')
            return {'asset': asset['id'], 'zxy': [z, tx, ty], 'declared_crs': declared,
                    'nontransparent_pixels': int(a.sum()), 'mask_overlap_iou': overlap, 'mean_rgb_error': error,
                    'passed': declared == 'EPSG:3857' and overlap >= 0.95 and error is not None and error <= 5}

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(check, asset) for asset in rasters]
            for future in as_completed(futures):
                result = future.result()
                checks.append(result)
                if len(checks) % 50 == 0 or not result['passed']:
                    print('Projection checked', len(checks), '/', len(rasters), result if not result['passed'] else '', flush=True)
    checks.sort(key=lambda item: item['asset'])
    report = {'checked_at_utc': datetime.now(timezone.utc).isoformat(), 'data_version': catalog['data_version'],
              'raster_count': len(checks), 'passed': all(c['passed'] for c in checks),
              'method': 'For every layer, compare a z8 GeoWebCache tile containing a valid native pixel against an independent nearest-neighbor render of its EPSG:3857 display raster. Require alpha-mask IoU >= .95 and mean RGB error <= 5/255.',
              'checks': checks}
    target = Path(__file__).resolve().parents[1] / 'reports/data-refresh-20261115-map-projection.json'
    target.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: report[k] for k in ['raster_count', 'passed', 'data_version']}), flush=True)
    if not report['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
