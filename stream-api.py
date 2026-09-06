#!/usr/bin/env python3
"""
stream_api.py  (optimised)
──────────────────────────
Key changes vs original:
  - Single persistent Playwright browser (launched once at startup)
  - Resource blocking: images, fonts, css, tracking abort before page processes them
  - Page navigation aborted immediately on stream found (no more polling delay)
  - TV player fix: broader selector sweep + direct video.play() JS fallback
  - Asyncio event loop runs permanently in a background thread; HTTP handler
    submits coroutines to it via run_coroutine_threadsafe (no new loop per request)
"""

import asyncio
import json
import re
import time
import argparse
from concurrent.futures import Future
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs, quote, urljoin
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
http_client = urllib3.PoolManager(cert_reqs='CERT_NONE')

try:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
except ImportError:
    raise SystemExit("pip install playwright && playwright install chromium")

try:
    from playwright_stealth import Stealth
except ImportError:
    raise SystemExit("pip install playwright-stealth")

# ── Config ────────────────────────────────────────────────────────────────────

BASE            = "https://cinejoy.to"
STREAM_EXTS     = (".m3u8", ".mp4", ".mpd")
RESOLVE_TIMEOUT = 35   # seconds

# Resources to block — saves 4–6s of page load time
BLOCKED_TYPES = {
    "image", "media", "font", "stylesheet",
    "ping", "other",
}
BLOCKED_URL_PATTERNS = (
    "google-analytics", "googletagmanager", "doubleclick",
    "facebook.net", "hotjar", "clarity.ms", "ads",
    "cdn-cgi/rum", "beacon.min.js", "cloudflareinsights",
)

BLOCKED_CHUNKS = set()  # reserved for future safe chunk blocking

# ── Stream cache ──────────────────────────────────────────────────────────────
import threading

_cache: dict = {}           # key -> {result, expires}
_cache_lock = threading.Lock()
CACHE_TTL = 1800            # 30 minutes

def cache_key(tmdb_id, media_type, season=1, episode=1):
    if media_type == "movie":
        return f"movie:{tmdb_id}"
    return f"tv:{tmdb_id}:{season}:{episode}"

def cache_get(key):
    with _cache_lock:
        entry = _cache.get(key)
        if entry and time.monotonic() < entry["expires"]:
            return entry["result"]
        return None

def cache_set(key, result):
    with _cache_lock:
        _cache[key] = {"result": result, "expires": time.monotonic() + CACHE_TTL}





# ── Global browser state ──────────────────────────────────────────────────────

_browser      = None
_stealth      = None
_loop         = None   # the persistent asyncio loop running in bg thread

# ── Resolver ──────────────────────────────────────────────────────────────────

