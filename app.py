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

BASE            = "https://cinejoy.pk"
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
CACHE_TTL = 0               # disabled

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

async def resolve(tmdb_id: int, media_type: str, season: int = 1, episode: int = 1, client_ip: str = None) -> dict:
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

    extra_headers = {"X-Forwarded-For": client_ip} if client_ip else {}
    context = await _browser.new_context(
        viewport={"width": 1280, "height": 720},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        ignore_https_errors=True,
        extra_http_headers=extra_headers,
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

    # Hook fetch + XHR inside the page to catch MSE/blob stream sources
    await context.add_init_script("""
        window.__stream_urls__ = [];
        const _push = (u) => {
            if (!u || typeof u !== 'string') return;
            if (/[.]m3u8|[.]mp4|[.]mpd/.test(u) && !u.startsWith('blob:')) {
                if (!window.__stream_urls__.includes(u)) window.__stream_urls__.push(u);
            }
        };
        const _fetch = window.fetch;
        window.fetch = function(...a) {
            _push(typeof a[0] === 'string' ? a[0] : a[0]?.url);
            return _fetch.apply(this, a);
        };
        const _open = XMLHttpRequest.prototype.open;
        XMLHttpRequest.prototype.open = function(m, u, ...r) {
            _push(u);
            return _open.apply(this, [m, u, ...r]);
        };
    """)

    def on_request(req):
        u = req.url
        parsed_path = urlparse(u).path
        if re.search(r'/(init|seg|chunk|segment)[_\-]?\d*\.(mp4|m4s|ts)$', parsed_path, re.I):
            return
        if any(parsed_path.endswith(ext) for ext in STREAM_EXTS):
            print(f"  [t+{time.monotonic()-t0:.2f}s] stream intercepted: {u}")
            if u not in stream_urls:
                stream_urls.append(u)
                found.set()

    page = await context.new_page()
    page.on("request", on_request)

    print(f"  [debug] navigating to {page_url}")
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
    print(f"  [debug] page loaded t+{time.monotonic()-t0:.2f}s, clicking...")
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

    async def is_fake(url: str) -> bool:
        try:
            import httpx as _httpx
            base_url = url.rsplit("/", 1)[0] + "/"
            cookies = {c["name"]: c["value"] for c in await context.cookies()}
            hdrs = {
                "Referer": f"{BASE}/",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                "Origin": BASE,
            }
            async with _httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
                r = await c.get(url, headers=hdrs, cookies=cookies)
                print(f"  [is_fake] status={r.status_code} url={url}")
                if r.status_code != 200:
                    return True
                # If it ends in .m3u8 and is valid, it is always real — stop here
                if url.endswith(".m3u8") and r.text.strip().startswith("#EXTM3U"):
                    return False
                # Verify it is actually an m3u8 file before doing anything else
                if not r.text.strip().startswith("#EXTM3U"):
                    print(f"  [is_fake] not an m3u8 (got actual image or junk) — FAKE")
                    return True
                image_exts = (".jpg", ".jpeg", ".png", ".webp")
                # segment pattern: playlist_000.jpg, seg_001.png, chunk_002.jpeg etc
                seg_pattern = re.compile(r'(playlist|seg|chunk|segment)[_\-]\d+\.(jpg|jpeg|png|webp)$', re.I)
                has_image_segments = False
                has_real_sub = False
                for line in r.text.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if any(line.endswith(ext) for ext in image_exts):
                        if seg_pattern.search(line):
                            # numbered segment disguised as image — this IS a real playlist
                            has_image_segments = True
                            continue
                        # non-numbered image line — probe as possible sub-playlist
                        sub_url = urljoin(base_url, line)
                        print(f"  [is_fake] probing disguised: {sub_url}")
                        try:
                            sr = await c.get(sub_url, headers=hdrs, cookies=cookies)
                            if sr.status_code == 200 and "#EXTM3U" in sr.text:
                                print(f"  [is_fake] real sub-playlist found: {sub_url}")
                                if sub_url not in stream_urls:
                                    if "1080p" in sub_url:
                                        stream_urls.append(sub_url)  # end = reversed() picks first
                                    else:
                                        stream_urls.insert(0, sub_url)  # start = lower priority
                                    found.set()
                                has_real_sub = True
                        except Exception as ex:
                            print(f"  [is_fake] sub-probe failed: {ex}")
                # real if it has disguised segments OR we found sub-playlists (master is fake but subs are real)
                if has_image_segments:
                    print(f"  [is_fake] disguised-segment playlist — treating as REAL")
                    return False
                return has_real_sub  # fake master that yielded subs → mark fake so we pick a sub next
        except Exception as e:
            print(f"  [is_fake] exception: {e}")
            return True

    server_selectors = [
        '[class*="server"]', '[class*="Server"]',
        '[class*="source"]', '[class*="Source"]',
        'ul[class*="list"] li', '[class*="provider"] li',
        '[class*="btn-server"]',
    ]

    print(f"  [debug] stream_urls so far: {stream_urls}")
    best = None
    tried_urls = set()
    max_attempts = 5
    wait_timeout = RESOLVE_TIMEOUT * 1.5 if media_type == "tv" else RESOLVE_TIMEOUT * 0.85

    for attempt in range(max_attempts):
        def ok(u):
            if u in tried_urls: return False
            return not re.search(r"/(init|seg|chunk|segment)[_\-]?\d*\.(mp4|m4s|ts)$", urlparse(u).path, re.I)
        candidate = next((u for u in reversed(stream_urls) if ok(u)), None)
        if not candidate:
            print(f"  [debug] no candidate yet, waiting up to {wait_timeout:.1f}s...")
            found.clear()
            try:
                await asyncio.wait_for(found.wait(), timeout=wait_timeout)
            except asyncio.TimeoutError:
                break
            candidate = next((u for u in reversed(stream_urls) if ok(u)), None)
        if not candidate:
            break

        tried_urls.add(candidate)
        print(f"  [attempt {attempt+1}] validating: {candidate}")

        if not await is_fake(candidate):
            best = candidate
            print(f"  [attempt {attempt+1}] real stream found")
            break

        print(f"  [attempt {attempt+1}] fake — trying next server...")
        switched = False
        for sel in server_selectors:
            try:
                els = await page.query_selector_all(sel)
                for el in els:
                    if await el.is_visible():
                        await el.click(timeout=2000)
                        switched = True
                        await asyncio.sleep(2)
                        break
                if switched:
                    break
            except Exception:
                pass
        if not switched:
            # Pull any URLs caught by the JS hook
            try:
                js_urls = await page.evaluate("window.__stream_urls__ || []")
                print(f"  [js-hook] urls seen by fetch/XHR: {js_urls}")
                for u in js_urls:
                    if u not in stream_urls:
                        stream_urls.append(u)
                        found.set()
            except Exception as e:
                print(f"  [js-hook] failed: {e}")

            # Dump full page body to find settings/server buttons
            try:
                html = await page.evaluate("""
                    (() => {
                        // find settings button area
                        const candidates = [...document.querySelectorAll('button, [class*="setting"], [class*="server"], [class*="gear"]')];
                        return candidates.map(el => el.outerHTML).join('\n').slice(0, 3000);
                    })()
                """)
                print(f"  [DOM-buttons] {html}")
            except Exception as e:
                print(f"  [DOM] failed: {e}")
            await asyncio.sleep(3)

    await page.evaluate("window.stop()")
    await context.close()

    if not best:
        return {"error": "no stream found"}

    result = {
        "status":  "ok",
        "raw_url": best,
        "all":     list(tried_urls),
        "referer": f"{BASE}/",
        "type":    media_type,
        "tmdb":    tmdb_id,
        **({"season": season, "episode": episode} if media_type == "tv" else {}),
    }
    cache_set(ck, result)

    return result
    
def run_resolve(tmdb_id, media_type, season=1, episode=1, client_ip=None):
    """Submit resolve() to the persistent event loop; block until done."""
    future: Future = asyncio.run_coroutine_threadsafe(
        resolve(tmdb_id, media_type, season, episode, client_ip),
        _loop,
    )
    return future.result(timeout=RESOLVE_TIMEOUT * 2 + 20)


# ── HTTP handler ──────────────────────────────────────────────────────────────

ROUTE_MOVIE = re.compile(r"^/stream/movie/(\d+)$")
ROUTE_TV    = re.compile(r"^/stream/tv/(\d+)/(\d+)/(\d+)$")


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"  {self.address_string()}  {fmt % args}")

    def get_base_url(self):
        host  = self.headers.get("Host", "localhost:8888")
        proto = self.headers.get("X-Forwarded-Proto", "https")
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

            # detect disguised m3u8 (e.g. playlist.jpg that is actually m3u8)
            raw_content = resp.read()
            resp.release_conn()
            is_m3u8 = ".m3u8" in target_url or "mpegurl" in content_type or raw_content.lstrip()[:7] == b"#EXTM3U"
            if is_m3u8:
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                content = raw_content.decode("utf-8", errors="ignore")

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
                self.send_header("Content-Length", str(len(raw_content)))
                self.end_headers()
                self.wfile.write(raw_content)

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
            tmdb_id   = int(m.group(1))
            client_ip = self.headers.get("X-Forwarded-For", "").split(",")[0].strip() or self.address_string()
            print(f"  resolving movie tmdb={tmdb_id} ...")
            t0     = time.monotonic()
            result = run_resolve(tmdb_id, "movie", client_ip=client_ip)
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
            tmdb_id   = int(m.group(1))
            season    = int(m.group(2))
            episode   = int(m.group(3))
            client_ip = self.headers.get("X-Forwarded-For", "").split(",")[0].strip() or self.address_string()
            print(f"  resolving tv tmdb={tmdb_id} s{season}e{episode} ...")
            t0     = time.monotonic()
            result = run_resolve(tmdb_id, "tv", season, episode, client_ip=client_ip)
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
