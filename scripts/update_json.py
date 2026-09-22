#!/usr/bin/env python3
"""
Auto-updater: fetches Tapmad + SonyLiv live/upcoming match data and merges
them into a SINGLE output JSON file that follows Tapmad's schema.

How it works
------------
1. TAPMAD_URL is treated as the "source of truth" for structure. Its JSON
   shape (HeaderInfo / Stats / Matches[...]) is never changed.
2. SONYLIV_URL is fetched and every entry in its "live_matches" list is
   converted (reshaped) into a Tapmad-style "Matches" entry.
3. Both match lists are merged into one list.
4. The merged list is sorted so the newest matches (by EventStartDate) sit
   at the top, with "Live" matches given priority over "Upcoming" ones —
   the way these playlists are normally ordered.
5. HeaderInfo/Stats are recalculated for the merged result and the whole
   thing is written to OUTPUT_FILE, but ONLY if the content actually
   changed (byte-for-byte compare), to avoid pointless commits.

Design goals (kept from the previous version of this script):
- Never crash the whole run if ONE source fails — fall back to whatever
  the other source produced, and only give up completely if BOTH fail.
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
from datetime import datetime, timezone

# --- Sources --------------------------------------------------------------
TAPMAD_URL = (
    "https://gist.githubusercontent.com/albatr0ssss/"
    "3cff7a26be49b1d352c15f615067e7cd/raw/tapmad_bd.json"
)
SONYLIV_URL = (
    "https://raw.githubusercontent.com/srhady/SonyLiv/"
    "refs/heads/main/sonyliv_playlist.json"
)

# --- Output -----------------------------------------------------------------
# Single merged file, in Tapmad's schema. Rename here if you'd rather call
# it something else (e.g. "data/tapmad_bd.json") — nothing else needs to change.
OUTPUT_FILE = "data/merged_playlist.json"
STATUS_FILE = "data/status.json"

TIMEOUT = 15          # seconds per request
MAX_RETRIES = 4        # attempts per source
RETRY_BACKOFF = 2      # seconds, doubles each retry

DATE_RE = re.compile(r"\b(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})\b")
MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
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
def stable_category_id(tournament_name: str) -> int:
    """Deterministic CategoryId for SonyLiv-derived matches, kept out of
    Tapmad's own id range (1000-1999 in observed samples) and stable across
    runs so it doesn't cause needless diffs/commits."""
    digest = hashlib.md5(tournament_name.encode("utf-8")).hexdigest()
    return 9000 + (int(digest[:8], 16) % 1000)


def clean_video_name(title: str) -> str:
    """Strip the trailing ' - DD Mon YYYY' date Sonyliv appends to titles,
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


def parse_event_datetime(value: str):
    """Parse Tapmad's 'YYYY-MM-DD HH:MM:SS' EventStartDate. Returns None
    (sorted last) if it can't be parsed."""
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Conversion: SonyLiv live_matches[] -> Tapmad-style Matches[] entries
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
            "CategoryId": stable_category_id(tournament),
            "UrlSlug": slugify(f"{tournament}-live") or "live-match",
            "stream_url": f"{base_url}{token}",
        })

    return converted


# --------------------------------------------------------------------------
# Merge + sort
# --------------------------------------------------------------------------
STATUS_PRIORITY = {"Live": 0, "Upcoming": 1}


def sort_key(match: dict):
    status_rank = STATUS_PRIORITY.get(match.get("Status"), 2)
    dt = parse_event_datetime(match.get("EventStartDate", ""))
    # Newest first within each status group -> sort by datetime descending.
    # Unparseable dates sort last within their group.
    dt_key = dt.timestamp() if dt else float("-inf")
    return (status_rank, -dt_key)


def merge(tapmad_data: dict, sonyliv_matches: list) -> dict:
    tapmad_matches = tapmad_data.get("Matches", [])
    merged_matches = tapmad_matches + sonyliv_matches
    merged_matches.sort(key=sort_key)

    live_count = sum(1 for m in merged_matches if m.get("Status") == "Live")
    upcoming_count = sum(1 for m in merged_matches if m.get("Status") == "Upcoming")

    header = dict(tapmad_data.get("HeaderInfo", {}))
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

    tapmad_data = None
    sonyliv_matches = []
    any_failed = False

    try:
        tapmad_data = fetch_json(TAPMAD_URL)
    except Exception as e:
        print(f"[ERROR] could not fetch Tapmad source: {e}", file=sys.stderr)
        any_failed = True

    try:
        sonyliv_data = fetch_json(SONYLIV_URL)
        sonyliv_matches = convert_sonyliv_to_tapmad_schema(sonyliv_data)
    except Exception as e:
        print(f"[ERROR] could not fetch/convert SonyLiv source: {e}", file=sys.stderr)
        any_failed = True

    if tapmad_data is None:
        # Without Tapmad's structure/HeaderInfo we have nothing solid to
        # merge into. Keep whatever output already exists and bail cleanly.
        print("[ERROR] Tapmad source unavailable — keeping previous output "
              "file untouched this run.", file=sys.stderr)
        write_status(any_changed=False, any_failed=True)
        return 0

    merged = merge(tapmad_data, sonyliv_matches)
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
