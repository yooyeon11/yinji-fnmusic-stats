"""A dependency-free, authenticated web app and FN Music recording gateway."""
import argparse
import gzip
import hashlib
import ipaddress
import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .storage import Archive

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("journal")
VERSION = "1.2.1"
MAX_BODY = 2 * 1024 * 1024
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"}
LOGOUT_COOKIE = "journal_logout"
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _netloc_key(netloc, scheme):
    """(scheme, host, port) with default ports filled in, or None if invalid."""
    parsed = urllib.parse.urlsplit("//" + netloc)
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    return scheme, host, port if port is not None else _DEFAULT_PORTS[scheme]
_INTERNAL_V4 = tuple(ipaddress.ip_network(value) for value in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10",
    "169.254.0.0/16", "127.0.0.0/8"))
_INTERNAL_V6 = tuple(ipaddress.ip_network(value) for value in (
    "::1/128", "fc00::/7", "fe80::/10"))


def private_peer(peer):
    """Whether a client address is on the local/internal network.

    Only loopback, RFC1918, link-local, CGNAT and IPv6 ULA count as internal.
    Documentation ranges such as 203.0.113.0/24 are not reachable LAN peers
    and deliberately do not qualify.
    """
    try:
        address = ipaddress.ip_address(str(peer))
    except ValueError:
        return False
    networks = _INTERNAL_V4 if address.version == 4 else _INTERNAL_V6
    return any(address in network for network in networks)


