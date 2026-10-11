"""Personal listening-report additions, computed only from observed plays.

All numeric timestamps are milliseconds. Boundaries may also be datetime objects
for Archive.report callers; naive datetimes are interpreted in the supplied tz.
The interval is [start, end), as observed at now (future plays are excluded).
Ratios are distinct named artists, (artist, album) pairs, and GUIDs respectively,
divided by the number of period plays, multiplied by 100 (not shares of a pie).
Unknown artists/albums are not manufactured. Values are rounded to two decimals.
Discovery means first observation in this account, not a release or global debut.
"""
from collections import Counter
from datetime import datetime, timedelta
from math import isfinite


def _local(value, tz):
    if isinstance(value, datetime):
        return value.replace(tzinfo=tz) if value.tzinfo is None else value.astimezone(tz)
    return datetime.fromtimestamp(value / 1000, tz)


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _artist(row):
    return _text(row.get("artist"))


def _album(row):
    name = _text(row.get("album"))
    return (_artist(row), name) if name else None


def _track(row):
    return _text(row.get("guid")) or None


def _percentage(numerator, denominator):
    return round(100 * numerator / denominator, 2) if denominator else 0.0


def _genres(row, track_map):
    raw = _text(row.get("genre"))
    if not raw:
        raw = _text(track_map.get(row.get("guid"), {}).get("genre"))
    # Multiple real tags may count a play in more than one series, but duplicate
    # tags on the same play count only once. No synthetic "unknown" genre.
    return {_text(name) for name in raw.split(";") if _text(name)}


def _release_year(row, track_map):
    value = track_map.get(row.get("guid"), {}).get("release_year")
    if isinstance(value, str):
        value = value.strip()
        if len(value) != 4 or not value.isascii() or not value.isdigit():
            return None
        value = int(value)
    if isinstance(value, int) and not isinstance(value, bool) and 1000 <= value <= 9999:
        return value
    return None


def _duration(row):
    value = row.get("duration_ms")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value):
        return max(0, int(value))
    return 0


def _human_duration(milliseconds):
    seconds = milliseconds // 1000
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分钟"
    if minutes:
        return f"{minutes} 分钟 {seconds} 秒"
    return f"{seconds} 秒"


def _new_top(rows, key_function, firsts, start_ms, end_ms):
    counts, examples = Counter(), {}
    for row in rows:
        key = key_function(row)
        if key is not None and start_ms <= firsts.get(key, end_ms) < end_ms:
            counts[key] += 1
            # Choose a deterministic representative regardless of input order.
            if key not in examples or row["played_at"] < examples[key]["played_at"]:
                examples[key] = row
    if not counts:
        return None
    key = min(counts, key=lambda item: (-counts[item], str(item)))
    row = examples[key]
    name = key[1] if isinstance(key, tuple) else _text(row.get("title")) or "待同步曲目"
    return {"name": name, "artist": row.get("artist"), "guid": row.get("guid"),
            "cover_id": row.get("cover_id"), "plays": counts[key]}


