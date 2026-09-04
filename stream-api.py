#!/usr/bin/env python3
"""
stream_api.py
─────────────
REST API that resolves a TMDB ID → live HLS stream URL with a built-in CORS/Referer proxy.

Endpoints:
    GET /stream/movie/<tmdb_id>
    GET /stream/tv/<tmdb_id>/<season>/<episode>
    GET /proxy?url=<encoded_url>

Returns (from /stream/...):
    {
        "status": "ok",
        "url":    "http://localhost:8888/proxy?url=https%3A%2F%2Finfo.movieboxnoob.cc...",
        "raw_url": "https://info.movieboxnoob.cc/playlist/...",
        "referer": "https://cinejoy.to/",
        "type":   "movie",
        "tmdb":   27205
    }
"""

import asyncio
import json
import re
import time
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs, quote, unquote, urljoin
from threading import Thread
import urllib3

# Disable insecure HTTPS warnings for upstream calls
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

BASE         = "https://cinejoy.to"
STREAM_EXTS  = (".m3u8", ".mp4", ".mpd")
RESOLVE_TIMEOUT = 30   # seconds per resolve attempt

# ── Resolver ──────────────────────────────────────────────────────────────────

async def resolve(tmdb_id: int, media_type: str, season: int = 1, episode: int = 1) -> dict:
    """
    Load the cinejoy watch page and intercept the master playlist URL.
    Returns dict with url + referer on success, error key on failure.
    """
    if media_type == "movie":
        page_url = f"{BASE}/watch/movie/{tmdb_id}"
    else:
        page_url = f"{BASE}/watch/tv/{tmdb_id}/{season}/{episode}"

    stream_urls = []
    found       = asyncio.Event()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-gpu", "--mute-audio"],
        )
        stealth  = Stealth()
        context  = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            ignore_https_errors=True,
        )
        await stealth.apply_stealth_async(context)

        # Intercept fetch/XHR at the JS level to catch blob URLs too
        await context.add_init_script("""
            window._streams = [];
            const _fetch = window.fetch;
            window.fetch = function(...a) {
                const u = typeof a[0]==='string' ? a[0] : (a[0]||{}).url||'';
                return _fetch.apply(this, a).then(r => {
                    const cl = r.clone();
                    cl.text().then(t => {
                        if (t.includes('#EXTM3U') || t.includes('.m3u8'))
                            window._streams.push(u);
                    }).catch(()=>{});
                    return r;
                });
            };
        """)

        def on_request(req):
            u  = req.url
            if any(u.endswith(ext) for ext in STREAM_EXTS):
                if u not in stream_urls:
                    stream_urls.append(u)
                    found.set()

        page = await context.new_page()
        page.on("request", on_request)

        try:
            await page.goto(page_url, timeout=RESOLVE_TIMEOUT * 1000,
                            wait_until="domcontentloaded")
        except PWTimeout:
            await browser.close()
            return {"error": "page load timed out"}
        except Exception as e:
            await browser.close()
            return {"error": str(e)[:120]}

        # Click any play button
        for sel in [
            'button[class*="play"]', '[class*="play-btn"]',
            '.jw-icon-display', '[aria-label="Play"]',
            '[class*="Play"]', '.play-button', '#play', 'video',
        ]:
            try:
                el = await page.query_selector(sel)
                if el:
                    await el.click(timeout=2000)
                    break
            except Exception:
                pass

        # Poll JS-intercepted streams alongside network events
        async def poll():
            while not found.is_set():
                await asyncio.sleep(1.5)
                try:
                    js_streams = await page.evaluate("window._streams")
                    for u in js_streams:
                        if u not in stream_urls:
                            stream_urls.append(u)
                            found.set()
                except Exception:
                    pass

        try:
            await asyncio.wait_for(
                asyncio.gather(found.wait(), poll()),
                timeout=RESOLVE_TIMEOUT * 0.9,
            )
        except asyncio.TimeoutError:
            pass

        await browser.close()

    if not stream_urls:
        return {"error": "no stream found"}

    # Prefer master playlist over segment playlists
    def score(u):
        if "playlist" in u:    return 0
        if "video_" not in u and "audio_" not in u: return 1
        return 2

    best = sorted(stream_urls, key=score)[0]

    return {
        "status":  "ok",
        "raw_url": best,
        "all":     stream_urls,
        "referer": f"{BASE}/",
        "type":    media_type,
        "tmdb":    tmdb_id,
        **({"season": season, "episode": episode} if media_type == "tv" else {}),
    }


