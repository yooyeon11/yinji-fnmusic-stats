"""Playback detection by polling the account's play history.

Merged from the user's earlier fnOS scrobbler prototype. The history endpoint
returns entries newest-first and exposes no playback timestamp, so a play is
recorded when its entry is first observed and the record is marked
confidence="observed". The first poll only seeds the entries already present;
old history is never backfilled. Entries already captured through the gateway
or browser bridge around the same time are not duplicated.

Real-server semantics (verified against a live fnOS instance): the list keeps
one entry per track, a replay moves the existing entry towards the front
instead of adding one, and the server may trim or compact the list on its own.
So a poll whose list no longer aligns with the baseline must not be discarded
 wholesale. Two signals stay attributable on their own:
- a GUID that was absent from the baseline is a first-seen play;
- a single known GUID that advanced to the front while every other GUID kept
  its relative order is one replay.
Any other shape (several movers, mid-list reshuffles, removals at the front)
cannot be attributed and records nothing — but first-seen entries are still
kept, and the baseline is simply updated instead of being used to guess.
"""
from __future__ import annotations

import time

from .connector import parse_artists, parse_track

DEDUPE_WINDOW_MS = 15 * 60 * 1000
MAX_TRACKED_KEYS = 500
PAGE_SIZE = 100


def history_keys(items):
    """Ordered per-entry keys (newest first); empty string for unusable items."""
    keys = []
    for item in items:
        guid = item.get("guid") if isinstance(item, dict) else None
        keys.append(guid if isinstance(guid, str) and guid else "")
    return keys


def count_new(previous, current):
    """How many entries are new at the front of ``current``.

    Both lists are newest-first. The largest suffix of ``current`` that equals
    a prefix of ``previous`` is the shared overlap; everything before it is
    new, which keeps consecutive repeats of the same track countable. When
    nothing aligns (history rotated or was trimmed) the caller must not guess
    positionally: ``aligned`` is False and the caller falls back to signals
    that stay meaningful without alignment.
    """
    if not previous:
        return len(current), True
    for overlap in range(min(len(previous), len(current)), 0, -1):
        if current[len(current) - overlap:] == previous[:overlap]:
            return len(current) - overlap, True
    return 0, False


def dedupe_order(keys):
    """Usable GUIDs in order, first occurrence wins, unusable entries dropped."""
    order = []
    seen = set()
    for key in keys:
        if key and key not in seen:
            seen.add(key)
            order.append(key)
    return order


def single_promotion(previous, current):
    """The GUID that advanced to the front as one replay, or None.

    ``previous`` and ``current`` are deduplicated newest-first orders of the
    same account's history. A replay promotes exactly one existing GUID to
    the front while the others keep their relative order; the server may trim
    the tail in the same step, so the remainder only has to match a truncated
    prefix. A GUID that was already at the front changed nothing observable,
    and shapes like swaps or front removals cannot be attributed.
    """
    if not previous or not current or previous == current:
        return None
    guid = current[0]
    if guid not in previous or previous[0] == guid:
        return None
    rest = current[1:]
    remainder = [key for key in previous if key != guid]
    if len(remainder) < len(rest):
        return None
    return guid if remainder[:len(rest)] == rest else None


def detect(archive, account, client, now_ms=None):
    """Run one detection pass and return a summary for logging and tests."""
    user = account["user_id"]
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    items = client.recent(PAGE_SIZE)
    keys = history_keys(items)
    previous = archive.get_state(user, "poll_keys")
    if not isinstance(previous, list):
        # First observation establishes the baseline; existing history is
        # never converted into playback records.
        archive.set_state(user, "poll_keys", keys[:MAX_TRACKED_KEYS])
        archive.set_state(user, "poll_seeded_at", now)
        return {"seeded": len(keys), "recorded": 0, "deduped": 0, "skipped": 0}
    result = {"seeded": 0, "recorded": 0, "deduped": 0, "skipped": 0}
    count, aligned = count_new(previous, keys)
    if aligned:
        selected = list(items[:count])
    else:
        result["unaligned"] = True
        prev_order = dedupe_order(previous)
        if not prev_order:
            # The whole baseline was unusable; re-seed without attributing.
            selected = []
        else:
            order, item_of = [], {}
            for key, item in zip(keys, items):
                if key and key not in item_of:
                    order.append(key)
                    item_of[key] = item
            newcomers = [key for key in order if key not in set(prev_order)]
            fresh = set(newcomers)
            promoted = single_promotion(prev_order, [key for key in order if key not in fresh])
            picked = newcomers + ([promoted] if promoted else [])
            selected = [item_of[key] for key in picked]
    archive.set_state(user, "poll_keys", keys[:MAX_TRACKED_KEYS])
    for item in reversed(selected):
        guid = item.get("guid") if isinstance(item, dict) else None
        if not isinstance(guid, str) or not guid:
            result["skipped"] += 1
            continue
        if archive.has_exact_play(user, guid, now, DEDUPE_WINDOW_MS):
            result["deduped"] += 1
            continue
        archive.upsert_track(user, parse_track(item))
        for artist in parse_artists(item):
            archive.upsert_artist(user, artist["name"], artist["cover_id"])
        if archive.insert_observed(user, guid, now) == "accepted":
            result["recorded"] += 1
        else:
            result["deduped"] += 1
    return result
