"""Explain published gaps using the exact raw QA inputs recorded by the release.

Class counts use native pixel centres, not the downsampled display image.
Classification never modifies scientific data or turns missing values into zero.
"""
from functools import lru_cache
import json
from pathlib import Path
import struct
import warnings
import zlib

import numpy as np
import rasterio
from rasterio.errors import NotGeoreferencedWarning
from rasterio.features import geometry_mask
from rasterio.warp import transform, transform_geom, calculate_default_transform, reproject, Resampling
from shapely.geometry import Point, shape
from shapely.ops import unary_union

LEGEND = [
    {"code": 1, "key": "valid", "label": "已发布有效值", "color": "#477653"},
    {"code": 2, "key": "cloud", "label": "产品标记云影响", "color": "#8586b5"},
    {"code": 3, "key": "low_quality", "label": "未通过质量筛选", "color": "#cf9047"},
    {"code": 4, "key": "excluded_surface", "label": "水体／雪冰／阴影等排除项", "color": "#6498a6"},
    {"code": 5, "key": "missing", "label": "源数据或质量标记缺测", "color": "#a1a496"},
    {"code": 6, "key": "other_exclusion", "label": "其他处理排除／原因未细分", "color": "#a0757d"},
]


def safe_file(root, relative):
    path = (Path(root) / relative).resolve()
    path.relative_to(Path(root).resolve())
    if not path.is_file():
        raise FileNotFoundError("quality input unavailable")
    return path


def present(values, nodata):
    return np.isfinite(values) & (values != nodata if nodata is not None else True)


def lst_codes(raw, raw_nodata, qc, qc_nodata):
    has_raw, has_qa = present(raw, raw_nodata), present(qc, qc_nodata)
    bits = np.where(has_qa, qc, 0).astype(np.uint16)
    good = has_raw & has_qa & ((bits & 3) == 0) & (((bits >> 2) & 3) == 0) & (((bits >> 6) & 3) <= 1)
    codes = np.full(raw.shape, 5, dtype=np.uint8)
    codes[has_raw & has_qa] = 3
    # MOD11 mandatory QA == 2 explicitly means LST not produced due to cloud.
    codes[has_qa & ((bits & 3) == 2)] = 2
    codes[good] = 1
    return codes


def ndvi_codes(raw, raw_nodata, qa, qa_nodata, rel, rel_nodata):
    has_raw = present(raw, raw_nodata) & (raw >= -2000) & (raw <= 10000)
    has_qa = present(qa, qa_nodata) & present(rel, rel_nodata)
    bits = np.where(has_qa, qa, 0).astype(np.uint16)
    codes = np.full(raw.shape, 5, dtype=np.uint8)
    codes[has_raw & has_qa] = 3
    excluded = has_qa & ((((bits >> 11) & 7) != 1) | ((bits & 0xC000) != 0) | (rel == 2))
    codes[excluded] = 4
    cloud = has_qa & ((rel == 3) | ((bits & 3) == 2) | ((bits & (1 << 10)) != 0))
    codes[cloud] = 2
    good = has_raw & has_qa & ((bits & 3) == 0) & (rel == 0) & (((bits >> 11) & 7) == 1) & ((bits & 0xC000) == 0)
    codes[good] = 1
    return codes


def read_aligned(root, path, template):
    with rasterio.open(safe_file(root, path)) as ds:
        if (ds.shape, ds.crs, ds.transform) != (template.shape, template.crs, template.transform):
            raise ValueError("quality input grid differs from published grid")
        return ds.read(1), ds.nodata


def png_bytes(rgba):
    h, w = rgba.shape[:2]
    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xffffffff)
    data = b"".join(b"\0" + row.tobytes() for row in rgba)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">2I5B", w, h, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(data)) + chunk(b"IEND", b"")


def dependencies(root, raw_root, item, report):
    record = next((r for r in report.get("modis_dates", []) if r["date"] == item["date"]), {})
    inputs = record.get("inputs", {})
    raw_paths = [inputs.get("lst"), inputs.get("lst_qc"), *inputs.get("ndvi", {}).get("sources", [])]
    if item.get("source_path"):
        raw_paths.append(item["source_path"])
    entries = [(root, "aoi.geojson"), (root, "processing_report.json")]
    entries += [(root, item[k]) for k in ("raster_path", "geojson_path") if item.get(k)]
    entries += [(raw_root, p) for p in raw_paths if p]
    stamps = []
    for directory, relative in entries:
        try:
            path = safe_file(directory, relative)
            stat = path.stat()
            stamps.append((str(path), stat.st_size, stat.st_mtime_ns))
        except (OSError, ValueError):
            stamps.append((str(directory), relative, "missing"))
    return tuple(stamps)


