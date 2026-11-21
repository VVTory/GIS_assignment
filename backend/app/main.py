from datetime import date, timedelta
import json
from math import atan2, cos, isfinite, radians, sin, sqrt
import os
from pathlib import Path
from uuid import uuid4
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, field_validator
from .quality import quality as build_quality, quality_point
from .analysis import validate_geometry, study_geometry, raster_statistics, vector_statistics
from .http_boundary import install_boundary
from .map_service import asset_revision, registration, fetch_tile
from .smap_grid import footprints as smap_footprints
from .styles import PALETTES, STYLE_VERSION, rgba as colorize, style_for
from shapely.geometry import mapping

app = FastAPI(title="地表温度、植被与土壤湿度分析 WebGIS API", version="0.2.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = Path(os.environ.get("EF_DATA_ROOT", PROJECT_ROOT / "data" / "sample")).resolve()
RAW_ROOT = Path(os.environ.get("EF_RAW_ROOT", PROJECT_ROOT / "first_data")).resolve()
GEOSERVER_URL = os.environ.get("GEOSERVER_URL", "http://127.0.0.1:8600/geoserver").rstrip("/")
install_boundary(app, lambda: DATA_ROOT)


def _release_version():
    # Point queries only need the version, not hundreds of tile revisions.
    try:
        return json.loads((DATA_ROOT / "catalog.json").read_text(encoding="utf-8")).get("data_version")
    except (OSError, json.JSONDecodeError):
        return None


def _catalog_document() -> dict:
    path = DATA_ROOT / "catalog.json"
    if not path.is_file():
        return {"data_version": None, "status": "empty", "assets": [], "message": "No validated sample release is installed."}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"data_version": None, "status": "error", "assets": [], "message": "The sample catalog could not be read."}
    assets = []
    for asset in document.get("assets", []):
        if asset.get("status") != "ready":
            continue
        public_asset = {key: value for key, value in asset.items() if key not in {"raster_path", "map_raster_path", "preview_path", "geojson_path"}}
        asset_id = asset["id"]
        if asset.get("raster_path"):
            spec = style_for(asset["variable"], document.get("assets", []))
            revision = asset_revision(DATA_ROOT, asset, spec, document.get("data_version"))
            public_asset["tile_url"] = f"/api/v1/tiles/{asset_id}/{{z}}/{{x}}/{{y}}.png?v={revision}"
            if asset.get("preview_path"):
                public_asset["preview_url"] = f"/api/v1/rasters/{asset_id}/preview"
            public_asset["map_provider"] = "GeoServer + GeoWebCache when registered; versioned offline Rasterio tiles otherwise"
            public_asset["style_version"] = STYLE_VERSION
            public_asset["point_url"] = f"/api/v1/rasters/{asset_id}/point"
        elif asset.get("geojson_path"):
            public_asset["geojson_url"] = f"/api/v1/vectors/{asset_id}"
            public_asset["grid_url"] = f"/api/v1/vectors/{asset_id}/grid"
            public_asset["point_url"] = f"/api/v1/vectors/{asset_id}/point"
        public_asset["download_url"] = f"/api/v1/assets/{asset_id}/download"
        assets.append(public_asset)
    return {"data_version": document.get("data_version"), "status": "ready" if assets else "empty", "assets": assets, "provenance": document.get("provenance", {}), "aoi_url": "/api/v1/study-area", "report_url": "/api/v1/processing-report"}


def _asset(asset_id: str) -> dict:
    try:
        raw = json.loads((DATA_ROOT / "catalog.json").read_text(encoding="utf-8"))
        for item in raw.get("assets", []):
            if item.get("id") == asset_id:
                if item.get("status") == "ready":
                    return item
                raise HTTPException(409, detail={"code": "asset_not_ready", "asset_id": asset_id})
    except (OSError, json.JSONDecodeError):
        pass
    raise HTTPException(404, detail={"code": "asset_not_found", "asset_id": asset_id})


def _release_file(relative_path: str) -> Path:
    candidate = (DATA_ROOT / relative_path).resolve()
    try:
        candidate.relative_to(DATA_ROOT)
    except ValueError:
        raise HTTPException(404, detail="asset file not found")
    if not candidate.is_file():
        raise HTTPException(404, detail="asset file not found")
    return candidate

