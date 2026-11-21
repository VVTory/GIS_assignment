"""Bounded source-grid analysis. Never infer values from preview colours."""
import json
from math import isfinite

import numpy as np
import rasterio
from rasterio.errors import WindowError
from rasterio.features import geometry_mask, geometry_window
from rasterio.warp import transform_geom
from shapely.geometry import Point, mapping, shape
from fastapi import HTTPException

MAX_PIXELS = 1_000_000


def validate_geometry(geometry):
    polygon = shape(geometry)
    if polygon.is_empty or not polygon.is_valid or polygon.area == 0:
        raise ValueError("polygon must be nonempty and topologically valid")
    if polygon.bounds[2] - polygon.bounds[0] > 180:
        raise ValueError("antimeridian crossing is unsupported")
    return polygon


def study_geometry(root):
    path = root / "aoi.geojson"
    if not path.is_file():
        return None
    doc = json.loads(path.read_text(encoding="utf-8"))
    return shape(doc["features"][0]["geometry"])


def summarize(values):
    return {"min": float(values.min()) if values.size else None,
            "max": float(values.max()) if values.size else None,
            "mean": float(values.mean()) if values.size else None,
            "std": float(values.std(ddof=0)) if values.size else None,
            "median": float(np.median(values)) if values.size else None}


def raster_statistics(path, geometry):
    with rasterio.open(path) as src:
        projected = transform_geom("EPSG:4326", src.crs, geometry)
        try:
            window = geometry_window(src, [projected])
        except WindowError:
            return {"total_pixels": 0, "valid_pixels": 0, "valid_fraction": None, **summarize(np.array([]))}
        if window.width * window.height > MAX_PIXELS:
            raise HTTPException(422, detail="超过100万候选像元窗口预算，请缩小区域。")
        data = src.read(1, window=window, masked=True)
        inside = geometry_mask([projected], out_shape=data.shape, transform=src.window_transform(window), invert=True, all_touched=False)
        valid = inside & ~np.ma.getmaskarray(data) & np.isfinite(data.data)
        total, count = int(inside.sum()), int(valid.sum())
        return {"total_pixels": total, "valid_pixels": count,
                "valid_fraction": count / total if total else None,
                **summarize(data.data[valid].astype(np.float64))}


def vector_statistics(path, polygon):
    doc = json.loads(path.read_text(encoding="utf-8"))
    values = []
    for feature in doc["features"]:
        value = feature.get("properties", {}).get("soil_moisture")
        if value is not None and isfinite(value) and polygon.covers(Point(feature["geometry"]["coordinates"])):
            values.append(value)
    data = np.asarray(values, dtype=np.float64)
    # Published GeoJSON only contains valid points: missing centre locations
    # cannot be reconstructed, so region-specific completeness is unknown.
    return {"total_pixels": None, "valid_pixels": len(values), "valid_fraction": None,
            **summarize(data), "coverage_note": "仅统计区域内已发布有效原生格点，缺失格点位置未知，不计算区域有效比例。"}
