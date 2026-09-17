#!/usr/bin/env python3
"""
Merge configured public M3U playlists into one master.m3u.

Source names are used only internally to identify errors/logs.
They are never written into the generated playlist.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

SOURCES = [
    {
        "label": "Source 1",
        "url": "https://raw.githubusercontent.com/srhady/SonyLiv/refs/heads/main/sonyliv_playlist.m3u",
    },
    {
        "label": "Source 2",
        "url": "https://raw.githubusercontent.com/srhady/tapmad-bd/refs/heads/main/tapmad_bd.m3u",
    },
    {
        "label": "Source 3",
        "url": "https://raw.githubusercontent.com/srhady/bingstream/refs/heads/main/playlist.m3u",
    },
    {
        "label": "Source 4",
        "url": "https://raw.githubusercontent.com/srhady/axsports/refs/heads/main/playlist.m3u",
    },
]

OUTPUT = Path("master.m3u")
TIMEOUT = 30


def fetch(url: str) -> str:
    req = Request(
        url,
        headers={
            "User-Agent": "M3U-Merger/1.0",
            "Accept": "text/plain,*/*",
        },
    )
    with urlopen(req, timeout=TIMEOUT) as response:
        return response.read().decode("utf-8-sig", errors="replace")


def clean_extinf(extinf: str) -> str:
    # Remove source-specific source/group prefixes if they were previously
    # generated, while leaving the original playlist's group-title intact.
    match = re.search(r'group-title="([^"]*)"', extinf, flags=re.I)
    if not match:
        return extinf

    group = match.group(1).strip()

    # Do not add any source name. Preserve the original group as-is.
    return extinf


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
            current = [clean_extinf(line)]
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


def main() -> int:
    all_entries: list[list[str]] = []
    seen_urls: set[str] = set()
    failed: list[str] = []

    for source in SOURCES:
        try:
            text = fetch(source["url"])
            entries = parse_entries(text)

            for entry in entries:
                url = extract_url(entry)
                if not url or url in seen_urls:
                    continue

                seen_urls.add(url)
                all_entries.append(entry)

            print(f"{source['label']}: {len(entries)} entries fetched")
        except Exception as exc:
            failed.append(f"{source['label']}: {exc}")
            print(f"ERROR: {source['label']}: {exc}", file=sys.stderr)

    if not all_entries:
        print("No entries were collected; refusing to overwrite master.m3u.", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")

    output_lines = [
        "#EXTM3U",
        f"#last_update:{now}",
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
