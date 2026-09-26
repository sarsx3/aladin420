#!/usr/bin/env python3
"""
Auto-updater: fetches Bingstream + SonyLiv live/upcoming match data and
merges them into a SINGLE output JSON file that follows the ORIGINAL
Tapmad-style schema (HeaderInfo / Stats / Matches[...] with EntityId,
VideoName, ThumbnailStandard/TV, etc.) — Tapmad itself is no longer a
data source, but the output JSON format/shape is unchanged.

How it works
------------
1. BINGSTREAM_URL is now the primary/required data source (the role
   Tapmad used to play). Its raw shape (playlist_info / matches[] with
   league/team logos and a `link_live` array) is reshaped into a
   Tapmad-style "Matches" entry by convert_bingstream_to_tapmad_schema().
   Finished matches (FT/AET/CANC/...) are dropped — the old feed only
   ever carried Live/Upcoming matches, so this output does the same.
2. SONYLIV_URL is fetched and every entry in its "live_matches" list is
   converted into a Tapmad-style "Matches" entry exactly as before
   (convert_sonyliv_to_tapmad_schema() is unchanged from the old script).
3. Both match lists are merged into one list.
4. The merged list is sorted so the newest matches (by EventStartDate)
   sit at the top, with "Live" matches given priority over "Upcoming"
   ones — identical sort logic to the old script.
5. HeaderInfo/Stats are (re)built for the merged result — since there's
   no more Tapmad source to copy HeaderInfo from, PlaylistName/Telegram/
   Owner now come from the HEADER_INFO_BASE constant below (edit it if
   you want different values) — and the whole thing is written to
   OUTPUT_FILE, but ONLY if the content actually changed (byte-for-byte
   compare), to avoid pointless commits.

Field notes for the Bingstream -> Tapmad reshape
-------------------------------------------------
- ThumbnailStandard / ThumbnailTV: Bingstream has no single poster image,
  only team logos, so both fields fall back through
  localteam_logo -> visitorteam_logo -> league_logo -> "" (whichever is
  first available).
- CategoryId: kept deterministic/stable via an md5-of-name hash, same
  technique as before, just a different numeric range (2000-2999) than
  SonyLiv-derived entries (9000-9999, unchanged) so the two never collide.
- StageName: Tapmad used this for a round/stage label (e.g. "MATCHDAY 1"),
  which Bingstream doesn't provide. It's filled with a human-readable
  version of Bingstream's status code instead (e.g. "1st Half", "Half
  Time", "Upcoming") so the field still carries real information.
- stream_url: Bingstream's `link_live` entries usually come in pairs — a
  plain `stream_link` (a bare master playlist, often not directly
  playable/tokenized) and, on the second entry, a `videoURL` that carries
  the actual signed/tokenized playable link. pick_stream_url() prefers
  `videoURL` wherever one exists in the list and only falls back to
  `stream_link` when no `videoURL` is present at all (e.g. far-future
  "NS" matches that don't have a token yet).

Design goals (kept from the previous version of this script):
- Never crash the whole run if ONE source fails — fall back to whatever
  the other source produced, and only give up completely if Bingstream
  (the now-required structural source) fails.
- Retry each request with backoff on network errors.
- Byte-for-byte change detection (no pointless commits).
- Zero third-party dependencies (Python stdlib only -> fast, no pip step).
"""

import hashlib
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
# Single merged file, in the original Tapmad-style schema. Rename here if
# you'd rather call it something else — nothing else needs to change.
OUTPUT_FILE = "data/merged_playlist.json"
STATUS_FILE = "data/status.json"

# There's no more Tapmad source to copy HeaderInfo from, so it's static.
# Edit these three values if you want something else.
HEADER_INFO_BASE = {
    "PlaylistName": "Live Sports Matches Metadata",
    "Telegram": "https://t.me/livesportsplay",
    "Owner": "srhady",
}

TIMEOUT = 15          # seconds per request
MAX_RETRIES = 4        # attempts per source
RETRY_BACKOFF = 2      # seconds, doubles each retry

BD_OFFSET = timedelta(hours=6)  # Bangladesh Standard Time, no DST

DATE_RE = re.compile(r"\b(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})\b")
MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

