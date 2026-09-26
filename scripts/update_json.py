#!/usr/bin/env python3
"""
Auto-updater: fetches Bingstream + SonyLiv live/upcoming match data and
merges them into a SINGLE output JSON file that follows Bingstream's schema.

How it works
------------
1. BINGSTREAM_URL is treated as the "source of truth" for structure. Its
   JSON shape (playlist_info / matches[...] with league/team logos and a
   `link_live` stream-link array) is never changed.
2. SONYLIV_URL is fetched and every entry in its "live_matches" list is
   converted (reshaped) into a Bingstream-style "matches" entry — SonyLiv
   only gives us a single program title + thumbnail (no home/away teams),
   so those entries get empty localteam/visitorteam fields and their
   thumbnail is carried over in the extra "thumbnail" field instead.
3. Both match lists are merged into one list.
4. The merged list is sorted with in-play matches first, then upcoming
   ("NS"/"UPCOMING") matches, then finished ones — soonest/earliest
   `start_at` first within each group.
5. playlist_info.statistics is recalculated for the merged result and the
   whole thing is written to OUTPUT_FILE, but ONLY if the content actually
   changed (byte-for-byte compare), to avoid pointless commits.

NOTE: Tapmad is no longer a source. It used to define the output schema
(EntityId/VideoName/ThumbnailStandard/...); that schema had no room for
per-team logos, so it's been dropped in favour of Bingstream's own schema,
which already carries `localteam_logo` / `visitorteam_logo`. To give every
entry (including SonyLiv-derived ones, which have no team logos) a usable
picture, an extra "thumbnail" field is added to every match: for Bingstream
matches it falls back to localteam_logo -> visitorteam_logo -> league_logo,
and for SonyLiv matches it's SonyLiv's own thumbnail. A "source" field
("bingstream" / "sonyliv") and a short auto-generated "description" are
also added — neither breaks anything expecting the plain Bingstream shape,
since they're additive fields.

Design goals (kept from the previous version of this script):
- Never crash the whole run if ONE source fails — fall back to whatever
  the other source produced, and only give up completely if Bingstream
  (the structural source) fails.
- Retry each request with backoff on network errors.
- Byte-for-byte change detection (no pointless commits).
- Zero third-party dependencies (Python stdlib only -> fast, no pip step).
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# --- Sources --------------------------------------------------------------
BINGSTREAM_URL = (
    "https://raw.githubusercontent.com/srhady/bingstream/"
    "refs/heads/main/playlist.json"
)
SONYLIV_URL = (
    "https://raw.githubusercontent.com/srhady/SonyLiv/"
    "refs/heads/main/sonyliv_playlist.json"
)

# --- Output -----------------------------------------------------------------
# Single merged file, in Bingstream's schema. Rename here if you'd rather
# call it something else — nothing else needs to change.
OUTPUT_FILE = "data/merged_playlist.json"
STATUS_FILE = "data/status.json"

TIMEOUT = 15          # seconds per request
MAX_RETRIES = 4        # attempts per source
RETRY_BACKOFF = 2      # seconds, doubles each retry

BD_TZ = timezone(timedelta(hours=6))  # Bangladesh Standard Time, no DST

DATE_RE = re.compile(r"\b(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})\b")
MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

# Status buckets used for sort order + live/upcoming counts. Anything not
# listed in UPCOMING_STATUSES or FINISHED_STATUSES is treated as "in play"
# (LIVE, 1H, 2H, HT, ET, INT, ...) — that covers whatever short live-state
# codes Bingstream throws at us without needing to enumerate them all.
UPCOMING_STATUSES = {"NS", "UPCOMING", "TBD"}
FINISHED_STATUSES = {"FT", "AET", "PEN", "CANC", "POSTP", "ABD", "AWD", "WO", "FINISHED"}


# --------------------------------------------------------------------------
# Networking
# --------------------------------------------------------------------------
def fetch(url: str) -> bytes:
    last_err = None
    delay = RETRY_BACKOFF
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (auto-json-merge-bot)",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            last_err = e
            print(f"[WARN] attempt {attempt}/{MAX_RETRIES} failed for {url}: {e}",
                  file=sys.stderr)
            if attempt < MAX_RETRIES:
                time.sleep(delay)
                delay *= 2
    raise RuntimeError(f"All retries failed for {url}: {last_err}")


def fetch_json(url: str):
    return json.loads(fetch(url))


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def clean_video_name(title: str) -> str:
    """Strip the trailing ' - DD Mon YYYY' date SonyLiv appends to titles,
    keeping any language suffix like '(Hindi)' intact."""
    name = DATE_RE.sub("", title)
    name = re.sub(r"-\s*(\(.*\))?\s*$", r"\1", name).strip()
    name = re.sub(r"\s{2,}", " ", name).strip(" -")
    return name if name else title


def parse_title_date(title: str, fallback: datetime) -> datetime:
    """Pull a 'DD Mon YYYY' date out of a SonyLiv title and combine it with
    the current UTC time (these are live streams, so 'now' is the only
    meaningful time component SonyLiv actually gives us)."""
    m = DATE_RE.search(title)
    if not m:
        return fallback
    day, mon_str, year = int(m.group(1)), m.group(2), int(m.group(3))
    month = MONTHS.get(mon_str)
    if not month:
        return fallback
    try:
        return fallback.replace(year=year, month=month, day=day)
    except ValueError:
        return fallback


def slugify(text: str) -> str:
    slug = text.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def format_bd_time(dt_utc: datetime, with_seconds: bool = False) -> str:
    """Render a naive-UTC datetime as Bingstream's BD-time string, e.g.
    '1:00 PM 26-09-2026' (matches) or '5:32:25 PM 26-09-2026' (header)."""
    bd_dt = dt_utc.replace(tzinfo=timezone.utc).astimezone(BD_TZ)
    hour12 = bd_dt.strftime("%I").lstrip("0") or "12"
    if with_seconds:
        return f"{hour12}:{bd_dt.strftime('%M:%S %p %d-%m-%Y')}"
    return f"{hour12}:{bd_dt.strftime('%M %p %d-%m-%Y')}"


def status_priority(status: str) -> int:
    s = (status or "").upper()
    if s in UPCOMING_STATUSES:
        return 1
    if s in FINISHED_STATUSES:
        return 2
    return 0  # LIVE / 1H / 2H / HT / ET / etc. -> in play, shown first


def sort_key(match: dict):
    pr = status_priority(match.get("status"))
    try:
        start = int(match.get("start_at") or 0)
    except (TypeError, ValueError):
        start = 0
    # Soonest/earliest start first within each status group.
    return (pr, start)


# --------------------------------------------------------------------------
# Conversion: SonyLiv live_matches[] -> Bingstream-style matches[] entries
# --------------------------------------------------------------------------
def convert_sonyliv_to_bing_schema(sonyliv_data: dict) -> list:
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    now_iso = now_utc.strftime("%Y-%m-%d %H:%M:%S")
    converted = []

    for m in sonyliv_data.get("live_matches", []):
        title = (m.get("title") or "").strip()
        tournament = (m.get("tournament") or "").strip()
        video_name = clean_video_name(title) or title
        event_dt = parse_title_date(title, now_utc)
        thumb = m.get("thumbnail", "")
        base_url = m.get("base_url", "")
        token = m.get("token", "")

        try:
            match_id = int(m.get("match_id"))
        except (TypeError, ValueError):
            match_id = m.get("match_id")

        start_epoch = int(event_dt.replace(tzinfo=timezone.utc).timestamp())

        converted.append({
            "id": match_id,
            "status": "LIVE",
            "name": video_name,
            "slug": slugify(f"{tournament}-{video_name}") or slugify(video_name) or "live-stream",
            "score": "LIVE",
            "timelive": "LIVE",
            "is_playing": True,
            "has_ended": False,
            "is_waiting": False,
            "start_at": start_epoch,
            "timestamp": start_epoch,
            "bd_time": format_bd_time(event_dt),
            "ishot": False,
            "league_name": tournament,
            "league_logo": "",
            "localteam_name": "",
            "localteam_logo": "",
            "visitorteam_name": "",
            "visitorteam_logo": "",
            "thumbnail": thumb,
            "description": (
                f"Watch {video_name} live from {tournament}. Enjoy live "
                f"coverage with real-time action, key moments, and "
                f"non-stop excitement. Stream {tournament} online via "
                f"app, web, or smart TV."
            ),
            "source": "sonyliv",
            "link_live": [
                {
                    "stream_link": f"{base_url}{token}",
                    "display_name": "HD",
                    "line": "web",
                    "created_at": now_iso,
                    "updated_at": now_iso,
                }
            ],
            # SonyLiv streams are direct m3u8 links, not played through
            # Bingstream's own iframe embed — leave these blank so any
            # consuming app knows to use stream_link as-is.
            "cdn_domain": "",
            "referer": "",
        })

    return converted


# --------------------------------------------------------------------------
# Normalize a Bingstream match: keep every original field untouched, only
# add the extra (additive, safe-to-ignore) fields described up top.
# --------------------------------------------------------------------------
def normalize_bing_match(m: dict) -> dict:
    out = dict(m)
    out.setdefault("league_logo", "")
    out.setdefault("localteam_name", "")
    out.setdefault("localteam_logo", "")
    out.setdefault("visitorteam_name", "")
    out.setdefault("visitorteam_logo", "")

    out["thumbnail"] = (
        out.get("localteam_logo") or out.get("visitorteam_logo")
        or out.get("league_logo") or ""
    )

    name = out.get("name", "")
    league = out.get("league_name", "")
    if name and league:
        out["description"] = (
            f"Watch {name} live in the {league}. Stream online via app, "
            f"web, or smart TV."
        )
    elif name:
        out["description"] = f"Watch {name} live. Stream online via app, web, or smart TV."
    else:
        out["description"] = ""

    out["source"] = "bingstream"
    return out


# --------------------------------------------------------------------------
# Merge + sort
# --------------------------------------------------------------------------
def merge(bing_data: dict, sonyliv_matches: list) -> dict:
    bing_matches = [normalize_bing_match(m) for m in bing_data.get("matches", [])]
    merged_matches = bing_matches + sonyliv_matches
    merged_matches.sort(key=sort_key)

    live_count = sum(1 for m in merged_matches if status_priority(m.get("status")) == 0)
    upcoming_count = sum(1 for m in merged_matches if status_priority(m.get("status")) == 1)

    info = dict(bing_data.get("playlist_info", {}))
    info["last_update_time"] = format_bd_time(
        datetime.now(timezone.utc).replace(tzinfo=None), with_seconds=True
    )
    info["statistics"] = {
        "total_live": live_count,
        "total_upcoming": upcoming_count,
    }

    return {
        "playlist_info": info,
        "matches": merged_matches,
    }


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main() -> int:
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)

    bing_data = None
    sonyliv_matches = []
    any_failed = False

    try:
        bing_data = fetch_json(BINGSTREAM_URL)
    except Exception as e:
        print(f"[ERROR] could not fetch Bingstream source: {e}", file=sys.stderr)
        any_failed = True

    try:
        sonyliv_data = fetch_json(SONYLIV_URL)
        sonyliv_matches = convert_sonyliv_to_bing_schema(sonyliv_data)
    except Exception as e:
        print(f"[ERROR] could not fetch/convert SonyLiv source: {e}", file=sys.stderr)
        any_failed = True

    if bing_data is None:
        # Without Bingstream's structure/playlist_info we have nothing
        # solid to merge into. Keep whatever output already exists and
        # bail cleanly.
        print("[ERROR] Bingstream source unavailable — keeping previous "
              "output file untouched this run.", file=sys.stderr)
        write_status(any_changed=False, any_failed=True)
        return 0

    merged = merge(bing_data, sonyliv_matches)
    new_content = json.dumps(merged, ensure_ascii=False, indent=2) + "\n"

    old_content = None
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            old_content = f.read()

    any_changed = False
    if new_content != old_content:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write(new_content)
        any_changed = True
        print(f"[OK] {OUTPUT_FILE} updated ({len(new_content)} bytes, "
              f"{len(merged['matches'])} matches)")
    else:
        print(f"[SKIP] {OUTPUT_FILE} unchanged")

    write_status(any_changed=any_changed, any_failed=any_failed)
    return 0


def write_status(any_changed: bool, any_failed: bool):
    status = {
        "last_checked_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "any_changed_this_run": any_changed,
        "any_source_failed_this_run": any_failed,
    }
    os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
        f.write("\n")


if __name__ == "__main__":
    # Exit 0 always — a single failed source should not fail the whole
    # GitHub Actions job.
    sys.exit(main())
