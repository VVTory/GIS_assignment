from datetime import date
import json

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from processing.sample_pipeline import NODATA, _doy_layers, _processing_period, _process_modis, _publish_raster, _read_ndvi_for_day, inventory, run


def write_tif(path, data, *, nodata=-3000, scale=1.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", driver="GTiff", height=2, width=3, count=1, dtype=data.dtype, crs="EPSG:4326", transform=from_origin(-98, 37, 0.1, 0.1), nodata=nodata) as target:
        target.write(data, 1)
        target.scales = (scale,)


def test_ndvi_scales_and_masks_water_snow_cloud_and_fill(tmp_path):
    root = tmp_path / "inputs"
    for doy, value in (("2019145", 4000), ("2019161", 6000)):
        raw = np.full((2, 3), value, dtype=np.int16)
        raw[1, 1] = -3000
        qa = np.full((2, 3), 1 << 11, dtype=np.uint16)
        qa[0, 1] = 0  # Water, despite a good mandatory QA flag.
        qa[0, 2] |= 1 << 14
        qa[1, 2] |= 1
        reliability = np.zeros((2, 3), dtype=np.int16)
        reliability[1, 0] = 1
        write_tif(root / "NDVI" / f"MOD13A2_NDVI_doy{doy}.tif", raw, scale=0.0001)
        write_tif(root / "NDVI" / f"MOD13A2_VI_Quality_doy{doy}.tif", qa, nodata=65535)
        write_tif(root / "NDVI" / f"MOD13A2_pixel_reliability_doy{doy}.tif", reliability, nodata=-1)
    aoi = {"features": [{"geometry": {"type": "Polygon", "coordinates": [[[-99, 36], [-97, 36], [-97, 38], [-99, 38], [-99, 36]]]}}]}
    with rasterio.open(root / "NDVI/MOD13A2_NDVI_doy2019145.tif") as template:
        result, information = _read_ndvi_for_day(root, date(2019, 6, 2), template, aoi)
    assert information["weight_after"] == 0.5
    assert information["valid_pixels"] == 1
    assert result[0, 0] == pytest.approx(0.5)
    assert (result != NODATA).sum() == 1


def test_published_raster_converts_nan_to_nodata_and_tiles_small_input(tmp_path):
    source_path = tmp_path / "source.tif"
    write_tif(source_path, np.ones((2, 3), dtype=np.float32))
    values = np.array([[0.2, np.nan, 0.4], [np.nan, 0.6, 0.8]], dtype=np.float32)
    with rasterio.open(source_path) as source:
        asset = _publish_raster(values, source, tmp_path / "release", "ef-test", date(2019, 6, 2), "ef", "1", "test")
    with rasterio.open(tmp_path / "release" / asset["raster_path"]) as result:
        assert result.read(1, masked=True).count() == 4
        assert result.read(1)[0, 1] == NODATA
        assert result.is_tiled
    assert asset["preview_projection"] == "EPSG:3857"
    assert asset["statistics"]["valid_pixels"] == 4
    with rasterio.open(tmp_path / 'release' / asset['map_raster_path']) as display:
        assert display.crs.to_epsg() == 3857
        assert display.tags()['purpose'] == 'display_only_nearest_neighbor'
    with rasterio.open(source_path) as native:
        assert native.crs.to_epsg() == 4326


def test_inventory_reports_corrupt_header_and_optional_checksum(tmp_path):
    source = tmp_path / "inputs"
    source.mkdir()
    (source / "SMAP_L3_SM_P_broken.h5").write_bytes(b"not hdf5")
    (source / "readme.txt").write_text("input metadata")
    manifest = inventory(source, tmp_path / "release", hash_files=True)
    broken = next(item for item in manifest["files"] if item["product"] == "SMAP_H5")
    assert broken["status"] == "invalid"
    other = next(item for item in manifest["files"] if item["product"] == "other")
    assert len(other["sha256"]) == 64


@pytest.mark.parametrize('input_unit', ['K', '°C'])
def test_temperature_publication_values_masks_and_units(tmp_path, input_unit):
    source_path = tmp_path / 'source.tif'
    write_tif(source_path, np.ones((2, 3), dtype=np.float32))
    expected = np.array([[0, -5, NODATA], [26.85, NODATA, 40]], dtype=np.float32)
    data = expected.copy()
    if input_unit == 'K':
        data[data != NODATA] += 273.15
    data[1, 1] = np.nan
    original = data.copy()
    with rasterio.open(source_path) as src:
        asset = _publish_raster(data, src, tmp_path / 'release', 'lst-test', date(2025, 6, 2), 'lst', input_unit, 'test')
    # Publishing must not mutate the Kelvin array later passed to the EF model.
    np.testing.assert_array_equal(data, original)
    assert asset['unit'] == '°C'
    assert asset['statistics']['valid_pixels'] == 4
    assert asset['statistics']['min'] == pytest.approx(-5, abs=2e-5)
    assert asset['statistics']['max'] == pytest.approx(40, abs=2e-5)
    for key in ['raster_path', 'map_raster_path']:
        with rasterio.open(tmp_path / 'release' / asset[key]) as result:
            values = result.read(1, masked=True)
            assert result.tags()['units'] == '°C'
            assert result.units[0] == '°C'
            assert values.count() == 4
            assert np.isfinite(values.compressed()).all()
            if key == 'raster_path':
                np.testing.assert_allclose(values.filled(NODATA), expected, atol=2e-5, rtol=0)
    if input_unit == 'K':
        assert asset['unit_conversion']['formula'] == 'degC = K - 273.15'


def test_temperature_release_conversion_is_idempotent_and_keeps_other_assets(tmp_path):
    from scripts.convert_lst_celsius import convert_release
    source_path = tmp_path / 'source.tif'
    write_tif(source_path, np.full((2, 3), 300, dtype=np.float32))
    asset = {'id': 'lst-test', 'date': '2025-06-02', 'variable': 'lst', 'unit': 'K', 'method_version': 'test', 'status': 'ready', 'raster_path': 'source.tif', 'statistics': {'valid_pixels': 6, 'min': 300, 'max': 300, 'mean': 300}}
    other = {'id': 'ef-test', 'variable': 'ef', 'unit': '1', 'status': 'ready'}
    (tmp_path / 'catalog.json').write_text(json.dumps({'data_version': 'fixture', 'assets': [asset, other]}), encoding='utf-8')
    original = source_path.read_bytes()
    result = convert_release(tmp_path)
    assert result['converted_assets'] == 1
    catalog = json.loads((tmp_path / 'catalog.json').read_text(encoding='utf-8'))
    assert catalog['assets'][1] == other
    assert catalog['assets'][0]['unit'] == '°C'
    assert catalog['assets'][0]['statistics']['mean'] == pytest.approx(26.85)
    assert source_path.read_bytes() == original
    version = catalog['data_version']
    assert convert_release(tmp_path)['status'] == 'already_celsius'
    assert json.loads((tmp_path / 'catalog.json').read_text(encoding='utf-8'))['data_version'] == version


def test_pipeline_rejects_output_inside_raw_directory(tmp_path):
    with pytest.raises(ValueError, match="must not contain each other"):
        run(tmp_path, tmp_path / "release", tmp_path / "unused.geojson")


def test_latest_year_discovery_finds_misfiled_products_and_rejects_duplicates(tmp_path):
    root = tmp_path / "inputs"
    for folder, doy in [("old", "2019152"), ("MOD03", "2025121"), ("temperature", "2025243")]:
        write_tif(root / folder / f"MOD11A1.061_LST_Day_1km_doy{doy}.tif", np.ones((2, 3), dtype=np.int16))
    assert _processing_period(root, None) == (date(2025, 5, 1), date(2025, 8, 31))
    assert _processing_period(root, 2019) == (date(2019, 6, 1), date(2019, 6, 1))
    write_tif(root / "duplicate" / "MOD11A1.061_LST_Day_1km_doy2025121.tif", np.ones((2, 3), dtype=np.int16))
    with pytest.raises(ValueError, match="Ambiguous"):
        _doy_layers(root, "MOD11A1", "LST_Day_1km")


def test_missing_ndvi_does_not_discard_valid_lst(tmp_path):
    root, output = tmp_path / "inputs", tmp_path / "release"
    write_tif(root / "misfiled" / "MOD11A1.061_LST_Day_1km_doy2025153.tif", np.full((2, 3), 15000, dtype=np.int16), nodata=0, scale=0.02)
    write_tif(root / "quality" / "MOD11A1.061_QC_Day_doy2025153.tif", np.zeros((2, 3), dtype=np.int16), nodata=-999)
    aoi = {"features": [{"geometry": {"type": "Polygon", "coordinates": [[[-99, 36], [-97, 36], [-97, 38], [-99, 38], [-99, 36]]]}}]}
    assets, report = _process_modis(root, output, aoi, date(2025, 6, 2), date(2025, 6, 2))
    assert [asset['id'] for asset in assets if asset['status'] == 'ready'] == ['lst-2025-06-02']
    assert assets[0]['unit'] == '°C'
    assert assets[0]['statistics']['mean'] == pytest.approx(26.85)
    assert report[0]['inputs']['ndvi']['status'] == 'not_ready'
    assert report[0]['quality']['lst_valid_after_qa'] == 6


def test_ndvi_still_publishes_on_a_missing_lst_day(tmp_path):
    root, output = tmp_path / 'inputs', tmp_path / 'release'
    write_tif(root / 'MOD03' / 'MOD11A1.061_LST_Day_1km_doy2025152.tif', np.full((2, 3), 15000, dtype=np.int16), nodata=0, scale=0.02)
    for doy in ['2025152', '2025153']:
        write_tif(root / 'quality' / f'MOD11A1.061_QC_Day_doy{doy}.tif', np.zeros((2, 3), dtype=np.int16), nodata=-999)
    for doy, value in [('2025145', 4000), ('2025161', 6000)]:
        write_tif(root / 'other' / f'MOD13A2.061_NDVI_doy{doy}.tif', np.full((2, 3), value, dtype=np.int16), scale=0.0001)
        write_tif(root / 'other' / f'MOD13A2.061_VI_Quality_doy{doy}.tif', np.full((2, 3), 1 << 11, dtype=np.uint16), nodata=65535)
        write_tif(root / 'other' / f'MOD13A2.061_pixel_reliability_doy{doy}.tif', np.zeros((2, 3), dtype=np.int16), nodata=-1)
    aoi = {'features': [{'geometry': {'type': 'Polygon', 'coordinates': [[[-99, 36], [-97, 36], [-97, 38], [-99, 38], [-99, 36]]]}}]}
    assets, report = _process_modis(root, output, aoi, date(2025, 6, 1), date(2025, 6, 2))
    assert {a['id'] for a in assets if a['status'] == 'ready'} == {'lst-2025-06-01', 'ndvi-2025-06-01', 'ndvi-2025-06-02'}
    assert report[1]['reason'] == 'missing_lst_input'