class StatisticsRequest(BaseModel):
    asset_ids: list[str] = Field(min_length=1, max_length=10)
    geometry: dict
    mask_policy: str = "valid_only"
    pixel_rule: str = "center_inside"

    @field_validator("geometry")
    @classmethod
    def validate_polygon(cls, geometry: dict) -> dict:
        if geometry.get("type") != "Polygon" or not isinstance(geometry.get("coordinates"), list):
            raise ValueError("geometry must be a GeoJSON Polygon")
        rings = geometry["coordinates"]
        if not rings:
            raise ValueError("polygon must contain an exterior ring")
        count = 0
        for ring in rings:
            if not isinstance(ring, list) or len(ring) < 4:
                raise ValueError("polygon rings need at least four positions")
            count += len(ring)
            if count > 5000:
                raise ValueError("polygon exceeds the 5000-position limit")
            positions = []
            for position in ring:
                if not isinstance(position, (list, tuple)) or len(position) < 2:
                    raise ValueError("positions must contain longitude and latitude")
                lon, lat = position[0], position[1]
                if isinstance(lon, bool) or isinstance(lat, bool) or not isinstance(lon, (int, float)) or not isinstance(lat, (int, float)):
                    raise ValueError("longitude and latitude must be numeric")
                if not isfinite(lon) or not isfinite(lat) or not -180 <= lon <= 180 or not -90 <= lat <= 90:
                    raise ValueError("coordinates must be finite WGS84 longitude/latitude")
                positions.append((lon, lat))
            if positions[0] != positions[-1]:
                raise ValueError("polygon rings must be closed")
        validate_geometry(geometry)
        return geometry

@app.get("/health/live")
def live():
    return {"status": "ok"}

@app.get("/health/ready")
def ready():
    catalog = _catalog_document()
    return {"status": "ready", "catalog_status": catalog["status"], "ready_assets": len(catalog["assets"])}

@app.get("/api/v1/catalog")
def catalog():
    return _catalog_document()


@app.get("/api/v1/study-area")
def study_area():
    return FileResponse(_release_file("aoi.geojson"), media_type="application/geo+json")


@app.get("/api/v1/processing-report")
def processing_report():
    return FileResponse(_release_file("processing_report.json"), media_type="application/json")


def _quality(asset_id):
    try:
        return build_quality(DATA_ROOT, RAW_ROOT, _asset(asset_id), _release_version())
    except (OSError, ValueError) as exc:
        raise HTTPException(503, detail="质量诊断输入暂不可用，请检查原始文件与处理记录。") from exc


@app.get("/api/v1/assets/{asset_id}/quality")
def quality_metadata(asset_id: str):
    result = dict(_quality(asset_id)["metadata"])
    if result.get("image_coordinates"):
        result["image_url"] = f"/api/v1/assets/{asset_id}/quality.png"
    return result


