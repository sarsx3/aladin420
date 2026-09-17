#!/usr/bin/env python3
"""
Merge configured public M3U playlists into one master.m3u.

Source names are used only internally to identify errors/logs.
They are never written into the generated playlist.

Design goals (v2):
  1. raw.githubusercontent.com sits behind a CDN that caches each exact URL
     for ~5 minutes and does not honour Cache-Control headers from the
     client. Since we also run every 5 minutes, a plain fetch can very
     easily hand back the *previous* cached copy instead of the freshly
     updated one. We defeat this by appending a changing query string to
     every request, which forces the CDN to treat it as a new URL.
  2. A single bad/incomplete response from one source (network hiccup,
     the upstream repo mid-write, a temporary empty file, etc.) should
     not make the channel count in master.m3u suddenly crash. Each
     source's last good fetch is cached to disk (and committed to the
     repo, so it survives between Actions runs). If a fresh fetch fails
     outright, or looks suspiciously smaller than usual, we fall back to
     the cached copy for a short grace period instead of immediately
     trusting the bad response.
  3. master.m3u is only rewritten (and therefore only committed) when the
     actual channel list changes -- not just because 5 minutes passed.
     This keeps the git history meaningful and avoids burning Actions
     minutes on empty commits.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path
from urllib.error import URLError, HTTPError
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from urllib.request import Request, urlopen
from datetime import datetime, timezone

SOURCES = [
    {
        "label": "source_1",
        "url": "https://raw.githubusercontent.com/srhady/SonyLiv/refs/heads/main/sonyliv_playlist.m3u",
    },
    {
        "label": "source_2",
        "url": "https://raw.githubusercontent.com/srhady/tapmad-bd/refs/heads/main/tapmad_bd.m3u",
    },
    {
        "label": "source_3",
        "url": "https://raw.githubusercontent.com/srhady/bingstream/refs/heads/main/playlist.m3u",
    },
    {
        "label": "source_4",
        "url": "https://raw.githubusercontent.com/srhady/axsports/refs/heads/main/playlist.m3u",
    },
]

OUTPUT = Path("master.m3u")
CACHE_DIR = Path(".cache")
STATE_FILE = CACHE_DIR / "state.json"

TIMEOUT = 20
RETRIES = 3
RETRY_BACKOFF = 3  # seconds, doubles each retry

# If a fresh fetch has fewer than this fraction of the entries the cached
# copy had, treat it as "suspicious" rather than a real upstream change.
DROP_THRESHOLD = 0.5
# ...unless it stays that small for this many consecutive runs in a row,
# at which point we accept it as the new reality (so a genuine, lasting
# change on the source repo still gets picked up, just not instantly).
SUSPICIOUS_GRACE_RUNS = 3


def cache_busted(url: str) -> str:
    """Append a changing query param so the raw.githubusercontent CDN
    can't serve us a stale cached copy of this exact URL."""
    parts = urlsplit(url)
    query = parse_qsl(parts.query)
    query.append(("_cb", str(int(time.time() * 1000))))
    return urlunsplit(parts._replace(query=urlencode(query)))


def fetch(url: str) -> str:
    last_error: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            req = Request(
                cache_busted(url),
                headers={
                    "User-Agent": "M3U-Merger/2.0",
                    "Accept": "text/plain,*/*",
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Pragma": "no-cache",
                },
            )
            with urlopen(req, timeout=TIMEOUT) as response:
                return response.read().decode("utf-8-sig", errors="replace")
        except (URLError, HTTPError, TimeoutError) as exc:
            last_error = exc
            if attempt < RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
    raise last_error  # type: ignore[misc]


def parse_entries(text: str) -> list[list[str]]:
    lines = [line.rstrip("\r") for line in text.splitlines()]
    entries: list[list[str]] = []
    current: list[str] | None = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith("#EXTINF:"):
            if current:
                entries.append(current)
            current = [line]
            continue

        if current is not None:
            current.append(line)
            if not stripped.startswith("#"):
                entries.append(current)
                current = None

    if current:
        entries.append(current)

    return entries


