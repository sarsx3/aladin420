#!/usr/bin/env python3
"""
Auto-updater: fetches Tapmad + SonyLiv + Firebase live match data and merges
them into a SINGLE output JSON file that follows Tapmad's schema.

How it works
------------
1. TAPMAD_URL is treated as the "source of truth" for structure. Its JSON
   shape (HeaderInfo / Stats / Matches[...]) is never changed.
2. SONYLIV_URL is fetched and every entry in its "live_matches" list is
   converted (reshaped) into a Tapmad-style "Matches" entry.
3. FIREBASE_URL is fetched — it's a flat dict {id: matchObject, ...}. Each
   entry with visibility="public" is converted into Tapmad schema, filtering
   out already-ended matches (no stream + past time). "trash" key is skipped.
4. All three match lists are merged into one list.
5. The merged list is sorted so the newest matches (by EventStartDate) sit
   at the top, with "Live" matches given priority over "Upcoming" ones.
6. HeaderInfo/Stats are recalculated for the merged result and the whole
   thing is written to OUTPUT_FILE, but ONLY if the content actually
   changed (byte-for-byte compare), to avoid pointless commits.

Design goals:
- Never crash the whole run if ONE source fails — fall back gracefully.
- Retry each request with backoff on network errors.
- Byte-for-byte change detection (no pointless commits).
- Zero third-party dependencies (Python stdlib only).

Timezone rule (IMPORTANT):
- The Flutter app (live_events_service.dart) treats every EventStartDate
  as Bangladesh time (UTC+6) by appending '+06:00' before parsing.
- Therefore ALL sources must store EventStartDate in BD time (UTC+6).
  * Tapmad  → already in BD time ✅ (no change needed)
  * SonyLiv → was stored as UTC, now converted to BD time (+6 h) ✅
  * Firebase → matchDate/matchTime are BD time; previously subtracted
               FIREBASE_TZ_OFFSET (wrongly converting to UTC). Now stored
               as-is in BD time ✅
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
TAPMAD_URL = (
    "https://gist.githubusercontent.com/albatr0ssss/"
    "3cff7a26be49b1d352c15f615067e7cd/raw/tapmad_bd.json"
)
SONYLIV_URL = (
    "https://raw.githubusercontent.com/srhady/SonyLiv/"
    "refs/heads/main/sonyliv_playlist.json"
)
# Firebase Realtime Database — flat dict of match objects
FIREBASE_URL = (
    "https://priofy-6b9b4-default-rtdb.firebaseio.com/"
    "sports_events.json?auth=2gEYXaFECMKJNDrGUdv6ZhJH4ceHiokhHNrpePXF"
)

# Bangladesh timezone (UTC+6) — used for ALL sources so the Flutter app
# (kLiveEventsFeedUtcOffset = '+06:00') always gets the right local time.
BD_TZ = timezone(timedelta(hours=6))

# --- Output -----------------------------------------------------------------
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
# Helpers (shared)
# --------------------------------------------------------------------------
def stable_category_id(tournament_name: str) -> int:
    """Deterministic CategoryId, stable across runs to avoid needless diffs."""
    digest = hashlib.md5(tournament_name.encode("utf-8")).hexdigest()
    return 9000 + (int(digest[:8], 16) % 1000)


def clean_video_name(title: str) -> str:
    """Strip the trailing ' - DD Mon YYYY' date SonyLiv appends to titles."""
    name = DATE_RE.sub("", title)
    name = re.sub(r"-\s*(\(.*\))?\s*$", r"\1", name).strip()
    name = re.sub(r"\s{2,}", " ", name).strip(" -")
    return name if name else title


def parse_title_date(title: str, fallback: datetime) -> datetime:
    """Pull a 'DD Mon YYYY' date out of a SonyLiv title."""
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
    """Parse Tapmad's 'YYYY-MM-DD HH:MM:SS' EventStartDate."""
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def now_bd() -> datetime:
    """Current time in Bangladesh (UTC+6), timezone-aware."""
    return datetime.now(BD_TZ)