@app.get("/api/v1/assets/{asset_id}/quality.png")
def quality_image(asset_id: str):
    result = _quality(asset_id)
    if "png" not in result:
        raise HTTPException(422, detail="SMAP quality uses native grid polygons")
    return Response(result["png"], media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/api/v1/assets/{asset_id}/quality/point")
def quality_at_point(asset_id: str, lon: float = Query(ge=-180, le=180), lat: float = Query(ge=-90, le=90)):
    return quality_point(_quality(asset_id), lon, lat)


@app.get("/api/v1/rasters/{asset_id}")
def raster(asset_id: str):
    item = _asset(asset_id)
    return {key: value for key, value in item.items() if key not in {"raster_path", "map_raster_path", "preview_path", "geojson_path"}}


@app.get("/api/v1/rasters/{asset_id}/preview")
def raster_preview(asset_id: str):
    item = _asset(asset_id)
    if "preview_path" not in item:
        raise HTTPException(422, detail="asset is not a raster")
    return FileResponse(_release_file(item["preview_path"]), media_type="image/png", filename=f"{asset_id}.png")


@app.get("/api/v1/assets/{asset_id}/coverage")
def coverage(asset_id: str):
    item = _asset(asset_id)
    domain = study_geometry(DATA_ROOT)
    if "raster_path" in item and domain is not None:
        result = raster_statistics(_release_file(item["raster_path"]), mapping(domain))
        note = "分母为研究区内源网格中心落入的全部像元；分子为文件掩膜通过且数值有限的像元。"
        kind = "raster_pixels"
    elif "geojson_path" in item:
        stats = item.get("statistics", {})
        total, valid = stats.get("geolocated_grid_cells"), stats.get("valid_pixels", 0)
        result = {"total_pixels": total, "valid_pixels": valid, "valid_fraction": valid / total if total else None}
        note = "分母为当天研究区内有定位的SMAP原生格点，分子为QA=0的有效格点；不是面积覆盖率。"
        kind = "native_grid_centers"
    else:
        result = {"total_pixels": None, "valid_pixels": item.get("statistics", {}).get("valid_pixels"), "valid_fraction": None}
        note, kind = "缺少研究区边界，无法计算覆盖率。", "unknown"
    return {"asset_id": asset_id, **result, "coverage_kind": kind, "coverage_note": note,
            "data_version": _release_version()}


@app.get("/api/v1/assets/{asset_id}/download")
def download_asset(asset_id: str):
    item = _asset(asset_id)
    if "raster_path" in item:
        return FileResponse(_release_file(item["raster_path"]), media_type="image/tiff", filename=f"{asset_id}.tif")
    return FileResponse(_release_file(item["geojson_path"]), media_type="application/geo+json", filename=f"{asset_id}.geojson")


@app.get("/api/v1/rasters/{asset_id}/render")
def render_asset(asset_id: str):
    """A shared per-variable scale across all published dates for comparison."""
    import struct
    import zlib
    import numpy as np
    import rasterio
    from rasterio.warp import calculate_default_transform, reproject, Resampling
    from fastapi.responses import Response
    item = _asset(asset_id)
    if "raster_path" not in item:
        raise HTTPException(422, detail="asset is not a raster")
    peers = [a for a in _catalog_document()["assets"] if a["variable"] == item["variable"]]
    style = style_for(item["variable"], peers)
    lo, hi = style["range"]
    with rasterio.open(_release_file(item["raster_path"])) as src:
        if src.width * src.height > 1_000_000:
            raise HTTPException(422, detail="preview exceeds online budget")
        affine, width, height = calculate_default_transform(src.crs, "EPSG:3857", src.width, src.height, *src.bounds)
        if width * height > 2_000_000:
            raise HTTPException(422, detail="preview exceeds output budget")
        data = np.full((height, width), np.nan, dtype=np.float32)
        source = src.read(1, masked=True).astype(np.float32).filled(np.nan)
        reproject(source, data, src_transform=src.transform, src_crs=src.crs, src_nodata=np.nan,
                  dst_transform=affine, dst_crs="EPSG:3857", dst_nodata=np.nan, resampling=Resampling.nearest)
    valid = np.isfinite(data)
    rgba = colorize(data, valid, style)
    def chunk(kind, payload):
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xffffffff)
    rows = b"".join(b"\0" + row.tobytes() for row in rgba)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">2I5B", width, height, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b"")
    return Response(png, media_type="image/png", headers={"X-Render-Min": str(lo), "X-Render-Max": str(hi), "X-Style-Version": STYLE_VERSION})


@app.get("/api/v1/styles")
def styles():
    available = _catalog_document()["assets"]
    return {v: {**style_for(v, available), "variable": v} for v in PALETTES}