# Bingstream status codes that mean the match is over — these are dropped
# entirely rather than mapped into "Live"/"Upcoming".
FINISHED_STATUSES = {"FT", "AET", "PEN", "CANC", "POSTP", "ABD", "AWD", "WO", "FINISHED"}
UPCOMING_STATUSES = {"NS", "UPCOMING", "TBD"}

# Friendly StageName labels for Bingstream's short status codes.
STAGE_LABELS = {
    "LIVE": "Live",
    "1H": "1st Half",
    "2H": "2nd Half",
    "HT": "Half Time",
    "ET": "Extra Time",
    "P": "Penalties",
    "BREAK": "Break",
    "INT": "Interrupted",
    "NS": "Upcoming",
    "UPCOMING": "Upcoming",
    "TBD": "Upcoming",
}


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
def stable_category_id(name: str, base: int) -> int:
    """Deterministic CategoryId, stable across runs so it doesn't cause
    needless diffs/commits. `base` keeps different sources' generated ids
    in separate, non-colliding ranges."""
    digest = hashlib.md5(name.encode("utf-8")).hexdigest()
    return base + (int(digest[:8], 16) % 1000)


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


def pick_stream_url(links: list):
    """Pick the best playable link out of a Bingstream `link_live` array.
    `videoURL` (when present) is the actual tokenized/playable stream;
    `stream_link` is a plain, often non-tokenized fallback."""
    for l in links:
        if l.get("videoURL"):
            return l["videoURL"]
    for l in links:
        if l.get("stream_link"):
            return l["stream_link"]
    return None


def parse_event_datetime(value: str):
    """Parse the Tapmad-style 'YYYY-MM-DD HH:MM:SS' EventStartDate.
    Returns None (sorted last) if it can't be parsed."""
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Conversion: SonyLiv live_matches[] -> Tapmad-style Matches[] entries
# (unchanged from the previous version of this script)
# --------------------------------------------------------------------------
def convert_sonyliv_to_tapmad_schema(sonyliv_data: dict) -> list:
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    converted = []

    for m in sonyliv_data.get("live_matches", []):
        title = m.get("title", "").strip()
        tournament = m.get("tournament", "").strip()
        video_name = clean_video_name(title) or title
        event_dt = parse_title_date(title, now_utc)
        thumb = m.get("thumbnail", "")
        base_url = m.get("base_url", "")
        token = m.get("token", "")

        try:
            entity_id = int(m.get("match_id"))
        except (TypeError, ValueError):
            entity_id = m.get("match_id")

        description = (
            f"Watch {video_name} live from {tournament}. Enjoy live coverage "
            f"with real-time action, key moments, and non-stop excitement. "
            f"Stream {tournament} online via app, web, or smart TV."
        )

        converted.append({
            "EntityId": entity_id,
            "VideoName": video_name,
            "CategoryName": tournament,
            "StageName": "Live",
            "EventStartDate": event_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "Description": description,
            "ThumbnailStandard": thumb,
            "ThumbnailTV": thumb,
            "IsFreeToWatch": False,
            "Status": "Live",
            "CategoryId": stable_category_id(tournament, base=9000),
            "UrlSlug": slugify(f"{tournament}-live") or "live-match",
            "stream_url": f"{base_url}{token}",
        })

    return converted


