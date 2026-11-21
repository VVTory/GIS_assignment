"""Build a traceable, small-area EF sample release from the local public data."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import warnings
import zlib
from collections import Counter
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from rasterio.features import geometry_mask
from rasterio.errors import NotGeoreferencedWarning
from rasterio.warp import calculate_default_transform, reproject, Resampling, transform, transform_geom

from backend.app.algorithms.empirical_tvx import BaselineParameters, estimate


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AOI = ROOT / "docs" / "SGP_AOI.geojson"
DEFAULT_DATA = ROOT / "first_data"
DEFAULT_OUTPUT = ROOT / "data" / "sample"
LEGACY_SMAP_DIR = "SPL3SMP_009-20261112_101206"
NODATA = -9999.0
LST_UNIT = '°C'
LST_UNIT_VERSION = 'lst-celsius-v1'
LST_CONVERSION = {'variable': 'lst', 'source_unit': 'K', 'output_unit': LST_UNIT, 'formula': 'degC = K - 273.15', 'offset': -273.15, 'version': LST_UNIT_VERSION, 'ef_calculation_unit': 'K'}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _category(path: Path) -> str:
    name = path.name
    if name.startswith("MOD11A1."):
        return "MOD11A1"
    if name.startswith("MOD11A2."):
        return "MOD11A2"
    if name.startswith("MOD13A2."):
        return "MOD13A2"
    if name.startswith("MOD03."):
        return "MOD03"
    if name.startswith("MOD06_L2."):
        return "MOD06_L2"
    if name.startswith("MOD07_L2."):
        return "MOD07_L2"
    if name.startswith("SMAP_L3_SM_P_") and path.suffix.lower() == ".h5":
        return "SMAP_H5"
    if name.startswith("SMAP_L3_SM_P_") and path.suffix.lower() in {".tif", ".tiff"}:
        return "SMAP_legacy_tif"
    if re.search(r"(?i)(ARM|EBBR|STAMP)", name):
        return "ARM"
    return "other"


def _header_status(path: Path, category: str) -> tuple[str, str | None]:
    with path.open("rb") as stream:
        header = stream.read(8)
    if category == "SMAP_H5" or path.suffix.lower() == ".nc":
        if header == bytes.fromhex("894844460d0a1a0a") or header[:4] in (b"CDF\x01", b"CDF\x02", b"CDF\x05"):
            return "valid_container_header", None
        return "invalid", "Unrecognized HDF5/NetCDF header"
    if path.suffix.lower() == ".hdf":
        if header[:4] == bytes.fromhex("0e031301"):
            return "valid_container_header", None
        return "invalid", "Unrecognized HDF4 header"
    if path.suffix.lower() in {".tif", ".tiff"}:
        if header[:4] in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"):
            return "valid_container_header", None
        return "invalid", "Unrecognized TIFF header"
    return "inventory_only", None


def inventory(data_root: Path, output: Path, *, hash_files: bool) -> dict[str, Any]:
    """Record every file and explicitly quarantine the previously bad SMAP rasters."""
    entries: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for path in sorted(p for p in data_root.rglob("*") if p.is_file()):
        category = _category(path)
        counts[category] += 1
        stat = path.stat()
        if category == "SMAP_legacy_tif":
            status, header_error = "quarantined", None
        elif stat.st_size == 0:
            status, header_error = "invalid", "Empty file"
        else:
            status, header_error = _header_status(path, category)
        entry: dict[str, Any] = {
            "path": path.relative_to(data_root).as_posix(),
            "product": category,
            "size_bytes": stat.st_size,
            "modified_at_ns": stat.st_mtime_ns,
            "status": status,
        }
        if path.suffix.lower() in {".tif", ".tiff"} and status != "quarantined":
            try:
                with rasterio.open(path) as source:
                    entry["raster"] = {"shape": list(source.shape), "crs": str(source.crs), "scale": source.scales[0], "offset": source.offsets[0], "nodata": source.nodata}
                    source.read(1)
                    if source.crs is None:
                        entry.update(status="invalid", reason="Missing raster CRS")
            except rasterio.errors.RasterioError as exc:
                entry.update(status="invalid", reason=str(exc))
        if category == "SMAP_legacy_tif":
            entry["reason"] = "Superseded 1x1 GeoTIFF export with invalid geotransform; use the raw HDF5 source."
        elif header_error:
            entry["reason"] = header_error
        elif stat.st_size == 0:
            entry["reason"] = "Empty file"
        elif hash_files:
            entry["sha256"] = sha256(path)
        entries.append(entry)

    output.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "created_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source_root_label": data_root.name,
        "file_count": len(entries),
        "counts_by_product": dict(sorted(counts.items())),
        "files": entries,
    }
    _write_json(output / "input_manifest.json", manifest)
    return manifest


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def _doy_to_date(code: str) -> date:
    return datetime.strptime(code[:7], "%Y%j").date()


def _doy_layers(data_root: Path, product: str, layer: str) -> dict[date, Path]:
    """Discover products by filename, including downloads placed in other folders."""
    result = {}
    for path in sorted(data_root.rglob(f"{product}*_{layer}_doy*.tif")):
        match = re.search(r"_doy(\d{7})", path.name)
        if not match:
            continue
        day = _doy_to_date(match.group(1))
        if day in result:
            raise ValueError(f"Ambiguous {product}/{layer} for {day}: {result[day]} and {path}")
        result[day] = path
    return result


def _find_doy_layer(data_root: Path, product: str, layer: str, doy: str) -> Path | None:
    return _doy_layers(data_root, product, layer).get(_doy_to_date(doy))


def _processing_period(data_root: Path, year: int | None) -> tuple[date, date]:
    days = sorted(set(_doy_layers(data_root, "MOD11A1", "LST_Day_1km")) | set(_doy_layers(data_root, "MOD11A1", "QC_Day")))
    if not days:
        raise ValueError("No dated MOD11A1 LST or QC inputs were found.")
    selected_year = year if year is not None else days[-1].year
    selected = [day for day in days if day.year == selected_year]
    if not selected:
        raise ValueError(f"No MOD11A1 input dates for {selected_year}.")
    return selected[0], selected[-1]


def _read_aligned(path: Path, template: rasterio.io.DatasetReader) -> np.ndarray:
    with rasterio.open(path) as source:
        if (source.shape, source.transform, source.crs) != (template.shape, template.transform, template.crs):
            raise ValueError(f"Grid mismatch: {path.name}")
        return source.read(1)


def _profile_like(source: rasterio.io.DatasetReader) -> dict[str, Any]:
    profile = source.profile.copy()
    profile.update(driver="GTiff", count=1, dtype="float32", compress="deflate", tiled=True, blockxsize=256, blockysize=256)
    return profile


def _write_tif(path: Path, source: rasterio.io.DatasetReader, data: np.ndarray, *, units: str) -> None:
    profile = _profile_like(source)
    profile.update(nodata=NODATA)
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data.astype(np.float32), 1)
        dst.update_tags(units=units, AREA_OR_POINT="Area")
        dst.set_band_unit(1, units)


def _write_map_tif(path: Path, source: rasterio.io.DatasetReader, data: np.ndarray, map_transform, *, units: str) -> None:
    """Write a display-only projection; analysis continues to use the native file."""
    profile = _profile_like(source)
    profile.update(crs="EPSG:3857", transform=map_transform, height=data.shape[0], width=data.shape[1], nodata=NODATA)
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, 'w', **profile) as dst:
        dst.write(data.astype(np.float32), 1)
        dst.update_tags(units=units, AREA_OR_POINT='Area', purpose='display_only_nearest_neighbor')
        dst.set_band_unit(1, units)


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def _write_png(path: Path, data: np.ndarray, bounds: list[float], render_range: list[float]) -> list[list[float]]:
    valid = np.isfinite(data) & (data != NODATA)
    lo, hi = render_range
    scaled = np.zeros(data.shape, dtype=np.float32)
    if valid.any():
        scaled[valid] = np.clip((data[valid] - lo) / max(hi - lo, 1e-6), 0.0, 1.0)
    red = np.clip(35 + 215 * scaled, 0, 255).astype(np.uint8)
    green = np.clip(92 + 120 * scaled, 0, 255).astype(np.uint8)
    blue = np.clip(190 - 150 * scaled, 0, 255).astype(np.uint8)
    rgba = np.stack([red, green, blue, valid.astype(np.uint8) * 220], axis=-1)
    rows = b"".join(bytes([0]) + rgba[row].tobytes() for row in range(rgba.shape[0]))
    png = b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", struct.pack(">2I5B", rgba.shape[1], rgba.shape[0], 8, 6, 0, 0, 0)) + _png_chunk(b"IDAT", zlib.compress(rows, 6)) + _png_chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    west, south, east, north = bounds
    return [[west, north], [east, north], [east, south], [west, south]]


def _publish_raster(array: np.ndarray, profile_source: rasterio.io.DatasetReader, output: Path, asset_id: str, day: date, variable: str, units: str, method: str) -> dict[str, Any]:
    # Publication owns the unit conversion. Keep the caller's Kelvin array intact
    # because EF and saturation-vapour calculations require Kelvin inputs.
    conversion = None
    if variable == 'lst':
        if units == 'K':
            valid = np.isfinite(array) & (array != NODATA)
            converted = np.full(array.shape, NODATA, dtype=np.float32)
            converted[valid] = (array[valid].astype(np.float64) - 273.15).astype(np.float32)
            array, units, conversion = converted, LST_UNIT, LST_CONVERSION.copy()
        elif units != LST_UNIT:
            raise ValueError('LST publication expects K or °C, not ' + units)
    array = np.where(np.isfinite(array), array, NODATA).astype(np.float32)
    tif_path = output / "rasters" / f"{asset_id}.tif"
    _write_tif(tif_path, profile_source, array, units=units)
    height, width = array.shape
    transform_wgs84, width_wgs84, height_wgs84 = calculate_default_transform(
        profile_source.crs, "EPSG:3857", width, height, *profile_source.bounds
    )
    geographic = np.full((height_wgs84, width_wgs84), NODATA, dtype=np.float32)
    reproject(
        source=array,
        destination=geographic,
        src_transform=profile_source.transform,
        src_crs=profile_source.crs,
        src_nodata=NODATA,
        dst_transform=transform_wgs84,
        dst_crs="EPSG:3857",
        dst_nodata=NODATA,
        resampling=Resampling.nearest,
    )
    map_path = output / "map_rasters" / f"{asset_id}.tif"
    _write_map_tif(map_path, profile_source, geographic, transform_wgs84, units=units)
    east = transform_wgs84.c + width_wgs84 * transform_wgs84.a
    south = transform_wgs84.f + height_wgs84 * transform_wgs84.e
    xs, ys = transform("EPSG:3857", "EPSG:4326", [transform_wgs84.c, east], [south, transform_wgs84.f])
    bounds = [xs[0], ys[0], xs[1], ys[1]]
    values = array[np.isfinite(array) & (array != NODATA)]
    if variable == "lst":
        render_range = [float(np.percentile(values, 2)), float(np.percentile(values, 98))] if values.size else [-3.15, 46.85]
    else:
        render_range = [-0.2, 1.0] if variable == "ndvi" else [0.0, 1.0]
    preview_path = output / "previews" / f"{asset_id}.png"
    coordinates = _write_png(preview_path, geographic, bounds, render_range)
    statistics = {"valid_pixels": int(values.size), "min": float(values.min()) if values.size else None, "max": float(values.max()) if values.size else None, "mean": float(values.mean()) if values.size else None}
    return {
        "id": asset_id,
        "date": day.isoformat(),
        "variable": variable,
        "unit": units,
        **({'unit_conversion': conversion} if conversion else {}),
        "method_version": method,
        "status": "ready" if values.size else "not_ready",
        "raster_path": tif_path.relative_to(output).as_posix(),
        "map_raster_path": map_path.relative_to(output).as_posix(),
        "map_projection": "EPSG:3857",
        "preview_path": preview_path.relative_to(output).as_posix(),
        "bounds": bounds,
        "image_coordinates": coordinates,
        "statistics": statistics,
        "render_range": render_range,
        "preview_projection": "EPSG:3857",
    }


def _read_ndvi_for_day(data_root: Path, day: date, template: rasterio.io.DatasetReader, aoi: dict[str, Any]) -> tuple[np.ndarray, dict[str, Any]]:
    dated = sorted((d, p) for d, p in _doy_layers(data_root, "MOD13A2", "NDVI").items() if d.year == day.year)
    before = [(d, p) for d, p in dated if d <= day]
    after = [(d, p) for d, p in dated if d >= day]
    if not before or not after:
        raise ValueError(f"No bracketing MOD13A2 composites for {day.isoformat()}.")
    d0, p0 = before[-1]
    d1, p1 = after[0]
    with rasterio.open(p0) as ds0, rasterio.open(p1) as ds1:
        for source in (ds0, ds1):
            if source.shape != template.shape or source.transform != template.transform or source.crs != template.crs:
                raise ValueError("MOD13A2 and MOD11A1 grids differ; explicitly align them before processing.")
        raw0, raw1 = ds0.read(1).astype(np.float32), ds1.read(1).astype(np.float32)
        qa0_path = _find_doy_layer(data_root, "MOD13A2", "VI_Quality", p0.name.split("doy", 1)[1][:7])
        qa1_path = _find_doy_layer(data_root, "MOD13A2", "VI_Quality", p1.name.split("doy", 1)[1][:7])
        rel0_path = _find_doy_layer(data_root, "MOD13A2", "pixel_reliability", p0.name.split("doy", 1)[1][:7])
        rel1_path = _find_doy_layer(data_root, "MOD13A2", "pixel_reliability", p1.name.split("doy", 1)[1][:7])
        if not all((qa0_path, qa1_path, rel0_path, rel1_path)):
            raise ValueError("NDVI quality layers are incomplete for the bracketing composites.")
        qa0, qa1 = _read_aligned(qa0_path, template), _read_aligned(qa1_path, template)
        rel0, rel1 = _read_aligned(rel0_path, template), _read_aligned(rel1_path, template)
        # Retain clear land only: exclude inland water, coast, snow and shadow.
        def good_quality(raw: np.ndarray, nodata: float, qa: np.ndarray, reliability: np.ndarray) -> np.ndarray:
            return (raw != nodata) & (raw >= -2000) & (raw <= 10000) & ((qa & 3) == 0) & (reliability == 0) & (((qa >> 11) & 7) == 1) & ((qa & 0xC000) == 0)
        mask0 = good_quality(raw0, ds0.nodata, qa0, rel0)
        mask1 = good_quality(raw1, ds1.nodata, qa1, rel1)
        geom = transform_geom("EPSG:4326", template.crs, aoi["features"][0]["geometry"])
        inside = geometry_mask([geom], out_shape=template.shape, transform=template.transform, invert=True, all_touched=False)
        mask0 &= inside
        mask1 &= inside
        ndvi0 = raw0 * ds0.scales[0] + ds0.offsets[0]
        ndvi1 = raw1 * ds1.scales[0] + ds1.offsets[0]
        weight = 0.0 if d0 == d1 else (day - d0).days / (d1 - d0).days
        both = mask0 & mask1
        result = np.full(template.shape, NODATA, dtype=np.float32)
        result[both] = ndvi0[both] * (1.0 - weight) + ndvi1[both] * weight
        return result, {"before": d0.isoformat(), "after": d1.isoformat(), "weight_after": round(float(weight), 6), "sources": [p.relative_to(data_root).as_posix() for p in (p0, p1, qa0_path, qa1_path, rel0_path, rel1_path)], "quality_rule": "Both composites: MODLAND QA == 0, reliability == 0, land/water bits 11-13 == 1, snow/shadow bits 14-15 == 0", "temporal_assumption": "Linear interpolation between nominal composite start dates, not daily observations", "valid_pixels": int(both.sum())}


def _process_modis(data_root: Path, output: Path, aoi: dict[str, Any], start: date, end: date) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    assets: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    by_date = _doy_layers(data_root, "MOD11A1", "LST_Day_1km")
    qc_dates = _doy_layers(data_root, "MOD11A1", "QC_Day")
    view_dates = _doy_layers(data_root, "MOD11A1", "Day_view_time")
    template_path = next((p for day, p in sorted(by_date.items()) if start <= day <= end), None)
    if template_path is None:
        raise ValueError("No LST template is available for the selected period.")
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        print(f"MODIS {day.isoformat()}", flush=True)
        lst_path = by_date.get(day)
        qc_path, view_path = qc_dates.get(day), view_dates.get(day)
        # Vegetation availability does not depend on the presence of daily LST.
        with rasterio.open(lst_path or template_path) as lst_ds:
            try:
                ndvi, ndvi_info = _read_ndvi_for_day(data_root, day, lst_ds, aoi)
                assets.append(_publish_raster(ndvi, lst_ds, output, f"ndvi-{day.isoformat()}", day, "ndvi", "1", "MOD13A2.061_linear_interpolation"))
            except ValueError as exc:
                ndvi = np.full(lst_ds.shape, NODATA, dtype=np.float32)
                ndvi_info = {"valid_pixels": 0, "status": "not_ready", "reason": str(exc)}
            record = {"date": day.isoformat(), "status": "skipped", "inputs": {"lst": lst_path.relative_to(data_root).as_posix() if lst_path else None, "lst_qc": qc_path.relative_to(data_root).as_posix() if qc_path else None, "day_view_time": view_path.relative_to(data_root).as_posix() if view_path else None, "ndvi": ndvi_info}, "quality": {"lst_valid_after_qa": 0, "ndvi_valid_after_qa": ndvi_info["valid_pixels"], "joint_valid": 0}}
            if lst_path is None or qc_path is None:
                record["reason"] = "missing_lst_input" if lst_path is None else "missing_lst_qc"
                reports.append(record)
                continue
            with rasterio.open(qc_path) as qc_ds:
                qc = qc_ds.read(1)
                qc_shape, qc_transform, qc_crs, qc_nodata = qc_ds.shape, qc_ds.transform, qc_ds.crs, qc_ds.nodata
            if lst_ds.shape != qc_shape or lst_ds.transform != qc_transform or lst_ds.crs != qc_crs:
                record["reason"] = "LST and QC grids differ."
                reports.append(record)
                continue
            raw = lst_ds.read(1).astype(np.float32)
            valid = (raw != lst_ds.nodata) & np.isfinite(raw) & (qc != qc_nodata)
            valid &= (qc & 3) == 0
            valid &= ((qc >> 2) & 3) == 0
            valid &= ((qc >> 6) & 3) <= 1
            geom = transform_geom("EPSG:4326", lst_ds.crs, aoi["features"][0]["geometry"])
            inside = geometry_mask([geom], out_shape=lst_ds.shape, transform=lst_ds.transform, invert=True, all_touched=False)
            valid &= inside
            lst_k = np.full(lst_ds.shape, NODATA, dtype=np.float32)
            lst_k[valid] = raw[valid] * lst_ds.scales[0] + lst_ds.offsets[0]
            joint = valid & (ndvi != NODATA)
            assets.append(_publish_raster(lst_k, lst_ds, output, f"lst-{day.isoformat()}", day, "lst", "K", "MOD11A1.061"))
            baseline = estimate(lst_k, ndvi, valid_mask=joint, parameters=BaselineParameters())
            qc_relpath = Path("quality") / f"ef-{day.isoformat()}-qc.tif"
            _write_tif(output / qc_relpath, lst_ds, baseline["qc"], units="quality_code")
            if baseline["status"] == "ready":
                ef_asset = _publish_raster(baseline["ef"], lst_ds, output, f"ef-{day.isoformat()}", day, "ef", "1", "empirical_tvx_v1_sample")
                ef_asset["scientific_status"] = "engineering_baseline_not_paper_reproduction"
                assets.append(ef_asset)
            else:
                # Remove only this pipeline's obsolete EF outputs from prior runs.
                for folder, suffix in (("rasters", "tif"), ("previews", "png")):
                    (output / folder / f"ef-{day.isoformat()}.{suffix}").unlink(missing_ok=True)
            record.update({
                "status": baseline["status"],
                "reason": ndvi_info.get("reason") or baseline["reason"],
                "quality": {"lst_valid_after_qa": int(valid.sum()), "ndvi_valid_after_qa": ndvi_info["valid_pixels"], "joint_valid": int(joint.sum())},
                "baseline": baseline["diagnostics"],
                "quality_raster": qc_relpath.as_posix(),
                "qc_counts": {str(code): int((baseline["qc"] == code).sum()) for code in np.unique(baseline["qc"])},
            })
            reports.append(record)
    return assets, reports


def _process_smap(data_root: Path, output: Path, aoi: dict[str, Any], start: date, end: date) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    assets: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    seen = set()
    for source in sorted(data_root.rglob("SMAP_L3_SM_P_*.h5")):
        if source.parent.name == LEGACY_SMAP_DIR:
            continue
        match = re.search(r"SMAP_L3_SM_P_(?:E_)?(\d{8})_", source.name)
        if not match:
            raise ValueError(f"Unrecognized SMAP acquisition date: {source.name}")
        day = datetime.strptime(match.group(1), "%Y%m%d").date()
        if not start <= day <= end:
            continue
        if day in seen:
            raise ValueError(f"Multiple SMAP AM products for {day}; select a product explicitly before publishing.")
        seen.add(day)
        print(f"SMAP {day.isoformat()} ({source.name})", flush=True)
        # The HDF5 arrays have no affine transform; coordinates are read explicitly.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", NotGeoreferencedWarning)
            with rasterio.open(source) as container:
                subdatasets = container.subdatasets
        def dataset(field: str) -> str:
            suffix = f"://Soil_Moisture_Retrieval_Data_AM/{field}"
            match = next((name for name in subdatasets if name.endswith(suffix)), None)
            if match is None:
                raise ValueError(f"Missing SMAP AM field {field} in {source.name}.")
            return match
        arrays = {}
        metadata = {}
        for field in ("latitude_centroid", "longitude_centroid", "soil_moisture", "retrieval_qual_flag"):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", NotGeoreferencedWarning)
                with rasterio.open(dataset(field)) as ds:
                    arrays[field] = ds.read(1).astype(np.float32 if field != "retrieval_qual_flag" else np.uint16)
                    if field == "soil_moisture":
                        metadata = ds.tags()
        product = metadata.get("Metadata_DatasetIdentification_shortName")
        version = metadata.get("Metadata_DatasetIdentification_ECSVersionID")
        grid_km = float(metadata.get("Metadata_GridSpatialRepresentation_Column_resolution", 0))
        if product not in {"SPL3SMP", "SPL3SMP_E"} or grid_km not in {9.0, 36.0} or not version:
            raise ValueError(f"Unsupported SMAP product/grid metadata in {source.name}")
        cell_size_m = 36032.220840584 / (4 if grid_km == 9 else 1)
        lat, lon = arrays["latitude_centroid"], arrays["longitude_centroid"]
        sm, qa = arrays["soil_moisture"], arrays["retrieval_qual_flag"]
        if not (lat.shape == lon.shape == sm.shape == qa.shape):
            raise ValueError(f"SMAP geolocation and retrieval arrays differ in {source.name}.")
        # Exact polygon containment (including holes), rather than a bounding box.
        from rasterio.features import rasterize
        from rasterio.transform import from_origin
        # Test geolocated points with the same polygon using a tiny pixel centered
        # on each coordinate. There are only a few dozen candidates in this AOI.
        ring = aoi["features"][0]["geometry"]["coordinates"][0]
        west, south = min(p[0] for p in ring), min(p[1] for p in ring)
        east, north = max(p[0] for p in ring), max(p[1] for p in ring)
        inside = np.isfinite(lat) & np.isfinite(lon) & (lat >= south) & (lat <= north) & (lon >= west) & (lon <= east)
        for row, col in zip(*np.where(inside)):
            tiny = from_origin(float(lon[row, col]) - 5e-7, float(lat[row, col]) + 5e-7, 1e-6, 1e-6)
            inside[row, col] = bool(rasterize([(aoi["features"][0]["geometry"], 1)], out_shape=(1, 1), transform=tiny)[0, 0])
        geolocated = inside & (lat != -9999) & (lon != -9999)
        valid = geolocated & (sm >= 0) & (sm <= 1) & np.isfinite(sm) & (qa == 0)
        features = []
        for row, col in zip(*np.where(valid)):
            features.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [float(lon[row, col]), float(lat[row, col])]}, "properties": {"date": day.isoformat(), "soil_moisture": float(sm[row, col]), "unit": "m3/m3", "quality_flag": int(qa[row, col]), "grid_row": int(row), "grid_col": int(col), "coordinate_source": "EASE_grid_centroid", "product": product, "product_version": version, "cell_size_m": cell_size_m}})
        asset_id = f"smap-am-{day.isoformat()}"
        relpath = Path("vectors") / f"{asset_id}.geojson"
        _write_json(output / relpath, {"type": "FeatureCollection", "features": features})
        assets.append({"id": asset_id, "date": day.isoformat(), "variable": "soil_moisture", "unit": "m3/m3", "method_version": f"{product}.{version}_AM_QA0", "product": product, "grid_spacing_km": grid_km, "cell_size_m": cell_size_m, "source_path": source.relative_to(data_root).as_posix(), "status": "ready" if features else "not_ready", "geojson_path": relpath.as_posix(), "statistics": {"valid_pixels": len(features), "geolocated_grid_cells": int(geolocated.sum()), "quality_rule": "retrieval_qual_flag == 0"}})
        reports.append({"date": day.isoformat(), "status": "ready" if features else "not_ready", "product": product, "product_version": version, "grid_spacing_km": grid_km, "cell_size_m": cell_size_m, "input": source.relative_to(data_root).as_posix(), "grid_shape": list(sm.shape), "geolocated_grid_cells_in_aoi": int(geolocated.sum()), "qa0_valid_cells": len(features), "other_or_missing_quality_cells": int(geolocated.sum()) - len(features)})
    return assets, reports


def _check_swaths(data_root: Path) -> dict[str, Any]:
    groups: dict[tuple[str, str], dict[str, str]] = {}
    pattern = re.compile(r"^(MOD03|MOD06_L2|MOD07_L2)\.A(\d{7})\.(\d{4})\.(\d+)\.")
    for path in data_root.rglob("*"):
        if not path.is_file():
            continue
        match = pattern.match(path.name)
        if match:
            groups.setdefault((match.group(2), match.group(3)), {})[match.group(1)] = match.group(4)
    records = []
    for (doy, overpass), products in sorted(groups.items()):
        records.append({"date": _doy_to_date(doy).isoformat(), "overpass_utc": overpass[:2] + ":" + overpass[2:], "products": products, "complete_triplet": set(products) == {"MOD03", "MOD06_L2", "MOD07_L2"}, "collection_mismatch": len(set(products.values())) > 1})
    return {"matched_granule_count": len(records), "granules": records}


def run(data_root: Path, output: Path, aoi_path: Path, *, hash_files: bool = False, year: int | None = None) -> dict[str, Any]:
    data_root, output, aoi_path = data_root.resolve(), output.resolve(), aoi_path.resolve()
    if not data_root.is_dir():
        raise FileNotFoundError(f"Input data folder not found: {data_root}")
    if data_root == output or data_root in output.parents or output in data_root.parents:
        raise ValueError("Input and output folders must not contain each other.")
    aoi = json.loads(aoi_path.read_text(encoding="utf-8"))
    if not aoi.get("features") or aoi["features"][0].get("geometry", {}).get("type") != "Polygon":
        raise ValueError("AOI must be a GeoJSON FeatureCollection containing a Polygon.")
    start, end = _processing_period(data_root, year)
    print(f"Selected acquisition period: {start} through {end}; inventorying inputs", flush=True)
    input_manifest = inventory(data_root, output, hash_files=hash_files)
    modis_assets, modis_reports = _process_modis(data_root, output, aoi, start, end)
    smap_assets, smap_reports = _process_smap(data_root, output, aoi, start, end)
    assets = modis_assets + smap_assets
    published = sorted((asset for asset in assets if asset["status"] == "ready"), key=lambda item: (item["date"], item["variable"]))
    _write_json(output / "aoi.geojson", aoi)
    created_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    identity = json.dumps([(item["path"], item["size_bytes"], item["modified_at_ns"]) for item in input_manifest["files"]], sort_keys=True)
    release_id = f"local-{start.year}-{hashlib.sha256(identity.encode()).hexdigest()[:10]}-{LST_UNIT_VERSION}"
    catalog = {"schema_version": 1, "data_version": release_id, "created_at_utc": created_at, "status": "ready" if published else "empty", "assets": published, "provenance": {"input_manifest": "input_manifest.json", "aoi": "aoi.geojson", "acquisition_start": start.isoformat(), "acquisition_end": end.isoformat(), "selected_year": start.year, "sample_scope": f"{start} through {end}; latest acquisition year selected; not annual calibration"}}
    report = {
        "status": "completed_with_gaps",
        "data_version": release_id,
        "temperature_units": LST_CONVERSION.copy(),
        "created_at_utc": created_at,
        "acquisition_start": start.isoformat(),
        "acquisition_end": end.isoformat(),
        "selected_year": start.year,
        "selection_policy": "Latest MOD11A1 acquisition year by default; other years inventoried and retained in raw input, not mixed into this release.",
        "input_file_count": input_manifest["file_count"],
        "input_counts": input_manifest["counts_by_product"],
        "invalid_input_count": sum(item["status"] == "invalid" for item in input_manifest["files"]),
        "baseline_parameters": asdict(BaselineParameters()),
        "qc_legend": {"0": "valid EF", "1": "missing or excluded input", "2": "insufficient edge bins", "3": "dry edge <= wet edge", "4": "relative dryness outside [0,1]", "5": "non-decreasing dry edge", "6": "EF outside [0,1]"},
        "legacy_smap_tifs_quarantined": input_manifest["counts_by_product"].get("SMAP_legacy_tif", 0),
        "swath_check": _check_swaths(data_root),
        "modis_dates": modis_reports,
        "smap_dates": smap_reports,
        "published_assets": [asset["id"] for asset in published],
        "published_counts": dict(Counter(asset["variable"] for asset in published)),
        "unpublished_assets": [{"id": asset["id"], "date": asset["date"], "variable": asset["variable"], "reason": "no_valid_pixels_after_qa"} for asset in assets if asset["status"] != "ready"],
        "missing_lst_dates": [item["date"] for item in modis_reports if item.get("reason") == "missing_lst_input"],
        "unavailable_ndvi_dates": [item["date"] for item in modis_reports if item.get("inputs", {}).get("ndvi", {}).get("status") == "not_ready"],
        "limitations": [
            "This selected-period local release is not annual calibration.",
            "Missing LST files, failed QA and unavailable bracketing NDVI composites remain missing; NDVI is not extrapolated beyond the last composite.",
            "MOD03 Collection 007 and MOD06/MOD07 Collection 061 are reported as mixed; do not treat the mixed triplets as final scientific inputs.",
            "ARM EBBR observations are not present; independent station validation is not run.",
            "Legacy 1x1 SMAP GeoTIFFs are quarantined; raw HDF5 subdatasets are used.",
            "NDVI uses nominal composite start-date interpolation; these are not daily NDVI observations.",
            "EF uses surface temperature as an air-temperature proxy; no microwave calibration is applied.",
            "SMAP grid spacing and product version come from each HDF5 product's metadata; enhanced grid spacing is not an independent fine-resolution retrieval or 1 km downscaling.",
            "Non-decreasing fitted dry edges and EF outside [0,1] are rejected, not clipped to plausible values.",
        ],
    }
    _write_json(output / "processing_report.json", report)
    lines = ["# 真实数据处理结果", "", f"输入文件：{input_manifest['file_count']}；发布图层：{len(published)}。", f"本次观测日期：{start} 至 {end}；版本：{release_id}。", "其他年份的原始数据保留，不混入当前时序。", "", "| 日期 | EF状态 | 联合有效像元 | EF输出像元 | 原因 |", "|---|---|---:|---:|---|"]
    for item in modis_reports:
        lines.append(f"| {item['date']} | {item['status']} | {item.get('quality', {}).get('joint_valid', 0)} | {item.get('baseline', {}).get('output_pixels', 0)} | {item.get('reason') or '—'} |")
    lines += ["", "| 日期 | SMAP AM有效格点 | 状态 |", "|---|---:|---|"]
    for item in smap_reports:
        lines.append(f"| {item['date']} | {item['qa0_valid_cells']} | {item['status']} |")
    lines += ["", "## 使用边界", "", "EF为工程基线，尚未完成论文方法复现、年度校准或独立站点验证。", "云污染、缺文件和失败拟合不补造；没有前后两期NDVI时不外推。", "NDVI按合成期名义起始日插值；SMAP保留产品原网格、来源与质量标志。", "MOD03/MOD06/MOD07只检查清单及过境配对，本轮不参与EF运算。", "比例因子、QA规则、源路径、参数、逐日拟合诊断与QC编码见JSON报告和输入清单。"]
    (output / "处理结果说明.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    # Publish the catalog only after every processing stage has completed.
    _write_json(output / "catalog.json", catalog)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--aoi", type=Path, default=DEFAULT_AOI)
    parser.add_argument("--hash-files", action="store_true", help="Compute SHA256 for every input file; may take a while for the raw orbit files.")
    parser.add_argument("--year", type=int, help="Select an acquisition year; defaults to the latest MOD11A1 year present.")
    args = parser.parse_args()
    report = run(args.data_root.resolve(), args.output.resolve(), args.aoi.resolve(), hash_files=args.hash_files, year=args.year)
    print(json.dumps({"status": report["status"], "input_file_count": report["input_file_count"], "published_assets": len(report["published_assets"]), "report": str((args.output / "processing_report.json").resolve())}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