# --------------------------------------------------------------------------
# Conversion: SonyLiv live_matches[] -> Tapmad-style Matches[]
# --------------------------------------------------------------------------
def convert_sonyliv_to_tapmad_schema(sonyliv_data: dict) -> list:
    # FIX: use BD time as the reference "now" so EventStartDate is stored
    # in UTC+6, matching what the Flutter app expects (kLiveEventsFeedUtcOffset
    # = '+06:00'). Previously this used datetime.now(timezone.utc) which made
    # every SonyLiv match appear 6 hours early in the app.
    now_bd_dt = now_bd()
    converted = []

    for m in sonyliv_data.get("live_matches", []):
        title = m.get("title", "").strip()
        tournament = m.get("tournament", "").strip()
        video_name = clean_video_name(title) or title

        # parse_title_date returns a datetime with the same tzinfo as fallback;
        # since fallback is now BD-aware, the result is also BD-aware.
        event_dt_bd = parse_title_date(title, now_bd_dt)

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
            # EventStartDate stored as BD time (naive string) — Flutter adds
            # +06:00 when parsing (kLiveEventsFeedUtcOffset).
            "EventStartDate": event_dt_bd.strftime("%Y-%m-%d %H:%M:%S"),
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
# Conversion: Firebase sports_events{} -> Tapmad-style Matches[]
# --------------------------------------------------------------------------

def _parse_firebase_datetime(match_date: str, match_time: str) -> datetime | None:
    """
    Parse Firebase matchDate (DD/MM/YYYY) + matchTime (e.g. '9:55 PM')
    into a naive datetime that represents Bangladesh time (UTC+6).

    FIX: previously this subtracted FIREBASE_TZ_OFFSET to produce a
    "UTC-equivalent" value. But the Flutter app later re-adds +06:00
    (kLiveEventsFeedUtcOffset), which would have been correct — except
    _determine_firebase_status() was comparing the subtracted value against
    datetime.utcnow(), causing a double-offset error in status detection.
    Now we store the raw BD time as-is (no subtraction). The Flutter app
    will correctly interpret it as UTC+6.
    """
    try:
        dt_str = f"{match_date.strip()} {match_time.strip()}"
        # Try 12-hour format first
        dt = datetime.strptime(dt_str, "%d/%m/%Y %I:%M %p")
    except ValueError:
        try:
            # Try 24-hour format as fallback
            dt = datetime.strptime(dt_str, "%d/%m/%Y %H:%M")
        except ValueError:
            return None
    # dt is already in BD time — return as-is (naive, BD-local)
    return dt


def _determine_firebase_status(
    dt_bd: datetime | None,
    has_streams: bool,
) -> str:
    """
    Firebase JSON has no explicit Status field.
    Derive it from match datetime vs now — both in BD time (UTC+6):
      - If >15 min in future       → "Upcoming"
      - If within ±120 min window  → "Live"    (live window heuristic)
      - If more than 120 min past  → "Ended"
    Falls back to "Upcoming" when datetime can't be parsed.

    FIX: previously compared a UTC-shifted value against datetime.utcnow().
    Now compares BD time directly against now_bd() for consistency.
    """
    if dt_bd is None:
        return "Upcoming"

    # now_bd() is timezone-aware; make dt_bd comparable by treating it as BD.
    now_bd_naive = now_bd().replace(tzinfo=None)
    diff_minutes = (now_bd_naive - dt_bd).total_seconds() / 60  # positive = past

    if diff_minutes < -15:
        return "Upcoming"
    elif diff_minutes <= 120:
        return "Live"
    else:
        return "Ended"


