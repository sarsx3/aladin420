#!/usr/bin/env python3
"""
Auto-updater: fetches Tapmad + SonyLiv + Firebase live match data and merges
them into a SINGLE output JSON file that follows Tapmad's schema.

Timezone strategy (ALL sources → Bangladesh Standard Time UTC+6)
-----------------------------------------------------------------
• Tapmad   : times are PKT (UTC+5)  → add 1 hour  → BST (UTC+6)
• SonyLiv  : times are UTC (UTC+0)  → add 6 hours → BST (UTC+6)
• Firebase : matchDate/matchTime stored as BD local (UTC+6) → keep as-is

All EventStartDate values are written as "YYYY-MM-DD HH:MM:SS" in BST.
Flutter reads them with DateTime.tryParse(...).toLocal() — since the device
is in BST and the value is already BST, this is a no-op: correct time shown.
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

# ── Timezone constants ────────────────────────────────────────────────────────
BST_OFFSET = timedelta(hours=6)   # Bangladesh Standard Time  UTC+6
PKT_OFFSET = timedelta(hours=5)   # Pakistan Standard Time    UTC+5  (Tapmad)

# ── Sources ───────────────────────────────────────────────────────────────────
TAPMAD_URL = (
    "https://gist.githubusercontent.com/albatr0ssss/"
    "3cff7a26be49b1d352c15f615067e7cd/raw/tapmad_bd.json"
)
SONYLIV_URL = (
    "https://raw.githubusercontent.com/srhady/SonyLiv/"
    "refs/heads/main/sonyliv_playlist.json"
)
FIREBASE_URL = (
    "https://priofy-6b9b4-default-rtdb.firebaseio.com/"
    "sports_events.json?auth=2gEYXaFECMKJNDrGUdv6ZhJH4ceHiokhHNrpePXF"
)

# ── Output ────────────────────────────────────────────────────────────────────
OUTPUT_FILE   = "data/merged_playlist.json"
STATUS_FILE   = "data/status.json"

TIMEOUT       = 15
MAX_RETRIES   = 4
RETRY_BACKOFF = 2

DATE_RE = re.compile(r"\b(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})\b")
MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}
BST_FMT = "%Y-%m-%d %H:%M:%S"


# ── Timezone helpers ──────────────────────────────────────────────────────────

def now_bst() -> datetime:
    """Current time as a naive datetime in BST (UTC+6)."""
    return datetime.utcnow() + BST_OFFSET


def pkt_to_bst(dt: datetime) -> datetime:
    """Naive PKT datetime (UTC+5) → BST (UTC+6): +1 hour."""
    return dt + timedelta(hours=1)


def utc_to_bst(dt: datetime) -> datetime:
    """Naive UTC datetime (UTC+0) → BST (UTC+6): +6 hours."""
    return dt + timedelta(hours=6)


def fmt_bst(dt: datetime) -> str:
    return dt.strftime(BST_FMT)


# ── Networking ────────────────────────────────────────────────────────────────

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


# ── Shared helpers ────────────────────────────────────────────────────────────

def stable_category_id(tournament_name: str) -> int:
    digest = hashlib.md5(tournament_name.encode("utf-8")).hexdigest()
    return 9000 + (int(digest[:8], 16) % 1000)


def clean_video_name(title: str) -> str:
    name = DATE_RE.sub("", title)
    name = re.sub(r"-\s*(\(.*\))?\s*$", r"\1", name).strip()
    name = re.sub(r"\s{2,}", " ", name).strip(" -")
    return name if name else title


def parse_title_date(title: str, fallback: datetime) -> datetime:
    """Pull DD Mon YYYY from a SonyLiv title; keep time from fallback."""
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


def parse_bst_datetime(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, BST_FMT)
    except (TypeError, ValueError):
        return None


# ── Source 1: Tapmad → BST ───────────────────────────────────────────────────
# Tapmad is a Pakistani service → all times in PKT (UTC+5).
# Convert: PKT + 1 hour = BST (UTC+6).

def normalize_tapmad_times(matches: list) -> list:
    """Convert every Tapmad EventStartDate from PKT (UTC+5) → BST (UTC+6)."""
    for m in matches:
        raw = m.get("EventStartDate", "")
        try:
            dt_pkt = datetime.strptime(raw, BST_FMT)
            m["EventStartDate"] = fmt_bst(pkt_to_bst(dt_pkt))
        except (ValueError, TypeError):
            pass   # leave malformed dates untouched
    return matches


# ── Source 2: SonyLiv → BST ──────────────────────────────────────────────────
# SonyLiv time fallback was previously now_utc (UTC+0).
# Fix: use now_bst() as fallback so the time component is already in BST.
# When a date IS found in the title, only y/m/d are replaced — the time
# component stays from the fallback (now_bst()), so still correct BST.

def convert_sonyliv_to_tapmad_schema(sonyliv_data: dict) -> list:
    fallback_bst = now_bst()   # ← BST fallback (was UTC before)
    converted = []

    for m in sonyliv_data.get("live_matches", []):
        title      = m.get("title", "").strip()
        tournament = m.get("tournament", "").strip()
        video_name = clean_video_name(title) or title
        # parse_title_date only changes y/m/d — time stays BST from fallback
        event_dt_bst = parse_title_date(title, fallback_bst)
        thumb    = m.get("thumbnail", "")
        base_url = m.get("base_url", "")
        token    = m.get("token", "")

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
            "EntityId":          entity_id,
            "VideoName":         video_name,
            "CategoryName":      tournament,
            "StageName":         "Live",
            "EventStartDate":    fmt_bst(event_dt_bst),   # ← BST
            "Description":       description,
            "ThumbnailStandard": thumb,
            "ThumbnailTV":       thumb,
            "IsFreeToWatch":     False,
            "Status":            "Live",
            "CategoryId":        stable_category_id(tournament),
            "UrlSlug":           slugify(f"{tournament}-live") or "live-match",
            "stream_url":        f"{base_url}{token}",
        })

    return converted


# ── Source 3: Firebase → BST ─────────────────────────────────────────────────
# Firebase matchDate ("DD/MM/YYYY") + matchTime ("9:55 PM") are ALREADY in
# Bangladesh time (BST = UTC+6). Parse directly — no conversion needed.
# Previously the script was subtracting 6 hours (treating them as UTC+6 and
# converting to UTC) which caused a 6-hour error in the output.

def _parse_firebase_datetime_bst(match_date: str, match_time: str) -> datetime | None:
    """
    Parse Firebase date+time as-is (already BST).
    No offset arithmetic — just a plain strptime.
    """
    try:
        dt_str = f"{match_date.strip()} {match_time.strip()}"
        try:
            return datetime.strptime(dt_str, "%d/%m/%Y %I:%M %p")   # 12-hr
        except ValueError:
            return datetime.strptime(dt_str, "%d/%m/%Y %H:%M")       # 24-hr
    except (ValueError, AttributeError):
        return None


def _determine_firebase_status(dt_bst: datetime | None, has_streams: bool) -> str:
    """Compare BST match time against current BST time."""
    if dt_bst is None:
        return "Upcoming"

    diff_minutes = (now_bst() - dt_bst).total_seconds() / 60   # positive = past

    if diff_minutes < -15:
        return "Upcoming"
    elif diff_minutes <= 120:
        return "Live"
    else:
        return "Ended"


def _firebase_sources(entry: dict) -> list:
    sources   = []
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

        drm_scheme  = (btn.get("drmScheme")     or "").strip()
        drm_license = (btn.get("drmLicenseUrl") or "").strip()
        if drm_scheme:
            source_entry["drmScheme"] = drm_scheme
        if drm_license:
            source_entry["drmLicenseUrl"] = drm_license

        headers = btn.get("headers")
        if isinstance(headers, dict):
            filtered = {k: v for k, v in headers.items() if v}
            if filtered:
                source_entry["headers"] = filtered

        sources.append(source_entry)

    return sources


def convert_firebase_to_tapmad_schema(firebase_data: dict) -> list:
    converted = []

    for key, entry in firebase_data.items():
        if key == "trash" or not isinstance(entry, dict):
            continue

        visibility = (entry.get("visibility") or "").strip().lower()
        if visibility != "public":
            continue

        raw_id = entry.get("id", key)
        try:
            entity_id = int(raw_id)
        except (TypeError, ValueError):
            entity_id = raw_id

        team1 = (entry.get("team1") or "").strip()
        team2 = (entry.get("team2") or "").strip()

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
            continue

        league         = (entry.get("league")        or "").strip()
        sport_category = (entry.get("sportCategory") or "").strip()
        match_format   = (entry.get("matchFormat")   or "").strip()
        logo1          = (entry.get("logo1")         or "").strip()
        logo2          = (entry.get("logo2")         or "").strip()

        # ── DateTime — parse directly as BST (no conversion) ─────────────
        match_date  = (entry.get("matchDate") or "").strip()
        match_time  = (entry.get("matchTime") or "").strip()
        dt_bst      = _parse_firebase_datetime_bst(match_date, match_time)
        event_start = fmt_bst(dt_bst) if dt_bst is not None else ""

        sources     = _firebase_sources(entry)
        has_streams = len(sources) > 0
        status      = _determine_firebase_status(dt_bst, has_streams)

        if status == "Ended" and not has_streams:
            continue

        if match_format:
            stage_name = match_format
        elif status == "Live":
            stage_name = "Live"
        else:
            stage_name = "Upcoming"

        category_id = stable_category_id(league or sport_category or "sports")
        slug_base   = league or sport_category or video_name
        url_slug    = slugify(slug_base) or "live-match"

        hot     = entry.get("hotMatch") == "yes" or entry.get("is_hot") is True
        hot_tag = " 🔥" if hot else ""
        description = (
            f"Watch {video_name}{hot_tag} — {league}. "
            f"Live streaming from {sport_category} event. "
            f"Stream via app, web, or smart TV."
        )

        primary_stream = sources[0]["url"] if sources else ""

        converted.append({
            "EntityId":          entity_id,
            "VideoName":         video_name,
            "CategoryName":      league,
            "StageName":         stage_name,
            "EventStartDate":    event_start,   # ← BST, parsed directly
            "Description":       description,
            "ThumbnailStandard": logo1,
            "ThumbnailTV":       "",
            "IsFreeToWatch":     False,
            "Status":            status,
            "CategoryId":        category_id,
            "UrlSlug":           url_slug,
            "stream_url":        primary_stream,
            "sources":           sources,
            "Team1Name":         team1,
            "Team2Name":         team2,
            "Team1Logo":         logo1,
            "Team2Logo":         logo2,
        })

    return converted


# ── Merge + sort ──────────────────────────────────────────────────────────────
STATUS_PRIORITY = {"Live": 0, "Upcoming": 1}


def sort_key(match: dict):
    status_rank = STATUS_PRIORITY.get(match.get("Status"), 2)
    dt = parse_bst_datetime(match.get("EventStartDate", ""))
    dt_key = dt.timestamp() if dt else float("-inf")
    return (status_rank, -dt_key)


def merge(tapmad_data: dict, sonyliv_matches: list, firebase_matches: list) -> dict:
    # Tapmad times are PKT → normalize to BST first
    tapmad_matches = normalize_tapmad_times(tapmad_data.get("Matches", []))

    merged_matches = tapmad_matches + sonyliv_matches + firebase_matches
    merged_matches.sort(key=sort_key)

    live_count     = sum(1 for m in merged_matches if m.get("Status") == "Live")
    upcoming_count = sum(1 for m in merged_matches if m.get("Status") == "Upcoming")

    header = dict(tapmad_data.get("HeaderInfo", {}))
    header["LastUpdate"] = now_bst().strftime("%I:%M %p %d-%m-%Y") + " BST"

    return {
        "HeaderInfo": header,
        "Stats": {
            "LiveCount":     live_count,
            "UpcomingCount": upcoming_count,
        },
        "Matches": merged_matches,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)

    tapmad_data      = None
    sonyliv_matches  = []
    firebase_matches = []
    any_failed       = False

    # ── Source 1: Tapmad (PKT → BST done in merge()) ─────────────────────
    try:
        tapmad_data = fetch_json(TAPMAD_URL)
        print(f"[OK] Tapmad: {len(tapmad_data.get('Matches', []))} matches "
              f"(PKT → BST conversion will apply)")
    except Exception as e:
        print(f"[ERROR] could not fetch Tapmad source: {e}", file=sys.stderr)
        any_failed = True

    # ── Source 2: SonyLiv (UTC → BST via now_bst() fallback) ─────────────
    try:
        sonyliv_data    = fetch_json(SONYLIV_URL)
        sonyliv_matches = convert_sonyliv_to_tapmad_schema(sonyliv_data)
        print(f"[OK] SonyLiv: {len(sonyliv_matches)} matches (times in BST)")
    except Exception as e:
        print(f"[ERROR] could not fetch/convert SonyLiv source: {e}", file=sys.stderr)
        any_failed = True

    # ── Source 3: Firebase (already BST — parsed directly) ────────────────
    try:
        firebase_data    = fetch_json(FIREBASE_URL)
        firebase_matches = convert_firebase_to_tapmad_schema(firebase_data)
        print(f"[OK] Firebase: {len(firebase_matches)} matches "
              f"(times already BST, public non-ended only)")
    except Exception as e:
        print(f"[ERROR] could not fetch/convert Firebase source: {e}", file=sys.stderr)
        any_failed = True

    if tapmad_data is None:
        print("[ERROR] Tapmad source unavailable — keeping previous output "
              "file untouched.", file=sys.stderr)
        write_status(any_changed=False, any_failed=True)
        return 0

    merged      = merge(tapmad_data, sonyliv_matches, firebase_matches)
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
        total    = len(merged["Matches"])
        live     = merged["Stats"]["LiveCount"]
        upcoming = merged["Stats"]["UpcomingCount"]
        print(f"[OK] {OUTPUT_FILE} updated — "
              f"{total} total matches ({live} live, {upcoming} upcoming) "
              f"— all EventStartDate values in BST (UTC+6)")
    else:
        print(f"[SKIP] {OUTPUT_FILE} unchanged")

    write_status(any_changed=any_changed, any_failed=any_failed)
    return 0


def write_status(any_changed: bool, any_failed: bool):
    status = {
        "last_checked_bst":           now_bst().strftime("%Y-%m-%dT%H:%M:%S+06:00"),
        "any_changed_this_run":       any_changed,
        "any_source_failed_this_run": any_failed,
    }
    os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
        f.write("\n")


if __name__ == "__main__":
    sys.exit(main())
