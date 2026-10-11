"""End-to-end loopback MusicClient -> Service -> HTTP -> disposable archive.

Also smoke-tests the browser fixture API without starting the preview server
on its fixed port. All HTTP servers bind ephemeral 127.0.0.1 ports.
"""
import csv
import http.client
import io
import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

from app.connector import MusicClient, MusicError
from app.server import Handler, Service
from scripts.preview_fixture import NASFixtureHandler, USER, USERNAME, PASSWORD, TRACKS, seed


class FlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.nas = ThreadingHTTPServer(("127.0.0.1", 0), NASFixtureHandler)
        cls.nas.daemon_threads = True
        cls.origin = f"http://127.0.0.1:{cls.nas.server_port}"
        cls.nas_thread = threading.Thread(target=cls.nas.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        cls.nas_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.nas.shutdown()
        cls.nas.server_close()
        cls.nas_thread.join(timeout=2)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="journal-flow-test-", ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.service = Service(self.temp.name, self.origin)
        self.account_id = seed(self.service, self.origin)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.service = self.service
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        status, headers, body = self.request("POST", "/api/connect", {
            "server": self.origin, "username": USERNAME, "password": PASSWORD})
        self.assertEqual(status, 200, body)
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]

    def close_server(self):
        self.service.stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, body=None, authenticated=False):
        headers = {"Cookie": self.cookie} if authenticated else {}
        if body is not None:
            body = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_real_connector_login_me_history_track_and_cover_contract(self):
        client = MusicClient(self.origin)
        result = client.login(USERNAME, PASSWORD)
        self.assertEqual(result["user"], USER)
        self.assertIsInstance(result["token"], str)
        self.assertEqual(client.me(), USER)
        self.assertEqual(client.history(), TRACKS)
        metadata = client.track(TRACKS[0]["guid"])
        self.assertEqual(metadata["track"]["guid"], TRACKS[0]["guid"])
        self.assertEqual(metadata["audioSpec"]["duration"], TRACKS[0]["duration"])
        body, content_type = client.cover("fixture-cover-0")
        self.assertEqual(content_type, "image/svg+xml")
        self.assertIn(b"TEST SAMPLE", body)

    def test_fixture_connect_nonempty_reports_and_paginated_records(self):
        status, _, body = self.request("GET", "/api/status", authenticated=True)
        self.assertEqual(status, 200)
        status_body = json.loads(body)
        self.assertTrue(status_body["connected"])
        self.assertEqual(status_body["collection"]["exact"], 67)
        self.assertEqual(self.service.session(self.cookie)["user"]["guid"], "guidfixture")
        for period in ("week", "month", "year"):
            with self.subTest(period=period):
                status, _, body = self.request("GET", "/api/report?period=" + period, authenticated=True)
                self.assertEqual(status, 200, body)
                report = json.loads(body)
                self.assertGreaterEqual(report["totals"]["plays"], 40)
                self.assertGreater(report["totals"]["estimated_duration_ms"], 0)
                self.assertGreater(len(report["tops"]["tracks"]), 0)
        status, _, body = self.request("GET", "/api/report?period=week&offset=1", authenticated=True)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(json.loads(body)["totals"]["plays"], 12)
        status, _, body = self.request("GET", "/api/records?page=2&size=25", authenticated=True)
        self.assertEqual(status, 200)
        records = json.loads(body)
        self.assertEqual(records["total"], 67)
        self.assertEqual(len(records["items"]), 25)
        self.assertEqual(records["page"], 2)

    def test_fixture_csv_has_all_records_and_formula_is_escaped(self):
        status, _, content = self.request("GET", "/api/export", authenticated=True)
        self.assertEqual(status, 200)
        rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 68)
        formula_rows = [row for row in rows[1:] if "TEST_FORMULA" in row[2]]
        self.assertTrue(formula_rows)
        self.assertTrue(all(row[2].startswith("'=") for row in formula_rows))
        self.assertTrue(all(row[6] == "test_fixture" for row in rows[1:]))

    def test_fixture_player_html_confirmed_event_and_bridge_deduplication(self):
        status, headers, body = self.request("GET", "/music/", authenticated=True)
        self.assertEqual(status, 200)
        self.assertIn(b"TEST FIXTURE NAS", body)
        self.assertIn(b"/api/collector.js", body)
        self.assertIn("music-token=journal-gateway", headers["Set-Cookie"])
        payload = {"events": [{"eventType": "track_play", "occurredAt": int(time.time() * 1000),
                               "payload": {"trackGUID": TRACKS[0]["guid"]}}]}
        status, _, body = self.request("POST", "/music/api/v1/event/report", payload, authenticated=True)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["code"], 0)
        status, _, body = self.request("POST", "/api/ingest", payload, authenticated=True)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["deduped"], 1)
        self.assertEqual(self.service.archive.records(self.account_id)["total"], 68)

    def test_real_connector_rejected_login_is_safe_and_does_not_erase_fixture(self):
        with self.assertRaises(MusicError):
            MusicClient(self.origin).login(USERNAME, "wrong-test-password")
        status, _, body = self.request("POST", "/api/connect", {
            "server": self.origin, "username": USERNAME, "password": "wrong-test-password"})
        self.assertEqual(status, 401)
        self.assertNotIn(b"wrong-test-password", body)
        self.assertEqual(self.service.archive.records(self.account_id)["total"], 67)


if __name__ == "__main__":
    unittest.main()
