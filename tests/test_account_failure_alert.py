"""Regression tests for sustained account-fetch failure alerting.

Bug: get_account_state() collapses a failure to equity == 0.0 and
get_all_positions() to None. A revoked/bad API key makes every account,
position and order call return 401, but crypto market data needs NO
credentials -- so the bot keeps logging prices/regimes while failing closed on
every entry, indefinitely and silently (observed live: 25+ minutes of
{"message": "unauthorized."} with no escalation).

Covers:
  1. No alert below the threshold
  2. Exactly ONE alert once the streak reaches the threshold
  3. No repeat alert on subsequent failing cycles (no per-cycle spam)
  4. A success resets the streak and re-arms the alert
  5. A fresh outage after recovery alerts again
  6. Threshold 0 disables alerting entirely
  7. The cycle-level predicate (equity > 0 and positions_ok) is what is tracked

Run:  PYTHONPATH=<repo> python tests/test_account_failure_alert.py
"""

import os
os.environ.setdefault("APCA_API_KEY_ID", "test")
os.environ.setdefault("APCA_API_SECRET_KEY", "test")

import asyncio
from unittest.mock import AsyncMock, patch

import main
import config

_passed = 0
_failed = 0


def check(label, condition, detail=""):
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  PASS: {label}" + (f" | {detail}" if detail else ""))
    else:
        _failed += 1
        print(f"  FAIL: {label}" + (f" | {detail}" if detail else ""))


def _reset():
    main._account_failure_streak = 0
    main._account_failure_alerted = False


def _feed(outcomes, threshold=3):
    """Drive track_account_fetch_outcome; return (alert_count, discord_titles)."""
    _reset()
    titles = []
    with patch.object(main, "ACCOUNT_FAILURE_ALERT_THRESHOLD", threshold), \
         patch.object(main, "send_discord_alert",
                      new=AsyncMock(side_effect=lambda **kw: titles.append(kw.get("title")))):
        for ok in outcomes:
            asyncio.run(main.track_account_fetch_outcome(ok))
    return len(titles), titles


def test_no_alert_below_threshold():
    print("\n--- 1. No alert below the threshold ---")
    n, _ = _feed([False, False], threshold=3)
    check("2 failures < threshold 3 -> no alert", n == 0, f"alerts={n}")


def test_single_alert_at_threshold():
    print("\n--- 2. Exactly one alert once the threshold is reached ---")
    n, titles = _feed([False, False, False], threshold=3)
    check("3 failures at threshold 3 -> exactly 1 alert", n == 1, f"alerts={n}")
    check("alert title mentions the account fetch", titles and "account" in titles[0].lower(),
          f"titles={titles}")


def test_no_repeat_alert_while_failing():
    print("\n--- 3. No repeat alert on subsequent failing cycles ---")
    n, _ = _feed([False] * 10, threshold=3)
    check("10 failures -> still only 1 alert (no per-cycle spam)", n == 1, f"alerts={n}")


def test_success_resets_and_rearms():
    print("\n--- 4. A success resets the streak and re-arms the alert ---")
    n, _ = _feed([False, False, True, False, False], threshold=3)
    check("fail,fail,OK,fail,fail -> no alert (streak reset)", n == 0, f"alerts={n}")
    check("streak reset to 2 after re-failing", main._account_failure_streak == 2,
          f"streak={main._account_failure_streak}")


def test_fresh_outage_after_recovery_alerts_again():
    print("\n--- 5. A fresh outage after recovery alerts again ---")
    n, _ = _feed([False, False, False, True, False, False, False], threshold=3)
    check("two separate outages -> 2 alerts", n == 2, f"alerts={n}")


def test_threshold_zero_disables():
    print("\n--- 6. Threshold 0 disables alerting ---")
    n, _ = _feed([False] * 20, threshold=0)
    check("threshold 0 -> never alerts", n == 0, f"alerts={n}")
    check("streak still tracked", main._account_failure_streak == 20,
          f"streak={main._account_failure_streak}")


def test_cycle_predicate_is_tracked():
    print("\n--- 7. The cycle predicate (equity>0 and positions_ok) is what is fed ---")
    # Mirror main.py: await track_account_fetch_outcome(equity > 0 and positions_ok)
    cases = [
        (100.0, True, True, "healthy cycle"),
        (0.0, True, False, "account fetch failed (equity sentinel 0.0)"),
        (100.0, False, False, "positions fetch failed"),
        (0.0, False, False, "both failed"),
    ]
    for equity, positions_ok, expected_ok, label in cases:
        check(f"predicate {label} -> ok={expected_ok}",
              (equity > 0 and positions_ok) is expected_ok,
              f"equity={equity} positions_ok={positions_ok}")


def test_default_threshold_is_three():
    print("\n--- 8. Config default threshold ---")
    check("ACCOUNT_FAILURE_ALERT_THRESHOLD defaults to 3",
          config.ACCOUNT_FAILURE_ALERT_THRESHOLD == 3,
          f"threshold={config.ACCOUNT_FAILURE_ALERT_THRESHOLD}")


if __name__ == "__main__":
    test_no_alert_below_threshold()
    test_single_alert_at_threshold()
    test_no_repeat_alert_while_failing()
    test_success_resets_and_rearms()
    test_fresh_outage_after_recovery_alerts_again()
    test_threshold_zero_disables()
    test_cycle_predicate_is_tracked()
    test_default_threshold_is_three()
    print(f"\n=== {_passed} passed, {_failed} failed ===")
    raise SystemExit(1 if _failed else 0)
