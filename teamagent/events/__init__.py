"""Event-attribution package — 365-day archive + analysis.

Submodules:
- archive: build comprehensive event archive (FRED + programmatic CB + COT + geo)
- detector: detect significant price moves per (pair, day, session)
- attribution: match events to moves, build (currency × event-type × session) table
- traps: identify trader-trap patterns (false breakouts, fake-news reactions)
- profile: per-pair behavior profile (Asia/London/Overlap/NY)

See `scripts/build_event_attribution_report.py` for the main entrypoint.
"""
