"""Exercise the live local GeoServer, its GWC tile route and API cache."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.main import DATA_ROOT, GEOSERVER_URL, app


def main():
    service = httpx.get(GEOSERVER_URL + "/rest/about/version.json", auth=("admin", "geoserver"), timeout=15, trust_env=False)
    service.raise_for_status()
    version_data = service.json()["about"]["resource"]
    versions = {item["@name"]: item.get("Version") for item in version_data if item.get("@name") in {"GeoServer", "GeoWebCache"}}
    client = TestClient(app)
    assets = client.get("/api/v1/catalog").json()["assets"]
    rasters = [item for item in assets if item.get("tile_url")]
    checks = []
    for item in rasters:
        url = f"/api/v1/tiles/{item['id']}/5/7/12.png?v=acceptance"
        first = client.get(url)
        second = client.get(url)
        passed = (first.status_code == 200 and second.status_code == 200
                  and first.headers.get("x-map-provider") == "geoserver-geowebcache"
                  and second.headers.get("x-webgis-cache") == "tile:HIT"
                  and first.content.startswith(bytes([137, 80, 78, 71, 13, 10, 26, 10])))
        checks.append({"asset": item["id"], "status": first.status_code, "provider": first.headers.get("x-map-provider"),
                       "gwc": first.headers.get("x-gwc-cache"), "api_cache_repeat": second.headers.get("x-webgis-cache"),
                       "png_bytes": len(first.content), "passed": passed})
    result = {"checked_at_utc": datetime.now(timezone.utc).isoformat(), "service_url": GEOSERVER_URL,
              "service_versions": versions, "release": DATA_ROOT.name, "raster_count": len(rasters),
              "passed": all(item["passed"] for item in checks), "checks": checks}
    out = Path(__file__).resolve().parents[2] / "reports" / "geoserver-verification.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")
    print(json.dumps({"report": str(out), "passed": result["passed"], "count": len(checks), "versions": versions}, ensure_ascii=False))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
