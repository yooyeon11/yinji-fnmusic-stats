"""Playback detection tests: history diffing, observed records, dedupe windows."""
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from app.connector import MusicClient
from app.poller import MAX_TRACKED_KEYS, count_new, dedupe_order, detect, history_keys, single_promotion
from app.storage import Archive
from app.timezones import get_timezone
from scripts.preview_fixture import NASFixtureHandler, TOKEN, TRACKS

NOW = 1_760_000_000_000


class StubClient:
    def __init__(self, items, uncapped=False):
        self.items = items
        self.uncapped = uncapped

    def recent(self, size=100):
        return list(self.items) if self.uncapped else list(self.items)[:size]


class HistoryServer(BaseHTTPRequestHandler):
    """Mutable local history endpoint for detection integration tests."""
    items = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        if urlsplit(self.path).path != "/music/api/v1/play-history/list":
            self.send_error(404)
            return
        body = json.dumps({"code": 0, "message": "ok", "data": {"list": list(self.items), "total": len(self.items)}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class CountNewTests(unittest.TestCase):
    def test_no_previous_counts_everything(self):
        self.assertEqual(count_new([], ["a", "b"]), (2, True))

    def test_prefix_and_repeat_detection(self):
        self.assertEqual(count_new(["a", "b"], ["x", "a", "b"]), (1, True))
        self.assertEqual(count_new(["a", "b"], ["x", "y", "a", "b"]), (2, True))
        self.assertEqual(count_new(["a", "b"], ["a", "a", "b"]), (1, True))
        self.assertEqual(count_new(["a", "a", "b"], ["a", "a", "b"]), (0, True))

    def test_unaligned_history_is_not_guessed(self):
        self.assertEqual(count_new(["a", "b", "c"], ["x", "y"]), (0, False))
        self.assertEqual(count_new(["a", "b"], []), (0, False))

    def test_history_keys_marks_unusable_entries(self):
        self.assertEqual(history_keys([{"guid": "a"}, {"nope": 1}, "bad", {"guid": ""}]), ["a", "", "", ""])


class OrderHelperTests(unittest.TestCase):
    def test_dedupe_order_keeps_first_occurrence_and_drops_unusable(self):
        self.assertEqual(dedupe_order(["b", "", "a", "b", "a", "c"]), ["b", "a", "c"])
        self.assertEqual(dedupe_order(["", ""]), [])

    def test_promotion_to_front_is_detected(self):
        self.assertEqual(single_promotion(["a", "b", "c"], ["b", "a", "c"]), "b")
        self.assertEqual(single_promotion(["a", "b", "c"], ["c", "a", "b"]), "c")
        self.assertEqual(single_promotion(["a", "b", "c", "d"], ["c", "a", "b", "d"]), "c")

    def test_promotion_across_tail_trim_is_detected(self):
        self.assertEqual(single_promotion(["a", "b", "c", "d"], ["c", "a", "b"]), "c")
        self.assertEqual(single_promotion(["a", "b", "c", "d", "e", "f"], ["d", "a", "b", "c"]), "d")

    def test_unchanged_or_front_guid_is_not_a_promotion(self):
        self.assertIsNone(single_promotion(["a", "b", "c"], ["a", "b", "c"]))
        # tail trimmed without any play: the front GUID never moved
        self.assertIsNone(single_promotion(["a", "b", "c"], ["a", "b"]))
        # a first-seen GUID at the front belongs to set difference
        self.assertIsNone(single_promotion(["a", "b"], ["x", "a", "b"]))

    def test_ambiguous_shapes_are_not_guessed(self):
        # several entries moved within one poll
        self.assertIsNone(single_promotion(["a", "b", "c", "d"], ["d", "c", "a", "b"]))
        # swap below the front
        self.assertIsNone(single_promotion(["a", "b", "c"], ["a", "c", "b"]))
        # front removal looks like a demotion of the rest, not a promotion
        self.assertIsNone(single_promotion(["a", "b", "c", "d"], ["b", "c", "d"]))
        # promoted GUID gained a follower it cannot account for
        self.assertIsNone(single_promotion(["a", "b", "c"], ["c", "b", "a"]))
        self.assertIsNone(single_promotion([], ["a", "b"]))


class DetectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.archive = Archive(self.temp.name)
        self.account = {"user_id": "u1"}

    def test_first_poll_only_seeds_and_never_backfills(self):
        result = detect(self.archive, self.account, StubClient([{"guid": "a"}, {"guid": "b"}]), now_ms=NOW)
        self.assertEqual(result, {"seeded": 2, "recorded": 0, "deduped": 0, "skipped": 0})
        self.assertEqual(self.archive.records("u1")["total"], 0)
        self.assertEqual(self.archive.get_state("u1", "poll_keys"), ["a", "b"])

    def test_new_entries_become_observed_plays(self):
        detect(self.archive, self.account, StubClient([{"guid": "a"}]), now_ms=NOW)
        later = NOW + 30_000
        result = detect(self.archive, self.account,
                        StubClient([{"guid": "new", "title": "新歌", "artists": [{"name": "歌手"}]}, {"guid": "a"}]),
                        now_ms=later)
        self.assertEqual(result["recorded"], 1)
        records = self.archive.records("u1")
        self.assertEqual(records["total"], 1)
        item = records["items"][0]
        self.assertEqual(item["guid"], "new")
        self.assertEqual(item["source"], "history_poll")
        self.assertEqual(item["confidence"], "observed")
        self.assertEqual(item["played_at"], later)
        self.assertEqual(item["title"], "新歌")
        self.assertEqual(self.archive.status("u1")["estimated"], 1)

    def test_consecutive_repeat_and_return_are_counted(self):
        detect(self.archive, self.account, StubClient([{"guid": "a"}]), now_ms=NOW)
        detect(self.archive, self.account, StubClient([{"guid": "a"}, {"guid": "a"}]), now_ms=NOW + 1000)
        detect(self.archive, self.account, StubClient([{"guid": "b"}, {"guid": "a"}, {"guid": "a"}]), now_ms=NOW + 2000)
        detect(self.archive, self.account, StubClient([{"guid": "a"}, {"guid": "b"}, {"guid": "a"}, {"guid": "a"}]), now_ms=NOW + 3000)
        self.assertEqual(self.archive.records("u1")["total"], 3)

    def test_gateway_exact_play_is_not_double_counted(self):
        detect(self.archive, self.account, StubClient([]), now_ms=NOW)
        event = {"eventType": "track_play", "occurredAt": NOW + 5000, "payload": {"trackGUID": "d"}}
        self.assertEqual(self.archive.ingest("u1", event, "gateway")["accepted"], 1)
        result = detect(self.archive, self.account, StubClient([{"guid": "d"}]), now_ms=NOW + 8000)
        self.assertEqual(result["deduped"], 1)
        self.assertEqual(result["recorded"], 0)
        self.assertEqual(self.archive.records("u1")["total"], 1)

    def test_stale_exact_play_outside_window_still_records(self):
        detect(self.archive, self.account, StubClient([]), now_ms=NOW)
        old = NOW - 30 * 60 * 1000
        event = {"eventType": "track_play", "occurredAt": old, "payload": {"trackGUID": "e"}}
        self.archive.ingest("u1", event, "gateway")
        result = detect(self.archive, self.account, StubClient([{"guid": "e"}]), now_ms=NOW)
        self.assertEqual(result["recorded"], 1)
        self.assertEqual(self.archive.records("u1")["total"], 2)

    def test_unaligned_poll_still_records_first_seen_entries(self):
        detect(self.archive, self.account, StubClient([{"guid": "a"}, {"guid": "b"}, {"guid": "c"}]), now_ms=NOW)
        result = detect(self.archive, self.account, StubClient([{"guid": "x"}, {"guid": "y"}]), now_ms=NOW + 1000)
        self.assertTrue(result.get("unaligned"))
        self.assertEqual(result["recorded"], 2)
        self.assertEqual({item["guid"] for item in self.archive.records("u1")["items"]}, {"x", "y"})
        self.assertEqual(self.archive.get_state("u1", "poll_keys"), ["x", "y"])

    def test_replay_that_promotes_to_front_is_recorded(self):
        detect(self.archive, self.account, StubClient([{"guid": "a"}, {"guid": "b"}, {"guid": "c"}]), now_ms=NOW)
        # server keeps one entry per track: replaying "b" moves it to the front
        result = detect(self.archive, self.account,
                        StubClient([{"guid": "b"}, {"guid": "a"}, {"guid": "c"}]), now_ms=NOW + 1000)
        self.assertTrue(result.get("unaligned"))
        self.assertEqual(result["recorded"], 1)
        item = self.archive.records("u1")["items"][0]
        self.assertEqual(item["guid"], "b")
        self.assertEqual(item["confidence"], "observed")

    def test_replay_promotion_with_tail_trim_is_recorded(self):
        detect(self.archive, self.account,
               StubClient([{"guid": "a"}, {"guid": "b"}, {"guid": "c"}, {"guid": "d"}]), now_ms=NOW)
        result = detect(self.archive, self.account,
                        StubClient([{"guid": "c"}, {"guid": "a"}, {"guid": "b"}]), now_ms=NOW + 1000)
        self.assertEqual(result["recorded"], 1)
        self.assertEqual(self.archive.records("u1")["items"][0]["guid"], "c")

    def test_unaligned_newcomer_and_replay_are_both_recorded(self):
        detect(self.archive, self.account, StubClient([{"guid": "a"}, {"guid": "b"}, {"guid": "c"}]), now_ms=NOW)
        result = detect(self.archive, self.account,
                        StubClient([{"guid": "new", "title": "新歌"}, {"guid": "b"}, {"guid": "a"}, {"guid": "c"}]),
                        now_ms=NOW + 1000)
        self.assertEqual(result["recorded"], 2)
        self.assertEqual({item["guid"] for item in self.archive.records("u1")["items"]}, {"b", "new"})

    def test_ambiguous_reordering_records_nothing_but_keeps_newcomers(self):
        detect(self.archive, self.account,
               StubClient([{"guid": "a"}, {"guid": "b"}, {"guid": "c"}, {"guid": "d"}]), now_ms=NOW)
        # two independent swaps: no single promotion explains this shape
        result = detect(self.archive, self.account,
                        StubClient([{"guid": "b"}, {"guid": "a"}, {"guid": "d"}, {"guid": "c"}]), now_ms=NOW + 1000)
        self.assertTrue(result.get("unaligned"))
        self.assertEqual(result["recorded"], 0)
        self.assertEqual(self.archive.records("u1")["total"], 0)
        self.assertEqual(self.archive.get_state("u1", "poll_keys"), ["b", "a", "d", "c"])

    def test_server_side_compaction_records_nothing(self):
        detect(self.archive, self.account,
               StubClient([{"guid": "a"}, {"guid": "b"}, {"guid": "c"}, {"guid": "d"}]), now_ms=NOW)
        # the server dropped "b" without any play
        result = detect(self.archive, self.account,
                        StubClient([{"guid": "a"}, {"guid": "c"}, {"guid": "d"}]), now_ms=NOW + 1000)
        self.assertTrue(result.get("unaligned"))
        self.assertEqual(result["recorded"], 0)
        self.assertEqual(self.archive.records("u1")["total"], 0)

    def test_unusable_baseline_reseeds_without_attributing(self):
        detect(self.archive, self.account, StubClient([{"nope": 1}]), now_ms=NOW)
        result = detect(self.archive, self.account, StubClient([{"guid": "a"}]), now_ms=NOW + 1000)
        self.assertTrue(result.get("unaligned"))
        self.assertEqual(result["recorded"], 0)
        self.assertEqual(self.archive.get_state("u1", "poll_keys"), ["a"])

    def test_unusable_entries_are_skipped_not_fabricated(self):
        detect(self.archive, self.account, StubClient([]), now_ms=NOW)
        result = detect(self.archive, self.account, StubClient([{"nope": 1}, {"guid": "z"}]), now_ms=NOW + 1000)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["recorded"], 1)
        self.assertEqual(self.archive.records("u1")["items"][0]["guid"], "z")

    def test_accounts_are_isolated(self):
        detect(self.archive, self.account, StubClient([{"guid": "a"}]), now_ms=NOW)
        other = {"user_id": "u2"}
        result = detect(self.archive, other, StubClient([{"guid": "a"}]), now_ms=NOW)
        self.assertEqual(result["seeded"], 1)
        detect(self.archive, other, StubClient([{"guid": "b"}, {"guid": "a"}]), now_ms=NOW + 1000)
        self.assertEqual(self.archive.records("u2")["total"], 1)
        self.assertEqual(self.archive.records("u1")["total"], 0)

    def test_tracked_keys_are_bounded(self):
        items = [{"guid": f"g{i}"} for i in range(MAX_TRACKED_KEYS + 40)]
        detect(self.archive, self.account, StubClient(items, uncapped=True), now_ms=NOW)
        self.assertEqual(len(self.archive.get_state("u1", "poll_keys")), MAX_TRACKED_KEYS)


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.nas = ThreadingHTTPServer(("127.0.0.1", 0), HistoryServer)
        cls.nas.daemon_threads = True
        cls.origin = f"http://127.0.0.1:{cls.nas.server_port}"
        cls.thread = threading.Thread(target=cls.nas.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.nas.shutdown()
        cls.nas.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        HistoryServer.items = []
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.archive = Archive(self.temp.name)
        self.client = MusicClient(self.origin, "unused-test-token")

    def test_real_client_poll_records_new_history_entry(self):
        account = {"user_id": "u1"}
        first = detect(self.archive, account, self.client, now_ms=NOW)
        self.assertEqual(first["seeded"], 0)
        HistoryServer.items = [{"guid": "live", "title": "现场", "artists": [{"name": "歌手"}],
                                "album": {"name": "专辑"}, "duration": 200000}]
        result = detect(self.archive, account, self.client, now_ms=NOW + 20_000)
        self.assertEqual(result["recorded"], 1)
        item = self.archive.records("u1")["items"][0]
        self.assertEqual((item["guid"], item["title"], item["album"]), ("live", "现场", "专辑"))
        self.assertEqual(self.archive.track("u1", "live")["duration_ms"], 200000)

    def test_recent_returns_raw_first_page(self):
        HistoryServer.items = [{"guid": "one", "title": "A"}, {"guid": "two", "title": "B"}]
        self.assertEqual(self.client.recent(100), HistoryServer.items)
        with self.assertRaises(ValueError):
            self.client.recent(0)
        with self.assertRaises(ValueError):
            self.client.recent(True)

    def test_recent_against_reference_fixture_nas(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), NASFixtureHandler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        try:
            client = MusicClient(f"http://127.0.0.1:{server.server_port}", TOKEN)
            self.assertEqual(client.recent(100), TRACKS)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class ReportSplitTests(unittest.TestCase):
    def test_report_separates_exact_and_observed(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Archive(directory)
            archive.upsert_track("u", {"guid": "a", "title": "歌", "artist": "手", "album": "专", "duration_ms": 60000})
            tz = get_timezone("Asia/Shanghai")
            now = datetime(2024, 3, 6, 12, tzinfo=tz)
            moment = int(now.timestamp() * 1000) - 3600_000
            archive.ingest("u", {"eventType": "track_play", "occurredAt": moment, "payload": {"trackGUID": "a"}})
            archive.insert_observed("u", "a", moment + 1000)
            report = archive.report("u", "week", now=now)
            self.assertEqual(report["totals"]["plays"], 2)
            self.assertEqual(report["totals"]["exact"], 1)
            self.assertEqual(report["totals"]["estimated"], 1)


if __name__ == "__main__":
    unittest.main()
