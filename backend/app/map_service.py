"""Only registered immutable GeoServer layers can be requested by the browser."""
from hashlib import sha256
import json
from pathlib import Path

import httpx
from fastapi import HTTPException

from .styles import STYLE_VERSION


def asset_revision(root, item, spec, data_version):
    path = (Path(root) / item["raster_path"]).resolve()
    path.relative_to(Path(root).resolve())
    stat = path.stat()
    content = [data_version, item["id"], item.get("method_version"), stat.st_size, stat.st_mtime_ns, spec, STYLE_VERSION]
    if item.get('map_raster_path'):
        display_path = (Path(root) / item['map_raster_path']).resolve()
        display_path.relative_to(Path(root).resolve())
        display_stat = display_path.stat()
        content.append(['display', display_stat.st_size, display_stat.st_mtime_ns, item.get('map_projection')])
    return sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()[:16]


def registration(root, item, spec, data_version):
    try:
        root = Path(root)
        manifest = json.loads((root / "map_services.json").read_text(encoding="utf-8"))
        record = manifest["layers"][item["id"]]
        if record["layer"] != f"webgis:{item['id']}":
            return None
        if record["revision"] == asset_revision(root, item, spec, data_version):
            return record
        # Preserve registrations of untouched variables in a unit-only release.
        catalog = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
        provenance = catalog.get("provenance", {})
        conversion = provenance.get("temperature_units", {})
        previous = provenance.get("previous_data_version")
        if (previous and item.get("variable") != conversion.get("variable")
                and record["revision"] == asset_revision(root, item, spec, previous)):
            return record
    except (OSError, KeyError, ValueError):
        pass
    return None


def fetch_tile(base_url, record, z, x, y, cached=True):
    # Public XYZ uses north-origin rows; GWC TMS uses south-origin rows.
    if cached:
        endpoint = base_url.rstrip('/') + "/gwc/service/tms/1.0.0/" + record["layer"] + "@EPSG:900913@png/" + f"{z}/{x}/{2 ** z - 1 - y}.png"
        parameters = None
    else:
        half = 20037508.342789244
        span = 2 * half / (2 ** z)
        west, north = -half + x * span, half - y * span
        endpoint = base_url.rstrip('/') + "/wms"
        parameters = {"service": "WMS", "version": "1.1.1", "request": "GetMap", "layers": record["layer"],
                      "styles": "", "srs": "EPSG:3857", "bbox": f"{west},{north-span},{west+span},{north}",
                      "width": 256, "height": 256, "format": "image/png", "transparent": "true"}
    try:
        response = httpx.get(endpoint, params=parameters, timeout=12, follow_redirects=False, trust_env=False)
        if response.status_code != 200 or not response.content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("map service did not return a PNG")
        if len(response.content) > 4 * 1024 * 1024:
            raise ValueError("map response exceeds budget")
        return response.content, response.headers.get("geowebcache-cache-result", "unknown")
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(503, detail="地图服务暂不可用，请稍后重试；数值查询仍可使用。") from exc
