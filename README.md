# `data` branch

This branch holds JSON snapshots produced every 15 minutes by
`.github/workflows/refresh_data.yml`.  The GitHub Pages SPA
(built from `main` via `gh_pages.yml`) reads these files at
runtime via jsDelivr CDN.

**Do not edit by hand.**  Changes will be overwritten by the
next scheduled run.
