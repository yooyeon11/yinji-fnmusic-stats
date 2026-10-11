"""Inject deterministic synthetic plays into the LOCAL journal database.

Purpose: full-feature UI/report testing with realistic data. Track metadata
(titles, artists, albums, covers, genres) comes from the REAL fnMusic account,
so every report renders with genuine library data; only the play events are
fabricated, in this machine's local test database (runtime/ by default).

This script never contacts the NAS for writes, never creates play history on
the music server, and never touches the NAS-installed instance's database.
Synthetic rows are marked source='synthetic_poll' / 'synthetic_gateway'
(confidence observed/exact) so the UI renders normal badges and cleanup is a
single statement.

Usage (from the project root):
    python scripts/inject_test_data.py             # inject a year of plays
    python scripts/inject_test_data.py --clean     # remove synthetic rows
    python scripts/inject_test_data.py --days 120  # shorter window

Generation is seeded, so re-running with the same window adds nothing new
(dedupe keys repeat); the "today" portion moves with the clock by design.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.connector import MusicClient, parse_track  # noqa: E402
from app.storage import Archive  # noqa: E402

SEED = 20261011
SOURCES = [("synthetic_gateway", "exact"), ("synthetic_poll", "observed")]
# every Nth play mimics a proxy-captured event with an exact timestamp
EXACT_EVERY = 5


def load_connection(data_dir: Path):
    path = data_dir / "connection.json"
    if not path.exists():
        sys.exit(f"No connection saved in {data_dir}; start the app and connect once first.")
    connection = json.loads(path.read_text(encoding="utf-8"))
    user = connection.get("user") or {}
    uid = str(user.get("guid") or user.get("id") or user.get("username") or user.get("name") or "")
    if not uid:
        sys.exit("Saved connection has no usable account identity.")
    return connection, uid


def account_id(archive: Archive, connection, uid: str) -> str:
    """The account key the running service computes for this connection."""
    import hashlib
    key = hashlib.sha256((connection["server"] + "|" + uid).encode()).hexdigest()
    with archive.connection() as conn:
        rows = [r["user_id"] for r in conn.execute("SELECT DISTINCT user_id FROM state")]
    if key in rows:
        return key
    if len(rows) == 1:
        return rows[0]
    sys.exit(f"Could not match account key; state has {len(rows)} account(s).")


def fetch_real_tracks(connection) -> list[dict]:
    client = MusicClient(connection["server"], connection["token"])
    try:
        items = client.history()
    except Exception as exc:  # MusicError / network
        sys.exit(f"Could not read real play history from {connection['server']}: {type(exc).__name__}")
    metas = []
    seen = set()
    for item in items:
        meta = parse_track(item)
        if meta["guid"] and meta["guid"] not in seen:
            seen.add(meta["guid"])
            metas.append(meta)
    if not metas:
        sys.exit("Real history is empty; nothing to base synthetic plays on.")
    return metas


def plan_days(days: int, rng: random.Random) -> list[int]:
    """Plays per day over the past ``days`` days (oldest first), never future."""
    plan = []
    for offset in range(days - 1, -1, -1):
        date = datetime.now() - timedelta(days=offset)
        weekend = date.weekday() >= 5
        base = rng.choices([0, 1, 2, 3], weights=[30, 30, 25, 15])[0]
        if weekend:
            base = rng.choices([0, 1, 2, 3, 4], weights=[25, 25, 20, 20, 10])[0]
        if rng.random() < 0.07:  # occasional binge day
            base += rng.randint(6, 12)
        plan.append(base)
    # keep the current and previous week lively for week-report comparisons
    for i in range(max(0, len(plan) - 7), len(plan)):
        plan[i] = max(plan[i], rng.randint(1, 3))
    for i in range(max(0, len(plan) - 14), len(plan) - 7):
        plan[i] = max(plan[i], rng.randint(1, 2))
    return plan


HOURS = [20, 21, 22, 19, 23, 12, 13, 18, 10, 0, 1]
HOUR_WEIGHTS = [22, 20, 16, 14, 8, 6, 5, 5, 2, 1, 1]


def plan_track_for_day(day_index, total_days, track_count, rng: random.Random, debuts: list[int]):
    """Weighted pick among tracks whose debut has happened; newest favoured."""
    candidates = [i for i in range(track_count) if debuts[i] <= day_index]
    if not candidates:
        return None
    weights = []
    for i in candidates:
        age = day_index - debuts[i]
        weight = 1 + max(0, 6 - age // 14)  # recent discoveries listened more
        weights.append(weight * (1 if rng.random() < 0.85 else 2))
    return rng.choices(candidates, weights=weights)[0]


def main():
    parser = argparse.ArgumentParser(description="Inject/clean synthetic plays in the LOCAL journal database.")
    parser.add_argument("--data-dir", default=str(ROOT / "runtime"))
    parser.add_argument("--days", type=int, default=371, help="how many days back to spread plays (default 371)")
    parser.add_argument("--clean", action="store_true", help="remove all synthetic rows")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    archive = Archive(data_dir)
    with archive.connection() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM plays WHERE source LIKE 'synthetic%'").fetchone()
        existing = row["n"]
    if args.clean:
        with archive.connection() as conn:
            cur = conn.execute("DELETE FROM plays WHERE source LIKE 'synthetic%'")
        print(f"Removed {cur.rowcount} synthetic row(s); real records untouched.")
        return
    if existing:
        print(f"Synthetic rows already present: {existing}. Re-running is idempotent for the same day; use --clean first for a fresh pattern.")

    connection, uid = load_connection(data_dir)
    user = account_id(archive, connection, uid)
    tracks = fetch_real_tracks(connection)
    for meta in tracks:
        archive.upsert_track(user, meta)

    rng = random.Random(SEED)
    days = max(14, min(int(args.days), 371 * 2))
    total_days = days
    debuts = sorted(rng.sample(range(0, max(1, total_days - 30)), min(len(tracks), max(1, total_days - 30))))
    debuts += [0] * (len(tracks) - len(debuts))  # more tracks than debut slots: debut immediately
    plan = plan_days(days, rng)

    now = datetime.now(archive.tz)
    now_ms = int(now.timestamp() * 1000)
    injected = {"synthetic_gateway": 0, "synthetic_poll": 0}
    index = 0
    for day_offset, (count_plays, date) in enumerate(zip(plan, (now - timedelta(days=days - 1 - i) for i in range(days)))):
        is_today = date.date() == now.date()
        max_hour = now.hour if is_today else 23
        for _ in range(count_plays):
            pick = plan_track_for_day(day_offset, total_days, len(tracks), rng, debuts)
            if pick is None:
                continue
            hour = min(rng.choices(HOURS, weights=HOUR_WEIGHTS)[0], max(max_hour, 0))
            minute = rng.randrange(0, 60)
            stamp = int(datetime(date.year, date.month, date.day, hour, minute, tzinfo=archive.tz).timestamp() * 1000)
            stamp = min(stamp, now_ms - 60_000)  # never fabricate a future timestamp
            meta = tracks[pick]
            source, confidence = SOURCES[0] if index % EXACT_EVERY == 0 else SOURCES[1]
            archive._record(user, meta["guid"], stamp, source, confidence)
            injected[source] += 1
            index += 1

    with archive.connection() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM plays WHERE user_id=?", (user,)).fetchone()["n"]
        first = conn.execute("SELECT MIN(played_at) AS t FROM plays WHERE user_id=?", (user,)).fetchone()["t"]
    print(f"Injected {sum(injected.values())} synthetic play(s) {injected} for account …{user[-8:]}.")
    print(f"Local records now: {total}; earliest {datetime.fromtimestamp(first / 1000, archive.tz):%Y-%m-%d}.")
    print("Clean up later with: python scripts/inject_test_data.py --clean")


if __name__ == "__main__":
    main()
