"""Repeatable in-process benchmark against one real published sample raster.

Run from backend with the project's virtual environment. This is a small
coursework benchmark, not a production load test.
"""
import argparse
import csv
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from time import perf_counter
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient

from app.main import DATA_ROOT, app
from app.http_boundary import query_cache


def percentile(values, p):
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, int((len(ordered) * p + .999999) - 1)))]


def run(requests, concurrency, enabled, url):
    import app.http_boundary as boundary

    boundary.QUERY_ENABLED = enabled
    query_cache.clear()
    client = TestClient(app)

    def request_once(_):
        started = perf_counter()
        response = client.get(url)
        local_ms = (perf_counter() - started) * 1000
        return response.status_code, local_ms, float(response.headers.get("x-webgis-elapsed-ms", "0")), response.headers.get("x-webgis-cache", "")

    cold = request_once(0)
    started = perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(request_once, range(requests)))
    elapsed = perf_counter() - started
    latencies = [row[1] for row in results]
    app_latencies = [row[2] for row in results]
    errors = sum(status != 200 for status, *_ in results)
    return {
        "cache_enabled": enabled, "concurrency": concurrency, "requests": requests,
        "cold_status": cold[0], "cold_cache": cold[3], "cold_ms": round(cold[1], 3),
        "median_ms": round(median(latencies), 3), "p95_ms": round(percentile(latencies, .95), 3),
        "app_median_ms": round(median(app_latencies), 3),
        "throughput_rps": round(requests / elapsed, 2), "errors": errors,
        "cache_hits": sum(":HIT" in row[3] for row in results),
        "cache_bypasses": sum(":BYPASS" in row[3] for row in results),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", type=int, default=60)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[2] / "reports")
    args = parser.parse_args()
    catalog = json.loads((DATA_ROOT / "catalog.json").read_text(encoding="utf-8"))
    asset = next(a for a in catalog["assets"] if a.get("status") == "ready" and a.get("raster_path"))
    url = f"/api/v1/rasters/{asset['id']}/point?lon=-97.8&lat=36.5"
    rows = [run(args.requests, workers, cache, url)
            for cache in (False, True) for workers in (1, 5, 10)]
    args.output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    csv_path = args.output / f"webgis-benchmark-{stamp}.csv"
    json_path = args.output / f"webgis-benchmark-{stamp}.json"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)
    environment = {"python": __import__("sys").version.split()[0], "release": catalog.get("data_version"),
                   "asset": asset["id"], "endpoint": url, "requests_per_case": args.requests,
                   "concurrency_levels": [1, 5, 10], "run_kind": "in-process TestClient; coursework microbenchmark"}
    json_path.write_text(json.dumps({"environment": environment, "results": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"csv": str(csv_path), "json": str(json_path), "results": rows}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