class Service:
    def __init__(self, directory, server="http://192.168.1.100:5666", timezone="Asia/Shanghai", poll_seconds=30, trust_lan=True):
        self.archive = Archive(directory, timezone)
        self.directory = Path(directory)
        self.default_server = server
        self.sessions = {}
        self.accounts = {}
        self.lock = threading.RLock()
        self.sync_lock = threading.Lock()
        self.attempts = {}
        self.stop = threading.Event()
        self.poll_seconds = max(5, int(poll_seconds))
        self.trust_lan = bool(trust_lan)
        path = self.directory / "connection.json"
        if path.exists():
            try:
                connection = json.loads(path.read_text(encoding="utf-8"))
                self.remember(connection, persist=False)
            except (ValueError, OSError, KeyError):
                log.warning("Saved connection could not be loaded")
        self.archive.prune_sessions()

    def remember(self, connection, persist=True):
        user = connection.get("user") or {}
        uid = str(user.get("guid") or user.get("id") or user.get("username") or user.get("name") or "")
        if not uid:
            raise ValueError("飞牛未返回可识别的账号信息")
        key = hashlib.sha256((connection["server"] + "|" + uid).encode()).hexdigest()
        with self.lock:
            self.accounts[key] = {**connection, "user_id": key}
        if persist:
            path = self.directory / "connection.json"
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(connection, ensure_ascii=False), encoding="utf-8")
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            temporary.replace(path)
        return key

    def session(self, cookie):
        try:
            parsed = cookies.SimpleCookie(cookie or "")
            token = parsed["journal_session"].value if "journal_session" in parsed else ""
        except cookies.CookieError:
            return None
        if not token:
            return None
        with self.lock:
            value = self.sessions.get(token)
            if value is None:
                # Sessions are persisted as token hashes, so restarting the
                # service does not log anyone out.
                saved = self.archive.load_session(token)
                if saved is None:
                    return None
                value = self.sessions[token] = {"account": saved[0], "expires": saved[1]}
            if value["expires"] <= time.time():
                self.sessions.pop(token, None)
                self.archive.drop_session(token)
                return None
            return self.accounts.get(value["account"])

    def resolve(self, cookie, peer):
        """Session for one request: cookie first, then the shared internal session.

        When a connection is saved and the request comes from the internal
        network, every device shares that account without logging in again.
        A browser that explicitly logged out (marker cookie) is excluded, and
        the whole behaviour is disabled with trust_lan=False.
        """
        account = self.session(cookie)
        if account is not None:
            return account
        if not self.trust_lan or not private_peer(peer):
            return None
        try:
            parsed = cookies.SimpleCookie(cookie or "")
        except cookies.CookieError:
            return None
        if LOGOUT_COOKIE in parsed:
            return None
        with self.lock:
            if not self.accounts:
                return None
            # The most recently connected account is the one this device would
            # have used; the choice is deterministic and shown in the UI.
            return max(self.accounts.values(), key=lambda item: item.get("connected_at") or 0)

    def connect(self, server, username, password):
        from .connector import MusicClient
        server = normalize_server(server)
        result = MusicClient(server).login(username, password)
        connection = {"server": server, "token": result["token"], "user": result["user"], "connected_at": int(time.time() * 1000)}
        account = self.remember(connection)
        session = secrets.token_urlsafe(32)
        expires = time.time() + 7 * 86400
        with self.lock:
            self.sessions[session] = {"account": account, "expires": expires}
        self.archive.save_session(session, account, expires)
        self.sync(self.accounts[account])
        try:
            # Establish the polling baseline immediately so a play right after
            # connecting is not mistaken for pre-existing history.
            self.detect_plays(self.accounts[account])
        except Exception as exc:
            log.warning("Initial playback detection failed: %s", type(exc).__name__)
        return session, self.accounts[account]

    def sync(self, account):
        from .connector import MusicClient, parse_artists, parse_track
        if not self.sync_lock.acquire(blocking=False):
            return
        try:
            client = MusicClient(account["server"], account["token"])
            user = client.me()
            with self.lock:
                account["user"] = user
            history = client.history()
            for item in history:
                meta = parse_track(item)
                self.archive.upsert_track(account["user_id"], meta)
                for artist in parse_artists(item):
                    self.archive.upsert_artist(account["user_id"], artist["name"], artist["cover_id"])
            self.archive.set_state(account["user_id"], "last_sync", int(time.time() * 1000))
            self.archive.set_state(account["user_id"], "error", None)
        except Exception as exc:
            log.warning("Metadata synchronization failed: %s", type(exc).__name__)
            self.archive.set_state(account["user_id"], "error", "飞牛连接失效或同步失败，请重新连接；既有记录不会删除。")
        finally:
            self.sync_lock.release()

    def enrich(self, account, body):
        from .connector import MusicClient, parse_artists, parse_track
        events = body if isinstance(body, list) else body.get("events", [body] if "eventType" in body else []) if isinstance(body, dict) else []
        if not isinstance(events, list):
            return
        for event in events[:1000]:
            if not isinstance(event, dict) or event.get("eventType") != "track_play":
                continue
            guid = (event.get("payload") or {}).get("trackGUID")
            if not guid or self.archive.track(account["user_id"], guid):
                continue
            try:
                metadata = MusicClient(account["server"], account["token"]).track(guid)
                self.archive.upsert_track(account["user_id"], parse_track(metadata))
                for artist in parse_artists(metadata):
                    self.archive.upsert_artist(account["user_id"], artist["name"], artist["cover_id"])
            except Exception:
                log.warning("Track metadata unavailable")

    def capture(self, account, body, source):
        result = self.archive.ingest(account["user_id"], body, source)
        if result["accepted"]:
            threading.Thread(target=self.enrich, args=(account, body), daemon=True).start()
        return result

    def detect_plays(self, account):
        from .connector import MusicClient
        from .poller import detect
        result = detect(self.archive, account, MusicClient(account["server"], account["token"]))
        if result.get("recorded"):
            log.info("Playback detection recorded %s play(s)%s", result["recorded"],
                     " after realignment" if result.get("unaligned") else "")
        elif result.get("unaligned"):
            log.info("Playback history was reordered or trimmed server-side; nothing attributable")
        return result

    def run_sync(self):
        while not self.stop.is_set():
            with self.lock:
                accounts = list(self.accounts.values())
            for account in accounts:
                self.sync(account)
            self.stop.wait(60)

    def run_poller(self):
        while not self.stop.is_set():
            with self.lock:
                accounts = list(self.accounts.values())
            for account in accounts:
                try:
                    self.detect_plays(account)
                except Exception as exc:
                    log.warning("Playback detection failed: %s", type(exc).__name__)
            self.stop.wait(self.poll_seconds)


