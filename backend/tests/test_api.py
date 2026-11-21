import importlib
import json

import numpy as np
import pytest
import rasterio
from fastapi.testclient import TestClient
from rasterio.transform import from_origin
from app.main import app

client = TestClient(app)
main_module = importlib.import_module("app.main")


@pytest.fixture(autouse=True)
def isolated_release(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "DATA_ROOT", tmp_path)
    return tmp_path


@pytest.fixture
def sample_release(isolated_release):
    root = isolated_release
    with rasterio.open(root / "test.tif", "w", driver="GTiff", height=2, width=2, count=1, dtype="float32", crs="EPSG:4326", transform=from_origin(-98, 37, 0.5, 0.5), nodata=-9999) as dst:
        dst.write(np.array([[0.4, -9999], [0.6, 0.8]], dtype=np.float32), 1)
    (root / "test.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    features = {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [-97.5, 36.5]}, "properties": {"soil_moisture": 0.31, "quality_flag": 0}}]}
    (root / "smap.geojson").write_text(json.dumps(features))
    assets = [
        {"id": "ef-test", "date": "2019-06-05", "variable": "ef", "unit": "1", "status": "ready", "raster_path": "test.tif", "preview_path": "test.png"},
        {"id": "smap-test", "date": "2019-06-05", "variable": "soil_moisture", "unit": "m3/m3", "status": "ready", "geojson_path": "smap.geojson"},
        {"id": "excluded-test", "status": "not_ready"},
    ]
    (root / "catalog.json").write_text(json.dumps({"data_version": "fixture", "assets": assets}))
    return root

def test_empty_catalog_and_live_health():
    assert client.get("/health/live").json()["status"] == "ok"
    catalog = client.get("/api/v1/catalog").json()
    assert catalog["assets"] == [] and catalog["status"] == "empty"

def test_query_bounds_and_unknown_asset():
    assert client.get("/api/v1/rasters/x/point?lon=181&lat=0").status_code == 422
    assert client.get("/api/v1/rasters/x/point?lon=-97&lat=36").status_code == 404

def test_statistics_rejects_unclosed_or_out_of_range_geometry():
    request = {"asset_ids": ["x"], "geometry": {"type": "Polygon", "coordinates": [[[-97, 36], [-96, 36], [-96, 37], [-97, 37]]]}}
    assert client.post("/api/v1/statistics", json=request).status_code == 422
    request["geometry"]["coordinates"][0][-1] = [181, 36]
    assert client.post("/api/v1/statistics", json=request).status_code == 422


def test_catalog_serves_only_published_files_without_local_paths(sample_release):
    response = client.get("/api/v1/catalog").json()
    assert response["status"] == "ready" and len(response["assets"]) == 2
    assert "raster_path" not in response["assets"][0]
    assert response["assets"][1]["point_url"].endswith("/vectors/smap-test/point")
    assert client.get(response["assets"][0]["preview_url"]).headers["content-type"] == "image/png"
    assert client.get("/api/v1/rasters/excluded-test").status_code == 409


def test_raster_point_value_nodata_and_outside(sample_release):
    valid = client.get("/api/v1/rasters/ef-test/point?lon=-97.75&lat=36.75").json()
    assert valid["valid"] and valid["value"] == pytest.approx(0.4)
    assert client.get("/api/v1/rasters/ef-test/point?lon=-97.25&lat=36.75").json()["reason"] == "nodata"
    assert client.get("/api/v1/rasters/ef-test/point?lon=0&lat=0").json()["reason"] == "outside_raster"


def test_smap_nearest_native_cell_and_distance_limit(sample_release):
    value = client.get("/api/v1/vectors/smap-test/point?lon=-97.5&lat=36.5").json()
    assert value["value"] == 0.31 and value["distance_m"] == 0
    assert value["matched_coordinates"] == [-97.5, 36.5]
    assert client.get("/api/v1/vectors/smap-test/point?lon=-99&lat=35").json()["valid"] is False


def test_series_uses_release_and_filters_dates(sample_release):
    result = client.get("/api/v1/series?lon=-97.75&lat=36.75&variable=ef").json()
    assert result["status"] == "ready" and result["points"][0]["value"] == pytest.approx(0.4)
    assert client.get("/api/v1/series?lon=-97.75&lat=36.75&start=2019-06-06").json()["points"] == []


def test_catalog_cannot_publish_files_outside_release(sample_release):
    (sample_release / "catalog.json").write_text(json.dumps({"assets": [{"id": "unsafe", "status": "ready", "preview_path": "../outside.png"}]}))
    assert client.get("/api/v1/rasters/unsafe/preview").status_code == 404


def test_statistics_and_coverage_count_missing_pixels_and_keep_valid_zero(sample_release):
    polygon = {"type": "Polygon", "coordinates": [[[-98, 36], [-97, 36], [-97, 37], [-98, 37], [-98, 36]]]}
    (sample_release / "aoi.geojson").write_text(json.dumps({"features": [{"geometry": polygon}]}))
    with rasterio.open(sample_release / "test.tif", "r+") as target:
        target.write(np.array([[0, -9999], [0.6, 0.8]], dtype=np.float32), 1)
    response = client.post("/api/v1/statistics", json={"asset_ids": ["ef-test"], "geometry": polygon})
    assert response.status_code == 200, response.text
    data = response.json()["results"][0]
    assert data["total_pixels"] == 4 and data["valid_pixels"] == 3
    assert data["valid_fraction"] == 0.75 and data["min"] == 0
    assert data["mean"] == pytest.approx(1.4 / 3)
    assert data["std"] == pytest.approx(np.std([0, 0.6, 0.8]))
    assert client.get("/api/v1/assets/ef-test/coverage").json()["valid_fraction"] == 0.75


def test_statistics_rejects_self_intersection_and_outside_domain(sample_release):
    polygon = {"type": "Polygon", "coordinates": [[[-98, 36], [-97, 37], [-98, 37], [-97, 36], [-98, 36]]]}
    assert client.post("/api/v1/statistics", json={"asset_ids": ["ef-test"], "geometry": polygon}).status_code == 422
    domain = {"type": "Polygon", "coordinates": [[[-98, 36], [-97, 36], [-97, 37], [-98, 37], [-98, 36]]]}
    (sample_release / "aoi.geojson").write_text(json.dumps({"features": [{"geometry": domain}]}))
    polygon["coordinates"] = [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]
    assert client.post("/api/v1/statistics", json={"asset_ids": ["ef-test"], "geometry": polygon}).status_code == 422


def test_series_has_calendar_gaps_and_download_is_real_file(sample_release):
    response = client.get("/api/v1/series?lon=-97.75&lat=36.75&variable=ef&start=2019-06-04&end=2019-06-06").json()
    assert len(response["points"]) == 3
    assert response["points"][0]["value"] is None
    assert response["points"][1]["value"] == pytest.approx(.4)
    assert response["points"][2]["reason"] == "no_published_asset"
    download = client.get("/api/v1/assets/ef-test/download")
    assert download.status_code == 200 and download.content == (sample_release / "test.tif").read_bytes()
    render = client.get("/api/v1/rasters/ef-test/render")
    assert render.status_code == 200 and render.content.startswith(b"\x89PNG")


def test_vector_statistics_does_not_invent_regional_missing_denominator(sample_release):
    polygon = {"type": "Polygon", "coordinates": [[[-98, 36], [-97, 36], [-97, 37], [-98, 37], [-98, 36]]]}
    response = client.post("/api/v1/statistics", json={"asset_ids": ["smap-test"], "geometry": polygon}).json()["results"][0]
    assert response["valid_pixels"] == 1 and response["mean"] == 0.31
    assert response["valid_fraction"] is None and response["total_pixels"] is None


def test_all_nodata_statistics_returns_null_values(sample_release):
    with rasterio.open(sample_release / "test.tif", "r+") as target:
        target.write(np.full((2, 2), -9999, dtype=np.float32), 1)
    polygon = {"type": "Polygon", "coordinates": [[[-98, 36], [-97, 36], [-97, 37], [-98, 37], [-98, 36]]]}
    result = client.post("/api/v1/statistics", json={"asset_ids": ["ef-test"], "geometry": polygon}).json()["results"][0]
    assert result["valid_pixels"] == 0 and result["valid_fraction"] == 0
    assert result["mean"] is None and result["std"] is None


def test_internal_raster_mask_agrees_in_point_and_statistics(sample_release):
    with rasterio.open(sample_release / "test.tif", "r+") as target:
        target.write_mask(np.array([[0, 0], [255, 255]], dtype=np.uint8))
    point = client.get("/api/v1/rasters/ef-test/point?lon=-97.75&lat=36.75").json()
    assert point["valid"] is False and point["reason"] == "nodata"
    polygon = {"type": "Polygon", "coordinates": [[[-98, 36], [-97, 36], [-97, 37], [-98, 37], [-98, 36]]]}
    result = client.post("/api/v1/statistics", json={"asset_ids": ["ef-test"], "geometry": polygon}).json()["results"][0]
    assert result["total_pixels"] == 4 and result["valid_pixels"] == 2
    assert result["mean"] == pytest.approx(0.7)


def test_variable_palettes_and_native_smap_grid_polygons(sample_release):
    from app.styles import PALETTES, STYLE_VERSION
    assert set(PALETTES) == {"ndvi", "lst", "soil_moisture", "ef"}
    assert len({tuple(p["colors"]) for p in PALETTES.values()}) == 4
    assert client.get("/api/v1/styles").json()["ndvi"]["version"] == STYLE_VERSION
    grid = client.get("/api/v1/vectors/smap-test/grid")
    assert grid.status_code == 200
    feature = grid.json()["features"][0]
    assert feature["geometry"]["type"] == "Polygon"
    assert feature["properties"]["footprint_status"] == "approximate_native_extent"
    assert feature["properties"]["center"] == [-97.5, 36.5]


def test_enhanced_smap_grid_uses_product_spacing(sample_release):
    from rasterio.warp import transform
    from app.smap_grid import CELL_SIZE_M
    path = sample_release / 'smap.geojson'
    document = json.loads(path.read_text())
    document['features'][0]['properties']['cell_size_m'] = CELL_SIZE_M / 4
    path.write_text(json.dumps(document))
    response = client.get('/api/v1/vectors/smap-test/grid')
    assert response.status_code == 200
    feature = response.json()['features'][0]
    ring = feature['geometry']['coordinates'][0]
    x, y = transform('EPSG:4326', 'EPSG:6933', *zip(*ring))
    assert max(x) - min(x) == pytest.approx(CELL_SIZE_M / 4, abs=0.01)
    assert max(y) - min(y) == pytest.approx(CELL_SIZE_M / 4, abs=0.01)


def test_geoserver_registration_is_invalidated_when_style_or_data_changes(sample_release):
    from app.map_service import asset_revision, registration
    from app.styles import style_for
    document = json.loads((sample_release / "catalog.json").read_text())
    item = document["assets"][0]
    style = style_for(item["variable"], document["assets"])
    revision = asset_revision(sample_release, item, style, document["data_version"])
    (sample_release / "map_services.json").write_text(json.dumps({"layers": {item["id"]: {"layer": "webgis:ef-test", "revision": revision}}}))
    assert registration(sample_release, item, style, document["data_version"])["layer"] == "webgis:ef-test"
    (sample_release / "map_services.json").write_text(json.dumps({"layers": {item["id"]: {"layer": "https://unexpected.example/wms", "revision": revision}}}))
    assert registration(sample_release, item, style, document["data_version"]) is None
    (sample_release / "map_services.json").write_text(json.dumps({"layers": {item["id"]: {"layer": "webgis:ef-test", "revision": revision}}}))
    item["method_version"] = "changed-method"
    assert registration(sample_release, item, style, document["data_version"]) is None


def test_unit_only_release_reuses_untouched_geoserver_registrations(sample_release):
    from app.map_service import asset_revision, registration
    from app.styles import style_for
    path = sample_release / 'catalog.json'
    document = json.loads(path.read_text())
    item = document['assets'][0]
    previous = document['data_version']
    style = style_for(item['variable'], document['assets'])
    revision = asset_revision(sample_release, item, style, previous)
    (sample_release / 'map_services.json').write_text(json.dumps({'layers': {item['id']: {'layer': 'webgis:' + item['id'], 'revision': revision}}}))
    document['data_version'] = previous + '-lst-celsius-v1'
    document['provenance'] = {'previous_data_version': previous, 'temperature_units': {'variable': 'lst', 'output_unit': '°C'}}
    path.write_text(json.dumps(document))
    assert registration(sample_release, item, style, document['data_version'])['revision'] == revision
    item['variable'] = 'lst'
    assert registration(sample_release, item, style_for('lst', document['assets']), document['data_version']) is None
def test_xyz_raster_tiles_have_bounds_and_cached_colored_pixels(sample_release):
    import struct
    import zlib
    first = client.get("/api/v1/tiles/ef-test/5/7/12.png?v=test")
    second = client.get("/api/v1/tiles/ef-test/5/7/12.png?v=test")
    assert first.status_code == 200 and first.content.startswith(b"\x89PNG")
    assert first.headers["x-map-provider"] == "rasterio-fallback"
    assert second.headers["x-webgis-cache"] == "tile:HIT"
    assert struct.unpack(">2I", first.content[16:24]) == (256, 256)
    position, compressed = 8, bytearray()
    while position < len(first.content):
        length = struct.unpack(">I", first.content[position:position + 4])[0]
        kind = first.content[position + 4:position + 8]
        payload = first.content[position + 8:position + 8 + length]
        if kind == b"IDAT": compressed.extend(payload)
        position += 12 + length
    pixels = zlib.decompress(compressed)
    assert len(pixels) == 256 * (1 + 256 * 4)
    assert max(pixels[4::4]) == 255
    for url in ["/api/v1/tiles/ef-test/5/32/0.png", "/api/v1/tiles/ef-test/15/0/0.png"]:
        assert client.get(url).status_code == 422


def test_geoserver_outage_keeps_local_raster_tiles_available(sample_release, monkeypatch):
    from fastapi import HTTPException
    from app.map_service import asset_revision
    from app.styles import style_for
    item = json.loads((sample_release / "catalog.json").read_text())["assets"][0]
    revision = asset_revision(sample_release, item, style_for(item["variable"], [item]), "fixture")
    (sample_release / "map_services.json").write_text(json.dumps({"layers": {item["id"]: {"layer": "webgis:ef-test", "revision": revision}}}))
    def unavailable(*args, **kwargs):
        raise HTTPException(503, "service offline")
    monkeypatch.setattr(main_module, "fetch_tile", unavailable)
    response = client.get("/api/v1/tiles/ef-test/5/7/12.png?v=offline")
    assert response.status_code == 200 and response.content.startswith(b"\x89PNG")
    assert response.headers["x-map-provider"] == "rasterio-fallback"
    assert response.headers["x-gwc-cache"] == "GWC-unavailable"


def test_request_cache_security_allowlist_and_release_invalidation(sample_release):
    from app.http_boundary import query_cache, tile_cache
    query_cache.clear(); tile_cache.clear()
    first = client.get("/api/v1/catalog")
    assert first.status_code == 200 and first.headers["x-webgis-cache"] == "query:MISS"
    assert client.get("/api/v1/catalog").headers["x-webgis-cache"] == "query:HIT"
    assert client.get("/api/v1/catalog?CQL_FILTER=INCLUDE").status_code == 422
    assert client.get("/api/v1/catalog?foo=1").status_code == 422
    assert client.get("/api/v1/rasters/ef-test/point?lon=-97.5&lon=-98&lat=36.5").status_code == 422
    too_large = {"asset_ids": ["ef-test"], "geometry": {"type": "Polygon", "coordinates": [[[-98, 36], [-97, 36], [-97, 37], [-98, 37], [-98, 36]]]}, "ignored": "x" * 600_000}
    too_large_response = client.post("/api/v1/statistics", json=too_large)
    assert too_large_response.status_code == 413
    assert "x-content-type-options" in first.headers
    before = first.headers["x-webgis-revision"]
    with rasterio.open(sample_release / "test.tif", "r+") as target:
        target.write(np.array([[0.5, -9999], [0.6, 0.8]], dtype=np.float32), 1)
    updated = client.get("/api/v1/catalog")
    assert updated.status_code == 200 and updated.headers["x-webgis-revision"] != before
    assert updated.headers["x-webgis-cache"] == "query:MISS"


def test_webgis_geometry_and_unsupported_query_parameters(sample_release):
    polygon = {"type": "Polygon", "coordinates": [[[-98, 36], [-97.5, 36], [-97, 36.5], [-97.5, 37], [-98, 36]]]}
    response = client.post("/api/v1/statistics", json={"asset_ids": ["ef-test"], "geometry": polygon})
    assert response.status_code == 200, response.text
    assert response.headers["x-webgis-cache"] in {"query:MISS", "query:HIT"}
    assert client.get("/api/v1/series?lon=-97.5&lat=36.5&CQL_FILTER=INCLUDE").status_code == 422
