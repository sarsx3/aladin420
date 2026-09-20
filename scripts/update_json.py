#!/usr/bin/env python3
"""
Auto-updater: fetches source JSON files and mirrors them into this repo
as separate output files, only committing when content actually changes.

Design goals:
- Never crash the whole run if ONE source fails (other file still updates)
- Retry with backoff on network errors
- Byte-for-byte change detection (no pointless commits)
- Zero third-party dependencies (uses only Python stdlib -> fast, no pip step)
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error

# --- Configure your sources here: {output_file: source_url} -------------
SOURCES = {
    "data/bing_playlist.json": (
        "https://raw.githubusercontent.com/srhady/bingstream/"
        "refs/heads/main/playlist.json"
    ),
    "data/tapmad_bd.json": (
        "https://gist.githubusercontent.com/albatr0ssss/"
        "3cff7a26be49b1d352c15f615067e7cd/raw/tapmad_bd.json"
    ),
}

TIMEOUT = 15          # seconds per request
MAX_RETRIES = 4       # attempts per source
RETRY_BACKOFF = 2      # seconds, doubles each retry


def fetch(url: str) -> bytes:
    last_err = None
    delay = RETRY_BACKOFF
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (auto-json-mirror-bot)",
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


def main() -> int:
    any_changed = False
    any_failed = False

    for out_path, url in SOURCES.items():
        os.makedirs(os.path.dirname(out_path), exist_ok=True)

        try:
            raw = fetch(url)
            data = json.loads(raw)
        except Exception as e:
            # Do NOT stop the whole workflow — just keep the previous file
            print(f"[ERROR] could not update {out_path}: {e}", file=sys.stderr)
            any_failed = True
            continue

        new_content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"

        old_content = None
        if os.path.exists(out_path):
            with open(out_path, "r", encoding="utf-8") as f:
                old_content = f.read()

        if new_content != old_content:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(new_content)
            any_changed = True
            print(f"[OK] {out_path} updated ({len(new_content)} bytes)")
        else:
            print(f"[SKIP] {out_path} unchanged")

    # Write a small status file so you can always verify freshness publicly
    status = {
        "last_checked_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "any_changed_this_run": any_changed,
        "any_source_failed_this_run": any_failed,
    }
    os.makedirs("data", exist_ok=True)
    with open("data/status.json", "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
        f.write("\n")

    # Exit 0 always (a single failed source should not fail the whole job)
    return 0


if __name__ == "__main__":
    sys.exit(main())
