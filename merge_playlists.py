#!/usr/bin/env python3
"""
Merge public M3U playlists into one master.m3u.

The script:
- downloads each configured source playlist
- preserves #EXTVLCOPT and other per-entry metadata
- adds a source prefix to group-title so entries can be filtered
- removes exact duplicate stream URLs
- writes master.m3u atomically
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

SOURCES = [
    {
        "name": "SonyLiv",
        "url": "https://raw.githubusercontent.com/srhady/SonyLiv/refs/heads/main/sonyliv_playlist.m3u",
    },
    {
        "name": "Tapmad BD",
        "url": "https://raw.githubusercontent.com/srhady/tapmad-bd/refs/heads/main/tapmad_bd.m3u",
    },
    {
        "name": "Bingstream",
        "url": "https://raw.githubusercontent.com/srhady/bingstream/refs/heads/main/playlist.m3u",
    },
    {
        "name": "AXSports",
        "url": "https://raw.githubusercontent.com/srhady/axsports/refs/heads/main/playlist.m3u",
    },
]

OUTPUT = Path("master.m3u")
TIMEOUT = 30


def fetch(url: str) -> str:
    req = Request(
        url,
        headers={
            "User-Agent": "Flashify-M3U-Merger/1.0",
            "Accept": "text/plain,*/*",
        },
    )
    with urlopen(req, timeout=TIMEOUT) as response:
        data = response.read()
    return data.decode("utf-8-sig", errors="replace")


def replace_group_title(extinf: str, source_name: str) -> str:
    """Prefix group-title while preserving the rest of the EXTINF line."""
    match = re.search(r'group-title="([^"]*)"', extinf, flags=re.I)
    if not match:
        return extinf

    old_group = match.group(1).strip()
    new_group = f"{source_name} | {old_group}" if old_group else source_name

    return (
        extinf[: match.start(1)]
        + new_group.replace('"', "'")
        + extinf[match.end(1) :]
    )


def parse_entries(text: str, source_name: str) -> list[list[str]]:
    """
    Parse an M3U into entries. An entry starts at #EXTINF and ends at
    the first following non-comment URL line. Metadata such as EXTVLCOPT
    between them is retained.
    """
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
            current = [replace_group_title(line, source_name)]
            continue

        if current is not None:
            current.append(line)

            # A URL normally terminates an entry. Keep any comments before it.
            if not stripped.startswith("#"):
                entries.append(current)
                current = None

    if current:
        entries.append(current)

    return entries


def extract_url(entry: list[str]) -> str | None:
    for line in reversed(entry):
        s = line.strip()
        if s and not s.startswith("#"):
            return s
    return None


def main() -> int:
    all_entries: list[list[str]] = []
    seen_urls: set[str] = set()
    failed: list[str] = []

    for source in SOURCES:
        try:
            text = fetch(source["url"])
            entries = parse_entries(text, source["name"])

            for entry in entries:
                url = extract_url(entry)
                if not url:
                    continue

                # Exact URL deduplication only. Different servers/URLs remain.
                if url in seen_urls:
                    continue

                seen_urls.add(url)
                all_entries.append(entry)

            print(f"{source['name']}: {len(entries)} entries fetched")
        except Exception as exc:
            failed.append(f"{source['name']}: {exc}")
            print(f"ERROR: {source['name']}: {exc}", file=sys.stderr)

    # Safety: do not overwrite a previously valid playlist if every source failed.
    if not all_entries:
        print("No entries were collected; refusing to overwrite master.m3u.", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")

    output_lines = [
        "#EXTM3U",
        "#name:Flashify Master Auto Update Playlist",
        f"#last_update:{now}",
        "#generated_by:Flashify M3U Merger",
        "",
    ]

    for entry in all_entries:
        output_lines.extend(entry)
        output_lines.append("")

    tmp = OUTPUT.with_suffix(".m3u.tmp")
    tmp.write_text("\n".join(output_lines).rstrip() + "\n", encoding="utf-8")
    tmp.replace(OUTPUT)

    print(f"Master playlist: {len(all_entries)} unique entries")
    if failed:
        print("Warnings:")
        for item in failed:
            print(f" - {item}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