# --------------------------------------------------------------------------
# Conversion: Bingstream matches[] -> Tapmad-style Matches[] entries
# --------------------------------------------------------------------------
def convert_bingstream_to_tapmad_schema(bing_data: dict) -> list:
    converted = []

    for m in bing_data.get("matches", []):
        status_raw = (m.get("status") or "").upper()
        if status_raw in FINISHED_STATUSES:
            continue  # the old feed never carried finished matches either

        tapmad_status = "Upcoming" if status_raw in UPCOMING_STATUSES else "Live"
        stage_name = STAGE_LABELS.get(status_raw, status_raw.title() if status_raw else "Live")

        try:
            start_at = int(m.get("start_at") or 0)
        except (TypeError, ValueError):
            start_at = 0
        if start_at:
            event_dt_bd = datetime.utcfromtimestamp(start_at) + BD_OFFSET
        else:
            event_dt_bd = datetime.now(timezone.utc).replace(tzinfo=None) + BD_OFFSET
        event_start_date = event_dt_bd.strftime("%Y-%m-%d %H:%M:%S")
        day_label = event_dt_bd.strftime("%d-%b")
        time_label = event_dt_bd.strftime("%I:%M %p")

        name = m.get("name") or m.get("league_name") or "Live Match"
        league_name = m.get("league_name") or name
        home = m.get("localteam_name") or ""
        away = m.get("visitorteam_name") or ""

        thumb = (
            m.get("localteam_logo") or m.get("visitorteam_logo")
            or m.get("league_logo") or ""
        )

        if home and away:
            description = (
                f"Watch {name} live in the {league_name}. Featuring competitive "
                f"action with key moments and non-stop excitement throughout the "
                f"game. The {name} match will take place on {day_label}, at "
                f"{time_label}. Stream {league_name} online via app, web, or "
                f"smart TV."
            )
        else:
            description = (
                f"Watch {name} live from {league_name}. Enjoy live coverage with "
                f"real-time action, key moments, and non-stop excitement. Stream "
                f"{league_name} online via app, web, or smart TV."
            )

        entry = {
            "EntityId": m.get("id"),
            "VideoName": name,
            "CategoryName": league_name,
            "StageName": stage_name,
            "EventStartDate": event_start_date,
            "Description": description,
            "ThumbnailStandard": thumb,
            "ThumbnailTV": thumb,
            "IsFreeToWatch": False,
            "Status": tapmad_status,
            "CategoryId": stable_category_id(league_name, base=2000),
            "UrlSlug": m.get("slug") or slugify(name) or "live-match",
        }

        stream_url = pick_stream_url(m.get("link_live") or [])
        if stream_url:
            entry["stream_url"] = stream_url

        converted.append(entry)

    return converted


# --------------------------------------------------------------------------
# Merge + sort (unchanged from the previous version of this script)
# --------------------------------------------------------------------------
STATUS_PRIORITY = {"Live": 0, "Upcoming": 1}


def sort_key(match: dict):
    status_rank = STATUS_PRIORITY.get(match.get("Status"), 2)
    dt = parse_event_datetime(match.get("EventStartDate", ""))
    # Newest first within each status group -> sort by datetime descending.
    # Unparseable dates sort last within their group.
    dt_key = dt.timestamp() if dt else float("-inf")
    return (status_rank, -dt_key)


def merge(bing_matches: list, sonyliv_matches: list) -> dict:
    merged_matches = bing_matches + sonyliv_matches
    merged_matches.sort(key=sort_key)

    live_count = sum(1 for m in merged_matches if m.get("Status") == "Live")
    upcoming_count = sum(1 for m in merged_matches if m.get("Status") == "Upcoming")

    header = dict(HEADER_INFO_BASE)
    header["LastUpdate"] = time.strftime("%I:%M %p %d-%m-%Y", time.gmtime())

    return {
        "HeaderInfo": header,
        "Stats": {
            "LiveCount": live_count,
            "UpcomingCount": upcoming_count,
        },
        "Matches": merged_matches,
    }


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main() -> int:
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)

    bing_data = None
    bing_matches = []
    sonyliv_matches = []
    any_failed = False

    try:
        bing_data = fetch_json(BINGSTREAM_URL)
        bing_matches = convert_bingstream_to_tapmad_schema(bing_data)
    except Exception as e:
        print(f"[ERROR] could not fetch/convert Bingstream source: {e}", file=sys.stderr)
        any_failed = True

    try:
        sonyliv_data = fetch_json(SONYLIV_URL)
        sonyliv_matches = convert_sonyliv_to_tapmad_schema(sonyliv_data)
    except Exception as e:
        print(f"[ERROR] could not fetch/convert SonyLiv source: {e}", file=sys.stderr)
        any_failed = True

    if bing_data is None:
        # Without Bingstream we have nothing solid to build the merged
        # output from. Keep whatever output already exists and bail cleanly.
        print("[ERROR] Bingstream source unavailable — keeping previous "
              "output file untouched this run.", file=sys.stderr)
        write_status(any_changed=False, any_failed=True)
        return 0

    merged = merge(bing_matches, sonyliv_matches)
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
              f"{len(merged['Matches'])} matches)")
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
