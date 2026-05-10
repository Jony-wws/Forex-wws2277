"""Pure-logic tests for SNIPER that run without pandas/yfinance.

The sandbox has no internet access so we can't install those heavy
deps, but we can still validate the deterministic parts: slot math,
news-blackout detection, and the 5h boundary iterator.

Run with: python3 tests/test_sniper_logic.py
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone


# ---------- Constants mirrored from app/sniper.py (do not edit) ----------

SLOT_HOURS = 5
NFP_BLACKOUT_WINDOW_MIN = 30


# ---------- Functions copy-pasted verbatim from app/sniper.py ----------

def slot_bounds(now_utc):
    midnight = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    hour_into_day = (now_utc - midnight).total_seconds() / 3600.0
    slot_index = int(hour_into_day // SLOT_HOURS)
    slot_start = midnight + timedelta(hours=slot_index * SLOT_HOURS)
    slot_end = slot_start + timedelta(hours=SLOT_HOURS)
    return slot_start, slot_end


def _is_first_friday(dt):
    return dt.weekday() == 4 and dt.day <= 7


def in_news_blackout(now_utc):
    candidates = []
    if _is_first_friday(now_utc):
        candidates.append((now_utc.replace(hour=12, minute=30, second=0, microsecond=0), "NFP"))
    if now_utc.weekday() == 2:
        candidates.append((now_utc.replace(hour=18, minute=0, second=0, microsecond=0), "FOMC"))
    w = timedelta(minutes=NFP_BLACKOUT_WINDOW_MIN)
    for ts, label in candidates:
        if ts - w <= now_utc <= ts + w:
            return True, label
    return False, ""


def iter_slots(start, end):
    cur = start.replace(minute=0, second=0, microsecond=0)
    hour_aligned = (cur.hour // SLOT_HOURS) * SLOT_HOURS
    cur = cur.replace(hour=hour_aligned)
    out = []
    while cur < end:
        nxt = cur + timedelta(hours=SLOT_HOURS)
        if nxt > end:
            break
        out.append((cur, nxt))
        cur = nxt
    return out


# ---------- Tests ----------

def run():
    tests = []
    fails = []

    def check(name, cond, detail=""):
        if cond:
            tests.append((name, "OK"))
        else:
            fails.append((name, detail))

    # 1. Slot math for 17:30 UTC
    s, e = slot_bounds(datetime(2026, 5, 10, 17, 30, tzinfo=timezone.utc))
    check("slot_bounds 17:30 -> 15→20",
          s.hour == 15 and e.hour == 20, f"{s} {e}")

    # 2. Slot math for 00:00 UTC
    s, e = slot_bounds(datetime(2026, 5, 10, 0, 0, tzinfo=timezone.utc))
    check("slot_bounds 00:00 -> 00→05",
          s.hour == 0 and e.hour == 5, f"{s} {e}")

    # 3. Last slot of day crosses midnight
    s, e = slot_bounds(datetime(2026, 5, 10, 23, 59, tzinfo=timezone.utc))
    check("slot_bounds 23:59 -> 20→01(next day)",
          s.hour == 20 and e.day == 11, f"{s} {e}")

    # 4. 04:59 still in 00→05 slot
    s, e = slot_bounds(datetime(2026, 5, 10, 4, 59, tzinfo=timezone.utc))
    check("slot_bounds 04:59 -> 00→05",
          s.hour == 0 and e.hour == 5, f"{s} {e}")

    # 5. NFP blackout on 1st Friday of May 2026 (May 1)
    t = datetime(2026, 5, 1, 12, 30, tzinfo=timezone.utc)
    check("May 1 2026 is first Friday",
          _is_first_friday(t))
    blk, _ = in_news_blackout(t)
    check("NFP blackout 2026-05-01 12:30", blk)

    # 6. 30 min before NFP still blocked
    blk, _ = in_news_blackout(
        datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc))
    check("NFP pre-window 12:00", blk)

    # 7. 31 min before NFP NOT blocked
    blk, _ = in_news_blackout(
        datetime(2026, 5, 1, 11, 59, tzinfo=timezone.utc))
    check("11:59 outside window", not blk)

    # 8. FOMC Wednesday 18:00
    t = datetime(2026, 5, 13, 18, 0, tzinfo=timezone.utc)
    check("May 13 2026 is Wednesday", t.weekday() == 2)
    blk, label = in_news_blackout(t)
    check("FOMC Wed 18:00", blk and "FOMC" in label)

    # 9. iter_slots produces exactly the expected boundaries
    start = datetime(2026, 5, 10, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 5, 10, 20, 0, tzinfo=timezone.utc)
    slots = iter_slots(start, end)
    check("iter_slots full day 00→20 = 4 slots",
          len(slots) == 4, f"got {len(slots)}")
    check("first slot starts 00:00",
          slots[0][0].hour == 0)
    check("last slot ends 20:00",
          slots[-1][1].hour == 20)

    for name, _ in tests:
        print(f"  [OK] {name}")
    for name, detail in fails:
        print(f"  [FAIL] {name}: {detail}")

    if fails:
        raise SystemExit(1)
    print(f"\n{len(tests)} / {len(tests)} tests passed.")


if __name__ == "__main__":
    run()
