import csv
import io
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from app.timezones import get_timezone

from app.storage import Archive


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.archive = Archive(self.temp.name)
        self.tz = get_timezone("Asia/Shanghai")
        self.now = datetime(2024, 3, 6, 12, tzinfo=self.tz)
        self.meta = {"guid": "one", "title": "歌曲", "artist": "歌手", "album": "专辑", "duration_ms": 180000}
        self.archive.upsert_track("a", self.meta)

    def tearDown(self):
        self.temp.cleanup()

    def event(self, date, guid="one"):
        return {"eventType": "track_play", "occurredAt": int(date.timestamp() * 1000), "payload": {"trackGUID": guid}}

    def test_duplicate_event_across_sources_and_repeat_listen(self):
        event = self.event(self.now)
        self.assertEqual(self.archive.ingest("a", {"events": [event]})["accepted"], 1)
        self.assertEqual(self.archive.ingest("a", [event], "gateway")["deduped"], 1)
        self.assertEqual(self.archive.ingest("a", self.event(self.now + timedelta(minutes=3)))["accepted"], 1)
        self.assertEqual(self.archive.status("a")["exact"], 2)

    def test_accounts_are_isolated(self):
        self.archive.ingest("a", self.event(self.now))
        self.assertEqual(self.archive.records("b")["total"], 0)
        self.assertEqual(self.archive.report("b", now=self.now)["totals"]["plays"], 0)

    def test_artist_covers_override_ranking_images(self):
        self.archive.ingest("a", self.event(self.now))
        # without a stored artist image the ranking keeps the track cover (none here)
        report = self.archive.report("a", "week", now=self.now)
        self.assertIsNone(report["tops"]["artists"][0]["cover_id"])
        self.archive.upsert_artist("a", "歌手", "artist-image-id")
        report = self.archive.report("a", "week", now=self.now)
        self.assertEqual(report["tops"]["artists"][0]["cover_id"], "artist-image-id")
        self.assertEqual(self.archive.artist_covers("a"), {"歌手": "artist-image-id"})
        # an empty cover must never erase an existing image
        self.archive.upsert_artist("a", "歌手", "")
        self.assertEqual(self.archive.artist_covers("a"), {"歌手": "artist-image-id"})
        # artists stay isolated per account
        self.assertEqual(self.archive.artist_covers("b"), {})

    def test_month_and_leap_year_bounds(self):
        start, end, _ = self.archive.bounds("month", 1, self.now)
        self.assertEqual(start.isoformat(), "2024-02-01T00:00:00+08:00")
        self.assertEqual((end - start).days, 29)
        report = self.archive.report("a", "year", 0, self.now)
        self.assertEqual(len(report["calendar"]), 366)
        self.assertEqual(len(report["timeline"]), 12)

    def test_timezone_midnight_and_zero_filled_comparison(self):
        local = datetime(2024, 3, 4, 0, 1, tzinfo=self.tz)
        self.archive.ingest("a", self.event(local))
        report = self.archive.report("a", "week", now=self.now)
        self.assertEqual(report["timeline"][0]["plays"], 1)
        self.assertEqual(len(report["timeline"]), 7)
        self.assertEqual(report["hourly"][0], 1)
        self.assertEqual(report["totals"]["estimated_duration_ms"], 180000)
        self.assertIsNone(report["change_percent"])

    def test_same_title_different_guid_is_not_merged(self):
        self.archive.upsert_track("a", {**self.meta, "guid": "two", "artist": "另一歌手"})
        self.archive.ingest("a", [self.event(self.now), self.event(self.now, "two")])
        report = self.archive.report("a", now=self.now)
        self.assertEqual(report["totals"]["tracks"], 2)
        self.assertEqual(len(report["tops"]["tracks"]), 2)

    def test_discovery_uses_first_observation_not_future_counts(self):
        self.archive.ingest("a", self.event(self.now))
        self.archive.ingest("a", self.event(self.now + timedelta(weeks=2)))
        report = self.archive.report("a", now=self.now)
        self.assertEqual(report["discovery"]["artists"], 1)
        self.assertEqual(report["discovery"]["top_artist"]["plays"], 1)

    def test_invalid_events_never_become_now(self):
        invalid = [{"eventType": "track_pause", "occurredAt": 1},
                   {"eventType": "track_play", "payload": {"trackGUID": "one"}, "occurredAt": "bad"},
                   {"eventType": "track_play", "payload": {"trackGUID": "one"}, "occurredAt": True},
                   {"eventType": "track_play", "payload": {"trackGUID": "one"}, "occurredAt": 1}]
        self.assertEqual(self.archive.ingest("a", invalid)["skipped"], 4)
        self.assertEqual(self.archive.status("a")["exact"], 0)

    def test_metadata_sync_is_not_a_play(self):
        self.assertEqual(self.archive.status("a")["library_tracks"], 1)
        self.assertEqual(self.archive.status("a")["exact"], 0)

    def test_missing_meta_is_enriched_without_new_record(self):
        self.archive.ingest("a", self.event(self.now, "unknown"))
        self.archive.upsert_track("a", {**self.meta, "guid": "unknown", "title": "补全"})
        records = self.archive.records("a")
        self.assertEqual(records["total"], 1)
        self.assertEqual(records["items"][0]["title"], "补全")

    def test_csv_and_search_escape(self):
        self.archive.upsert_track("a", {**self.meta, "title": "=IMPORTXML()"})
        self.archive.ingest("a", self.event(self.now))
        body = self.archive.export("a").decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(body)))
        self.assertTrue(rows[1][2].startswith("'="))
        self.assertEqual(self.archive.records("a", q="%")["total"], 0)
        self.assertEqual(self.archive.records("a", q="IMPORT")["total"], 1)

    def test_validation(self):
        with self.assertRaises(ValueError):
            self.archive.report("a", "invalid")
        with self.assertRaises(ValueError):
            self.archive.records("a", size=1000)


if __name__ == "__main__":
    unittest.main()
