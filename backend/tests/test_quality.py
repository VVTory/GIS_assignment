import importlib
import json
import numpy as np
import pytest
import rasterio
from fastapi.testclient import TestClient
from rasterio.transform import from_origin
from app.quality import lst_codes, ndvi_codes, quality_point
from shapely.geometry import box

main = importlib.import_module('app.main')


def test_lst_cloud_requires_explicit_flag_and_missing_is_distinct():
    raw = np.array([[300, 0, 300, 300, 300, 300]])
    qc = np.array([[0, 2, 65, -999, 128, 64]])
    assert lst_codes(raw, 0, qc, -999).tolist() == [[1, 2, 3, 5, 3, 1]]


def test_ndvi_cloud_composite_surface_and_missing_classes():
    raw = np.array([[5000, 0, 5000, 5000, 5000, 5000]])
    qa = np.array([[2048, 2048, 0, 2049, -999, 2048]])
    rel = np.array([[0, 3, 0, 1, 0, 2]])
    assert ndvi_codes(raw, -3000, qa, -999, rel, -1).tolist() == [[1, 2, 4, 3, 5, 4]]


@pytest.fixture
def release(tmp_path, monkeypatch):
    root, raw = tmp_path / 'release', tmp_path / 'raw'
    root.mkdir(); raw.mkdir()
    monkeypatch.setattr(main, 'DATA_ROOT', root); monkeypatch.setattr(main, 'RAW_ROOT', raw)
    transform = from_origin(-98, 37, 1, 1)
    def write(path, data, nodata):
        with rasterio.open(path, 'w', driver='GTiff', height=2, width=3, count=1, dtype='float32', crs='EPSG:4326', transform=transform, nodata=nodata) as ds:
            ds.write(np.array(data, dtype='float32'), 1)
    write(raw / 'source.tif', [[300, 0, 300], [300, 0, 300]], 0)
    write(raw / 'qc.tif', [[0, 2, 65], [0, -999, 0]], -999)
    write(root / 'out.tif', [[25, -9999, -9999], [25, -9999, -9999]], -9999)
    asset = {'id':'lst-fixture', 'date':'2025-05-01', 'variable':'lst', 'unit':'°C', 'status':'ready', 'raster_path':'out.tif'}
    (root / 'catalog.json').write_text(json.dumps({'data_version':'quality-test','assets':[asset]}))
    (root / 'processing_report.json').write_text(json.dumps({'modis_dates':[{'date':'2025-05-01','inputs':{'lst':'source.tif','lst_qc':'qc.tif'}}]}))
    (root / 'aoi.geojson').write_text(json.dumps({'type':'FeatureCollection','features':[{'type':'Feature','geometry':{'type':'Polygon','coordinates':[[[-98,35],[-95,35],[-95,37],[-98,37],[-98,35]]]}}]}))
    return root, raw, write


def test_quality_counts_point_image_and_published_mask_authority(release):
    client = TestClient(main.app)
    doc = client.get('/api/v1/assets/lst-fixture/quality').json()
    assert doc['total_pixels'] == 6
    assert doc['valid_pixels'] == 2
    assert {r['code']:r['count'] for r in doc['legend']} == {1:2,2:1,3:1,4:0,5:1,6:1}
    assert sum(x['count'] for x in doc['legend']) == 6
    assert len(doc['image_coordinates']) == 4
    point = client.get('/api/v1/assets/lst-fixture/quality/point?lon=-96.5&lat=36.5').json()
    assert point['key'] == 'cloud'
    image = client.get(doc['image_url'])
    assert image.status_code == 200 and image.content.startswith(b'\x89PNG')
    assert client.get('/api/v1/assets/lst-fixture/quality/point?lon=-99&lat=36').json()['key'] == 'outside'
    assert client.get('/api/v1/assets/lst-fixture/quality/point?lon=181&lat=36').status_code == 422
    assert client.get('/api/v1/assets/lst-fixture/quality?url=external').status_code == 422


def test_raw_change_invalidates_quality_cache(release):
    root, raw, write = release
    client = TestClient(main.app)
    before = client.get('/api/v1/assets/lst-fixture/quality').json()
    write(raw / 'qc.tif', [[0, -999, 65], [0, -999, 0]], -999)
    after = client.get('/api/v1/assets/lst-fixture/quality').json()
    assert next(x['count'] for x in before['legend'] if x['code'] == 2) == 1
    assert next(x['count'] for x in after['legend'] if x['code'] == 2) == 0


def test_missing_raw_qa_does_not_invent_clouds(release):
    root, raw, _ = release
    (raw / 'qc.tif').unlink()
    doc = TestClient(main.app).get('/api/v1/assets/lst-fixture/quality').json()
    assert doc['detailed_qa_available'] is False
    assert next(x['count'] for x in doc['legend'] if x['code'] == 2) == 0
    assert next(x['count'] for x in doc['legend'] if x['code'] == 5) == 4


def test_native_grid_footprint_does_not_classify_points_outside_study_area():
    metadata = {'asset_id':'smap', 'date':'2025-06-01', 'notes':[], 'source_dates':[],
                'geojson':{'features':[{'geometry':box(-98.1,35.9,-97.9,36.1).__geo_interface__,
                                         'properties':{'quality_code':3}}]}}
    result = {'metadata':metadata, 'domain':box(-98,35,-96,37)}
    assert quality_point(result, -98.05, 36)['key'] == 'outside'
    assert quality_point(result, -97.95, 36)['key'] == 'low_quality'