def extract_url(entry: list[str]) -> str | None:
    for line in reversed(entry):
        value = line.strip()
        if value and not value.startswith("#"):
            return value
    return None


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(state: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def cache_path(label: str) -> Path:
    return CACHE_DIR / f"{label}.m3u"


def load_cached_entries(label: str) -> list[list[str]] | None:
    path = cache_path(label)
    if not path.exists():
        return None
    return parse_entries(path.read_text(encoding="utf-8", errors="replace"))


def save_cached_entries(label: str, text: str) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    cache_path(label).write_text(text, encoding="utf-8")


def resolve_source(source: dict, state: dict) -> tuple[list[list[str]], str]:
    """Fetch one source and decide which entries to actually use.

    Returns (entries, status_message).
    """
    label = source["label"]
    cached_entries = load_cached_entries(label)
    src_state = state.setdefault(label, {"suspicious_streak": 0})

    try:
        text = fetch(source["url"])
        fresh_entries = parse_entries(text)
    except Exception as exc:  # noqa: BLE001 - we want to fall back on anything
        if cached_entries is not None:
            return cached_entries, f"fetch failed ({exc}); used cached copy ({len(cached_entries)} entries)"
        return [], f"fetch failed ({exc}); no cache available"

    if not fresh_entries:
        if cached_entries is not None:
            return cached_entries, f"fetch returned 0 entries; used cached copy ({len(cached_entries)} entries)"
        return [], "fetch returned 0 entries; no cache available"

    if cached_entries:
        drop_ratio = len(fresh_entries) / max(len(cached_entries), 1)
        if drop_ratio < DROP_THRESHOLD:
            src_state["suspicious_streak"] = src_state.get("suspicious_streak", 0) + 1
            if src_state["suspicious_streak"] < SUSPICIOUS_GRACE_RUNS:
                return (
                    cached_entries,
                    f"fresh fetch looked suspicious ({len(fresh_entries)} vs "
                    f"{len(cached_entries)} cached, streak "
                    f"{src_state['suspicious_streak']}/{SUSPICIOUS_GRACE_RUNS}); used cached copy",
                )
            # Accepted as the new normal after repeated confirmation.
            src_state["suspicious_streak"] = 0
            save_cached_entries(label, text)
            return fresh_entries, f"accepted smaller count after {SUSPICIOUS_GRACE_RUNS} consecutive confirms ({len(fresh_entries)} entries)"

    src_state["suspicious_streak"] = 0
    save_cached_entries(label, text)
    return fresh_entries, f"{len(fresh_entries)} entries fetched"


def entries_signature(entries: list[list[str]]) -> str:
    joined = "\n".join("\n".join(entry) for entry in entries)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def main() -> int:
    state = load_state()
    all_entries: list[list[str]] = []
    seen_urls: set[str] = set()
    hard_failures: list[str] = []

    for source in SOURCES:
        entries, status = resolve_source(source, state)
        print(f"{source['label']}: {status}")

        if not entries:
            hard_failures.append(source["label"])
            continue

        for entry in entries:
            url = extract_url(entry)
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            all_entries.append(entry)

    if not all_entries:
        save_state(state)
        print("No entries were collected from any source (fresh or cached); refusing to overwrite master.m3u.", file=sys.stderr)
        return 1

    new_signature = entries_signature(all_entries)
    old_signature = state.get("_master_signature")

    if new_signature == old_signature and OUTPUT.exists():
        print(f"No channel-list changes detected ({len(all_entries)} unique entries); leaving master.m3u untouched.")
        if hard_failures:
            print("Sources using cached/fallback data this run: " + ", ".join(hard_failures))
        save_state(state)
        return 0

    state["_master_signature"] = new_signature
    save_state(state)

    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    output_lines = ["#EXTM3U", f"#last_update:{now}", ""]
    for entry in all_entries:
        output_lines.extend(entry)
        output_lines.append("")

    tmp = OUTPUT.with_suffix(".m3u.tmp")
    tmp.write_text("\n".join(output_lines).rstrip() + "\n", encoding="utf-8")
    tmp.replace(OUTPUT)

    print(f"Master playlist updated: {len(all_entries)} unique entries")
    if hard_failures:
        print("Warnings - sources using cached/fallback data this run:")
        for label in hard_failures:
            print(f" - {label}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
