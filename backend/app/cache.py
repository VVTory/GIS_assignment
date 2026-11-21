"""Bounded in-process caches for this single-worker local deployment.

Keys include the release manifest, dependency file identities, style version and
the entire validated request. Failures are never stored. No public purge route.
"""
from collections import OrderedDict
from hashlib import sha256
import json
from pathlib import Path
from threading import RLock
from time import monotonic

from .styles import STYLE_VERSION


def release_signature(root):
    root = Path(root).resolve()
    path = root / "catalog.json"
    if not path.is_file():
        return sha256(str(root).encode()).hexdigest()[:24]
    blob = path.read_bytes()
    try:
        document = json.loads(blob)
    except ValueError:
        document = {}
    records = [str(root), sha256(blob).hexdigest(), STYLE_VERSION]
    paths = {"aoi.geojson", "processing_report.json", "map_services.json"}
    for asset in document.get("assets", []):
        paths.update(asset[k] for k in ("raster_path", "map_raster_path", "geojson_path") if asset.get(k))
    for relative in sorted(paths):
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
            stat = candidate.stat()
            records.append([relative, stat.st_size, stat.st_mtime_ns])
        except (ValueError, OSError):
            records.append([relative, "unavailable"])
    return sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()[:24]


class ResponseCache:
    def __init__(self, max_entries=256, max_bytes=32 * 1024 * 1024, ttl=300):
        self.entries = OrderedDict()
        self.max_entries, self.max_bytes, self.ttl = max_entries, max_bytes, ttl
        self.size = 0
        self.lock = RLock()

    def get(self, key):
        with self.lock:
            item = self.entries.get(key)
            if item is None:
                return None
            expiry, body, headers = item
            if expiry < monotonic():
                self.entries.pop(key)
                self.size -= len(body)
                return None
            self.entries.move_to_end(key)
            return body, headers.copy()

    def put(self, key, body, headers):
        if len(body) > self.max_bytes:
            return
        with self.lock:
            old = self.entries.pop(key, None)
            if old:
                self.size -= len(old[1])
            self.entries[key] = (monotonic() + self.ttl, body, headers.copy())
            self.size += len(body)
            while len(self.entries) > self.max_entries or self.size > self.max_bytes:
                _, removed = self.entries.popitem(last=False)
                self.size -= len(removed[1])

    def clear(self):
        with self.lock:
            self.entries.clear()
            self.size = 0