def _firebase_sources(entry: dict) -> list:
    """
    Extract streaming sources from Firebase match entry.
    Firebase uses 'buttons' (or 'link_live' which is a duplicate) — each
    button has 'stream_link' (the URL) and 'name' (the label).
    Also includes 'headers' and 'drmScheme'/'drmLicenseUrl' metadata.
    """
    sources = []
    seen_urls = set()

    raw_list = entry.get("buttons") or entry.get("link_live") or []
    if not isinstance(raw_list, list):
        raw_list = []

    for btn in raw_list:
        if not isinstance(btn, dict):
            continue
        url = (btn.get("stream_link") or btn.get("url") or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)

        label = (btn.get("display_name") or btn.get("name") or "").strip()
        if not label:
            label = f"Server {len(sources) + 1}"

        source_entry = {"label": label, "url": url}

        # Pass along DRM and headers metadata — the Flutter app's
        # _extractSources already ignores unknown keys gracefully.
        drm_scheme = (btn.get("drmScheme") or "").strip()
        drm_license = (btn.get("drmLicenseUrl") or "").strip()
        if drm_scheme:
            source_entry["drmScheme"] = drm_scheme
        if drm_license:
            source_entry["drmLicenseUrl"] = drm_license

        headers = btn.get("headers")
        if isinstance(headers, dict):
            # Only include non-empty header values
            filtered_headers = {k: v for k, v in headers.items() if v}
            if filtered_headers:
                source_entry["headers"] = filtered_headers

        sources.append(source_entry)

    return sources


def convert_firebase_to_tapmad_schema(firebase_data: dict) -> list:
    """
    Convert Firebase sports_events flat dict into Tapmad-style Matches[].

    Field mapping:
      firebase.id          -> EntityId
      team1 + " vs " + team2 -> VideoName  (or 'name' field if present)
      league               -> CategoryName
      matchDate+matchTime  -> EventStartDate (BD time, stored as-is)
      sportCategory        -> part of Description
      logo1                -> ThumbnailStandard + Team1Logo (ThumbnailTV left empty)
      logo2                -> Team2Logo
      team1                -> Team1Name  (also used in VideoName)
      team2                -> Team2Name  (also used in VideoName)
      buttons[].stream_link-> sources list
      matchFormat          -> StageName (e.g. "ODI") or derived from status
      hotMatch/is_hot      -> priority hint stored in extra Description
      leagueLogo           -> UrlSlug fallback image (unused in schema but
                              stored in Description for reference)
      visibility="public"  -> included; "unpublic" -> skipped
      key="trash"          -> always skipped
    """
    converted = []

    for key, entry in firebase_data.items():
        # Skip the trash container and any non-dict values
        if key == "trash" or not isinstance(entry, dict):
            continue

        # Only include publicly visible matches
        visibility = (entry.get("visibility") or "").strip().lower()
        if visibility != "public":
            continue

        # --- Core fields ---
        raw_id = entry.get("id", key)
        try:
            entity_id = int(raw_id)
        except (TypeError, ValueError):
            entity_id = raw_id

        team1 = (entry.get("team1") or "").strip()
        team2 = (entry.get("team2") or "").strip()
        # Use 'name' if explicitly set, else build "Team1 vs Team2"
        explicit_name = (entry.get("name") or "").strip()
        if explicit_name:
            video_name = explicit_name
        elif team1 and team2:
            video_name = f"{team1} vs {team2}"
        elif team1:
            video_name = team1
        elif team2:
            video_name = team2
        else:
            continue  # No usable name — skip

        league = (entry.get("league") or "").strip()
        sport_category = (entry.get("sportCategory") or "").strip()
        match_format = (entry.get("matchFormat") or "").strip()
        logo1 = (entry.get("logo1") or "").strip()
        logo2 = (entry.get("logo2") or "").strip()

        # --- DateTime (BD time, stored as naive string) ---
        match_date = (entry.get("matchDate") or "").strip()
        match_time = (entry.get("matchTime") or "").strip()
        # FIX: dt_bd is now raw BD time (no UTC subtraction).
        dt_bd = _parse_firebase_datetime(match_date, match_time)

        if dt_bd is not None:
            event_start = dt_bd.strftime("%Y-%m-%d %H:%M:%S")
        else:
            event_start = ""

        # --- Streams ---
        sources = _firebase_sources(entry)
        has_streams = len(sources) > 0

        # --- Status (compared in BD time) ---
        status = _determine_firebase_status(dt_bd, has_streams)

        # Skip ended matches with no streams — nothing useful to show
        if status == "Ended" and not has_streams:
            continue

        # --- StageName ---
        if match_format:
            stage_name = match_format          # e.g. "ODI", "T20"
        elif status == "Live":
            stage_name = "Live"
        else:
            stage_name = "Upcoming"

        # --- CategoryId ---
        category_id = stable_category_id(league or sport_category or "sports")

        # --- UrlSlug ---
        slug_base = league or sport_category or video_name
        url_slug = slugify(slug_base) or "live-match"

        # --- Description ---
        hot = entry.get("hotMatch") == "yes" or entry.get("is_hot") is True
        hot_tag = " 🔥" if hot else ""
        description = (
            f"Watch {video_name}{hot_tag} — {league}. "
            f"Live streaming from {sport_category} event. "
            f"Stream via app, web, or smart TV."
        )

        # --- Primary stream_url (first source, for backward compat) ---
        primary_stream = sources[0]["url"] if sources else ""

        converted.append({
            "EntityId": entity_id,
            "VideoName": video_name,
            "CategoryName": league,
            "StageName": stage_name,
            "EventStartDate": event_start,
            "Description": description,
            "ThumbnailStandard": logo1,
            "ThumbnailTV": "",          # Firebase has no TV thumbnail
            "IsFreeToWatch": False,
            "Status": status,
            "CategoryId": category_id,
            "UrlSlug": url_slug,
            "stream_url": primary_stream,
            # Multi-source list — picked up by _extractSources in the
            # Flutter live_events_service.dart (it checks 'sources' key)
            "sources": sources,
            # Team details — extra fields beyond the core Tapmad schema.
            # Stored here so the Flutter app can read them from Channel.extra
            # via _channelFromMatch (any unknown key lands in extra map as-is).
            "Team1Name": team1,
            "Team2Name": team2,
            "Team1Logo": logo1,
            "Team2Logo": logo2,
        })

    return converted


