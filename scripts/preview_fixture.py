"""TEST FIXTURE ONLY: isolated browser preview on 127.0.0.1:5679.

Run: python scripts/preview_fixture.py
Login: fixture / test-only. NAS address is prefilled from an ephemeral loopback
server. All generated sample records and saved connections live in a temporary
directory removed at exit, never in the application's runtime directory.
"""
import argparse
import hashlib
import json
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.connector import parse_track
from app.server import Handler, Service
from app.timezones import get_timezone

USERNAME = "fixture"
PASSWORD = "test-only"
USER = {"guid": "guidfixture", "name": "fixture"}
TOKEN = "test-fixture-token-not-a-real-credential"
TRACKS = [
    {"guid": f"fixture-track-{i}", "title": title,
     "artists": [{"name": artist}], "album": {"name": album, "coverId": f"fixture-cover-{i}"},
     "duration": 150000 + i * 17000, "genres": ["测试流行" if i % 2 else "测试独立"], "year": 2020 + i % 6}
    for i, (title, artist, album) in enumerate([
        ("测试 · 夜色", "样本歌手 A", "样本专辑 夜航"), ("测试 · 风在路上", "样本歌手 B", "样本专辑 旅途"),
        ("测试 · 慢时光", "样本歌手 A", "样本专辑 夜航"), ("测试 · 夏日回声", "样本歌手 C", "样本专辑 夏日"),
        ("测试 · 星河", "样本歌手 B", "样本专辑 旅途"), ("测试 · 晴天", "样本歌手 D", "样本专辑 晴天"),
        ("测试 · 周末", "样本歌手 C", "样本专辑 夏日"), ("=TEST_FORMULA_SAMPLE()", "样本歌手 D", "样本专辑 安全导出"),
    ])
]
PLAYER_HTML = b'''<!doctype html><html><head><meta charset="utf-8"><title>TEST FIXTURE NAS</title></head>
<body style="background:#17171c;color:#eee;font:16px sans-serif;padding:70px 30px">
<h1>TEST FIXTURE NAS - simulated player</h1><p>No real NAS connection or playable audio.</p>
<button id="play">Send one confirmed test play</button><p id="result"></p>
<script>document.getElementById('play').onclick=async()=>{const e={eventType:'track_play',occurredAt:Date.now(),payload:{trackGUID:'fixture-track-0'}};
const r=await fetch('/music/api/v1/event/report',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({events:[e]})});
document.getElementById('result').textContent=JSON.stringify(await r.json());};</script></body></html>'''


class NASFixtureHandler(BaseHTTPRequestHandler):
    """Local-only fnMusic API shape, for the real connector and HTTP gateway."""
    def log_message(self, *args):
        pass

    def send_fixture(self, status, content, content_type="application/json; charset=utf-8"):
        if isinstance(content, dict):
            content = json.dumps(content, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def api(self, data=None, code=0, status=200):
        self.send_fixture(status, {"code": code, "message": "TEST FIXTURE", "data": data})

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        except ValueError:
            return self.api(code=400, status=400)
        path = urlsplit(self.path).path
        if path == "/music/api/v1/user/password-login":
            if (body.get("username") != USERNAME or
                    body.get("password") != hashlib.sha256(PASSWORD.encode()).hexdigest()):
                return self.api(code=401, status=401)
            return self.api({"userToken": TOKEN, "user": USER})
        if self.headers.get("Cookie") != "music-token=" + TOKEN:
            return self.api(code=401, status=401)
        if path == "/music/api/v1/event/report":
            return self.api()
        return self.api(code=404, status=404)

    def do_GET(self):
        parsed = urlsplit(self.path)
        params = parse_qs(parsed.query)
        if self.headers.get("Cookie") != "music-token=" + TOKEN:
            return self.api(code=401, status=401)
        if parsed.path in ("/music", "/music/"):
            return self.send_fixture(200, PLAYER_HTML, "text/html; charset=utf-8")
        if parsed.path == "/music/api/v1/user/me":
            return self.api(USER)
        if parsed.path == "/music/api/v1/play-history/list":
            page = int(params.get("page", ["1"])[0])
            return self.api({"list": TRACKS if page == 1 else [], "total": len(TRACKS)})
        if parsed.path == "/music/api/v1/track/metadata":
            guid = params.get("guid", [""])[0]
            track = next((item for item in TRACKS if item["guid"] == guid), None)
            return self.api({"track": track, "audioSpec": {"duration": track["duration"]}}) if track else self.api(code=404, status=404)
        if parsed.path == "/music/api/v1/static/cover":
            cover = params.get("coverId", ["fixture-cover-0"])[0]
            try:
                index = int(cover.rsplit("-", 1)[-1])
            except ValueError:
                index = 0
            colors = ["#cc5266", "#6765ad", "#5d8d86", "#ca8857"]
            svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256"><rect width="256" height="256" fill="{colors[index % 4]}"/>'
                   f'<circle cx="128" cy="104" r="50" fill="#ffffff22"/><text x="128" y="198" text-anchor="middle" fill="white" font-size="19">TEST SAMPLE {index}</text></svg>').encode()
            return self.send_fixture(200, svg, "image/svg+xml")
        return self.api(code=404, status=404)

    do_HEAD = do_GET


