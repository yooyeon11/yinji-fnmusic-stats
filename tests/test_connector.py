"""Offline connector tests. Never contact the user's music service."""
from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from app.connector import MusicClient, MusicError, _authx, parse_artists, parse_track


class MockMusicHandler(BaseHTTPRequestHandler):
    records = []
    login_error = False
    history_pages = {}
    redirects_hit = 0

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def do_HEAD(self):
        self.handle_request()

    def handle_request(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        type(self).records.append((self.command, self.path, dict(self.headers), body))
        path = urlsplit(self.path).path
        status, content_type = 200, "application/json"
        headers = {}
        if path == "/music/api/v1/user/password-login":
            value = {"code": 120001, "msg": "echoed-sensitive-input", "data": None} if self.login_error else {
                "code": 0, "data": {"userToken": "mock-token", "user": {"name": "mock-user"}},
            }
        elif path == "/music/api/v1/user/me":
            value = {"code": 0, "data": {"name": "mock-user"}}
        elif path == "/music/api/v1/play-history/list":
            page = int(parse_qs(urlsplit(self.path).query)["page"][0])
            value = {"code": 0, "data": self.history_pages.get(page, {"list": [], "total": 0})}
        elif path == "/music/api/v1/track/metadata":
            guid = parse_qs(urlsplit(self.path).query)["guid"][0]
            value = {"code": 0, "data": {"track": {"guid": guid, "title": "Test"}, "audioSpec": {"duration": 1200}}}
        elif path == "/music/api/v1/static/cover":
            value, content_type = b"\x89PNG\r\n", "image/png"
        elif path == "/music/error":
            status, value, content_type = 403, b"original forbidden body", "text/plain"
        elif path == "/music/redirect":
            status, value, headers["Location"] = 302, b"redirect body", "http://127.0.0.1:1/outside"
        elif path == "/music/same-redirect":
            status, value, headers["Location"] = 307, b"same origin redirect", "/music/redirect-target"
        elif path == "/music/redirect-target":
            type(self).redirects_hit += 1
            value = b"should not follow"
        elif path == "/music/echo":
            status, value, content_type = 206, body or b"binary\x00\xff", "application/octet-stream"
            headers.update({"Content-Range": "bytes 0-9/100", "ETag": '"mock"',
                            "Content-Encoding": "identity", "Connection": "close"})
        else:
            value = b"public mock HTML"
            content_type = "text/html"
        raw = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)


class ConnectorHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), MockMusicHandler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.server = "http://127.0.0.1:" + str(cls.httpd.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join()

    def setUp(self):
        MockMusicHandler.records.clear()
        MockMusicHandler.login_error = False
        MockMusicHandler.history_pages = {}
        MockMusicHandler.redirects_hit = 0
        self.client = MusicClient(self.server + "/music/")

    def test_server_normalization(self):
        self.assertEqual(self.client.server, self.server)
        self.assertEqual(MusicClient(self.server + "/").server, self.server)

    def test_login_exact_wire_format_and_memory_only(self):
        output = io.StringIO()
        with patch("sys.stdout", output), patch("sys.stderr", output):
            result = self.client.login(" mock-user ", "dummy-password-only-for-test")
        self.assertEqual(result, {"token": "mock-token", "user": {"name": "mock-user"}})
        self.assertEqual(self.client.token, "mock-token")
        self.assertEqual(output.getvalue(), "")
        method, path, headers, raw = MockMusicHandler.records[-1]
        payload = json.loads(raw)
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/music/api/v1/user/password-login")
        self.assertEqual(set(payload), {"username", "password", "deviceId"})
        self.assertEqual(payload["username"], "mock-user")
        self.assertEqual(payload["password"], hashlib.sha256(b"dummy-password-only-for-test").hexdigest())
        self.assertRegex(payload["deviceId"], r"^[a-f0-9]{32}$")
        self.assertNotIn(b"dummy-password-only-for-test", raw)
        self.assertRegex(headers["Authx"], r"^nonce=\d{6}&timestamp=\d+&sign=[a-f0-9]{32}$")
        self.assertNotIn("Cookie", headers)

    def test_login_failure_clears_token_and_sanitizes_message(self):
        self.client.token = "old-token"
        MockMusicHandler.login_error = True
        with self.assertRaises(MusicError) as context:
            self.client.login("mock-user", "dummy-password")
        self.assertEqual(context.exception.code, 120001)
        self.assertEqual(context.exception.status, 200)
        self.assertNotIn("echoed-sensitive-input", str(context.exception))
        self.assertEqual(self.client.token, "")

    def test_login_missing_fields_does_not_accept_token(self):
        with patch.object(self.client, "_json", return_value={"userToken": "secret-but-no-user"}):
            with self.assertRaises(MusicError):
                self.client.login("mock", "dummy")
        self.assertEqual(self.client.token, "")

    def test_me_cookie_injection(self):
        self.client.token = "mock-token/+="
        self.assertEqual(self.client.me(), {"name": "mock-user"})
        self.assertEqual(MockMusicHandler.records[-1][2]["Cookie"], "music-token=mock-token%2F%2B%3D")

    def test_history_empty(self):
        self.assertEqual(self.client.history(), [])

    def test_history_pagination_keeps_raw_values_order_duplicates(self):
        # No playedAt: updatedAt is metadata and is deliberately left untouched.
        first = {"guid": "same-track", "updatedAt": 1710000000, "custom": {"x": 1}}
        second = {"guid": "same-track", "playedAt": 1710000000123}
        MockMusicHandler.history_pages = {
            1: {"list": [first], "total": 2},
            2: {"list": [second], "total": 2},
        }
        items = self.client.history()
        self.assertEqual(items, [first, second])
        self.assertNotIn("playedAt", items[0])
        self.assertNotIn("historyId", items[0])
        self.assertEqual([parse_qs(urlsplit(record[1]).query)["page"][0]
                          for record in MockMusicHandler.records], ["1", "2"])

    def test_history_without_total_stops_on_short_page(self):
        MockMusicHandler.history_pages = {1: {"list": [{"guid": "g"}]}}
        self.assertEqual(self.client.history(), [{"guid": "g"}])
        self.assertEqual(len(MockMusicHandler.records), 1)

    def test_history_repeated_page_error(self):
        MockMusicHandler.history_pages = {
            1: {"list": [{"guid": "g"}], "total": 500},
            2: {"list": [{"guid": "g"}], "total": 500},
        }
        with self.assertRaisesRegex(MusicError, "pagination did not advance"):
            self.client.history()

    def test_track_exact_parameter_and_raw_metadata(self):
        value = self.client.track("guid/+&?")
        self.assertEqual(value["track"]["guid"], "guid/+&?")
        self.assertEqual(value["audioSpec"]["duration"], 1200)
        self.assertEqual(parse_track(value)["duration_ms"], 1200)
        self.assertEqual(MockMusicHandler.records[-1][0], "GET")

    def test_cover_exact_path_bytes_and_mime(self):
        body, content_type = self.client.cover("cover+with/slash")
        self.assertEqual(body, b"\x89PNG\r\n")
        self.assertEqual(content_type, "image/png")
        self.assertEqual(urlsplit(MockMusicHandler.records[-1][1]).path, "/music/api/v1/static/cover")
        self.assertEqual(parse_qs(urlsplit(MockMusicHandler.records[-1][1]).query),
                         {"coverId": ["cover+with/slash"]})

    def test_proxy_preserves_status_binary_and_end_to_end_headers(self):
        self.client.token = "real-mock-token"
        status, headers, body = self.client.proxy("POST", "/music/echo?q=1", b"raw\x00body", {
            "Cookie": "journal-session=must-not-leak; music-token=sentinel; other=local",
            "Host": "local.gateway", "Range": "bytes=0-9", "Content-Type": "application/octet-stream",
            "Connection": "close, X-Hop", "X-Hop": "remove", "Authorization": "existing-header",
        })
        self.assertEqual(status, 206)
        self.assertEqual(body, b"raw\x00body")
        self.assertEqual(headers["Content-Range"], "bytes 0-9/100")
        self.assertEqual(headers["ETag"], '"mock"')
        self.assertEqual(headers["Content-Encoding"], "identity")
        self.assertNotIn("Connection", headers)
        request_headers = MockMusicHandler.records[-1][2]
        self.assertEqual(request_headers["Cookie"], "music-token=real-mock-token")
        self.assertNotIn("must-not-leak", str(request_headers))
        self.assertEqual(request_headers["Host"], urlsplit(self.server).netloc)
        self.assertNotIn("X-Hop", request_headers)
        self.assertEqual(request_headers["Range"], "bytes=0-9")
        self.assertEqual(request_headers["Authorization"], "existing-header")
        self.assertNotIn("Authx", request_headers)

    def test_proxy_without_connection_does_not_forward_sentinel_cookie(self):
        self.client.proxy("GET", "/music/", headers={"Cookie": "music-token=sentinel"})
        self.assertNotIn("Cookie", MockMusicHandler.records[-1][2])

    def test_proxy_raw_http_error(self):
        status, headers, body = self.client.proxy("GET", "/music/error")
        self.assertEqual((status, body), (403, b"original forbidden body"))
        self.assertEqual(headers["Content-Type"], "text/plain")

    def test_proxy_external_redirect_never_followed(self):
        status, headers, body = self.client.proxy("GET", "/music/redirect")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "http://127.0.0.1:1/outside")
        self.assertEqual(body, b"redirect body")
        self.assertEqual(len(MockMusicHandler.records), 1)

    def test_proxy_same_origin_redirect_returned_verbatim(self):
        status, headers, body = self.client.proxy("GET", "/music/same-redirect")
        self.assertEqual(status, 307)
        self.assertEqual(headers["Location"], "/music/redirect-target")
        self.assertEqual(MockMusicHandler.redirects_hit, 0)

    def test_proxy_head(self):
        status, _, body = self.client.proxy("HEAD", "/music/")
        self.assertEqual((status, body), (200, b""))

    def test_proxy_blocks_non_music_paths_before_network(self):
        invalid = ["http://evil/music/", "//evil/music/", "/outside", "/musicology/", "music/",
                   "/music/../outside", "/music/%2e%2e/outside", "/music/%252e%252e/outside",
                   "/music/%5c../outside", "/music\\..\\outside", "/music/\x00", "/music/%00",
                   "/music/#fragment", " /music/", "/music/\r\nInjected: header"]
        for path in invalid:
            with self.subTest(path=repr(path)), self.assertRaises(ValueError):
                self.client.proxy("GET", path)
        self.assertEqual(MockMusicHandler.records, [])

    def test_proxy_allows_music_root_and_query_containing_external_url(self):
        self.client.proxy("GET", "/music")
        self.client.proxy("GET", "/music/?redirect=http%3A%2F%2Fexample.test%2Foutside")
        self.assertEqual(len(MockMusicHandler.records), 2)

    def test_json_http_and_schema_errors(self):
        cases = [(401, b'{"code":120001}'), (200, b"not JSON"), (200, b"[]"),
                 (200, b'{"code":true}'), (200, b'{"code":0,"data":[]}')]
        for status, body in cases:
            with self.subTest(status=status, body=body):
                with patch.object(self.client, "proxy", return_value=(status, {}, body)):
                    with self.assertRaises(MusicError):
                        self.client.me()

    def test_string_zero_result_code(self):
        with patch.object(self.client, "proxy", return_value=(200, {}, b'{"code":"0","data":{"name":"mock"}}')):
            self.assertEqual(self.client.me(), {"name": "mock"})

    def test_invalid_history_shape(self):
        with patch.object(self.client, "_json", return_value={"list": "not-list"}):
            with self.assertRaises(MusicError):
                self.client.history()

    def test_cover_errors_and_unknown_mime(self):
        with patch.object(self.client, "proxy", return_value=(404, {}, b"missing")):
            with self.assertRaises(MusicError):
                self.client.cover("id")
        with patch.object(self.client, "proxy", return_value=(200, {}, b"raw")):
            self.assertEqual(self.client.cover("id"), (b"raw", "application/octet-stream"))

    def test_bad_input(self):
        for server in ["file:///music/", "http://user:password@example.test", "https://example.test/other",
                       "https://example.test/music?secret=1", "http://example.test:invalid", "http://x/\n"]:
            with self.subTest(server=server), self.assertRaises(ValueError):
                MusicClient(server)
        with self.assertRaises(ValueError):
            self.client.track("")
        with self.assertRaises(ValueError):
            self.client.cover("")
        with self.assertRaises(ValueError):
            self.client.login("", "dummy")
        with self.assertRaises(ValueError):
            self.client.proxy("CONNECT", "/music/")


