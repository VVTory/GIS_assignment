"""Migrate an installed Kelvin LST release without overwriting source rasters.

All new files are staged and verified before switching the published catalog.
Running again on a Celsius release is a no-op. EF inputs remain Kelvin.
"""
from copy import deepcopy
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sys
from uuid import uuid4

import numpy as np
import rasterio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from processing.sample_pipeline import LST_CONVERSION, LST_UNIT, LST_UNIT_VERSION, NODATA, _publish_raster


def write_json(path, document):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def convert_release(root: Path):
    root = root.resolve()
    catalog_path = root / 'catalog.json'
    blob = catalog_path.read_bytes()
    catalog = json.loads(blob)
    temperatures = [a for a in catalog['assets'] if a.get('variable') == 'lst']
    if not temperatures:
        raise ValueError('No published temperature assets found.')
    if all(a.get('unit') == LST_UNIT for a in temperatures):
        return {'status': 'already_celsius', 'assets': len(temperatures), 'data_version': catalog['data_version']}
    if any(a.get('unit') != 'K' for a in temperatures):
        raise ValueError('Unexpected or mixed LST units; conversion stopped before any file changes.')

    # Immutable new paths let requests using the old catalog finish safely.
    staged = root / 'unit_releases' / (LST_UNIT_VERSION + '-' + uuid4().hex[:10])
    staged.mkdir(parents=True, exist_ok=False)
    (staged / 'source_catalog.json').write_bytes(blob)
    report_path = root / 'processing_report.json'
    report = json.loads(report_path.read_text(encoding='utf-8')) if report_path.is_file() else None
    if report_path.is_file():
        (staged / 'source_processing_report.json').write_bytes(report_path.read_bytes())

    updated, checks = deepcopy(catalog), []
    for item in updated['assets']:
        if item.get('variable') != 'lst':
            continue
        source_path = (root / item['raster_path']).resolve()
        source_path.relative_to(root)
        with rasterio.open(source_path) as src:
            data = src.read(1, masked=True)
            old_mask = np.ma.getmaskarray(data) | ~np.isfinite(data.data)
            values = np.where(old_mask, NODATA, data.data)
            result = _publish_raster(values, src, staged, item['id'], date.fromisoformat(item['date']), 'lst', 'K', item['method_version'])
        with rasterio.open(staged / result['raster_path']) as dst:
            new = dst.read(1, masked=True)
            assert np.array_equal(old_mask, np.ma.getmaskarray(new)), item['id']
            expected = data.data[~old_mask].astype(np.float64) - 273.15
            error = float(np.max(np.abs(new.data[~old_mask] - expected))) if expected.size else 0.0
            assert error < 1e-5, (item['id'], error)
            assert dst.tags()['units'] == LST_UNIT and dst.units[0] == LST_UNIT
        if item.get('map_raster_path'):
            with rasterio.open(root / item['map_raster_path']) as old_map, rasterio.open(staged / result['map_raster_path']) as new_map:
                assert (old_map.crs, old_map.transform, old_map.shape) == (new_map.crs, new_map.transform, new_map.shape)
                before, after = old_map.read(1, masked=True), new_map.read(1, masked=True)
                assert np.array_equal(np.ma.getmaskarray(before), np.ma.getmaskarray(after)), item['id']
                valid = ~np.ma.getmaskarray(before)
                assert np.allclose(after.data[valid], before.data[valid].astype(np.float64) - 273.15, rtol=0, atol=1e-5)
        for key in ('raster_path', 'map_raster_path', 'preview_path'):
            result[key] = (staged / result[key]).relative_to(root).as_posix()
        assert result['statistics']['valid_pixels'] == item['statistics']['valid_pixels']
        checks.append({'asset_id': item['id'], 'valid_pixels': result['statistics']['valid_pixels'], 'max_error_celsius': error, 'old_range_k': [item['statistics']['min'], item['statistics']['max']], 'new_range_celsius': [result['statistics']['min'], result['statistics']['max']]})
        item.update(result)
        print('converted ' + item['id'], flush=True)

    previous_version = catalog['data_version']
    new_version = previous_version + '-' + LST_UNIT_VERSION
    updated['data_version'] = new_version
    updated.setdefault('provenance', {}).update(temperature_units=LST_CONVERSION.copy(), previous_data_version=previous_version)
    summary = {'status': 'passed', 'converted_at_utc': datetime.now(timezone.utc).isoformat(), 'data_version': new_version, 'previous_data_version': previous_version, 'converted_assets': len(checks), 'conversion': LST_CONVERSION, 'source_catalog': (staged / 'source_catalog.json').relative_to(root).as_posix(), 'source_files_preserved': True, 'all_masks_preserved': True, 'other_assets_unchanged': True, 'checks': checks}
    write_json(staged / 'conversion_verification.json', summary)
    # Refuse to publish if another process changed the release during staging.
    if catalog_path.read_bytes() != blob:
        raise RuntimeError('Catalog changed during conversion; staged outputs were not published.')
    if report is not None:
        report.update(data_version=new_version, temperature_units=LST_CONVERSION.copy(), temperature_conversion_report=(staged / 'conversion_verification.json').relative_to(root).as_posix())
        write_json(report_path, report)
    write_json(catalog_path, updated)
    return summary


if __name__ == '__main__':
    result = convert_release(ROOT / 'data' / 'sample')
    write_json(ROOT / 'reports' / 'lst-celsius-conversion.json', result)
    print(json.dumps({k: v for k, v in result.items() if k != 'checks'}, ensure_ascii=False, indent=2))
