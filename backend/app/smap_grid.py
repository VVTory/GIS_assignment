"""Approximate native cell footprints, not a downscaled retrieval product.

SPL3SMP and SPL3SMP_E use 36 km and 9 km EASE-Grid 2.0 grid spacing (EPSG:6933).
Build a product-specific square around each supplied centre, then transform its
corners back. Centres from the HDF5 can differ slightly from nominal grid nodes;
therefore footprints are explicitly approximate and never used for statistics.
"""
import json
from rasterio.warp import transform

CELL_SIZE_M = 36032.220840584


def footprints(path):
    doc = json.loads(path.read_text(encoding="utf-8"))
    result = []
    for feature in doc["features"]:
        lon, lat = feature["geometry"]["coordinates"]
        xs, ys = transform("EPSG:4326", "EPSG:6933", [lon], [lat])
        cell_size_m = feature["properties"].get("cell_size_m", CELL_SIZE_M)
        if cell_size_m not in (CELL_SIZE_M, CELL_SIZE_M / 4):
            raise ValueError("Unsupported published SMAP grid spacing")
        half = cell_size_m / 2
        corners = [(xs[0] - half, ys[0] - half), (xs[0] + half, ys[0] - half), (xs[0] + half, ys[0] + half), (xs[0] - half, ys[0] + half), (xs[0] - half, ys[0] - half)]
        x, y = transform("EPSG:6933", "EPSG:4326", *zip(*corners))
        result.append({"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [list(map(list, zip(x, y)))]},
                       "properties": {**feature["properties"], "cell_size_m": cell_size_m, "footprint_status": "approximate_native_extent", "center": [lon, lat]}})
    return {"type": "FeatureCollection", "features": result, "note": "按产品元数据网格间距绘制的EASE-Grid近似格面；不代表独立空间分辨率，不是下尺度，不参与面积加权统计。"}