class TrackAndSignatureTests(unittest.TestCase):
    def test_parse_artists_dedupes_and_keeps_covers(self):
        raw = {"artists": [{"name": " A ", "coverId": "artist-one"}, {"name": "A", "coverId": "artist-two"},
                           {"name": "B"}, {"nope": 1}, "C", {"name": "  "}]}
        self.assertEqual(parse_artists(raw), [
            {"name": "A", "cover_id": "artist-one"},
            {"name": "B", "cover_id": ""},
            {"name": "C", "cover_id": ""},
        ])

    def test_parse_artists_from_metadata_and_legacy_shapes(self):
        self.assertEqual(parse_artists({"track": {"artists": [{"name": "Duo", "coverId": 123}]}}),
                         [{"name": "Duo", "cover_id": "123"}])
        self.assertEqual(parse_artists({"artist": "Solo"}), [{"name": "Solo", "cover_id": ""}])
        self.assertEqual(parse_artists({"artists": ["A", "B"]}),
                         [{"name": "A", "cover_id": ""}, {"name": "B", "cover_id": ""}])
        self.assertEqual(parse_artists({}), [])
        self.assertEqual(parse_artists("bad"), [])

    def test_flat_history_track(self):
        raw = {"guid": "g", "title": " Title ", "duration": 234567,
               "artists": [{"name": " A "}, {"name": "B"}], "genres": [{"name": "Rock"}, {"name": "Pop"}],
               "album": {"name": " Album ", "originalReleaseYear": 2020, "coverId": "album-cover"},
               "updatedAt": 12345}
        self.assertEqual(parse_track(raw), {
            "guid": "g", "title": "Title", "artist": "A / B", "album": "Album", "duration_ms": 234567,
            "genre": "Rock;Pop", "cover_id": "album-cover", "release_year": 2020,
        })
        self.assertEqual(raw["title"], " Title ")
        self.assertNotIn("playedAt", raw)

    def test_metadata_track_with_audio_spec_duration(self):
        raw = {"track": {"guid": "g", "title": "T", "duration": 10, "year": 1999,
                         "album": "Album text", "artists": ["A"], "genres": ["G"], "coverId": 123},
               "audioSpec": {"duration": 123456}}
        self.assertEqual(parse_track(raw), {
            "guid": "g", "title": "T", "artist": "A", "album": "Album text", "duration_ms": 123456,
            "genre": "G", "cover_id": "123", "release_year": 1999,
        })

    def test_real_metadata_shape_with_null_year_and_album_release_date(self):
        # Synthetic values in the real NAS response shape; no private IDs/title.
        raw = {"track": {"guid": "synthetic-guid", "title": "Synthetic title", "year": None,
                         "coverId": "synthetic-cover", "duration": 123000,
                         "createdAt": 1700000000, "updatedAt": 1710000000,
                         "album": {"guid": "synthetic-album", "name": "Synthetic album",
                                   "releaseDate": "2024-06-21", "coverId": "synthetic-cover"}},
               "audioSpec": {"duration": 181351, "codec": "aac", "container": "m4a"}}
        parsed = parse_track(raw)
        self.assertEqual(parsed["release_year"], 2024)
        self.assertEqual(parsed["cover_id"], "synthetic-cover")
        self.assertEqual(parsed["duration_ms"], 181351)
        self.assertEqual(set(parsed), {"guid", "title", "artist", "album", "duration_ms",
                                       "genre", "cover_id", "release_year"})
        self.assertNotIn("playedAt", raw["track"])

    def test_release_year_precedence_and_unknown_zero(self):
        cases = [
            ({"year": 1999, "album": {"originalReleaseYear": 2001, "releaseDate": "2024-06-21"}}, 1999),
            ({"year": None, "album": {"originalReleaseYear": 2001, "releaseDate": "2024-06-21"}}, 2001),
            ({"year": 0, "album": {"originalReleaseYear": 0, "releaseDate": "2024-06-21"}}, 2024),
            ({"year": None, "album": {"releaseDate": "2023"}}, 2023),
            ({"year": 2000.5, "album": {"releaseDate": "2024-06-21"}}, 2024),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(parse_track(raw)["release_year"], expected)

    def test_invalid_release_dates_never_use_metadata_timestamps(self):
        for value in [None, "", "0000-00-00", "2023-02-29", "2024-13-01", "2024-invalid",
                      "0000", 1710000000, {"year": 2024}]:
            with self.subTest(release_date=value):
                self.assertIsNone(parse_track({"year": None, "album": {"releaseDate": value},
                                               "createdAt": 1700000000, "updatedAt": 1710000000})["release_year"])
        self.assertEqual(parse_track({"album": {"releaseDate": "2024-02-29"}})["release_year"], 2024)

    def test_missing_and_invalid_fields(self):
        self.assertEqual(parse_track({}), {"guid": "", "title": "", "artist": "", "album": "",
                                          "duration_ms": 0, "genre": "", "cover_id": "", "release_year": None})
        for duration in [None, True, -123, "invalid", float("nan"), float("inf")]:
            self.assertEqual(parse_track({"duration": duration})["duration_ms"], 0)
        self.assertIsNone(parse_track({"year": 0})["release_year"])
        self.assertEqual(parse_track({"duration": "1000", "year": "2001"})["duration_ms"], 1000)
        with self.assertRaises(TypeError):
            parse_track([])

    def test_signature_matches_public_js_get_query_rules(self):
        from app.connector import _SIGN_API_KEY
        path = "/music/api/v1/track/metadata?z=ignore&guid=A%2BB+%26%E4%B8%AD&z=last&x=null"
        # JS keeps last z, drops x=null, sorts names, decodes %XX but not '+'.
        data_hash = hashlib.md5("guid=A+B &中&z=last".encode()).hexdigest()
        material = "_".join(("NDzZTVxnRKP8Z0jXg1VAMonaG8akvh", "/music/api/v1/track/metadata",
                             "100042", "1700000000123", data_hash, _SIGN_API_KEY))
        expected = hashlib.md5(material.encode()).hexdigest()
        with patch("app.connector.secrets.randbelow", return_value=42), patch("app.connector.time.time", return_value=1700000000.123):
            self.assertEqual(_authx("GET", path, None), f"nonce=100042&timestamp=1700000000123&sign={expected}")

    def test_signature_matches_public_js_post_json(self):
        from app.connector import _SIGN_API_KEY
        raw = b'{"username":"dummy","password":"not-a-real-digest","deviceId":"dummy"}'
        digest = hashlib.md5(raw).hexdigest()
        material = "_".join(("NDzZTVxnRKP8Z0jXg1VAMonaG8akvh", "/music/api/v1/user/password-login",
                             "100042", "1700000000123", digest, _SIGN_API_KEY))
        expected = hashlib.md5(material.encode()).hexdigest()
        with patch("app.connector.secrets.randbelow", return_value=42), patch("app.connector.time.time", return_value=1700000000.123):
            self.assertEqual(_authx("POST", "/music/api/v1/user/password-login", raw),
                             f"nonce=100042&timestamp=1700000000123&sign={expected}")


if __name__ == "__main__":
    unittest.main()