async def resolve(tmdb_id: int, media_type: str, season: int = 1, episode: int = 1) -> dict:
    global _browser, _stealth

    if media_type == "movie":
        page_url = f"{BASE}/watch/movie/{tmdb_id}"
    else:
        page_url = f"{BASE}/watch/tv/{tmdb_id}/{season}/{episode}"

    # ── Cache check ───────────────────────────────────────────────────────────
    ck = cache_key(tmdb_id, media_type, season, episode)
    cached = cache_get(ck)
    if cached:
        print(f"  cache hit: {ck}")
        return {**cached, "cached": True}

    stream_urls: list[str] = []
    found = asyncio.Event()

    t0 = time.monotonic()

    context = await _browser.new_context(
        viewport={"width": 1280, "height": 720},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        ignore_https_errors=True,
    )
    await _stealth.apply_stealth_async(context)


    async def handle_route(route):
        req = route.request
        if req.resource_type in BLOCKED_TYPES:
            await route.abort()
            return
        if any(p in req.url for p in BLOCKED_URL_PATTERNS):
            await route.abort()
            return
        if any(c in req.url for c in BLOCKED_CHUNKS):
            await route.abort()
            return
        await route.continue_()

    await context.route("**/*", handle_route)

    def on_request(req):
        u = req.url
        if any(u.endswith(ext) for ext in STREAM_EXTS):
            print(f"  [t+{time.monotonic()-t0:.2f}s] stream intercepted: {u}")
            if u not in stream_urls:
                stream_urls.append(u)
                found.set()

    page = await context.new_page()
    page.on("request", on_request)

    try:
        await page.goto(
            page_url,
            timeout=RESOLVE_TIMEOUT * 1000,
            wait_until="domcontentloaded",
        )
        def on_response(resp):
            pass  # keep response pipeline active
        page.on("response", on_response)

    except PWTimeout:
        await context.close()
        return {"error": "page load timed out"}
    except Exception as e:
        await context.close()
        return {"error": str(e)[:120]}

    # Click play
    play_selectors = [
        'button[class*="play"]', '[class*="play-btn"]',
        '.jw-icon-display', '[aria-label="Play"]',
        '[class*="Play"]', '.play-button', '#play',
        '[class*="player"] button', '.vjs-big-play-button',
        '[data-testid*="play"]',
    ]
    clicked = False
    for sel in play_selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                await el.click(timeout=2000)
                clicked = True

                break
        except Exception:
            pass

    if not clicked:

        try:
            await page.evaluate("""
                const v = document.querySelector('video');
                if (v) { v.muted = true; v.play().catch(()=>{}); }
            """)
        except Exception:
            pass

    wait_timeout = RESOLVE_TIMEOUT * 1.5 if media_type == "tv" else RESOLVE_TIMEOUT * 0.85
    try:
        await asyncio.wait_for(found.wait(), timeout=wait_timeout)
        await page.evaluate("window.stop()")
    except asyncio.TimeoutError:
        pass
    await context.close()

    if not stream_urls:
        return {"error": "no stream found"}

    def score(u):
        if "playlist" in u:                          return 0
        if "video_" not in u and "audio_" not in u:  return 1
        return 2

    best = sorted(stream_urls, key=score)[0]

    result = {
        "status":  "ok",
        "raw_url": best,
        "all":     stream_urls,
        "referer": f"{BASE}/",
        "type":    media_type,
        "tmdb":    tmdb_id,
        **({"season": season, "episode": episode} if media_type == "tv" else {}),
    }
    cache_set(ck, result)

    return result
    
def run_resolve(tmdb_id, media_type, season=1, episode=1):
    """Submit resolve() to the persistent event loop; block until done."""
    future: Future = asyncio.run_coroutine_threadsafe(
        resolve(tmdb_id, media_type, season, episode),
        _loop,
    )
    return future.result(timeout=RESOLVE_TIMEOUT + 10)


# ── HTTP handler ──────────────────────────────────────────────────────────────

