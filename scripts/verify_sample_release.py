"""Verify the actual installed sample files and their public API responses."""
from collections import Counter
import json
from pathlib import Path

import numpy as np
import rasterio
from rasterio.warp import transform
from rasterio.warp import transform_geom
from rasterio.features import geometry_mask
from fastapi.testclient import TestClient

from backend.app.main import app, DATA_ROOT


def verify() -> dict:
    root = Path(DATA_ROOT)
    raw = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
    client = TestClient(app)
    published = client.get("/api/v1/catalog").json()
    assert published["status"] == "ready", published
    assert len(published["assets"]) == len(raw["assets"])
    raster_count = point_count = 0
    examples = []
    domain = client.get("/api/v1/study-area").json()["features"][0]["geometry"]
    region = {"type": "Polygon", "coordinates": [[[-98.2, 36.15], [-96.9, 36.15], [-96.9, 37.15], [-98.2, 37.15], [-98.2, 36.15]]]}
    analysis_checks = []
    for asset in raw["assets"]:
        asset_id = asset["id"]
        if "raster_path" in asset:
            with rasterio.open(root / asset["raster_path"]) as source:
                data = source.read(1, masked=True)
                valid = ~np.ma.getmaskarray(data)
                values = data.compressed()
                assert values.size == asset["statistics"]["valid_pixels"], asset_id
                assert np.isfinite(values).all(), asset_id
                if asset["variable"] == "lst":
                    assert asset.get("unit") == "°C", asset_id
                    assert source.units[0] == "°C" and source.tags().get("units") == "°C", asset_id
                    conversion = asset.get("unit_conversion", {})
                    assert conversion.get("formula") == "degC = K - 273.15" and conversion.get("ef_calculation_unit") == "K", asset_id
                if asset["variable"] == "ef":
                    assert np.all((values >= 0) & (values <= 1)), asset_id
                rows, cols = np.where(valid)
                row, col = int(rows[len(rows) // 2]), int(cols[len(cols) // 2])
                x, y = source.xy(row, col)
                lon, lat = transform(source.crs, "EPSG:4326", [x], [y])
                response = client.get(f"/api/v1/rasters/{asset_id}/point", params={"lon": lon[0], "lat": lat[0]})
                point = response.json()
                assert response.status_code == 200 and point["valid"], point
                assert abs(point["value"] - float(data[row, col])) < 1e-5
                assert point["unit"] == asset.get("unit"), asset_id
                # Independent full-array masks check the API's windowed analysis.
                def expected(geometry):
                    inside = geometry_mask([transform_geom("EPSG:4326", source.crs, geometry)],
                                           out_shape=data.shape, transform=source.transform, invert=True)
                    keep = inside & ~np.ma.getmaskarray(data) & np.isfinite(data.data)
                    selected = data.data[keep].astype(np.float64)
                    return int(inside.sum()), selected
                total, covered = expected(domain)
                coverage = client.get(f"/api/v1/assets/{asset_id}/coverage").json()
                assert coverage["total_pixels"] == total and coverage["valid_pixels"] == covered.size
                response = client.post("/api/v1/statistics", json={"asset_ids": [asset_id], "geometry": region})
                assert response.status_code == 200, response.text
                result = response.json()["results"][0]
                region_total, selected_values = expected(region)
                assert result["total_pixels"] == region_total and result["valid_pixels"] == selected_values.size
                for field, operation in [("min", np.min), ("max", np.max), ("mean", np.mean), ("std", np.std)]:
                    if selected_values.size:
                        assert abs(result[field] - float(operation(selected_values))) < 1e-8, (asset_id, field)
                    else:
                        assert result[field] is None
                analysis_checks.append({"asset_id": asset_id, "aoi_valid": int(covered.size), "aoi_candidates": total, "region_valid": int(selected_values.size)})
                if asset["variable"] == "ef":
                    examples.append({"asset_id": asset_id, "lon": lon[0], "lat": lat[0], "value": point["value"]})
            preview = client.get(f"/api/v1/rasters/{asset_id}/preview")
            assert preview.status_code == 200 and preview.content.startswith(b"\x89PNG\r\n\x1a\n")
            raster_count += 1
        else:
            response = client.get(f"/api/v1/vectors/{asset_id}")
            features = response.json()["features"]
            assert len(features) == asset["statistics"]["valid_pixels"] > 0
            lon, lat = features[0]["geometry"]["coordinates"]
            point = client.get(f"/api/v1/vectors/{asset_id}/point", params={"lon": lon, "lat": lat}).json()
            assert point["valid"] and point["distance_m"] == 0
            assert point["value"] == features[0]["properties"]["soil_moisture"]
            point_count += 1
    assert client.get("/api/v1/study-area").status_code == 200
    report = client.get("/api/v1/processing-report").json()
    assert report["invalid_input_count"] == 0
    summary = {"status": "passed", "input_files": report["input_file_count"], "assets_by_variable": dict(Counter(item["variable"] for item in raw["assets"])), "raster_queries_checked": raster_count, "smap_queries_checked": point_count, "coverage_and_statistics_checked": analysis_checks, "example_ef_points": examples}
    (root / "verification.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


if __name__ == "__main__":
    print(json.dumps(verify(), ensure_ascii=False, indent=2))