@app.get("/api/v1/tiles/{asset_id}/{z}/{x}/{y}.png")
def map_tile(asset_id: str, z: int, x: int, y: int):
    if not (0 <= z <= 14 and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise HTTPException(422, detail="z/x/y超出XYZ瓦片范围")
    item = _asset(asset_id)
    if "raster_path" not in item: raise HTTPException(422, detail="asset is not a raster")
    document = _catalog_document()
    spec = style_for(item["variable"], document["assets"])
    revision = asset_revision(DATA_ROOT, item, spec, document.get("data_version"))
    record = registration(DATA_ROOT, item, spec, document.get("data_version"))
    if record:
        try:
            body, hit = fetch_tile(GEOSERVER_URL, record, z, x, y, cached=True)
            provider, cache_status = "geoserver-geowebcache", hit
        except HTTPException as exc:
            if exc.status_code != 503:
                raise
            body = _render_tile_from_raster(item, z, x, y, spec)
            provider, cache_status = "rasterio-fallback", "GWC-unavailable"
    else:
        body = _render_tile_from_raster(item, z, x, y, spec)
        provider, cache_status = "rasterio-fallback", "BYPASS-unregistered"
    return Response(body, media_type="image/png", headers={"X-Map-Provider": provider, "X-GWC-Cache": cache_status, "X-Layer-Revision": revision})


def _render_tile_from_raster(item, z, x, y, spec):
    import struct
    import zlib
    import numpy as np
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import from_bounds as affine_from_bounds
    from rasterio.warp import reproject
    half = 20037508.342789244
    span = 2 * half / (2 ** z)
    west, north = -half + x * span, half - y * span
    destination = np.full((256, 256), np.nan, dtype=np.float32)
    transform = affine_from_bounds(west, north - span, west + span, north, 256, 256)
    with rasterio.open(_release_file(item["raster_path"])) as src:
        values = src.read(1, masked=True).astype(np.float32).filled(np.nan)
        reproject(values, destination, src_transform=src.transform, src_crs=src.crs, src_nodata=np.nan,
                  dst_transform=transform, dst_crs="EPSG:3857", dst_nodata=np.nan, resampling=Resampling.bilinear)
    valid = np.isfinite(destination)
    rgba = colorize(destination, valid, spec)
    def chunk(kind, payload):
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xffffffff)
    scanlines = b"".join(b"\0" + row.tobytes() for row in rgba)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">2I5B", 256, 256, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(scanlines, 6))
            + chunk(b"IEND", b""))


@app.get("/api/v1/vectors/{asset_id}/grid")
def vector_grid(asset_id: str):
    item = _asset(asset_id)
    if "geojson_path" not in item: raise HTTPException(422, detail="asset is not a native vector grid")
    return smap_footprints(_release_file(item["geojson_path"]))


@app.get("/api/v1/vectors/{asset_id}")
def vector_asset(asset_id: str):
    item = _asset(asset_id)
    if "geojson_path" not in item:
        raise HTTPException(422, detail="asset is not a vector layer")
    return FileResponse(_release_file(item["geojson_path"]), media_type="application/geo+json", filename=f"{asset_id}.geojson")