ROUTE_MOVIE = re.compile(r"^/stream/movie/(\d+)$")
ROUTE_TV    = re.compile(r"^/stream/tv/(\d+)/(\d+)/(\d+)$")


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"  {self.address_string()}  {fmt % args}")

    def get_base_url(self):
        host  = self.headers.get("Host", "localhost:8888")
        proto = self.headers.get("X-Forwarded-Proto", "http")
        return f"{proto}://{host}"

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def json(self, code: int, data: dict):
        body = json.dumps(data, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def handle_proxy(self):
        parsed_url = urlparse(self.path)
        params     = parse_qs(parsed_url.query)
        target_url = params.get("url", [None])[0]

        if not target_url:
            self.json(400, {"error": "Missing 'url' parameter"})
            return

        client_ip = (
            self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or self.address_string()
        )

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0.0.0 Safari/537.36",
            "Referer":    f"{BASE}/",
            "Origin":     BASE,
            "X-Forwarded-For": client_ip,
        }
        if "Range" in self.headers:
            headers["Range"] = self.headers["Range"]

        try:
            resp = http_client.request("GET", target_url, headers=headers, preload_content=False)

            self.send_response(resp.status)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "*")

            content_type = resp.headers.get("Content-Type", "application/octet-stream")
            self.send_header("Content-Type", content_type)

            if ".m3u8" in target_url or "mpegurl" in content_type:
                content = resp.read().decode("utf-8", errors="ignore")
                resp.release_conn()

                proxy_base      = f"{self.get_base_url()}/proxy?url="
                rewritten_lines = []

                for line in content.splitlines():
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#"):
                        abs_url = urljoin(target_url, stripped)
                        rewritten_lines.append(f"{proxy_base}{quote(abs_url, safe='')}")
                    elif 'URI="' in line:
                        def replace_uri(match):
                            uri     = match.group(1)
                            abs_url = urljoin(target_url, uri)
                            return f'URI="{proxy_base}{quote(abs_url, safe="")}"'
                        rewritten_lines.append(re.sub(r'URI="([^"]+)"', replace_uri, line))
                    else:
                        rewritten_lines.append(line)

                body = "\n".join(rewritten_lines).encode("utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                if "Content-Length" in resp.headers:
                    self.send_header("Content-Length", resp.headers["Content-Length"])
                if "Content-Range" in resp.headers:
                    self.send_header("Content-Range", resp.headers["Content-Range"])
                self.end_headers()
                for chunk in resp.stream(32768):
                    self.wfile.write(chunk)
                resp.release_conn()

        except Exception as e:
            self.json(500, {"error": f"Proxy request failed: {str(e)}"})

    def do_GET(self):
        path = urlparse(self.path).path

        if path in ("/", "/health"):
            self.json(200, {"status": "ok", "routes": [
                "/stream/movie/<tmdb_id>",
                "/stream/tv/<tmdb_id>/<season>/<episode>",
                "/proxy?url=<encoded_url>",
            ]})
            return

        if path == "/proxy":
            self.handle_proxy()
            return

        m = ROUTE_MOVIE.match(path)
        if m:
            tmdb_id = int(m.group(1))
            print(f"  resolving movie tmdb={tmdb_id} ...")
            t0     = time.monotonic()
            result = run_resolve(tmdb_id, "movie")
            result["elapsed_ms"] = int((time.monotonic() - t0) * 1000)

            if result.get("status") == "ok":
                result["url"] = f"{self.get_base_url()}/proxy?url={quote(result['raw_url'], safe='')}"
                code = 200
            else:
                code = 502

            self.json(code, result)
            return

        m = ROUTE_TV.match(path)
        if m:
            tmdb_id = int(m.group(1))
            season  = int(m.group(2))
            episode = int(m.group(3))
            print(f"  resolving tv tmdb={tmdb_id} s{season}e{episode} ...")
            t0     = time.monotonic()
            result = run_resolve(tmdb_id, "tv", season, episode)
            result["elapsed_ms"] = int((time.monotonic() - t0) * 1000)

            if result.get("status") == "ok":
                result["url"] = f"{self.get_base_url()}/proxy?url={quote(result['raw_url'], safe='')}"
                code = 200
            else:
                code = 502

            self.json(code, result)
            return

        self.json(404, {"error": "unknown route"})


# ── Startup ───────────────────────────────────────────────────────────────────

async def start_browser():
    global _browser, _stealth
    pw       = await async_playwright().start()
    _browser = await pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--mute-audio",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-default-apps",
            "--no-first-run",
        ],
    )
    _stealth = Stealth()
    print("  browser ready")


def run_event_loop(loop: asyncio.AbstractEventLoop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


def main():
    global _loop

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", default=8888, type=int)
    args = parser.parse_args()

    # Start the persistent asyncio loop in a background thread
    _loop = asyncio.new_event_loop()
    import threading
    t = threading.Thread(target=run_event_loop, args=(_loop,), daemon=True)
    t.start()

    # Launch browser inside that loop and wait for it to be ready
    future = asyncio.run_coroutine_threadsafe(start_browser(), _loop)
    future.result(timeout=30)

    server = HTTPServer((args.host, args.port), Handler)
    print(f"  stream-api on http://{args.host}:{args.port}")
    print(f"  movie:  http://localhost:{args.port}/stream/movie/<tmdb_id>")
    print(f"  tv:     http://localhost:{args.port}/stream/tv/<tmdb_id>/<season>/<episode>")
    print(f"  proxy:  http://localhost:{args.port}/proxy?url=<url>")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  bye.")


if __name__ == "__main__":
    main()