def run_resolve(tmdb_id, media_type, season=1, episode=1):
    """Run the async resolver in a fresh event loop (called from HTTP thread)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(resolve(tmdb_id, media_type, season, episode))
    finally:
        loop.close()


# ── HTTP handler ──────────────────────────────────────────────────────────────

ROUTE_MOVIE = re.compile(r"^/stream/movie/(\d+)$")
ROUTE_TV    = re.compile(r"^/stream/tv/(\d+)/(\d+)/(\d+)$")


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"  {self.address_string()}  {fmt % args}")

    def get_base_url(self):
        host = self.headers.get("Host", "localhost:8888")
        proto = self.headers.get("X-Forwarded-Proto", "http")
        return f"{proto}://{host}"

    def do_OPTIONS(self):
        """Handle CORS pre-flight requests."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def json(self, code: int, data: dict):
        body = json.dumps(data, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type",  "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def handle_proxy(self):
        """Proxies HLS playlists & segments with required Referer & CORS headers."""
        parsed_url = urlparse(self.path)
        params = parse_qs(parsed_url.query)
        target_url = params.get("url", [None])[0]

        if not target_url:
            self.json(400, {"error": "Missing 'url' parameter"})
            return

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0.0.0 Safari/537.36",
            "Referer": f"{BASE}/",
            "Origin": BASE
        }

        # Forward byte range headers if present
        if "Range" in self.headers:
            headers["Range"] = self.headers["Range"]

        try:
            resp = http_client.request("GET", target_url, headers=headers, preload_content=False)
            
            self.send_response(resp.status)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "*")
            
            content_type = resp.headers.get("Content-Type", "application/octet-stream")
            self.send_header("Content-Type", content_type)

            # Rewrite playlists to point sub-resources back to proxy
            if ".m3u8" in target_url or "mpegurl" in content_type:
                content = resp.read().decode("utf-8", errors="ignore")
                resp.release_conn()

                proxy_base = f"{self.get_base_url()}/proxy?url="

                rewritten_lines = []
                for line in content.splitlines():
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#"):
                        # Rewrite stream URI to go through proxy
                        abs_url = urljoin(target_url, stripped)
                        rewritten_lines.append(f"{proxy_base}{quote(abs_url, safe='')}")
                    elif 'URI="' in line:
                        # Rewrite inline URIs (audio tracks, subtitles, etc)
                        def replace_uri(match):
                            uri = match.group(1)
                            abs_url = urljoin(target_url, uri)
                            return f'URI="{proxy_base}{quote(abs_url, safe="")}"'
                        rewritten_line = re.sub(r'URI="([^"]+)"', replace_uri, line)
                        rewritten_lines.append(rewritten_line)
                    else:
                        rewritten_lines.append(line)

                body = "\n".join(rewritten_lines).encode("utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                # Stream raw video chunks directly
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
                "/proxy?url=<encoded_url>"
            ]})
            return

        if path == "/proxy":
            self.handle_proxy()
            return

        host_header = self.headers.get("Host", "localhost:8888")

        m = ROUTE_MOVIE.match(path)
        if m:
            tmdb_id = int(m.group(1))
            print(f"  resolving movie tmdb={tmdb_id} ...")
            t0     = time.monotonic()
            result = run_resolve(tmdb_id, "movie")
            result["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
            
            if result.get("status") == "ok":
                raw_url = result["raw_url"]
                result["url"] = f"{self.get_base_url()}/proxy?url={quote(raw_url, safe='')}"
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
                raw_url = result["raw_url"]
                result["url"] = f"{self.get_base_url()}/proxy?url={quote(raw_url, safe='')}"
                code = 200
            else:
                code = 502

            self.json(code, result)
            return

        self.json(404, {"error": "unknown route"})


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", default=8888, type=int)
    args = parser.parse_args()

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
