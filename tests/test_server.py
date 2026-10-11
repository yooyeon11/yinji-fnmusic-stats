"""Real HTTP contract tests, using only loopback NAS and disposable archives.

Run from the new project root: python -m unittest discover -s tests -p test_server.py -v
No real NAS credentials, requests, or runtime database are used.
"""
import csv
import gzip
import http.client
import io
import json
import logging
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit

from app.server import Handler, Service, MAX_BODY, private_peer


EVENT_PATH = "/music/api/v1/event/report"
STAMP = 1709712000000
MEDIA = bytes(range(256)) * 1024
HTML = b'<!doctype html><html><head><title>Fake NAS</title></head><body>player</body></html>'


def event(guid="one", stamp=STAMP):
    # Exact fnMusic frontend event contract; not fabricated history timestamps.
    return {"eventType": "track_play", "occurredAt": stamp,
            "payload": {"trackGUID": guid}}


class FakeMusicClient:
    calls = []

    def __init__(self, server, token=""):
        if urlsplit(server).hostname != "127.0.0.1":
            raise AssertionError("Tests must never contact a real NAS")
        self.server, self.token = server, token

    def login(self, username, password):
        type(self).calls.append(("login", username, password))
        if password == "rejected":
            raise RuntimeError("credential-secret upstream-secret-" + username)
        self.token = "upstream-secret-" + username
        return {"token": self.token, "user": {"guid": "guid-" + username, "name": username}}

    def me(self):
        name = self.token.removeprefix("upstream-secret-")
        return {"guid": "guid-" + name, "name": name}

    def history(self):
        return [{"guid": "one", "title": "First song", "artists": [{"name": "Artist A"}],
                 "album": {"name": "Album A", "coverId": "cover-one"},
                 "duration": 180000, "updatedAt": STAMP, "createdAt": STAMP}]

    def recent(self, size=100):
        return self.history()

    def track(self, guid):
        return {"track": {"guid": guid, "title": "Metadata " + guid,
                          "artists": [{"name": "Artist B"}], "album": {"name": "Album B"}},
                "audioSpec": {"duration": 210000}}

    def cover(self, cover_id):
        type(self).calls.append(("cover", cover_id, self.token))
        return b"\x89PNG\r\n\x1a\nfixture", "image/png"


class FakeNASHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, body, content_type="application/json", headers=None, length=True):
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if length:
            self.send_header("Content-Length", str(len(body)))
        else:
            self.send_header("Connection", "close")
            self.close_connection = True
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def handle_request(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.requests.append({"method": self.command, "path": self.path,
                                     "headers": dict(self.headers), "body": body})
        path = urlsplit(self.path).path
        if path == EVENT_PATH:
            return self.reply(self.server.event_status, self.server.event_reply)
        if path in ("/music", "/music/", "/music/gzip.html", "/music/no-head.html"):
            content = HTML if path != "/music/no-head.html" else b"<html><body>player</body></html>"
            headers = {"Set-Cookie": "music-token=upstream-secret-alice; Path=/",
                       "Content-Security-Policy": "script-src 'none'", "X-Frame-Options": "DENY"}
            if path == "/music/gzip.html":
                content = gzip.compress(content)
                headers["Content-Encoding"] = "gzip"
            return self.reply(200, content, "text/html; charset=utf-8", headers)
        if path == "/music/redirect":
            return self.reply(302, b"", headers={"Location": self.server.origin + "/music/"})
        if path == "/music/external-redirect":
            return self.reply(302, b"", headers={"Location": "http://127.0.0.1:1/never-contact"})
        if path == "/music/gzip.js":
            return self.reply(200, gzip.compress(b"window.fixture = true;"), "application/javascript",
                              {"Content-Encoding": "gzip"})
        if path in ("/music/audio", "/music/stream"):
            content, status, headers = MEDIA, 200, {"Accept-Ranges": "bytes"}
            if self.headers.get("Range"):
                begin, end = self.headers["Range"][6:].split("-")
                begin, end = int(begin), int(end)
                content, status = MEDIA[begin:end + 1], 206
                headers["Content-Range"] = f"bytes {begin}-{end}/{len(MEDIA)}"
            return self.reply(status, content, "audio/mpeg", headers, length=path != "/music/stream")
        if path == "/music/api/echo":
            return self.reply(200, {"method": self.command, "bytes": len(body)})
        return self.reply(404, {"code": 404})

    do_GET = handle_request
    do_HEAD = handle_request
    do_POST = handle_request
    do_PUT = handle_request
    do_PATCH = handle_request
    do_DELETE = handle_request


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.nas = ThreadingHTTPServer(("127.0.0.1", 0), FakeNASHandler)
        cls.nas.daemon_threads = True
        cls.nas.origin = f"http://127.0.0.1:{cls.nas.server_port}"
        cls.nas_thread = threading.Thread(target=cls.nas.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        cls.nas_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.nas.shutdown()
        cls.nas.server_close()
        cls.nas_thread.join(timeout=2)

    def setUp(self):
        # ignore_cleanup_errors: daemon enrichment threads may still hold the
        # SQLite file for a moment when the disposable directory is removed.
        self.temp = tempfile.TemporaryDirectory(prefix="journal-http-test-", ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.client_patch = patch("app.connector.MusicClient", FakeMusicClient)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        FakeMusicClient.calls = []
        self.nas.requests = []
        self.nas.event_status = 200
        self.nas.event_reply = {"code": 0, "message": "success", "data": None}
        # Strict mode here: these tests pin the cookie-only guarantees. The
        # shared internal-network session is covered in SharedLanSessionTests.
        self.service = Service(self.temp.name, self.nas.origin, trust_lan=False)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.httpd.service = self.service
        self.origin = f"http://127.0.0.1:{self.httpd.server_port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.cookie = self.connect("alice")
        self.account = self.service.session(self.cookie)
        # Preseed normalized metadata for event fixtures so daemon enrichment
        # cannot outlive a disposable database during teardown.
        for guid in ("two", "three"):
            self.service.archive.upsert_track(self.account["user_id"], {
                "guid": guid, "title": "Song " + guid, "artist": "Artist B",
                "album": "Album B", "duration_ms": 210000})

    def close_server(self):
        self.service.stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, body=None, cookie=None, headers=None, raw=False):
        outgoing = dict(headers or {})
        if cookie is not None:
            outgoing["Cookie"] = cookie
        if body is not None and not raw:
            body = json.dumps(body).encode()
            outgoing.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=outgoing)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def json_request(self, method, path, body=None, cookie=None, headers=None):
        status, response_headers, content = self.request(method, path, body, cookie, headers)
        return status, response_headers, json.loads(content)

    def connect(self, username):
        status, headers, body = self.json_request("POST", "/api/connect", {
            "server": self.nas.origin + "/music/", "username": username, "password": "credential-secret"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"ok": True, "user": {"name": username}})
        return headers["Set-Cookie"].split(";", 1)[0]

    def ingest(self, body, cookie=None):
        return self.json_request("POST", "/api/ingest", body, cookie or self.cookie)

    def records(self, cookie=None, query=""):
        status, _, body = self.json_request("GET", "/api/records" + query, cookie=cookie or self.cookie)
        self.assertEqual(status, 200, body)
        return body

    def test_private_endpoints_require_real_session(self):
        for path in ("/api/report", "/api/records", "/api/export", "/api/covers/cover-one",
                     "/api/collector.js", "/music/", "/music/audio"):
            for cookie in (None, "journal_session=forged", "music-token=upstream-secret-alice"):
                with self.subTest(path=path, cookie=cookie):
                    status, _, body = self.json_request("GET", path, cookie=cookie)
                    self.assertEqual(status, 401, body)
        for path in ("/api/ingest", "/api/sync", "/api/disconnect", EVENT_PATH):
            with self.subTest(path=path):
                self.assertEqual(self.json_request("POST", path, event())[0], 401)

    def test_public_health_and_anonymous_status_do_not_leak_identity(self):
        self.assertEqual(self.json_request("GET", "/api/health")[0], 200)
        status, _, body = self.json_request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertFalse(body["connected"])
        self.assertNotIn("alice", json.dumps(body))
        self.assertNotIn("token", body)

    def test_connect_cookie_and_token_secrecy(self):
        status, headers, body = self.request("POST", "/api/connect", {
            "username": "bob", "password": "credential-secret", "server": self.nas.origin})
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"]
        for flag in ("HttpOnly", "SameSite=Strict", "Path=/", "Max-Age=604800"):
            self.assertIn(flag, cookie)
        self.assertNotIn("upstream-secret", cookie)
        self.assertNotIn(b"credential-secret", body)
        self.assertNotIn(b"upstream-secret", body)
        status, _, body = self.request("GET", "/api/status", cookie=self.cookie)
        self.assertEqual(status, 200)
        self.assertNotIn(b"upstream-secret", body)
        self.assertNotIn(b"journal_session", body)
        self.assertNotIn("password", json.loads(Path(self.temp.name, "connection.json").read_text(encoding="utf-8")))
        self.assertIn(("login", "bob", "credential-secret"), FakeMusicClient.calls)

    def test_failed_login_sanitizes_exception_and_logs(self):
        with self.assertLogs("journal", level=logging.WARNING) as captured:
            status, _, body = self.request("POST", "/api/connect", {
                "username": "alice", "password": "rejected", "server": self.nas.origin})
        self.assertEqual(status, 401)
        for secret in ("credential-secret", "upstream-secret"):
            self.assertNotIn(secret.encode(), body)
            self.assertNotIn(secret, " ".join(captured.output))

    def test_connect_rejects_invalid_parameters(self):
        for payload in ({}, {"username": "alice", "password": ""}, [],
                        {"username": "alice", "password": "x", "server": "file:///etc/passwd"},
                        {"username": "alice", "password": "x", "server": "http://a:b@127.0.0.1"},
                        {"username": "alice", "password": "x", "server": self.nas.origin + "/other"}):
            with self.subTest(payload=payload):
                self.assertEqual(self.json_request("POST", "/api/connect", payload)[0], 400)

    def test_cross_site_writes_rejected_before_nas_or_archive(self):
        for method, path in (("POST", "/api/connect"), ("POST", "/api/ingest"),
                             ("POST", EVENT_PATH), ("PUT", "/music/api/echo"),
                             ("DELETE", "/music/api/echo"), ("PATCH", "/music/api/echo")):
            with self.subTest(method=method, path=path):
                status, _, _ = self.request(method, path, event(), self.cookie,
                                            {"Origin": "https://hostile.invalid"})
                self.assertEqual(status, 403)
        self.assertEqual(self.nas.requests, [])
        self.assertEqual(self.records()["total"], 0)

    def test_same_origin_post_is_allowed(self):
        self.assertEqual(self.ingest(event())[0], 200)
        self.assertEqual(self.json_request("POST", "/api/ingest", event("two"), self.cookie,
                                          {"Origin": self.origin})[2]["accepted"], 1)

    def test_logout_and_expired_session_are_unauthorized(self):
        bob = self.connect("bob")
        token = bob.split("=", 1)[1]
        self.service.sessions[token]["expires"] = time.time() - 1
        self.assertEqual(self.request("GET", "/api/records", cookie=bob)[0], 401)
        status, headers, _ = self.json_request("POST", "/api/disconnect", {}, self.cookie)
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(self.request("GET", "/api/records", cookie=self.cookie)[0], 401)

    def test_ingest_single_batch_duplicate_and_repeat_play(self):
        self.assertEqual(self.ingest(event())[2], {"ok": True, "accepted": 1, "deduped": 0, "skipped": 0})
        _, _, result = self.ingest({"events": [event(), event("two"), event("two"),
                                              event(stamp=STAMP + 180000), {"eventType": "track_pause"}]})
        self.assertEqual(result, {"ok": True, "accepted": 2, "deduped": 2, "skipped": 1})
        self.assertEqual(self.ingest([event(), event("two")])[2]["deduped"], 2)
        self.assertEqual(self.records()["total"], 3)

    def test_invalid_ingest_never_creates_record(self):
        invalid = [None, "event", {"eventType": "track_play", "payload": []},
                   {"eventType": "track_play", "payload": {"trackGUID": "one"}},
                   event(stamp=True), event(stamp="bad"), event(stamp=1),
                   event(stamp=int(time.time() * 1000) + 3600000), event(guid="")]
        self.assertEqual(self.ingest(invalid)[2]["skipped"], len(invalid))
        for payload in ({"events": "not-list"}, {"events": [event()] * 1001}):
            self.assertEqual(self.ingest(payload)[0], 400)
        self.assertEqual(self.records()["total"], 0)

    def test_ingest_body_validation(self):
        cases = [(b"{broken", {"Content-Type": "application/json"}, 400),
                 (b"42", {"Content-Type": "application/json"}, 400),
                 (b"{}", {"Content-Type": "text/plain"}, 415)]
        for body, headers, expected in cases:
            with self.subTest(body=body):
                self.assertEqual(self.request("POST", "/api/ingest", body, self.cookie, headers, raw=True)[0], expected)

    def test_ingest_rejects_oversized_json_body(self):
        status, _, _ = self.request("POST", "/api/ingest", b" " * (MAX_BODY + 1), self.cookie,
                                     {"Content-Type": "application/json"}, raw=True)
        self.assertEqual(status, 400)
        self.assertEqual(self.records()["total"], 0)

    def test_two_accounts_isolate_records_reports_export_and_status(self):
        bob = self.connect("bob")
        self.ingest(event())
        self.assertEqual(self.records(bob)["total"], 0)
        self.assertEqual(self.ingest(event(), bob)[2]["accepted"], 1)
        self.ingest(event("two"))
        self.assertEqual(self.records()["total"], 2)
        self.assertEqual(self.records(bob)["total"], 1)
        for cookie, expected in ((self.cookie, 2), (bob, 1)):
            status, _, body = self.json_request("GET", "/api/status", cookie=cookie)
            self.assertEqual(status, 200)
            self.assertEqual(body["collection"]["exact"], expected)
            status, _, body = self.json_request("GET", "/api/report?period=year", cookie=cookie)
            self.assertEqual(status, 200)
            self.assertEqual(body["totals"]["plays"], 0)  # Fixture is 2024, not now.
            status, _, body = self.request("GET", "/api/export", cookie=cookie)
            self.assertEqual(status, 200)
            self.assertEqual(len(list(csv.reader(io.StringIO(body.decode("utf-8-sig"))))), expected + 1)
        # Attempted client-supplied account ID cannot select Alice's records.
        self.assertEqual(self.records(bob, "?user_id=" + self.account["user_id"])["total"], 1)
        # Exercise report isolation with events inside the actual current period,
        # not merely the older event fixtures excluded by date filtering.
        stamp = int(time.time() * 1000)
        self.ingest([event(stamp=stamp), event("two", stamp)])
        self.ingest(event(stamp=stamp), bob)
        for cookie, expected in ((self.cookie, 2), (bob, 1)):
            for period in ("week", "month", "year"):
                status, _, report = self.json_request("GET", "/api/report?period=" + period, cookie=cookie)
                self.assertEqual(status, 200)
                self.assertEqual(report["totals"]["plays"], expected)

    def test_report_validation_and_shape(self):
        for query in ("period=day", "period=week&offset=-1", "offset=121", "offset=NaN", "offset=0.5"):
            with self.subTest(query=query):
                self.assertEqual(self.request("GET", "/api/report?" + query, cookie=self.cookie)[0], 400)
        for period, timeline_length in (("week", 7), ("month", None), ("year", 12)):
            status, _, body = self.json_request("GET", "/api/report?period=" + period, cookie=self.cookie)
            self.assertEqual(status, 200)
            self.assertEqual(body["period"], period)
            self.assertEqual(len(body["hourly"]), 24)
            self.assertLess(body["start"], body["end"])
            if timeline_length:
                self.assertEqual(len(body["timeline"]), timeline_length)

    def test_report_maximum_documented_offset_is_valid(self):
        for period in ("week", "month", "year"):
            with self.subTest(period=period):
                status, _, body = self.json_request("GET", f"/api/report?period={period}&offset=120", cookie=self.cookie)
                self.assertEqual(status, 200, body)

    def test_records_pagination_search_and_exclusive_end(self):
        self.ingest([event(), event("two", STAMP + 1000), event("three", STAMP + 2000)])
        page1 = self.records(query="?page=1&size=2")
        page2 = self.records(query="?page=2&size=2")
        self.assertEqual(page1["total"], 3)
        self.assertEqual([row["guid"] for row in page1["items"]], ["three", "two"])
        self.assertEqual([row["guid"] for row in page2["items"]], ["one"])
        self.assertEqual(self.records(query="?" + urlencode({"q": "Artist A"}))["total"], 1)
        self.assertEqual(self.records(query="?q=%25")["total"], 0)
        self.assertEqual(self.records(query="?q=%27%20OR%201%3D1--")["total"], 0)
        bounded = self.records(query=f"?start={STAMP}&end={STAMP + 1000}")
        self.assertEqual([row["guid"] for row in bounded["items"]], ["one"])
        self.assertEqual(self.records(query="?page=10&size=2")["items"], [])

    def test_records_reject_invalid_pagination_and_dates(self):
        for query in ("page=0", "page=-1", "page=abc", "page=100001", "size=0", "size=101", "start=bad", "end=bad"):
            with self.subTest(query=query):
                self.assertEqual(self.request("GET", "/api/records?" + query, cookie=self.cookie)[0], 400)

    def test_csv_download_neutralizes_all_formula_prefixes(self):
        prefixes = ["=SUM(1,2)", "+cmd", "-cmd", "@cmd", " \t=IMPORTXML()", "\t+cmd", "\r@cmd"]
        for index, prefix in enumerate(prefixes):
            self.service.archive.upsert_track(self.account["user_id"], {
                "guid": "one", "title": prefix, "artist": prefix, "album": prefix, "duration_ms": 180000})
            self.ingest(event(stamp=STAMP + index * 1000))
            status, headers, content = self.request("GET", "/api/export", cookie=self.cookie)
            self.assertEqual(status, 200)
            self.assertTrue(content.startswith(b"\xef\xbb\xbf"))
            self.assertIn("attachment", headers["Content-Disposition"])
            self.assertTrue(headers["Content-Type"].startswith("text/csv"))
            rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))
            for row in rows[1:]:
                for cell in row[2:5]:
                    self.assertEqual(cell, "'" + prefix)

    def test_metadata_history_and_manual_sync_do_not_invent_plays(self):
        self.assertEqual(self.records()["total"], 0)
        self.assertEqual(self.json_request("POST", "/api/sync", {}, self.cookie)[0], 200)
        self.assertEqual(self.records()["total"], 0)
        self.assertEqual(self.service.archive.track(self.account["user_id"], "one")["duration_ms"], 180000)

    def test_html_bridge_and_nonsecret_sentinel_cookie(self):
        for path in ("/music/", "/music", "/music/gzip.html", "/music/no-head.html"):
            with self.subTest(path=path):
                status, headers, content = self.request("GET", path, cookie=self.cookie)
                self.assertEqual(status, 200)
                self.assertIn(b'<script src="/api/collector.js"></script>', content)
                self.assertIn(b"player", content)
                self.assertEqual(content.count(b"/api/collector.js"), 1)
                self.assertIn("music-token=journal-gateway", headers["Set-Cookie"])
                self.assertIn("SameSite=Strict", headers["Set-Cookie"])
                self.assertIn("Path=/music/", headers["Set-Cookie"])
                self.assertNotIn("HttpOnly", headers["Set-Cookie"])
                self.assertNotIn("upstream-secret", headers["Set-Cookie"])
                self.assertNotIn("X-Frame-Options", headers)
                self.assertNotIn("Content-Encoding", headers)
                self.assertNotIn("Content-Security-Policy", headers)
                self.assertEqual(int(headers["Content-Length"]), len(content))

    def test_proxy_replaces_session_cookie_and_forwards_signature(self):
        status, _, _ = self.request("GET", "/music/audio", cookie=self.cookie + "; music-token=journal-gateway; other=private",
                                     headers={"authx": "nonce=fixture&timestamp=1&sign=fixture", "Accept-Encoding": "gzip"})
        self.assertEqual(status, 200)
        outgoing = self.nas.requests[-1]["headers"]
        self.assertEqual(outgoing["Cookie"], "music-token=upstream-secret-alice")
        self.assertNotIn("journal_session", outgoing["Cookie"])
        self.assertEqual(outgoing.get("Authx", outgoing.get("authx")), "nonce=fixture&timestamp=1&sign=fixture")
        self.assertEqual(outgoing["Accept-Encoding"], "identity")
        self.assertEqual(outgoing["Origin"], self.nas.origin)

    def test_byte_range_head_and_unknown_length_stream(self):
        status, headers, body = self.request("GET", "/music/audio", cookie=self.cookie, headers={"Range": "bytes=17-128"})
        self.assertEqual(status, 206)
        self.assertEqual(body, MEDIA[17:129])
        self.assertEqual(headers["Content-Range"], f"bytes 17-128/{len(MEDIA)}")
        self.assertEqual(headers["Content-Length"], "112")
        status, headers, body = self.request("HEAD", "/music/audio", cookie=self.cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(int(headers["Content-Length"]), len(MEDIA))
        status, _, body = self.request("GET", "/music/stream", cookie=self.cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body, MEDIA)

    def test_head_html_representation_length_matches_injected_get(self):
        get_status, get_headers, get_body = self.request("GET", "/music/", cookie=self.cookie)
        head_status, head_headers, head_body = self.request("HEAD", "/music/", cookie=self.cookie)
        self.assertEqual(get_status, 200)
        self.assertEqual(head_status, 200)
        self.assertEqual(head_body, b"")
        self.assertEqual(int(get_headers["Content-Length"]), len(get_body))
        self.assertEqual(head_headers["Content-Length"], get_headers["Content-Length"],
                         "HEAD must describe the same collector-injected HTML representation as GET")

    def test_non_html_compressed_proxy_remains_decodable(self):
        status, headers, body = self.request("GET", "/music/gzip.js", cookie=self.cookie)
        self.assertEqual(status, 200)
        decoded = gzip.decompress(body) if headers.get("Content-Encoding") == "gzip" else body
        self.assertEqual(decoded, b"window.fixture = true;", "Compressed bytes require Content-Encoding or decompression")

    def test_redirect_is_visible_and_same_nas_location_rewritten(self):
        status, headers, _ = self.request("GET", "/music/redirect", cookie=self.cookie)
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/music/")
        status, headers, _ = self.request("GET", "/music/external-redirect", cookie=self.cookie)
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "http://127.0.0.1:1/never-contact")
        self.assertEqual(len(self.nas.requests), 2)

    def test_proxy_blocks_traversal_and_non_music_paths(self):
        for path in ("/music/../private", "/music/%2e%2e/private", "/music/./audio"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path, cookie=self.cookie)[0], 400)
        self.assertEqual(self.request("GET", "/private", cookie=self.cookie)[0], 404)
        self.assertEqual(self.nas.requests, [])

    def test_confirmed_nas_code_zero_event_and_cross_channel_dedupe(self):
        payload = {"events": [event(), event("two"), {"eventType": "track_pause", "occurredAt": STAMP}]}
        status, _, response = self.json_request("POST", EVENT_PATH, payload, self.cookie)
        self.assertEqual(status, 200)
        self.assertEqual(response, self.nas.event_reply)
        self.assertEqual(json.loads(self.nas.requests[-1]["body"]), payload)
        records = self.records()
        self.assertEqual(records["total"], 2)
        self.assertTrue(all(row["source"] == "gateway" and row["confidence"] == "exact" for row in records["items"]))
        self.assertEqual(self.ingest(payload)[2]["deduped"], 2)
        self.assertEqual(self.records()["total"], 2)

    def test_bridge_then_gateway_same_event_is_only_one_record(self):
        self.assertEqual(self.ingest(event())[2]["accepted"], 1)
        self.assertEqual(self.json_request("POST", EVENT_PATH, event(), self.cookie)[0], 200)
        self.assertEqual(self.records()["total"], 1)
        self.assertEqual(self.records()["items"][0]["source"], "browser_bridge")

    def test_rejected_upstream_event_is_never_stored(self):
        for status, reply in ((200, {"code": 1001, "message": "denied"}),
                              (401, {"code": 0}), (500, {"code": 0}),
                              (200, {"data": None}), (200, {"code": "0"})):
            with self.subTest(status=status, reply=reply):
                self.nas.event_status, self.nas.event_reply = status, reply
                self.assertEqual(self.json_request("POST", EVENT_PATH, event(), self.cookie)[0], status)
                self.assertEqual(self.records()["total"], 0)

    def test_boolean_false_is_not_numeric_code_zero_confirmation(self):
        self.nas.event_reply = {"code": False, "message": "invalid protocol"}
        self.assertEqual(self.json_request("POST", EVENT_PATH, event(), self.cookie)[0], 200)
        self.assertEqual(self.records()["total"], 0, "JSON false is not the numeric NAS success code 0")

    def test_cover_proxy_contract_and_validation(self):
        status, headers, content = self.request("GET", "/api/covers/cover-one", cookie=self.cookie)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertTrue(content.startswith(b"\x89PNG"))
        self.assertIn(("cover", "cover-one", "upstream-secret-alice"), FakeMusicClient.calls)
        for path in ("/api/covers/", "/api/covers/%2e%2e", "/api/covers/" + "a" * 129):
            self.assertEqual(self.request("GET", path, cookie=self.cookie)[0], 400)

    def test_authenticated_collector_script_is_served(self):
        status, headers, body = self.request("GET", "/api/collector.js", cookie=self.cookie)
        self.assertEqual(status, 200)
        self.assertIn("application/javascript", headers["Content-Type"])
        self.assertIn(b"__musicJournalCollector", body)
        self.assertIn(self.account["user_id"].encode(), body)
        self.assertNotIn(b"upstream-secret", body)
        self.assertNotIn(self.cookie.encode(), body)

    def test_old_collector_tab_account_header_rejected_after_switch(self):
        bob = self.connect("bob")
        status, _, result = self.json_request("POST", "/api/ingest", event(), bob,
                                               {"X-Journal-Account": self.account["user_id"]})
        self.assertEqual(status, 409, result)
        self.assertEqual(self.records(bob)["total"], 0)
        self.assertEqual(self.records()["total"], 0)
        status, _, result = self.json_request("POST", "/api/ingest", event(), self.cookie,
                                               {"X-Journal-Account": self.account["user_id"]})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["accepted"], 1)


    def test_theme_script_is_served_for_day_night_switch(self):
        status, headers, body = self.request("GET", "/theme.js")
        self.assertEqual(status, 200)
        self.assertIn("application/javascript", headers["Content-Type"])
        self.assertIn(b"theme-", body)
        status, _, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"/theme.js", page)
        self.assertIn(b"theme-toggle", page)


    def test_write_origin_rules_tolerate_proxies_and_ports(self):
        port = self.httpd.server_port
        cases = [
            ({"Origin": self.origin}, 200),
            ({"Origin": "http://127.0.0.1", "Host": "127.0.0.1:80"}, 200),
            ({"Origin": "http://mynas.example", "Host": f"127.0.0.1:{port}",
              "Sec-Fetch-Site": "same-origin"}, 200),
            ({"Origin": "http://mynas.example", "Host": f"127.0.0.1:{port}"}, 403),
            ({"Origin": "null"}, 403),
            ({"Origin": f"http://127.0.0.1:{port + 1}"}, 403),
            ({"Origin": self.origin, "Sec-Fetch-Site": "same-site"}, 403),
            ({"Origin": self.origin, "Sec-Fetch-Site": "cross-site"}, 403),
        ]
        for headers, expected in cases:
            with self.subTest(headers=headers):
                status, _, _ = self.json_request("POST", "/api/connect", {
                    "server": self.nas.origin, "username": "alice",
                    "password": "credential-secret"}, headers=headers)
                self.assertEqual(status, expected)

    def test_cross_site_rejection_is_logged_with_evidence(self):
        with self.assertLogs("journal", level="WARNING") as captured:
            status, _, _ = self.json_request("POST", "/api/connect", {
                "server": self.nas.origin, "username": "alice", "password": "credential-secret"},
                headers={"Origin": "http://hostile.invalid", "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)
        joined = " ".join(captured.output)
        self.assertIn("Cross-site write rejected", joined)
        self.assertIn("hostile.invalid", joined)
        self.assertIn("cross-site", joined)

    def test_sessions_survive_service_restart_and_logout_removes_them(self):
        restarted = Service(self.temp.name, self.nas.origin, trust_lan=False)
        self.assertIsNotNone(restarted.session(self.cookie))
        self.assertEqual(restarted.session(self.cookie)["user"]["name"], "alice")
        status, _, _ = self.json_request("POST", "/api/disconnect", {}, self.cookie)
        self.assertEqual(status, 200)
        again = Service(self.temp.name, self.nas.origin, trust_lan=False)
        self.assertIsNone(again.session(self.cookie))

    def test_expired_persisted_sessions_are_ignored_and_pruned(self):
        self.service.archive.save_session("stale-token", "whatever", time.time() - 10)
        fresh = Service(self.temp.name, self.nas.origin, trust_lan=False)
        self.assertIsNone(fresh.session("journal_session=stale-token"))
        self.assertIsNone(fresh.archive.load_session("stale-token"))


class SharedLanSessionTests(unittest.TestCase):
    """Internal-network devices share the saved account session (trust mode)."""

    @classmethod
    def setUpClass(cls):
        cls.nas = ThreadingHTTPServer(("127.0.0.1", 0), FakeNASHandler)
        cls.nas.daemon_threads = True
        cls.nas.origin = f"http://127.0.0.1:{cls.nas.server_port}"
        cls.nas.requests = []
        cls.nas.event_status = 200
        cls.nas.event_reply = {"code": 0, "message": "success", "data": None}
        cls.nas_thread = threading.Thread(target=cls.nas.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        cls.nas_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.nas.shutdown()
        cls.nas.server_close()
        cls.nas_thread.join(timeout=2)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="journal-lan-test-", ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.client_patch = patch("app.connector.MusicClient", FakeMusicClient)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        FakeMusicClient.calls = []
        self.service = Service(self.temp.name, self.nas.origin, trust_lan=True)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.httpd.service = self.service
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.service.stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, body=None, cookie=None, headers=None):
        outgoing = dict(headers or {})
        if cookie is not None:
            outgoing["Cookie"] = cookie
        if body is not None:
            body = json.dumps(body).encode()
            outgoing.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=outgoing)
            response = connection.getresponse()
            return response.status, response.getheaders(), response.read()
        finally:
            connection.close()

    def json_request(self, method, path, body=None, cookie=None, headers=None):
        status, response_headers, content = self.request(method, path, body, cookie, headers)
        return status, response_headers, json.loads(content)

    @staticmethod
    def set_cookies(headers):
        return [value for key, value in headers if key.lower() == "set-cookie"]

    def connect(self, username):
        status, headers, body = self.json_request("POST", "/api/connect", {
            "server": self.nas.origin, "username": username, "password": "credential-secret"})
        self.assertEqual(status, 200, body)
        return headers

    def test_default_service_trusts_the_internal_network(self):
        self.assertTrue(Service(self.temp.name).trust_lan)

    def test_private_peer_classification(self):
        for peer in ("127.0.0.1", "::1", "192.168.1.50", "10.0.0.8", "172.16.0.99",
                     "100.100.1.2", "169.254.10.10", "fd00::1"):
            with self.subTest(peer=peer):
                self.assertTrue(private_peer(peer))
        for peer in ("8.8.8.8", "203.0.113.9", "2001:4860:4860::8888", "not-an-address", ""):
            with self.subTest(peer=peer):
                self.assertFalse(private_peer(peer))

    def test_saved_account_is_shared_without_per_device_login(self):
        self.connect("alice")
        # A brand new device (no cookie at all) already sees the saved account.
        status, _, body = self.json_request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertTrue(body["connected"])
        self.assertEqual(body["user"]["name"], "alice")
        self.assertEqual(self.json_request("GET", "/api/report")[0], 200)
        self.assertEqual(self.json_request("GET", "/api/records")[0], 200)
        self.assertEqual(self.request("GET", "/api/export")[0], 200)

    def test_logout_marker_excludes_only_that_browser(self):
        self.connect("alice")
        status, headers, _ = self.json_request("POST", "/api/disconnect", {})
        self.assertEqual(status, 200)
        cookies = self.set_cookies(headers)
        self.assertTrue(any(value.startswith("journal_logout=1") for value in cookies), cookies)
        self.assertTrue(any(value.startswith("journal_session=;") for value in cookies), cookies)
        # The browser that logged out stays logged out until it logs in again.
        self.assertEqual(self.json_request("GET", "/api/records", cookie="journal_logout=1")[0], 401)
        self.assertEqual(self.json_request("GET", "/api/status", cookie="journal_logout=1")[2]["connected"], False)
        # Other internal devices keep the shared session.
        self.assertEqual(self.json_request("GET", "/api/records")[0], 200)

    def test_connect_clears_the_logout_marker(self):
        self.connect("alice")
        self.json_request("POST", "/api/disconnect", {})
        headers = self.connect("alice")
        cookies = self.set_cookies(headers)
        self.assertTrue(any(value.startswith("journal_logout=;") for value in cookies), cookies)

    def test_public_peer_and_strict_mode_never_share(self):
        self.connect("alice")
        self.assertIsNotNone(self.service.resolve(None, "127.0.0.1"))
        self.assertIsNotNone(self.service.resolve(None, "192.168.1.50"))
        self.assertIsNone(self.service.resolve(None, "203.0.113.9"))
        strict = Service(self.temp.name, self.nas.origin, trust_lan=False)
        self.assertIsNone(strict.resolve(None, "127.0.0.1"))
        with tempfile.TemporaryDirectory(prefix="journal-lan-empty-") as empty:
            self.assertIsNone(Service(empty, self.nas.origin).resolve(None, "127.0.0.1"))

    def test_shared_session_survives_restart_through_saved_connection(self):
        self.connect("alice")
        restarted = Service(self.temp.name, self.nas.origin)
        account = restarted.resolve(None, "10.1.2.3")
        self.assertIsNotNone(account)
        self.assertEqual(account["user"]["name"], "alice")


if __name__ == "__main__":
    unittest.main()