def report_extras(rows, prev, all_rows, track_map, start, end, now, tz) -> dict:
    """Return independent modules ready for report.update().

    weekday uses Python's Monday=0..Sunday=6. consistency and daily average use
    elapsed *local calendar days*, including today for an ongoing period, with
    the denominator capped at the period end. No global benchmarks are implied.
    genre_stream uses months for periods longer than 100 local calendar days,
    days otherwise; zero-filled axes cover the whole requested period.
    """
    start, end, now = (_local(value, tz) for value in (start, end, now))
    if end <= start:
        raise ValueError("end must be after start")
    start_ms, end_ms, now_ms = (int(value.timestamp() * 1000) for value in (start, end, now))
    rows = [row for row in rows if start_ms <= row["played_at"] < end_ms and row["played_at"] <= now_ms]
    prev = [row for row in prev if row["played_at"] < start_ms and row["played_at"] <= now_ms]
    track_map = track_map or {}
    # Support non-midnight bounds without losing their final observed day.
    last_date = (end - timedelta(microseconds=1)).date()
    period_days = (last_date - start.date()).days + 1
    elapsed_days = max(0, (min(now.date(), last_date) - start.date()).days + 1) if now_ms >= start_ms else 0

    days, weekdays, previous_weekdays = Counter(), Counter(), Counter()
    genres, genre_buckets, decades, artists = Counter(), {}, Counter(), Counter()
    albums, tracks = set(), set()
    monthly = period_days > 100
    for row in rows:
        date = _local(row["played_at"], tz)
        day = date.date().isoformat()
        bucket = date.strftime("%Y-%m-01") if monthly else day
        days[day] += 1
        weekdays[date.weekday()] += 1
        if _artist(row):
            artists[_artist(row)] += 1
        if _album(row) is not None:
            albums.add(_album(row))
        if _track(row) is not None:
            tracks.add(_track(row))
        for genre in _genres(row, track_map):
            genres[genre] += 1
            genre_buckets.setdefault(genre, Counter())[bucket] += 1
        year = _release_year(row, track_map)
        if year is not None:
            decades[year // 10 * 10] += 1
    for row in prev:
        previous_weekdays[_local(row["played_at"], tz).weekday()] += 1

    first_artists, first_albums, first_tracks = {}, {}, {}
    # Only first timestamps are taken from the full archive; rankings below
    # use rows exclusively. Including current rows makes partial callers safe.
    for collection in (all_rows, rows):
        for row in collection:
            stamp = row["played_at"]
            if stamp > now_ms:
                continue
            for key, firsts in ((_artist(row) or None, first_artists),
                                (_album(row), first_albums), (_track(row), first_tracks)):
                if key is not None:
                    firsts[key] = min(stamp, firsts.get(key, stamp))
    new_artists = {name for name in artists if start_ms <= first_artists[name] < end_ms}
    new_albums = {key for key in albums if start_ms <= first_albums[key] < end_ms}
    plays = len(rows)
    top_artist_plays = max(artists.values(), default=0)
    ratios = {"artists": _percentage(len(artists), plays),
              "albums": _percentage(len(albums), plays),
              "tracks": _percentage(len(tracks), plays)}
    fingerprint = [
        {"label": "一致性", "value": _percentage(len(days), elapsed_days),
         "detail": f"有记录的 {len(days)} 天 ÷ 本期已过 {elapsed_days} 个日历日 × 100；不代表完整收听覆盖"},
        {"label": "发现率", "value": _percentage(len(new_artists), len(artists)),
         "detail": f"本期首次观察的 {len(new_artists)} 位歌手 ÷ 本期 {len(artists)} 位歌手 × 100"},
        {"label": "集中度", "value": _percentage(top_artist_plays, plays),
         "detail": f"播放最多的歌手 {top_artist_plays} 次 ÷ 本期 {plays} 次播放 × 100"},
        {"label": "重放率", "value": _percentage(plays - len(tracks), plays),
         "detail": f"(1 − 本期 {len(tracks)} 个不同曲目 GUID ÷ {plays} 次播放) × 100"},
    ]

    streak = longest = 0
    previous_date = None
    for day in sorted(days):
        date = datetime.fromisoformat(day).date()
        streak = streak + 1 if previous_date is not None and date - previous_date == timedelta(days=1) else 1
        longest = max(longest, streak)
        previous_date = date
    peak = min(days, key=lambda day: (-days[day], day)) if days else None
    duration = sum(_duration(row) for row in rows)
    daily_average = round(plays / elapsed_days, 2) if elapsed_days else 0.0
    quick_facts = [
        {"title": "估算时长", "value": _human_duration(duration),
         "detail": "按已知曲目长度累加估算，不代表实际听完时长；缺失长度计 0"},
        {"title": "平均每日", "value": daily_average,
         "detail": f"{plays} 次播放 ÷ 本期已过 {elapsed_days} 个日历日（包括无记录日），单位：次/日"},
        {"title": "最长连续有记录天数", "value": longest,
         "detail": "本期连续出现播放记录的日历日数，不代表连续收听时长"},
        {"title": "最活跃日", "value": peak,
         "detail": f"这一天有 {days[peak]} 次播放记录；并列时取最早日期" if peak else "本期暂无播放记录"},
    ]
    first = min(rows, key=lambda row: (row["played_at"], str(row.get("guid", "")))) if rows else None
    first_play = ({key: first.get(key) for key in ("title", "artist", "played_at", "cover_id")}
                  if first is not None else None)

    axis = []
    date = start.date().replace(day=1) if monthly else start.date()
    while date <= last_date:
        axis.append(date.isoformat())
        if monthly:
            date = date.replace(year=date.year + 1, month=1) if date.month == 12 else date.replace(month=date.month + 1)
        else:
            date += timedelta(days=1)
    genre_names = sorted(genres, key=lambda name: (-genres[name], name))
    return {
        "weekday": [{"day": day, "plays": weekdays[day], "previous": previous_weekdays[day]} for day in range(7)],
        "genres": [{"name": name, "plays": genres[name]} for name in genre_names],
        "decades": [{"decade": decade, "plays": decades[decade]} for decade in sorted(decades)],
        "ratios": ratios,
        "fingerprint": fingerprint,
        "quick_facts": quick_facts,
        "first_play": first_play,
        "discovery_extras": {
            "albums": len(new_albums),
            "top_album": _new_top(rows, _album, first_albums, start_ms, end_ms),
            "top_track": _new_top(rows, _track, first_tracks, start_ms, end_ms),
        },
        "genre_stream": {"days": axis, "series": [
            {"name": name, "values": [genre_buckets[name][bucket] for bucket in axis]} for name in genre_names]},
    }