# --------------------------------------------------------------------------
# Merge + sort
# --------------------------------------------------------------------------
STATUS_PRIORITY = {"Live": 0, "Upcoming": 1}


def sort_key(match: dict):
    status_rank = STATUS_PRIORITY.get(match.get("Status"), 2)
    dt = parse_event_datetime(match.get("EventStartDate", ""))
    dt_key = dt.timestamp() if dt else float("-inf")
    return (status_rank, -dt_key)


def merge(tapmad_data: dict, sonyliv_matches: list, firebase_matches: list) -> dict:
    tapmad_matches = tapmad_data.get("Matches", [])
    merged_matches = tapmad_matches + sonyliv_matches + firebase_matches
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
    firebase_matches = []
    any_failed = False

    # ── Source 1: Tapmad (structure anchor) ──────────────────────────────
    try:
        tapmad_data = fetch_json(TAPMAD_URL)
    except Exception as e:
        print(f"[ERROR] could not fetch Tapmad source: {e}", file=sys.stderr)
        any_failed = True

    # ── Source 2: SonyLiv ────────────────────────────────────────────────
    try:
        sonyliv_data = fetch_json(SONYLIV_URL)
        sonyliv_matches = convert_sonyliv_to_tapmad_schema(sonyliv_data)
        print(f"[OK] SonyLiv: {len(sonyliv_matches)} matches converted")
    except Exception as e:
        print(f"[ERROR] could not fetch/convert SonyLiv source: {e}", file=sys.stderr)
        any_failed = True

    # ── Source 3: Firebase ───────────────────────────────────────────────
    try:
        firebase_data = fetch_json(FIREBASE_URL)
        firebase_matches = convert_firebase_to_tapmad_schema(firebase_data)
        print(f"[OK] Firebase: {len(firebase_matches)} matches converted "
              f"(public, non-ended)")
    except Exception as e:
        print(f"[ERROR] could not fetch/convert Firebase source: {e}", file=sys.stderr)
        any_failed = True

    if tapmad_data is None:
        print("[ERROR] Tapmad source unavailable — keeping previous output "
              "file untouched this run.", file=sys.stderr)
        write_status(any_changed=False, any_failed=True)
        return 0

    merged = merge(tapmad_data, sonyliv_matches, firebase_matches)
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
        total = len(merged["Matches"])
        live = merged["Stats"]["LiveCount"]
        upcoming = merged["Stats"]["UpcomingCount"]
        print(f"[OK] {OUTPUT_FILE} updated — "
              f"{total} total matches ({live} live, {upcoming} upcoming)")
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
    sys.exit(main())
