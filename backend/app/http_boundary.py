"""Request limits and two separate response caches; no OGC passthrough."""
import asyncio
from hashlib import sha256
import os
import re
from time import perf_counter

from starlette.responses import JSONResponse, Response

from .cache import ResponseCache, release_signature

query_cache = ResponseCache(max_entries=512, max_bytes=32 * 1024 * 1024)
tile_cache = ResponseCache(max_entries=512, max_bytes=64 * 1024 * 1024, ttl=900)
QUERY_ENABLED = os.getenv("WEBGIS_QUERY_CACHE", "1") == "1"
TILE_ENABLED = os.getenv("WEBGIS_TILE_CACHE", "1") == "1"
MAX_BODY = 512 * 1024


def policy(path):
    if re.fullmatch(r"/api/v1/assets/[^/]+/quality(?:/point|\.png)?", path): return "quality", {"lon", "lat"} if path.endswith("/point") else set()
    if path == "/api/v1/catalog": return "query", set()
    if path == "/api/v1/statistics": return "query", set()
    if path == "/api/v1/series": return "query", {"lon", "lat", "variable", "start", "end"}
    if re.fullmatch(r"/api/v1/(rasters|vectors)/[^/]+/point", path): return "query", {"lon", "lat"}
    if re.fullmatch(r"/api/v1/assets/[^/]+/coverage", path): return "query", set()
    if re.fullmatch(r"/api/v1/(rasters/[^/]+/render|vectors/[^/]+/grid)", path): return "tile", set()
    if path.startswith("/api/v1/tiles/"): return "tile", {"v"}
    return None, set()


def install_boundary(app, get_root):
    # Striped locks collapse simultaneous identical misses, without unbounded keys.
    locks = [asyncio.Lock() for _ in range(64)]

    @app.middleware("http")
    async def boundary(request, call_next):
        started = perf_counter()
        path = request.url.path
        namespace, allowed = policy(path)
        pairs = list(request.query_params.multi_items())
        if namespace and (any(k not in allowed for k, _ in pairs) or len({k for k, _ in pairs}) != len(pairs)):
            return JSONResponse({"detail": "不支持的参数或重复参数；不接受CQL、任意样式或外部地址。"}, status_code=422)
        if namespace and len(request.scope.get("query_string", b"")) > 4096:
            return JSONResponse({"detail": "query exceeds limit"}, status_code=414)
        body = b""
        if path == "/api/v1/statistics" and request.method == "POST":
            try:
                declared = int(request.headers.get("content-length", "0"))
            except ValueError:
                return JSONResponse({"detail": "invalid content length"}, status_code=400)
            if declared > MAX_BODY:
                return JSONResponse({"detail": "statistics body exceeds 512KiB limit"}, status_code=413)
            # Check streaming requests as well as those declaring a length.
            chunks, count = [], 0
            async for chunk in request.stream():
                count += len(chunk)
                if count > MAX_BODY:
                    return JSONResponse({"detail": "statistics body exceeds 512KiB limit"}, status_code=413)
                chunks.append(chunk)
            body = b"".join(chunks)
            request._body = body
            async def replay_body():
                nonlocal body
                current, body = body, b""
                return {"type": "http.request", "body": current, "more_body": False}
            request._receive = replay_body
        enabled = namespace and namespace != "quality" and (QUERY_ENABLED if namespace == "query" else TILE_ENABLED)
        cache = query_cache if namespace == "query" else tile_cache
        signature = release_signature(get_root()) if namespace else ""
        key = sha256(repr((signature, request.method, path, sorted(pairs), body)).encode()).hexdigest()

        async def resolve():
            if enabled:
                hit = cache.get(key)
                if hit:
                    content, headers = hit
                    headers["X-WebGIS-Cache"] = namespace + ":HIT"
                    return Response(content, headers=headers)
            result = await call_next(request)
            if namespace and result.status_code == 200:
                content = b"".join([chunk async for chunk in result.body_iterator])
                headers = dict(result.headers)
                # Browser caches must not bypass release invalidation checks.
                headers["cache-control"] = "no-store"
                headers.pop("content-length", None)
                if enabled:
                    cache.put(key, content, headers)
                headers["X-WebGIS-Cache"] = namespace + (":MISS" if enabled else ":BYPASS")
                return Response(content, status_code=result.status_code, headers=headers, background=result.background)
            return result

        if enabled:
            async with locks[int(key[:8], 16) % len(locks)]:
                response = await resolve()
        else:
            response = await resolve()
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-WebGIS-Elapsed-Ms"] = f"{(perf_counter() - started) * 1000:.3f}"
        if namespace:
            response.headers["X-WebGIS-Revision"] = signature
        return response