@app.get("/api/v1/vectors/{asset_id}/point")
@app.get("/api/v1/rasters/{asset_id}/point")
def point(asset_id: str, lon: float = Query(ge=-180, le=180), lat: float = Query(ge=-90, le=90)):
    item = _asset(asset_id)
    if "geojson_path" in item:
        try:
            feature_collection = json.loads(_release_file(item["geojson_path"]).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise HTTPException(500, detail="Could not read vector asset")
        best = None
        for feature in feature_collection.get("features", []):
            coordinates = feature.get("geometry", {}).get("coordinates", [])
            if len(coordinates) < 2:
                continue
            x1, y1 = map(radians, (lon, lat))
            x2, y2 = map(radians, (coordinates[0], coordinates[1]))
            delta_x, delta_y = x2 - x1, y2 - y1
            haversine = sin(delta_y / 2) ** 2 + cos(y1) * cos(y2) * sin(delta_x / 2) ** 2
            distance = 6371000 * 2 * atan2(sqrt(haversine), sqrt(max(0.0, 1 - haversine)))
            if best is None or distance < best[0]:
                best = (distance, feature)
        if best is None or best[0] > 30000:
            return {"asset_id": asset_id, "valid": False, "reason": "no_nearby_valid_smap_cell"}
        properties = best[1].get("properties", {})
        return {"asset_id": asset_id, "date": item["date"], "variable": item["variable"], "value": properties.get("soil_moisture"), "unit": item["unit"], "valid": True, "quality_flag": properties.get("quality_flag"), "distance_m": round(best[0], 1), "matched_coordinates": best[1]["geometry"]["coordinates"], "sampling_method": "nearest_valid_native_grid_center_within_30km"}
    if "raster_path" not in item:
        raise HTTPException(422, detail="asset is not a raster")
    try:
        import numpy as np
        import rasterio
        from rasterio.warp import transform
    except ImportError:
        raise HTTPException(503, detail="raster query support is not installed")
    path = _release_file(item["raster_path"])
    try:
        with rasterio.open(path) as dataset:
            x, y = transform("EPSG:4326", dataset.crs, [lon], [lat])
            row, col = dataset.index(x[0], y[0])
            if not (0 <= row < dataset.height and 0 <= col < dataset.width):
                return {"asset_id": asset_id, "valid": False, "reason": "outside_raster"}
            sample = dataset.read(1, window=((row, row + 1), (col, col + 1)), masked=True)
            if np.ma.getmaskarray(sample)[0, 0]:
                return {"asset_id": asset_id, "valid": False, "reason": "nodata"}
            value = float(sample[0, 0])
            if not isfinite(value):
                return {"asset_id": asset_id, "valid": False, "reason": "nodata"}
            px, py = dataset.xy(row, col)
            plon, plat = transform(dataset.crs, "EPSG:4326", [px], [py])
            return {"asset_id": asset_id, "date": item["date"], "variable": item["variable"], "value": value, "unit": item["unit"], "valid": True,
                    "source_pixel": {"row": row, "col": col, "lon": plon[0], "lat": plat[0]}, "method_version": item.get("method_version"),
                    "data_version": _release_version()}
    except (OSError, ValueError) as exc:
        raise HTTPException(500, detail="Could not read raster asset") from exc

@app.get("/api/v1/series")
def series(lon: float = Query(ge=-180, le=180), lat: float = Query(ge=-90, le=90), variable: str = "ef", start: date | None = None, end: date | None = None):
    if variable not in {"ef", "lst", "ndvi", "soil_moisture"}:
        raise HTTPException(422, detail="unsupported variable")
    if start and end and (end < start or (end - start).days > 365):
        raise HTTPException(422, detail="date range must be ordered and at most 366 days")
    points = []
    for asset in sorted(_catalog_document()["assets"], key=lambda item: item["date"]):
        day = date.fromisoformat(asset["date"])
        if asset["variable"] == variable and (start is None or day >= start) and (end is None or day <= end):
            points.append({"date": asset["date"], **point(asset["id"], lon, lat)})
    if points:
        first = start or date.fromisoformat(points[0]["date"])
        last = end or date.fromisoformat(points[-1]["date"])
        if (last - first).days > 365:
            raise HTTPException(422, detail="date range must be at most 366 days")
        by_day = {p["date"]: p for p in points}
        points = [by_day.get((first + timedelta(days=i)).isoformat(), {"date": (first + timedelta(days=i)).isoformat(),
                   "valid": False, "value": None, "reason": "no_published_asset"}) for i in range((last - first).days + 1)]
    return {"request_id": str(uuid4()), "status": "ready" if points else "not_ready", "points": points, "data_version": _catalog_document().get("data_version"), "reason": None if points else "no published assets in range"}

@app.post("/api/v1/statistics")
def statistics(request: StatisticsRequest):
    if request.mask_policy != "valid_only" or request.pixel_rule != "center_inside":
        raise HTTPException(422, detail="unsupported statistics policy")
    polygon = validate_geometry(request.geometry)
    domain = study_geometry(DATA_ROOT)
    if domain is not None:
        polygon = polygon.intersection(domain)
        if polygon.is_empty or polygon.area == 0:
            raise HTTPException(422, detail="所选区域在研究区之外，请重新框选。")
    results = []
    if len(request.asset_ids) > 3:
        raise HTTPException(422, detail="在线统计每次最多3个图层。")
    for asset_id in request.asset_ids:
        item = _asset(asset_id)
        if "raster_path" in item:
            result = raster_statistics(_release_file(item["raster_path"]), mapping(polygon))
            weighting = "equal_source_pixels"
        else:
            result = vector_statistics(_release_file(item["geojson_path"]), polygon)
            weighting = "equal_valid_native_grid_centers"
        results.append({**result, "asset_id": asset_id, "date": item["date"], "variable": item["variable"],
                        "unit": item["unit"], "method_version": item.get("method_version"), "weighting_policy": weighting})
    return {"request_id": str(uuid4()), "status": "ready", "data_version": _catalog_document().get("data_version"),
            "mask_policy": request.mask_policy, "pixel_rule": request.pixel_rule, "geometry": mapping(polygon), "results": results}

@app.get("/api/v1/stations")
def stations(page: int = Query(1, ge=1), limit: int = Query(100, ge=1, le=500)):
    return {"type": "FeatureCollection", "features": [], "page": page, "limit": limit, "status": "not_ready"}

@app.get("/api/v1/stations/{station_id}/validation")
def validation(station_id: str):
    return {"station_id": station_id, "status": "not_ready", "pairs": [], "metrics": None, "reason": "No validated ARM observations are installed."}