def seed(service, origin, now=None):
    """Deterministic metadata and 67 explicit sample events across report periods."""
    now = now or datetime.now(get_timezone("Asia/Shanghai"))
    account_id = service.remember({"server": origin, "token": TOKEN, "user": USER}, persist=False)
    for item in TRACKS:
        service.archive.upsert_track(account_id, parse_track(item))
    week_start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=now.weekday())
    stamps = []
    # Forty this-week entries guarantee nonempty reports and record pagination,
    # including Monday early previews; every timestamp is before the current time.
    available_ms = max(1, int((now - week_start).total_seconds() * 1000))
    for i in range(40):
        stamps.append(int(week_start.timestamp() * 1000) + max(0, available_ms * (i + 1) // 42))
    for i in range(12):
        stamps.append(int((week_start - timedelta(days=7) + timedelta(hours=8 + i * 10)).timestamp() * 1000))
    prior_month = now.replace(day=1, hour=12, minute=0, second=0, microsecond=0) - timedelta(days=12)
    for i in range(8):
        stamps.append(int((prior_month + timedelta(hours=i * 3)).timestamp() * 1000))
    january = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    for i in range(7):
        moment = min(january + timedelta(days=i, hours=12), now - timedelta(hours=1 + i))
        stamps.append(int(moment.timestamp() * 1000))
    events = [{"eventType": "track_play", "occurredAt": stamp,
               "payload": {"trackGUID": TRACKS[i % len(TRACKS)]["guid"]}}
              for i, stamp in enumerate(stamps)]
    service.archive.ingest(account_id, events, "test_fixture")
    return account_id


def main():
    parser = argparse.ArgumentParser(description="TEST FIXTURE ONLY, disposable loopback browser preview")
    parser.add_argument("--port", type=int, default=5679)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="journal-browser-TEST-FIXTURE-") as directory:
        nas = ThreadingHTTPServer(("127.0.0.1", 0), NASFixtureHandler)
        nas.daemon_threads = True
        origin = f"http://127.0.0.1:{nas.server_port}"
        service = Service(directory, origin)
        seed(service, origin)
        httpd = None
        thread = threading.Thread(target=nas.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        thread.start()
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
            httpd.daemon_threads = True
            httpd.service = service
            print(f"TEST FIXTURE ONLY: http://127.0.0.1:{args.port}", flush=True)
            print(f"TEST FIXTURE fake NAS: {origin} | username={USERNAME} password={PASSWORD}", flush=True)
            print(f"TEST FIXTURE temporary database: {directory}; 67 simulated plays; NOT real user listening data", flush=True)
            print("TEST FIXTURE binds only 127.0.0.1; runtime is untouched. Ctrl+C removes temporary data.", flush=True)
            httpd.serve_forever(poll_interval=.1)
        except KeyboardInterrupt:
            pass
        finally:
            service.stop.set()
            if httpd:
                httpd.server_close()
            nas.shutdown()
            nas.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    main()