def quality(root, raw_root, item, version):
    root, raw_root = Path(root).resolve(), Path(raw_root).resolve()
    try:
        report = json.loads((root / "processing_report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        report = {}
    return _build(str(root), str(raw_root), json.dumps(item, sort_keys=True), version,
                  dependencies(root, raw_root, item, report))


@lru_cache(maxsize=8)
def _build(root_text, raw_text, item_json, version, stamps):
    root, raw_root, item = Path(root_text), Path(raw_text), json.loads(item_json)
    try:
        report = json.loads((root / "processing_report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        report = {}
    aoi = json.loads(safe_file(root, "aoi.geojson").read_text(encoding="utf-8"))
    geometry = unary_union([shape(f["geometry"]) for f in aoi["features"]])
    if "geojson_path" in item:
        return {**_smap(root, raw_root, item, version, geometry), "domain": geometry}
    with rasterio.open(safe_file(root, item["raster_path"])) as ds:
        data = ds.read(1, masked=True)
        valid = ~np.ma.getmaskarray(data) & np.isfinite(data.data)
        inside = geometry_mask([transform_geom("EPSG:4326", ds.crs, geometry.__geo_interface__)],
                               out_shape=ds.shape, transform=ds.transform, invert=True)
        codes = np.where(valid, 1, 5).astype(np.uint8)
        record = next((r for r in report.get("modis_dates", []) if r["date"] == item["date"]), {})
        inputs = record.get("inputs", {})
        notes, dates, supported = [], [], False
        try:
            layers = []
            if item["variable"] in ("lst", "ef"):
                raw, nodata = read_aligned(raw_root, inputs["lst"], ds)
                qc, qc_nodata = read_aligned(raw_root, inputs["lst_qc"], ds)
                layers.append(lst_codes(raw, nodata, qc, qc_nodata))
                notes.append("LST：MOD11A1 QC_Day mandatory QA=2 标为云；其他不合格质量标记与缺测分开显示。")
            if item["variable"] in ("ndvi", "ef"):
                info = inputs["ndvi"]
                sources = info["sources"]
                for i in (0, 1):
                    raw, nodata = read_aligned(raw_root, sources[i], ds)
                    qa, qa_nodata = read_aligned(raw_root, sources[i + 2], ds)
                    rel, rel_nodata = read_aligned(raw_root, sources[i + 4], ds)
                    layers.append(ndvi_codes(raw, nodata, qa, qa_nodata, rel, rel_nodata))
                dates = list(dict.fromkeys([info["before"], info["after"]]))
                notes.append("NDVI：显示前后合成期任一端的质量限制；云来自 reliability=3、MODLAND QA=2 或混合云位。不是当天云图。")
            if layers:
                codes[:] = 6
                codes[np.logical_and.reduce([a == 1 for a in layers])] = 1
                # Explicit cloud evidence takes precedence, then excluded surface,
                # then failed QA; unavailable inputs are kept distinct.
                for code in (5, 3, 4, 2):
                    codes[np.logical_or.reduce([a == code for a in layers])] = code
                supported = True
        except (KeyError, OSError, ValueError, TypeError):
            codes = np.where(valid, 1, 5).astype(np.uint8)
            notes = ["原始质量输入不可用，只显示已发布有效范围与缺测；无法据此区分云和低质量。"]
        # The release is authoritative: do not label published valid values as gaps.
        codes[valid] = 1
        codes[inside & ~valid & (codes == 1)] = 6
        codes[~inside] = 0
        legend = [{**x, "count": int((codes == x["code"]).sum())} for x in LEGEND]
        transform_out, w, h = calculate_default_transform(ds.crs, "EPSG:3857", ds.width, ds.height, *ds.bounds)
        factor = min(1.0, 1100 / max(w, h))
        if factor < 1:
            from affine import Affine
            transform_out *= Affine.scale(1 / factor, 1 / factor)
            w, h = max(1, round(w * factor)), max(1, round(h * factor))
        display = np.zeros((h, w), dtype=np.uint8)
        reproject(codes, display, src_transform=ds.transform, src_crs=ds.crs,
                  dst_transform=transform_out, dst_crs="EPSG:3857", resampling=Resampling.nearest)
        rgba = np.zeros((h, w, 4), dtype=np.uint8)
        for entry in LEGEND[1:]:
            color = entry["color"]
            rgba[display == entry["code"]] = [int(color[i:i + 2], 16) for i in (1, 3, 5)] + [205]
        from rasterio.transform import array_bounds
        west, south, east, north = array_bounds(h, w, transform_out)
        xs, ys = transform("EPSG:3857", "EPSG:4326", [west, east, east, west], [north, north, south, south])
        metadata = {"asset_id": item["id"], "date": item["date"], "variable": item["variable"], "data_version": version,
                    "status": "ready", "detailed_qa_available": supported, "legend": legend,
                    "total_pixels": int(inside.sum()), "valid_pixels": int((codes == 1).sum()),
                    "image_coordinates": list(map(list, zip(xs, ys))), "source_dates": dates, "notes": notes,
                    "scope": "源网格中心落入研究区；互斥分类，优先显示明确云标记；有效像元仍显示原变量色带。"}
        return {"metadata": metadata, "png": png_bytes(rgba), "codes": codes, "transform": ds.transform, "crs": ds.crs, "domain": geometry}


def _smap(root, raw_root, item, version, geometry):
    features = []
    try:
        path = safe_file(raw_root, item["source_path"])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", NotGeoreferencedWarning)
            with rasterio.open(path) as container:
                names = container.subdatasets
            arrays = {}
            for field in ("latitude_centroid", "longitude_centroid", "soil_moisture", "retrieval_qual_flag"):
                name = next(n for n in names if n.endswith("://Soil_Moisture_Retrieval_Data_AM/" + field))
                with rasterio.open(name) as ds:
                    arrays[field] = ds.read(1)
        lat, lon, sm, qa = [arrays[k] for k in ("latitude_centroid", "longitude_centroid", "soil_moisture", "retrieval_qual_flag")]
        west, south, east, north = geometry.bounds
        candidates = np.isfinite(lon) & np.isfinite(lat) & (lon >= west) & (lon <= east) & (lat >= south) & (lat <= north)
        cell_size = item.get("cell_size_m", 36032.220840584)
        for row, col in zip(*np.where(candidates)):
            x, y = float(lon[row, col]), float(lat[row, col])
            if not geometry.covers(Point(x, y)):
                continue
            flag, value = int(qa[row, col]), float(sm[row, col])
            code = 1 if flag == 0 and np.isfinite(value) and 0 <= value <= 1 else 5 if flag == 65535 or not np.isfinite(value) or not 0 <= value <= 1 else 3
            px, py = transform("EPSG:4326", "EPSG:6933", [x], [y])
            half = cell_size / 2
            gx, gy = transform("EPSG:6933", "EPSG:4326",
                               [px[0]-half, px[0]+half, px[0]+half, px[0]-half, px[0]-half],
                               [py[0]-half, py[0]-half, py[0]+half, py[0]+half, py[0]-half])
            features.append({"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [list(map(list, zip(gx, gy)))]},
                             "properties": {"quality_code": code, "quality_flag": flag, "center": [x, y], "row": int(row), "col": int(col)}})
        if len(features) != item["statistics"]["geolocated_grid_cells"]:
            raise ValueError("SMAP raw coordinates differ from published release")
        if sum(f["properties"]["quality_code"] == 1 for f in features) != item["statistics"]["valid_pixels"]:
            raise ValueError("SMAP raw validity differs from published release")
        note = "SMAP AM 原生格点反演质量：QA=0 且值在有效范围内；QA不合格与源缺测分开，不能从此标记推断云。格面为近似原生范围。"
        status = "ready"
    except (KeyError, OSError, ValueError, StopIteration):
        note = "原始 SMAP 质量输入不可用；仅已发布有效格点可查询，未伪造缺测格面或云分类。"
        status = "unavailable"
        features = []
    legend = [{**x, "count": sum(f["properties"]["quality_code"] == x["code"] for f in features)} for x in LEGEND]
    return {"metadata": {"asset_id": item["id"], "date": item["date"], "variable": item["variable"], "data_version": version,
                         "status": status, "detailed_qa_available": status == "ready", "legend": legend,
                         "total_pixels": len(features), "valid_pixels": sum(f["properties"]["quality_code"] == 1 for f in features),
                         "geojson": {"type": "FeatureCollection", "features": features}, "notes": [note], "source_dates": [item["date"]],
                         "scope": "原生定位格点计数，非面积覆盖率；无云分类。"}}


def quality_point(result, lon, lat):
    if "domain" in result and not result["domain"].covers(Point(lon, lat)):
        code = 0
    elif "codes" in result:
        xs, ys = transform("EPSG:4326", result["crs"], [lon], [lat])
        from rasterio.transform import rowcol
        row, col = rowcol(result["transform"], xs[0], ys[0])
        arr = result["codes"]
        code = int(arr[row, col]) if 0 <= row < arr.shape[0] and 0 <= col < arr.shape[1] else 0
    else:
        feature = next((f for f in result["metadata"].get("geojson", {}).get("features", []) if shape(f["geometry"]).covers(Point(lon, lat))), None)
        code = feature["properties"]["quality_code"] if feature else 0
    entry = next((x for x in LEGEND if x["code"] == code), {"code": 0, "key": "outside", "label": "研究区或质量诊断覆盖范围之外"})
    return {**entry, "asset_id": result["metadata"]["asset_id"], "date": result["metadata"]["date"],
            "notes": result["metadata"]["notes"], "source_dates": result["metadata"]["source_dates"]}
