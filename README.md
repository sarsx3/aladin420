# Flashify Master M3U

This repository automatically merges these public source playlists into `master.m3u`:

1. SonyLiv
2. Tapmad BD
3. Bingstream
4. AXSports

## How it works

GitHub Actions runs the Python merger every 15 minutes. The merger downloads the current source playlists, preserves entry metadata such as `#EXTVLCOPT`, prefixes `group-title` with the source name, removes exact duplicate URLs, and updates `master.m3u`.

## Important

Only use/redistribute playlist data and stream URLs when you have permission to do so and when the source's license/terms allow it.

The merger does not bypass authentication, DRM, geo-blocking, or any other access control. If a source playlist itself becomes unavailable, the workflow will report the error.

## Output URL

After pushing this repository, the master playlist can be used from:

https://raw.githubusercontent.com/YOUR_USERNAME/YOUR_REPOSITORY/main/master.m3u

Replace `YOUR_USERNAME/YOUR_REPOSITORY` with your actual GitHub repository path.
