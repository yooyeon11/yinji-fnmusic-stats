import copy
import json
import unittest
from datetime import datetime, timedelta, timezone, tzinfo

from app.report_extras import report_extras


TZ = timezone(timedelta(hours=8))


def ms(value):
    return int(value.timestamp() * 1000)


def play(date, guid="one", **metadata):
    return {"guid": guid, "title": "歌曲", "artist": "歌手", "album": "专辑",
            "genre": None, "duration_ms": 180000, "cover_id": "cover",
            "played_at": ms(date), "confidence": "exact", **metadata}


class ReportExtrasTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2024, 3, 4, tzinfo=TZ)
        self.end = self.start + timedelta(days=7)
        self.now = self.start + timedelta(days=2, hours=12)

    def report(self, rows=(), prev=(), all_rows=None, track_map=None, **bounds):
        return report_extras(rows, prev, rows if all_rows is None else all_rows,
                             track_map or {}, bounds.get("start", self.start),
                             bounds.get("end", self.end), bounds.get("now", self.now),
                             bounds.get("tz", TZ))

    @staticmethod
    def fingerprints(report):
        return {item["label"]: item["value"] for item in report["fingerprint"]}

    @staticmethod
    def facts(report):
        return {item["title"]: item["value"] for item in report["quick_facts"]}

    def test_empty_report_has_stable_shape_zeroes_and_no_fake_metadata(self):
        report = self.report()
        self.assertEqual(len(report["weekday"]), 7)
        self.assertEqual(report["ratios"], {"artists": 0, "albums": 0, "tracks": 0})
        self.assertTrue(all(value == 0 for value in self.fingerprints(report).values()))
        self.assertEqual(report["genres"], [])
        self.assertEqual(report["decades"], [])
        self.assertEqual(report["discovery_extras"], {"albums": 0, "top_album": None, "top_track": None})
        self.assertIsNone(report["first_play"])
        self.assertIsNone(self.facts(report)["最活跃日"])
        self.assertEqual(len(report["genre_stream"]["days"]), 7)
        self.assertEqual(report["genre_stream"]["series"], [])
        json.dumps(report, allow_nan=False)

    def test_weekday_is_monday_zero_and_uses_given_timezone_for_previous(self):
        # UTC Sunday becomes Monday in UTC+8; UTC Saturday becomes Sunday.
        current = play(datetime(2024, 3, 3, 16, 1, tzinfo=timezone.utc))
        previous = [play(datetime(2024, 2, 25, 16, 1, tzinfo=timezone.utc)),
                    play(datetime(2024, 3, 2, 18, tzinfo=timezone.utc))]
        report = self.report([current], previous)
        self.assertEqual(report["weekday"][0], {"day": 0, "plays": 1, "previous": 1})
        self.assertEqual(report["weekday"][6], {"day": 6, "plays": 0, "previous": 1})
        self.assertEqual(self.facts(report)["最活跃日"], "2024-03-04")

    def test_genres_split_semicolons_strip_and_deduplicate_tags_per_play(self):
        rows = [play(self.start, genre=" Rock ;Jazz; Rock;; "),
                play(self.start + timedelta(hours=1), "two", genre="Jazz"),
                play(self.start + timedelta(hours=2), "three", genre="")]
        report = self.report(rows)
        self.assertEqual(report["genres"], [{"name": "Jazz", "plays": 2}, {"name": "Rock", "plays": 1}])
        self.assertEqual(report["genre_stream"]["series"], [
            {"name": "Jazz", "values": [2, 0, 0, 0, 0, 0, 0]},
            {"name": "Rock", "values": [1, 0, 0, 0, 0, 0, 0]}])

    def test_missing_genre_can_use_real_metadata_but_not_a_synthetic_genre(self):
        rows = [play(self.start), play(self.start, "two", genre="Folk"), play(self.start, "unknown")]
        report = self.report(rows, track_map={"one": {"genre": "Jazz;Blues"}, "two": {"genre": "Rock"}})
        self.assertEqual(report["genres"], [{"name": "Blues", "plays": 1},
                                           {"name": "Folk", "plays": 1}, {"name": "Jazz", "plays": 1}])

    def test_decades_use_only_explicit_valid_release_years(self):
        values = [1998, "2001", None, True, 1998.0, "1998-01-01", "bad", 0, "１９９８", 10000]
        rows = [play(self.start, str(i), title="1980 hits", album="1960 collection") for i in range(len(values))]
        metadata = {str(i): {"release_year": value} for i, value in enumerate(values)}
        report = self.report(rows, track_map=metadata)
        self.assertEqual(report["decades"], [{"decade": 1990, "plays": 1}, {"decade": 2000, "plays": 1}])

    def test_ratios_use_distinct_names_artist_album_pairs_and_track_guids(self):
        rows = [play(self.start, "one", artist="A", album="Same"),
                play(self.start + timedelta(minutes=1), "one", artist="A", album="Same"),
                play(self.start + timedelta(minutes=2), "two", artist="B", album="Same"),
                play(self.start + timedelta(minutes=3), "three", artist=None, album=None)]
        report = self.report(rows)
        self.assertEqual(report["ratios"], {"artists": 50, "albums": 50, "tracks": 75})
        self.assertEqual(self.fingerprints(report)["重放率"], 25)
        self.assertEqual(self.fingerprints(report)["集中度"], 50)
        # Same title on different GUIDs is not merged; ratios need not sum to 100.
        self.assertEqual(sum(report["ratios"].values()), 175)

    def test_fingerprint_discovery_and_consistency_use_personal_elapsed_data(self):
        rows = [play(self.start, "old", artist="Old"),
                play(self.start + timedelta(days=2, hours=1), "new", artist="New")]
        history = [play(self.start - timedelta(days=50), "old", artist="Old"), *rows]
        report = self.report(rows, all_rows=history)
        self.assertEqual(self.fingerprints(report), {"一致性": 66.67, "发现率": 50, "集中度": 50, "重放率": 0})
        self.assertEqual(self.facts(report)["平均每日"], 0.67)
        self.assertTrue(all("全球" not in item["detail"] for item in report["fingerprint"]))

    def test_completed_period_denominator_is_capped_at_period_end(self):
        rows = [play(self.start)]
        report = self.report(rows, now=self.end + timedelta(days=300))
        self.assertEqual(self.fingerprints(report)["一致性"], 14.29)
        self.assertEqual(self.facts(report)["平均每日"], 0.14)

    def test_future_period_has_no_observations_and_no_division_by_zero(self):
        report = self.report([play(self.start)], now=self.start - timedelta(milliseconds=1))
        self.assertIsNone(report["first_play"])
        self.assertEqual(self.fingerprints(report)["一致性"], 0)
        self.assertEqual(self.facts(report)["平均每日"], 0)
        self.assertEqual(report["discovery_extras"]["albums"], 0)

    def test_rows_are_half_open_and_future_plays_are_excluded(self):
        rows = [play(self.start - timedelta(milliseconds=1), "before"),
                play(self.start, "at-start"), play(self.now, "at-now"),
                play(self.now + timedelta(milliseconds=1), "future"), play(self.end, "at-end")]
        report = self.report(rows)
        self.assertEqual(sum(item["plays"] for item in report["weekday"]), 2)
        self.assertEqual(report["ratios"]["tracks"], 100)
        self.assertEqual(report["first_play"]["played_at"], ms(self.start))
        self.assertEqual(self.fingerprints(report)["集中度"], 100)

    def test_quick_facts_duration_missing_values_streak_and_peak_tie(self):
        rows = [play(self.start, duration_ms=3600000),
                play(self.start + timedelta(hours=1), duration_ms=180000),
                play(self.start + timedelta(days=1), duration_ms=None),
                play(self.start + timedelta(days=1, hours=1), duration_ms=-1000),
                play(self.start + timedelta(days=3), duration_ms="bad")]
        report = self.report(rows, now=self.end)
        facts = self.facts(report)
        self.assertEqual(facts["估算时长"], "1 小时 3 分钟")
        self.assertEqual(facts["最长连续有记录天数"], 2)
        self.assertEqual(facts["最活跃日"], "2024-03-04")
        self.assertIn("不代表实际听完时长", report["quick_facts"][0]["detail"])
        self.assertIn("不代表连续收听时长", report["quick_facts"][2]["detail"])

    def test_first_play_is_earliest_in_period_not_archive_first_and_keeps_cover(self):
        old = play(self.start - timedelta(days=1), "old")
        first = play(self.start + timedelta(minutes=1), "new", title="First", artist="Artist", cover_id="image")
        later = play(self.start + timedelta(hours=1), "later")
        report = self.report([later, first], all_rows=[later, old, first])
        self.assertEqual(report["first_play"], {"title": "First", "artist": "Artist",
                                               "played_at": first["played_at"], "cover_id": "image"})

    def test_album_discovery_groups_by_artist_and_album_not_guid_or_album_name(self):
        old = play(self.start - timedelta(days=1), "old", artist="A", album="Same")
        rows = [play(self.start, "old", artist="A", album="Same"),
                play(self.start, "new-a-track", artist="A", album="Same"),
                play(self.start, "new-b-track", artist="B", album="Same"),
                play(self.start + timedelta(minutes=1), "b-two", artist="B", album="Same"),
                play(self.start, "no-album", album=None)]
        report = self.report(rows, all_rows=[old, *rows])
        self.assertEqual(report["discovery_extras"]["albums"], 1)
        self.assertEqual(report["discovery_extras"]["top_album"]["name"], "Same")
        self.assertEqual(report["discovery_extras"]["top_album"]["artist"], "B")
        self.assertEqual(report["discovery_extras"]["top_album"]["plays"], 2)

    def test_new_tops_use_only_period_counts_and_never_future_counts(self):
        old = play(self.start - timedelta(days=2), "old", album="Old")
        rows = [play(self.start, "new-one", album="One"),
                play(self.start + timedelta(minutes=1), "new-two", album="Two"),
                play(self.start + timedelta(minutes=2), "new-two", album="Two"),
                *[play(self.start + timedelta(minutes=3 + i), "old", album="Old") for i in range(4)]]
        future = [play(self.end + timedelta(days=i), "new-one", album="One") for i in range(1, 20)]
        report = self.report(rows, all_rows=[old, *rows, *future], now=self.end + timedelta(days=40))
        extras = report["discovery_extras"]
        self.assertEqual(extras["albums"], 2)
        self.assertEqual(extras["top_album"]["name"], "Two")
        self.assertEqual(extras["top_album"]["plays"], 2)
        self.assertEqual(extras["top_track"]["guid"], "new-two")
        self.assertEqual(extras["top_track"]["plays"], 2)

    def test_discovery_history_order_and_ties_are_deterministic(self):
        rows = [play(self.start + timedelta(minutes=1), "b", album="B"),
                play(self.start, "a", album="A")]
        forward = self.report(rows, all_rows=rows)
        backward = self.report(list(reversed(rows)), all_rows=list(reversed(rows)))
        self.assertEqual(forward, backward)
        self.assertEqual(forward["discovery_extras"]["top_track"]["guid"], "a")
        self.assertEqual(forward["discovery_extras"]["top_album"]["name"], "A")

    def test_year_stream_is_monthly_zero_filled_including_leap_day(self):
        start = datetime(2024, 1, 1, tzinfo=TZ)
        end = datetime(2025, 1, 1, tzinfo=TZ)
        rows = [play(datetime(2024, 2, 29, 23, tzinfo=TZ), genre="Jazz"),
                play(datetime(2024, 3, 1, tzinfo=TZ), genre="Jazz"),
                play(datetime(2024, 12, 31, 23, tzinfo=TZ), genre="Jazz")]
        stream = self.report(rows, start=start, end=end, now=end)["genre_stream"]
        self.assertEqual(stream["days"], [f"2024-{month:02d}-01" for month in range(1, 13)])
        self.assertEqual(stream["series"], [{"name": "Jazz", "values": [0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 1]}])

    def test_length_inference_is_daily_at_100_days_monthly_above_100(self):
        daily = self.report(end=self.start + timedelta(days=100))["genre_stream"]
        monthly = self.report(end=self.start + timedelta(days=101))["genre_stream"]
        self.assertEqual(len(daily["days"]), 100)
        self.assertEqual(monthly["days"], ["2024-03-01", "2024-04-01", "2024-05-01", "2024-06-01"])

    def test_month_period_is_daily_per_length_inference_and_excludes_end(self):
        start = datetime(2024, 2, 1, tzinfo=TZ)
        end = datetime(2024, 3, 1, tzinfo=TZ)
        rows = [play(end - timedelta(milliseconds=1), genre="Rock"), play(end, genre="Rock")]
        stream = self.report(rows, start=start, end=end, now=end)["genre_stream"]
        self.assertEqual(len(stream["days"]), 29)
        self.assertEqual(stream["days"][-1], "2024-02-29")
        self.assertEqual(sum(stream["series"][0]["values"]), 1)
        self.assertEqual(stream["series"][0]["values"][-1], 1)

    def test_millisecond_and_datetime_bounds_agree_and_inputs_are_not_mutated(self):
        rows = [play(self.start, genre="Rock;Jazz")]
        all_rows = [*rows, play(self.start - timedelta(days=1), "old")]
        metadata = {"one": {"release_year": 1998}}
        original = copy.deepcopy((rows, all_rows, metadata))
        datetimes = self.report(rows, all_rows=all_rows, track_map=metadata)
        timestamps = self.report(rows, all_rows=all_rows, track_map=metadata,
                                 start=ms(self.start), end=ms(self.end), now=ms(self.now))
        self.assertEqual(datetimes, timestamps)
        self.assertEqual((rows, all_rows, metadata), original)

    def test_naive_boundaries_use_supplied_timezone_not_system_timezone(self):
        rows = [play(self.start)]
        report = self.report(rows, start=self.start.replace(tzinfo=None),
                             end=self.end.replace(tzinfo=None), now=self.now.replace(tzinfo=None))
        self.assertEqual(report, self.report(rows))

    def test_dst_short_day_counts_calendar_days_not_twenty_four_hour_chunks(self):
        class SpringForward(tzinfo):
            """A tiny standard-library-only fixture for the 2024 US DST jump."""
            def utcoffset(self, value):
                cutoff = datetime(2024, 3, 10, 2)
                return timedelta(hours=-4 if value.replace(tzinfo=None) >= cutoff else -5)

            def dst(self, value):
                return self.utcoffset(value) - timedelta(hours=-5)

            def fromutc(self, value):
                utc = value.replace(tzinfo=None)
                hours = -4 if utc >= datetime(2024, 3, 10, 7) else -5
                return (utc + timedelta(hours=hours)).replace(tzinfo=self)

        tz = SpringForward()
        start = datetime(2024, 3, 9, tzinfo=tz)
        end = datetime(2024, 3, 12, tzinfo=tz)
        rows = [play(datetime(2024, 3, day, 12, tzinfo=tz)) for day in (9, 10, 11)]
        report = self.report(rows, start=ms(start), end=ms(end), now=ms(end), tz=tz)
        self.assertEqual(self.fingerprints(report)["一致性"], 100)
        self.assertEqual(self.facts(report)["平均每日"], 1)
        self.assertEqual(self.facts(report)["最长连续有记录天数"], 3)
        self.assertEqual(len(report["genre_stream"]["days"]), 3)

    def test_nonfinite_and_boolean_durations_do_not_break_json(self):
        rows = [play(self.start, duration_ms=value) for value in (float("nan"), float("inf"), True)]
        report = self.report(rows)
        self.assertEqual(self.facts(report)["估算时长"], "0 秒")
        json.dumps(report, allow_nan=False)

    def test_invalid_period_is_rejected(self):
        with self.assertRaises(ValueError):
            self.report(end=self.start)
        with self.assertRaises(ValueError):
            self.report(end=self.start - timedelta(days=1))


if __name__ == "__main__":
    unittest.main()