def normalize_server(value):
    if not isinstance(value, str):
        raise ValueError("请输入飞牛音乐地址")
    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("飞牛地址必须是 http/https 地址，不可包含账号密码")
    if parsed.path.rstrip("/") not in ("", "/music") or parsed.query or parsed.fragment:
        raise ValueError("请输入服务根地址或 /music/ 页面地址")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


class Handler(BaseHTTPRequestHandler):
    server_version = "MusicJournal/1.0"

    @property
    def service(self):
        return self.server.service

    def log_message(self, fmt, *args):
        log.info("%s %s", self.command, urllib.parse.urlsplit(self.path).path)

    def send(self, status, body=b"", content_type="application/json; charset=utf-8", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        if isinstance(body, str):
            body = body.encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            # JSON is never cached; images may be cached briefly; HTML/CSS/JS must
            # revalidate so an updated tool never serves stale markup or scripts.
            if content_type.startswith("application/json"):
                cache = "no-store"
            elif content_type.startswith("image/"):
                cache = "private, max-age=300"
            else:
                cache = "no-cache"
            self.send_header("Cache-Control", cache)
            for key, value in (headers.items() if isinstance(headers, dict) else (headers or ())):
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except OSError:
            # The client went away (reload, cancel, seek). Nothing to answer.
            pass

    def failure(self, status, message):
        self.send(status, {"ok": False, "error": message})

    def read_body(self, limit=None):
        cap = limit or MAX_BODY
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("不支持分块请求")
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 <= size <= cap:
            # Drain a bounded amount so the client can read the error response
            # instead of seeing a connection reset mid-upload.
            remaining = min(size, cap * 8) if size > 0 else 0
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            raise ValueError("请求体过大")
        return self.rfile.read(size)

    def authorized(self):
        account = self.service.resolve(self.headers.get("Cookie"), self.client_address[0])
        if not account:
            self.failure(401, "请先登录飞牛账号")
        return account

    def same_origin(self):
        """Whether a write request is same-origin, tolerating proxies and ports.

        Sec-Fetch-Site is set by the browser itself (page scripts cannot forge
        it): same-origin is trusted, same-site/cross-site are rejected. Without
        it, Origin is compared with Host using normalized default ports, so
        reverse proxies and explicit :80/:443 addresses no longer break while
        the same-site-other-port forgery stays blocked.
        """
        fetch_site = self.headers.get("Sec-Fetch-Site", "").strip().lower()
        if fetch_site == "same-origin":
            return True
        if fetch_site in ("same-site", "cross-site"):
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urllib.parse.urlsplit(origin)
        if parsed.scheme not in ("http", "https"):
            return False
        return _netloc_key(parsed.netloc, parsed.scheme) == _netloc_key(self.headers.get("Host", ""), "http")

    def reject_cross_site(self):
        # Diagnostic evidence for real-world rejections; never cookies or bodies.
        log.warning("Cross-site write rejected: origin=%r host=%r sec-fetch-site=%r %s %s",
                    self.headers.get("Origin"), self.headers.get("Host"),
                    self.headers.get("Sec-Fetch-Site"), self.command,
                    urllib.parse.urlsplit(self.path).path)
        return self.failure(403, "拒绝跨站请求")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self.handle_get()
        except (ValueError, TypeError, KeyError) as exc:
            self.failure(400, str(exc))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception:
            log.exception("Request failed")
            self.failure(502, "无法完成请求，请检查飞牛服务连接")

    def handle_get(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = {key: values[-1] for key, values in urllib.parse.parse_qs(parsed.query).items()}
        if path == "/api/health":
            return self.send(200, {"ok": True, "version": VERSION})
        if path == "/api/status":
            account = self.service.resolve(self.headers.get("Cookie"), self.client_address[0])
            if not account:
                return self.send(200, {"connected": False, "needs_login": True, "server": self.service.default_server,
                                       "notice": "请登录飞牛账号以查看私人收听记录"})
            user_id = account["user_id"]
            return self.send(200, {"connected": True, "needs_login": False, "user": {"name": account["user"].get("name") or account["user"].get("username")},
                                  "server": account["server"], "last_sync": self.service.archive.get_state(user_id, "last_sync"),
                                  "error": self.service.archive.get_state(user_id, "error"), "collection": self.service.archive.status(user_id),
                                  "notice": "代理与浏览器采集带精确时间；手机与原页面播放由轮询探测补记，时间按首次发现估算；无真实时间的旧历史不会回填。"})
        if path.startswith("/api/") or path.startswith("/music"):
            account = self.authorized()
            if not account:
                return
            archive = self.service.archive
            user_id = account["user_id"]
            start = int(query["start"]) if query.get("start") else None
            end = int(query["end"]) if query.get("end") else None
            if path == "/api/export/full":
                payload = archive.export_all(user_id)
                payload["account"] = {"name": account["user"].get("name") or account["user"].get("username")}
                return self.send(200, payload, "application/json",
                                 {"Content-Disposition": f'attachment; filename="yinji-backup-{time.strftime("%Y%m%d")}.json"'})
            if path == "/api/report":
                return self.send(200, archive.report(user_id, query.get("period", "week"), int(query.get("offset", 0))))
            if path == "/api/records":
                return self.send(200, archive.records(user_id, int(query.get("page", 1)), int(query.get("size", 25)), query.get("q", "")[:200], start, end))
            if path == "/api/export":
                body = archive.export(user_id, query.get("q", "")[:200], start, end)
                return self.send(200, body, "text/csv; charset=utf-8", {"Content-Disposition": 'attachment; filename="music-journal.csv"'})
            if path.startswith("/api/covers/"):
                from .connector import MusicClient
                cover_id = urllib.parse.unquote(path[len("/api/covers/"):])
                if not cover_id or len(cover_id) > 128 or not all(c.isalnum() or c in "_-" for c in cover_id):
                    return self.failure(400, "无效封面")
                try:
                    body, ctype = MusicClient(account["server"], account["token"]).cover(cover_id)
                except Exception:
                    return self.send(404, b"", "image/png")
                if not ctype.startswith("image/"):
                    return self.failure(502, "无效封面响应")
                return self.send(200, body, ctype)
            if path == "/api/collector.js":
                script = "window.__journalAccount=" + json.dumps(account["user_id"]) + ";\n"
                return self.send(200, script.encode() + (ROOT / "web" / "collector.js").read_bytes(), "application/javascript; charset=utf-8")
            if path == "/music" or path.startswith("/music/"):
                return self.proxy(account)
            return self.failure(404, "接口不存在")
        allowed = {"/": "index.html", "/app.css": "app.css", "/app.js": "app.js", "/theme.js": "theme.js", "/favicon.svg": "assets/icon.svg"}
        file = allowed.get(path)
        if not file:
            return self.failure(404, "页面不存在")
        types = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml"}
        return self.send(200, (ROOT / "web" / file).read_bytes(), types[Path(file).suffix],
                         {"Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'self'; base-uri 'self'; form-action 'self'"})

    def do_POST(self):
        try:
            if not self.same_origin():
                return self.reject_cross_site()
            path = urllib.parse.urlsplit(self.path).path
            if path.startswith("/music/"):
                account = self.authorized()
                return self.proxy(account) if account else None
            raw = self.read_body(64 * 1024 * 1024) if path == "/api/import" else self.read_body()
            if "application/json" not in self.headers.get("Content-Type", ""):
                return self.failure(415, "请使用 JSON 请求")
            body = json.loads(raw or b"{}")
            if not isinstance(body, (dict, list)):
                raise ValueError("无效请求")
            if path == "/api/connect":
                if not isinstance(body, dict):
                    raise ValueError("无效连接参数")
                peer = self.client_address[0]
                now = time.time()
                with self.service.lock:
                    attempts = [t for t in self.service.attempts.get(peer, []) if now - t < 60]
                    if len(attempts) >= 8:
                        return self.failure(429, "登录尝试频繁，请稍后重试")
                    self.service.attempts[peer] = attempts + [now]
                username, password = body.get("username"), body.get("password")
                if not isinstance(username, str) or not username or not isinstance(password, str) or not password:
                    raise ValueError("请输入账号和密码")
                try:
                    session, account = self.service.connect(body.get("server", self.service.default_server), username, password)
                except ValueError as exc:
                    return self.failure(400, str(exc))
                except Exception as exc:
                    log.warning("Connection rejected: %s", type(exc).__name__)
                    return self.failure(401, "连接失败：请检查飞牛地址、账号密码和网络")
                return self.send(200, {"ok": True, "user": {"name": account["user"].get("name") or username}},
                                 headers=[("Set-Cookie", f"{LOGOUT_COOKIE}=; SameSite=Strict; Path=/; Max-Age=0"),
                                          ("Set-Cookie", f"journal_session={session}; HttpOnly; SameSite=Strict; Path=/; Max-Age=604800")])
            account = self.authorized()
            if not account:
                return
            if path == "/api/disconnect":
                parsed = cookies.SimpleCookie(self.headers.get("Cookie", ""))
                token = parsed["journal_session"].value if "journal_session" in parsed else ""
                with self.service.lock:
                    self.service.sessions.pop(token, None)
                self.service.archive.drop_session(token)
                return self.send(200, {"ok": True}, headers=[
                    ("Set-Cookie", f"{LOGOUT_COOKIE}=1; SameSite=Strict; Path=/; Max-Age=31536000"),
                    ("Set-Cookie", "journal_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")])
            if path == "/api/sync":
                self.service.sync(account)
                return self.send(200, {"ok": True, "error": self.service.archive.get_state(account["user_id"], "error")})
            if path == "/api/ingest":
                owner = self.headers.get("X-Journal-Account")
                if owner and owner != account["user_id"]:
                    return self.failure(409, "账号已切换，请回到对应账号后同步待发记录")
                return self.send(200, self.service.capture(account, body, "browser_bridge"))
            if path == "/api/import":
                mode = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("mode", ["merge"])[-1]
                counts = self.service.archive.restore_all(account["user_id"], body, mode)
                return self.send(200, {"ok": True, **counts})
            return self.failure(404, "接口不存在")
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            self.failure(400, str(exc))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception:
            log.exception("Request failed")
            self.failure(502, "无法完成请求，请检查飞牛服务连接")

    def do_PUT(self):
        self.handle_proxy_method()

    def do_DELETE(self):
        self.handle_proxy_method()

    def do_PATCH(self):
        self.handle_proxy_method()

    def do_OPTIONS(self):
        self.send(204, b"", headers={"Allow": "GET, HEAD, POST, PUT, DELETE, PATCH, OPTIONS"})

    def handle_proxy_method(self):
        if not self.same_origin():
            return self.reject_cross_site()
        account = self.authorized()
        if account and self.path.startswith("/music/"):
            try:
                self.proxy(account)
            except (ValueError, OSError):
                self.failure(502, "飞牛请求失败")
        elif account:
            self.failure(405, "不支持的方法")

    def proxy(self, account):
        path = urllib.parse.urlsplit(self.path).path
        if path != "/music" and not path.startswith("/music/"):
            return self.failure(403, "不允许代理此路径")
        from .connector import _validate_path
        try:
            _validate_path(self.path)
        except ValueError:
            return self.failure(400, "无效路径")
        body = self.read_body() if self.command not in ("GET", "HEAD") else None
        url = account["server"] + self.path
        headers = {key: value for key, value in self.headers.items() if key.lower() not in HOP_HEADERS | {"host", "cookie", "content-length", "accept-encoding", "origin", "referer"}}
        headers["Cookie"] = "music-token=" + account["token"]
        headers["Accept-Encoding"] = "identity"
        headers["Origin"] = account["server"]
        headers["Referer"] = account["server"] + "/music/"
        upstream_method = "GET" if self.command == "HEAD" else self.command
        request = urllib.request.Request(url, data=body, headers=headers, method=upstream_method)
        # Redirects stay visible so an upstream redirect cannot silently proxy another host.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, hdrs, newurl):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
        try:
            response = opener.open(request, timeout=25)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            status = response.status
            ctype = response.headers.get("Content-Type", "application/octet-stream")
            response_headers = {key: value for key, value in response.headers.items() if key.lower() not in HOP_HEADERS | {"content-length", "set-cookie", "content-security-policy", "content-security-policy-report-only", "x-frame-options"}}
            if "Location" in response_headers:
                location = response_headers["Location"]
                if location.startswith(account["server"] + "/music"):
                    response_headers["Location"] = location[len(account["server"]):]
            if "text/html" in ctype:
                response_headers.pop("Content-Encoding", None)
                content = response.read(MAX_BODY + 1)
                if len(content) > MAX_BODY:
                    return self.failure(502, "飞牛页面过大")
                if response.headers.get("Content-Encoding") == "gzip":
                    content = gzip.decompress(content)
                text = content.decode("utf-8")
                bridge = '<script src="/api/collector.js"></script>'
                if "<head>" in text:
                    text = text.replace("<head>", "<head>" + bridge, 1)
                else:
                    text = bridge + text
                response_headers["Set-Cookie"] = "music-token=journal-gateway; SameSite=Strict; Path=/music/"
                return self.send(status, text, ctype, response_headers)
            if path.endswith("/event/report") and body and 200 <= status < 300:
                content = response.read(MAX_BODY + 1)
                try:
                    reply = json.loads(content)
                    if isinstance(reply, dict) and type(reply.get("code")) is int and reply["code"] == 0:
                        self.service.capture(account, json.loads(body), "gateway")
                except (ValueError, TypeError):
                    log.warning("Unrecognized upstream event response")
                return self.send(status, content, ctype, response_headers)
            self.send_response(status)
            for key, value in response_headers.items():
                self.send_header(key, value)
            length = response.headers.get("Content-Length")
            if length is not None:
                self.send_header("Content-Length", length)
            else:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            if self.command != "HEAD":
                try:
                    while True:
                        block = response.read(64 * 1024)
                        if not block:
                            break
                        self.wfile.write(block)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    # Playback or download was cancelled; the client is gone.
                    pass


def main():
    parser = argparse.ArgumentParser(description="音迹 · 飞牛音乐记录")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "5678")))
    parser.add_argument("--data-dir", default=os.environ.get("DATA_DIR", str(ROOT / "runtime")))
    parser.add_argument("--music-server", default=os.environ.get("FNMUSIC_SERVER", "http://192.168.1.100:5666"))
    parser.add_argument("--timezone", default=os.environ.get("TZ", "Asia/Shanghai"))
    parser.add_argument("--poll-seconds", type=int, default=int(os.environ.get("FNMUSIC_POLL_SEC", "30")))
    parser.add_argument("--trust-lan", dest="trust_lan", action="store_true",
                        default=os.environ.get("JOURNAL_TRUST_LAN", "1").strip().lower() not in ("0", "false", "no", "off"),
                        help="内网设备共用已保存的账号会话（默认开启；用 --no-trust-lan 或 JOURNAL_TRUST_LAN=0 关闭）")
    parser.add_argument("--no-trust-lan", dest="trust_lan", action="store_false")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    service = Service(args.data_dir, normalize_server(args.music_server), args.timezone, args.poll_seconds, args.trust_lan)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    httpd.service = service
    threading.Thread(target=service.run_sync, daemon=True).start()
    threading.Thread(target=service.run_poller, daemon=True).start()
    log.info("音迹 %s: http://%s:%s", VERSION, args.host, args.port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.stop.set()
        httpd.server_close()


if __name__ == "__main__":
    main()
