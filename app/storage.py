"""Private, per-account listening archive. All calendar boundaries use the configured timezone."""
import calendar
import csv
import hashlib
import io
import json
import sqlite3
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from .timezones import get_timezone
from .report_extras import report_extras


class Archive:
    def __init__(self, directory, timezone="Asia/Shanghai"):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "journal.sqlite3"
        self.tz = get_timezone(timezone)
        with self.connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS tracks (
                    user_id TEXT NOT NULL, guid TEXT NOT NULL, title TEXT, artist TEXT,
                    album TEXT, genre TEXT, duration_ms INTEGER, cover_id TEXT,
                    release_year INTEGER, updated_at INTEGER NOT NULL,
                    PRIMARY KEY(user_id, guid));
                CREATE TABLE IF NOT EXISTS plays (
                    id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, dedupe_key TEXT NOT NULL,
                    guid TEXT NOT NULL, played_at INTEGER NOT NULL, title TEXT, artist TEXT,
                    album TEXT, genre TEXT, duration_ms INTEGER, cover_id TEXT,
                    source TEXT NOT NULL, confidence TEXT NOT NULL, created_at INTEGER NOT NULL,
                    UNIQUE(user_id, dedupe_key));
                CREATE INDEX IF NOT EXISTS ix_plays_time ON plays(user_id, played_at);
                CREATE TABLE IF NOT EXISTS state (
                    user_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT,
                    PRIMARY KEY(user_id, key));
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, account TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS artist_images (
                    user_id TEXT NOT NULL, artist TEXT NOT NULL, cover_id TEXT,
                    updated_at INTEGER NOT NULL, PRIMARY KEY(user_id, artist));
            """)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=20000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def set_state(self, user, key, value):
        with self.connection() as conn:
            conn.execute("INSERT INTO state VALUES(?,?,?) ON CONFLICT(user_id,key) DO UPDATE SET value=excluded.value",
                         (user, key, json.dumps(value, ensure_ascii=False)))

    def get_state(self, user, key, default=None):
        with self.connection() as conn:
            row = conn.execute("SELECT value FROM state WHERE user_id=? AND key=?", (user, key)).fetchone()
        return json.loads(row[0]) if row else default

    def save_session(self, token, account, expires):
        """Persist a tool session by token hash so a restart keeps it alive."""
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self.connection() as conn:
            conn.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?)", (digest, account, float(expires)))

    def load_session(self, token):
        """Return (account, expires) for a live session, or None."""
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self.connection() as conn:
            row = conn.execute("SELECT account, expires FROM sessions WHERE token_hash=?", (digest,)).fetchone()
        if not row:
            return None
        return row["account"], float(row["expires"])

    def drop_session(self, token):
        if not token:
            return
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self.connection() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (digest,))

    def prune_sessions(self, now=None):
        moment = time.time() if now is None else float(now)
        with self.connection() as conn:
            conn.execute("DELETE FROM sessions WHERE expires<=?", (moment,))

    def upsert_track(self, user, meta):
        guid = meta.get("guid")
        if not guid:
            return
        fields = ("title", "artist", "album", "genre", "duration_ms", "cover_id", "release_year")
        values = [meta.get(key) for key in fields]
        with self.connection() as conn:
            updates = ",".join(f"{key}=COALESCE(excluded.{key},tracks.{key})" for key in fields)
            conn.execute(f"INSERT INTO tracks VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,guid) DO UPDATE SET {updates},updated_at=excluded.updated_at",
                         (user, guid, *values, int(time.time() * 1000)))
            conn.execute("""UPDATE plays SET title=COALESCE(?,title), artist=COALESCE(?,artist),
                album=COALESCE(?,album),genre=COALESCE(?,genre),duration_ms=COALESCE(?,duration_ms),
                cover_id=COALESCE(?,cover_id) WHERE user_id=? AND guid=?""", (*values[:6], user, guid))

    def track(self, user, guid):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM tracks WHERE user_id=? AND guid=?", (user, guid)).fetchone()
        return dict(row) if row else {}

    def upsert_artist(self, user, name, cover_id):
        """Remember an artist's fnMusic image id; empty covers never erase one."""
        name = (name or "").strip()
        if not name:
            return
        cover = str(cover_id) if cover_id else ""
        with self.connection() as conn:
            conn.execute("""INSERT INTO artist_images VALUES(?,?,?,?)
                ON CONFLICT(user_id,artist) DO UPDATE SET
                cover_id=COALESCE(NULLIF(excluded.cover_id,''),artist_images.cover_id), updated_at=excluded.updated_at""",
                (user, name, cover, int(time.time() * 1000)))

    def artist_covers(self, user):
        with self.connection() as conn:
            return {row["artist"]: row["cover_id"] for row in
                    conn.execute("SELECT artist, cover_id FROM artist_images WHERE user_id=? AND cover_id<>''", (user,))}

    def _record(self, user, guid, stamp, source, confidence):
        key = f"play:{guid}:{stamp}"
        meta = self.track(user, guid)
        with self.connection() as conn:
            cur = conn.execute("""INSERT OR IGNORE INTO plays
                (user_id,dedupe_key,guid,played_at,title,artist,album,genre,duration_ms,cover_id,source,confidence,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (user, key, guid, stamp, meta.get("title"), meta.get("artist"), meta.get("album"),
                 meta.get("genre"), meta.get("duration_ms"), meta.get("cover_id"), source, confidence, int(time.time() * 1000)))
        return "accepted" if cur.rowcount else "deduped"

    def insert_event(self, user, event, source="fnmusic_event"):
        if not isinstance(event, dict) or event.get("eventType") != "track_play":
            return "skipped"
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return "skipped"
        guid = payload.get("trackGUID")
        stamp = event.get("occurredAt")
        if not isinstance(guid, str) or not guid or len(guid) > 256 or isinstance(stamp, bool):
            return "skipped"
        try:
            stamp = int(stamp)
            if stamp < 100_000_000_000:
                stamp *= 1000
        except (TypeError, ValueError, OverflowError):
            return "skipped"
        # Reject bogus dates without silently substituting the time of ingestion.
        if not 946684800000 <= stamp <= int(time.time() * 1000) + 300000:
            return "skipped"
        return self._record(user, guid, stamp, source, "exact")

    def insert_observed(self, user, guid, observed_at, source="history_poll"):
        """Record a play detected by polling; observed_at is when it was first seen.

        Polling cannot know the real playback start, so the record is marked
        confidence="observed" and must never be presented as exact timing.
        """
        return self._record(user, guid, int(observed_at), source, "observed")

    def has_exact_play(self, user, guid, stamp, window_ms):
        """Whether an exactly-timed play of this track was recorded near ``stamp``.

        The poller uses this to avoid double-counting a play that the gateway
        or browser bridge already captured with a real event time.
        """
        with self.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM plays WHERE user_id=? AND guid=? AND confidence='exact' AND played_at BETWEEN ? AND ?",
                (user, guid, stamp - int(window_ms), stamp + 60000)).fetchone()
        return bool(row[0])

    def ingest(self, user, body, source="fnmusic_event"):
        if isinstance(body, list):
            events = body
        elif isinstance(body, dict):
            events = body.get("events", [body] if "eventType" in body else [])
        else:
            events = []
        if not isinstance(events, list) or len(events) > 1000:
            raise ValueError("一次最多接收 1000 条事件")
        results = Counter(self.insert_event(user, event, source) for event in events)
        return {"ok": True, **{key: results[key] for key in ("accepted", "deduped", "skipped")}}

    def export_all(self, user):
        """Complete per-account backup payload. Never includes sessions or tokens."""
        with self.connection() as conn:
            plays = [dict(row) for row in conn.execute(
                "SELECT * FROM plays WHERE user_id=? ORDER BY played_at, id", (user,))]
            tracks = [dict(row) for row in conn.execute(
                "SELECT * FROM tracks WHERE user_id=? ORDER BY guid", (user,))]
            artist_images = [dict(row) for row in conn.execute(
                "SELECT * FROM artist_images WHERE user_id=? ORDER BY artist", (user,))]
        for row in plays + tracks + artist_images:
            row.pop("user_id", None)
        return {"format": "yinji-backup", "version": 1,
                "exported_at": int(time.time() * 1000),
                "plays": plays, "tracks": tracks, "artist_images": artist_images}

    @staticmethod
    def _clean_backup_text(value, limit=300):
        return value.strip()[:limit] if isinstance(value, str) and value.strip() else None

    def _validate_play_row(self, row):
        if not isinstance(row, dict):
            return None
        guid = row.get("guid")
        stamp = row.get("played_at")
        if not isinstance(guid, str) or not guid.strip() or len(guid) > 256 or isinstance(stamp, bool):
            return None
        try:
            stamp = int(stamp)
        except (TypeError, ValueError):
            return None
        if not 946684800000 <= stamp <= int(time.time() * 1000) + 300000:
            return None
        confidence = row.get("confidence")
        confidence = confidence if confidence in ("exact", "observed") else "observed"
        source = row.get("source")
        source = source.strip()[:40] if isinstance(source, str) and source.strip() else "import"
        duration = row.get("duration_ms")
        if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
            duration = 0
        return {"guid": guid.strip(), "played_at": stamp, "source": source, "confidence": confidence,
                "title": self._clean_backup_text(row.get("title")), "artist": self._clean_backup_text(row.get("artist")),
                "album": self._clean_backup_text(row.get("album")), "genre": self._clean_backup_text(row.get("genre"), 200),
                "duration_ms": duration, "cover_id": self._clean_backup_text(row.get("cover_id"), 128) or ""}

    def restore_all(self, user, data, mode="merge"):
        """Import a backup payload. mode="restore" replaces this account's data,
        mode="merge" keeps existing rows and dedupes by guid+played_at."""
        if not isinstance(data, dict) or data.get("format") != "yinji-backup":
            raise ValueError("不是有效的音迹备份文件")
        if mode not in ("restore", "merge"):
            raise ValueError("无效的导入模式")
        plays, tracks, artists = data.get("plays"), data.get("tracks"), data.get("artist_images")
        if not isinstance(plays, list) or not isinstance(tracks, list) or not isinstance(artists, list):
            raise ValueError("备份文件数据不完整")
        if len(plays) > 500000 or len(tracks) > 200000 or len(artists) > 200000:
            raise ValueError("备份文件过大")
        clean_plays, skipped = [], 0
        seen_keys = set()
        for row in plays:
            clean = self._validate_play_row(row)
            if clean is None:
                skipped += 1
                continue
            key = f"play:{clean['guid']}:{clean['played_at']}"
            if key in seen_keys:
                skipped += 1
                continue
            seen_keys.add(key)
            clean_plays.append((key, clean))
        clean_tracks = [t for t in tracks if isinstance(t, dict)
                        and isinstance(t.get("guid"), str) and t.get("guid").strip()
                        and len(t["guid"]) <= 256]
        clean_artists = [a for a in artists if isinstance(a, dict)
                         and isinstance(a.get("artist"), str) and a.get("artist").strip()]

        now = int(time.time() * 1000)
        counts = {"plays": 0, "tracks": 0, "artists": 0, "skipped": skipped}
        track_sql = ("""INSERT INTO tracks VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id,guid) DO UPDATE SET """
                     + ",".join(f"{key}=COALESCE(excluded.{key},tracks.{key})" for key in
                                ("title", "artist", "album", "genre", "duration_ms", "cover_id", "release_year"))
                     + ",updated_at=excluded.updated_at")
        artist_sql = """INSERT INTO artist_images VALUES(?,?,?,?)
            ON CONFLICT(user_id,artist) DO UPDATE SET
            cover_id=COALESCE(NULLIF(excluded.cover_id,''),artist_images.cover_id), updated_at=excluded.updated_at"""
        with self.connection() as conn:
            if mode == "restore":
                for table in ("plays", "tracks", "artist_images"):
                    conn.execute(f"DELETE FROM {table} WHERE user_id=?", (user,))
            for t in clean_tracks:
                guid = t["guid"].strip()
                duration = t.get("duration_ms")
                duration = duration if isinstance(duration, int) and not isinstance(duration, bool) and duration >= 0 else None
                conn.execute(track_sql, (user, guid, self._clean_backup_text(t.get("title")),
                                         self._clean_backup_text(t.get("artist")), self._clean_backup_text(t.get("album")),
                                         self._clean_backup_text(t.get("genre"), 200), duration,
                                         self._clean_backup_text(t.get("cover_id"), 128), None, now))
                counts["tracks"] += 1
            for a in clean_artists:
                conn.execute(artist_sql, (user, a["artist"].strip(),
                                          self._clean_backup_text(a.get("cover_id"), 128), now))
                counts["artists"] += 1
            for key, play in clean_plays:
                cur = conn.execute("""INSERT OR IGNORE INTO plays
                    (user_id,dedupe_key,guid,played_at,title,artist,album,genre,duration_ms,cover_id,source,confidence,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (user, key, play["guid"], play["played_at"], play["title"], play["artist"], play["album"],
                     play["genre"], play["duration_ms"], play["cover_id"], play["source"], play["confidence"], now))
                if cur.rowcount:
                    counts["plays"] += 1
        return counts

    def bounds(self, period, offset=0, now=None):
        if period not in ("week", "month", "year"):
            raise ValueError("period 必须是 week、month 或 year")
        if not 0 <= offset <= 121:
            raise ValueError("offset 必须介于 0 与 121")
        now = now or datetime.now(self.tz)
        base = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if period == "week":
            start = base - timedelta(days=base.weekday(), weeks=offset)
            end = start + timedelta(days=7)
            label = f"{start:%Y.%m.%d} — {end - timedelta(days=1):%m.%d}"
        elif period == "month":
            n = base.year * 12 + base.month - 1 - offset
            year, month = divmod(n, 12)
            start = base.replace(year=year, month=month + 1, day=1)
            end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
            label = f"{start.year} 年 {start.month} 月"
        else:
            start = base.replace(year=base.year - offset, month=1, day=1)
            end = start.replace(year=start.year + 1)
            label = f"{start.year} 年"
        return start, end, label

    def rows(self, user, start=None, end=None, q=""):
        terms, args = ["user_id=?"], [user]
        if start is not None:
            terms.append("played_at>=?")
            args.append(start)
        if end is not None:
            terms.append("played_at<?")
            args.append(end)
        if q:
            terms.append("(COALESCE(title,'') LIKE ? ESCAPE '\\' OR COALESCE(artist,'') LIKE ? ESCAPE '\\' OR COALESCE(album,'') LIKE ? ESCAPE '\\')")
            pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            args.extend([pattern] * 3)
        with self.connection() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM plays WHERE " + " AND ".join(terms) + " ORDER BY played_at DESC,id DESC", args)]

    def records(self, user, page=1, size=25, q="", start=None, end=None):
        if not 1 <= page <= 100000 or not 1 <= size <= 100:
            raise ValueError("无效分页参数")
        rows = self.rows(user, start, end, q)
        return {"items": rows[(page - 1) * size:page * size], "total": len(rows), "page": page, "size": size}

    @staticmethod
    def totals(rows):
        return {"plays": len(rows), "estimated_duration_ms": sum(max(0, r["duration_ms"] or 0) for r in rows),
                "artists": len({r["artist"] for r in rows if r["artist"]}),
                "albums": len({(r["artist"], r["album"]) for r in rows if r["album"]}),
                "tracks": len({r["guid"] for r in rows}),
                "exact": sum(r["confidence"] == "exact" for r in rows),
                "estimated": sum(r["confidence"] != "exact" for r in rows)}

    def top(self, rows, dimension):
        groups = {}
        for row in rows:
            name = row[dimension] if dimension != "title" else row["title"] or "待同步曲目"
            if not name:
                continue
            key = row["guid"] if dimension == "title" else (row["artist"], name) if dimension == "album" else name
            if key not in groups:
                groups[key] = {"name": name, "artist": row["artist"], "guid": row["guid"], "cover_id": row["cover_id"], "plays": 0}
            groups[key]["plays"] += 1
        return sorted(groups.values(), key=lambda r: (-r["plays"], r["name"]))[:10]

    def report(self, user, period="week", offset=0, now=None):
        if not 0 <= offset <= 120:
            raise ValueError("offset 必须介于 0 与 120")
        now = now or datetime.now(self.tz)
        start, end, label = self.bounds(period, offset, now)
        before_start, before_end, _ = self.bounds(period, offset + 1, now)
        ms = lambda d: int(d.timestamp() * 1000)
        rows = self.rows(user, ms(start), ms(end))
        prev = self.rows(user, ms(before_start), ms(before_end))
        totals, previous = self.totals(rows), self.totals(prev)
        days, previous_days = Counter(), Counter()
        hours = [0] * 24
        for row in rows:
            date = datetime.fromtimestamp(row["played_at"] / 1000, self.tz)
            days[date.strftime("%Y-%m-%d")] += 1
            hours[date.hour] += 1
        for row in prev:
            date = datetime.fromtimestamp(row["played_at"] / 1000, self.tz)
            previous_days[(date - before_start).days] += 1
        totals["active_days"] = len(days)
        cal = [{"date": (start + timedelta(days=i)).strftime("%Y-%m-%d"), "plays": days[(start + timedelta(days=i)).strftime("%Y-%m-%d")]} for i in range((end - start).days)]
        if period == "year":
            timeline = []
            for month in range(1, 13):
                current_plays = sum(value for date, value in days.items() if int(date[5:7]) == month)
                prev_plays = sum(1 for r in prev if datetime.fromtimestamp(r["played_at"] / 1000, self.tz).month == month)
                timeline.append({"date": f"{start.year}-{month:02d}-01", "label": f"{month}月", "plays": current_plays, "previous": prev_plays})
        else:
            timeline = [{**day, "label": datetime.fromisoformat(day["date"]).strftime("%m/%d"), "previous": previous_days[i]} for i, day in enumerate(cal)]
        with self.connection() as conn:
            firsts = [dict(row) for row in conn.execute("SELECT guid,artist,MIN(played_at) AS first_at FROM plays WHERE user_id=? GROUP BY guid,artist", (user,))]
        first_artists = {}
        for item in firsts:
            if item["artist"]:
                first_artists[item["artist"]] = min(first_artists.get(item["artist"], item["first_at"]), item["first_at"])
        new_artists = {name for name, first in first_artists.items() if ms(start) <= first < ms(end)}
        tops = {"artists": self.top(rows, "artist"), "albums": self.top(rows, "album"), "tracks": self.top(rows, "title")}
        artist_images = self.artist_covers(user)
        for entry in tops["artists"]:
            if artist_images.get(entry["name"]):
                entry["cover_id"] = artist_images[entry["name"]]
        new_top = next((item for item in tops["artists"] if item["name"] in new_artists), None)
        discovery = {"artists": len(new_artists), "tracks": sum(ms(start) <= item["first_at"] < ms(end) for item in firsts), "top_artist": new_top}
        notice = "记录的是飞牛真实播放事件：代理与浏览器采集带精确时间，轮询探测按首次发现时间估算。时长按曲目长度估算，不代表实际听完时长。未经过音迹播放器的播放可能缺失；无时间戳的旧历史不计入报告。"
        insights = []
        if rows:
            peak_day = max(cal, key=lambda r: r["plays"])
            peak_hour = max(range(24), key=lambda h: hours[h])
            first = min(rows, key=lambda r: r["played_at"])
            insights = [{"title": "最常听歌的时段", "value": f"{peak_hour:02d}:00", "detail": f"这个时段共播放 {hours[peak_hour]} 次"},
                        {"title": "音乐最满的一天", "value": peak_day["date"][5:].replace("-", "/"), "detail": f"这一天共播放 {peak_day['plays']} 次"},
                        {"title": "本期第一首", "value": first["title"] or "待同步曲目", "detail": first["artist"] or "未知歌手"}]
        with self.connection() as conn:
            track_map = {row["guid"]: dict(row) for row in conn.execute("SELECT * FROM tracks WHERE user_id=?", (user,))}
        extras = report_extras(rows, prev, self.rows(user), track_map, start, end, now, self.tz)
        discovery.update(extras.pop("discovery_extras"))
        if discovery.get("top_artist") and artist_images.get(discovery["top_artist"]["name"]):
            discovery["top_artist"]["cover_id"] = artist_images[discovery["top_artist"]["name"]]
        return {"period": period, "offset": offset, "label": label, "start": ms(start), "end": ms(end), "timezone": str(self.tz),
                "complete": end <= now, "totals": totals, "previous": previous,
                "change_percent": round((totals["plays"] - previous["plays"]) / previous["plays"] * 100, 1) if previous["plays"] else None,
                "timeline": timeline, "tops": tops, "discovery": discovery, "hourly": hours, "calendar": cal,
                "insights": insights, "coverage": {"exact": totals["exact"], "estimated": totals["estimated"], "notice": notice}, **extras}

    def status(self, user):
        with self.connection() as conn:
            count = conn.execute("SELECT COUNT(*) FROM tracks WHERE user_id=?", (user,)).fetchone()[0]
            row = conn.execute("""SELECT COALESCE(SUM(confidence='exact'),0) AS exact,
                COALESCE(SUM(confidence<>'exact'),0) AS observed,
                MIN(played_at) AS first_record FROM plays WHERE user_id=?""", (user,)).fetchone()
        return {"exact": row["exact"], "estimated": row["observed"], "library_tracks": count, "first_record": row["first_record"]}

    def export(self, user, q="", start=None, end=None):
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["播放时间", "时区", "曲目", "歌手", "专辑", "估算曲目时长(秒)", "记录来源", "时间准确性"])
        def cell(value):
            value = str(value or "")
            return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value
        for row in self.rows(user, start, end, q):
            writer.writerow([datetime.fromtimestamp(row["played_at"] / 1000, self.tz).isoformat(), str(self.tz),
                             cell(row["title"]), cell(row["artist"]), cell(row["album"]),
                             (row["duration_ms"] or 0) / 1000, row["source"], row["confidence"]])
        return output.getvalue().encode("utf-8-sig")
