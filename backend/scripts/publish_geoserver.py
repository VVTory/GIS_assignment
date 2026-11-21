"""Publish the validated local raster release to a loopback GeoServer."""
import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

import httpx
import rasterio
from rasterio.warp import transform_bounds

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.main import DATA_ROOT
from app.map_service import asset_revision, registration
from app.styles import PALETTES, STYLE_VERSION, style_for


def sld_for(style_name, layer_name, spec):
    root = ET.Element("StyledLayerDescriptor", {"version": "1.0.0", "xmlns": "http://www.opengis.net/sld"})
    named = ET.SubElement(root, "NamedLayer")
    ET.SubElement(named, "Name").text = layer_name
    user = ET.SubElement(named, "UserStyle")
    ET.SubElement(user, "Name").text = style_name
    feature = ET.SubElement(user, "FeatureTypeStyle")
    rule = ET.SubElement(feature, "Rule")
    raster = ET.SubElement(rule, "RasterSymbolizer")
    ET.SubElement(raster, "Opacity").text = "1"
    color_map = ET.SubElement(raster, "ColorMap", {"type": "ramp"})
    low, high = spec["range"]
    count = len(spec["colors"])
    for index, color in enumerate(spec["colors"]):
        value = low + (high - low) * index / (count - 1)
        ET.SubElement(color_map, "ColorMapEntry", {"color": color, "quantity": f"{value:.12g}"})
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def clear_layer_tiles(client, base, layer_name):
    """Truncate only this republished layer before marking its revision current.

    Contract: GeoServer User Manual, geowebcache/rest/seed/.
    """
    endpoint = base + f"/gwc/rest/seed/{layer_name}"
    payload = {"seedRequest": {"name": layer_name, "gridSetId": "EPSG:900913", "zoomStart": 0,
                               "zoomStop": 14, "format": "image/png", "type": "truncate", "threadCount": 1}}
    response = client.post(endpoint + ".json", json=payload)
    response.raise_for_status()
    for _ in range(100):
        status = client.get(endpoint + ".json")
        status.raise_for_status()
        tasks = status.json()["long-array-array"]
        if not any(task[-1] in (0, 1) for task in tasks):
            if any(task[-1] == -1 for task in tasks):
                raise RuntimeError(f"Tile truncation aborted for {layer_name}")
            return
        time.sleep(0.2)
    raise RuntimeError(f"Tile truncation is still pending for {layer_name}; release registration withheld")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--variables', nargs='+', choices=sorted(PALETTES), help='Publish only selected variables and preserve verified registrations for others.')
    args = parser.parse_args()
    base = os.getenv("GEOSERVER_URL", "http://127.0.0.1:8600/geoserver").rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("This helper only publishes to a local loopback GeoServer.")
    auth = (os.getenv("GEOSERVER_USER", "admin"), os.getenv("GEOSERVER_PASSWORD", "geoserver"))
    workspace = "webgis"
    client = httpx.Client(base_url=base + "/rest", auth=auth, timeout=120, trust_env=False)
    about = client.get("/about/version.json")
    about.raise_for_status()
    geoserver_version = next(item.get("Version") for item in about.json()["about"]["resource"] if item.get("@name") == "GeoServer")
    if geoserver_version != "2.28.2":
        raise SystemExit(f"Expected GeoServer 2.28.2; found {geoserver_version}.")
    catalog = json.loads((DATA_ROOT / "catalog.json").read_text(encoding="utf-8"))
    all_assets = [item for item in catalog["assets"] if item.get("status") == "ready" and item.get("raster_path")]
    assets = [item for item in all_assets if not args.variables or item["variable"] in args.variables]
    specs = {variable: style_for(variable, assets) for variable in PALETTES}
    # Include the actual scale in the style name so older cached tiles cannot
    # silently retain a previous release's temperature range.
    from hashlib import sha256
    style_revision = {variable: sha256(json.dumps(specs[variable], sort_keys=True).encode()).hexdigest()[:10] for variable in PALETTES}
    styles = {variable: f"{workspace}_{variable}_{STYLE_VERSION}_{style_revision[variable]}" for variable in PALETTES}

    exists = client.get(f"/workspaces/{workspace}.json")
    if exists.status_code == 404:
        response = client.post("/workspaces", headers={"Content-Type": "text/xml"}, content=f"<workspace><name>{workspace}</name></workspace>")
        response.raise_for_status()
    elif exists.is_error:
        exists.raise_for_status()

    for variable, style_name in styles.items():
        sld = sld_for(style_name, f"{workspace}:style-template", specs[variable])
        current = client.get(f"/styles/{style_name}.json")
        if current.status_code == 404:
            response = client.post("/styles", params={"name": style_name}, headers={"Content-Type": "application/vnd.ogc.sld+xml"}, content=sld)
            response.raise_for_status()
        elif current.is_error:
            current.raise_for_status()

    manifest = {"schema_version": 1, "geoserver_version": geoserver_version, "style_version": STYLE_VERSION, "layers": {}}
    if args.variables:
        try:
            existing = json.loads((DATA_ROOT / 'map_services.json').read_text(encoding='utf-8')).get('layers', {})
        except (OSError, ValueError):
            existing = {}
        manifest['layers'] = {item['id']: existing[item['id']] for item in all_assets
                              if item['variable'] not in args.variables and item['id'] in existing
                              and registration(DATA_ROOT, item, style_for(item['variable'], all_assets), catalog.get('data_version'))}
    for item in assets:
        asset_id = item["id"]
        layer_name = f"{workspace}:{asset_id}"
        previous_layer = client.get(f"/layers/{layer_name}.json")
        if previous_layer.status_code != 404:
            previous_layer.raise_for_status()
        republishing = previous_layer.status_code == 200
        store = f"{workspace}/coveragestores/{asset_id}"
        path = (DATA_ROOT / item.get("map_raster_path", item["raster_path"])).resolve()
        if not path.is_relative_to(DATA_ROOT.resolve()):
            raise ValueError(f"Raster path leaves release root: {asset_id}")
        endpoint = f"/workspaces/{store}/file.geotiff"
        with path.open("rb") as stream:
            response = client.put(endpoint, params={"configure": "all"}, headers={"Content-Type": "image/tiff"}, content=stream.read())
        if response.status_code not in (200, 201):
            response.raise_for_status()

        coverage_endpoint = f"/workspaces/{store}/coverages/{asset_id}.json"
        coverage = client.get(coverage_endpoint)
        coverage.raise_for_status()
        coverage_config = {"enabled": True, "advertised": True}
        if item.get('map_raster_path'):
            with rasterio.open(path) as display:
                if display.crs.to_epsg() != 3857:
                    raise ValueError(f"Display raster must be EPSG:3857: {asset_id}")
                west, south, east, north = display.bounds
                lon0, lat0, lon1, lat1 = transform_bounds(display.crs, 'EPSG:4326', *display.bounds)
            coverage_config.update(nativeCRS='EPSG:3857', srs='EPSG:3857', projectionPolicy='FORCE_DECLARED',
                                   nativeBoundingBox={'minx': west, 'maxx': east, 'miny': south, 'maxy': north, 'crs': 'EPSG:3857'},
                                   latLonBoundingBox={'minx': lon0, 'maxx': lon1, 'miny': lat0, 'maxy': lat1, 'crs': 'EPSG:4326'})
        response = client.put(coverage_endpoint, headers={"Content-Type": "application/json"},
                              content=json.dumps({"coverage": coverage_config}))
        response.raise_for_status()
        style_name = styles[item["variable"]]
        layer_xml = f"<layer><defaultStyle><name>{style_name}</name></defaultStyle></layer>"
        response = client.put(f"/layers/{layer_name}", headers={"Content-Type": "text/xml"}, content=layer_xml)
        response.raise_for_status()
        if republishing:
            clear_layer_tiles(client, base, layer_name)
        spec = style_for(item["variable"], assets)
        revision = asset_revision(DATA_ROOT, item, spec, catalog.get("data_version"))
        manifest["layers"][asset_id] = {"layer": layer_name, "style": style_name, "revision": revision}
        print(f"published {asset_id} -> {layer_name}", flush=True)

    target = DATA_ROOT / "map_services.json"
    temporary = target.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")
    temporary.replace(target)
    print(f"registered {len(assets)} raster layers in {target}")


if __name__ == "__main__":
    main()
